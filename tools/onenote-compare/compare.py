"""OneNote -> Fortress comparison pipeline (see README.md).

    cd backend && uv run --frozen --with playwright==1.63.0 python ../tools/onenote-compare/compare.py \
        --converter <fortress-onenote> --one2html <one2html> --out <report dir> <input>... [--each DIR]

For every input ("unit": a .zip export, a notebook folder, a .one/.onepkg file):

1. reference: one2html renders every section to HTML (independent of our converter);
2. Fortress: fortress-onenote converts the input, the real importer imports the
   manifest into a fresh temporary NOTES_DIR, and every imported note is rendered
   from its block tree and from its Markdown export;
3. Chromium (Playwright) renders each to PNG + PDF and measures where the
   reference outlines are; a side-by-side composite is written per page;
4. ``checks.py`` diffs the two content models; the report lands in
   ``<out>/index.html`` (+ ``results.json``).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import checks  # noqa: E402
import fortress  # noqa: E402
import ref as refmod  # noqa: E402
from model import Model  # noqa: E402

SOURCE_SUFFIXES = (".one", ".onepkg")
PANE_WIDTH = 760
MAX_PANE_HEIGHT = 5000


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def slug(text: str, limit: int = 40) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return (value or "x")[:limit]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- inputs ------------------------------------------------------------------------------------


def safe_extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(archive) as handle:
        for info in handle.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or any(part == ".." for part in name.split("/")):
                continue
            destination = (target / name).resolve()
            if root not in destination.parents and destination != root:
                continue
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)


def section_files(folder: Path) -> list[Path]:
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if any(part.lower() == "onenote_recyclebin" for part in path.parts):
            continue
        if path.stat().st_size == 0:
            continue
        found.append(path)
    return found


def units_from_each(folder: Path, seen: set[str]) -> list[Path]:
    """Every .one/.onepkg/.zip under ``folder`` as its own unit, duplicates (same bytes) once."""

    units = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (*SOURCE_SUFFIXES, ".zip") or path.stat().st_size == 0:
            continue
        if any(part.lower() == "onenote_recyclebin" or part.startswith("_") for part in path.relative_to(folder).parts):
            continue
        digest = sha256(path)
        if digest in seen:
            continue
        seen.add(digest)
        units.append(path)
    return units


# --- reference -----------------------------------------------------------------------------------


def run_one2html(one2html: str, sources: list[Path], out: Path) -> list[dict]:
    """Render each section file; returns per-file status (one2html panics on some files)."""

    results = []
    for index, source in enumerate(sources):
        target = out / f"{index:03d}"
        target.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run([one2html, "-i", str(source), "-o", str(target)], capture_output=True, text=True,
                                  timeout=600)
            ok = proc.returncode == 0
            error = "" if ok else (proc.stderr or proc.stdout)[-2000:]
        except subprocess.TimeoutExpired:
            ok, error = False, "timeout"
        results.append({"file": str(source), "ok": ok, "error": error})
    return results


def reference_pages(out: Path) -> dict[str, Path]:
    pages = {}
    for path in sorted(out.rglob("*.html")):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        pid = refmod.page_id(head)
        if pid and pid not in pages:
            pages[pid] = path
    return pages


# --- rendering -----------------------------------------------------------------------------------

BOXES_JS = """
() => Array.from(document.body.children)
  .filter(el => !['SCRIPT', 'STYLE'].includes(el.tagName) && !el.classList.contains('title'))
  .map(el => { const r = el.getBoundingClientRect();
               return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height}; })
"""


class Renderer:
    def __init__(self):
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch()
        self.context = self.browser.new_context(viewport={"width": 1100, "height": 900})
        # Offline: only file:// (one2html pages reference a MathJax CDN and a polyfill).
        self.context.route(re.compile(r"^(https?|wss?):"), lambda route: route.abort())

    def render(self, html_path: Path, png: Path | None, pdf: Path | None = None, boxes: bool = False):
        page = self.context.new_page()
        try:
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=60000)
            page.wait_for_timeout(50)
            result = page.evaluate(BOXES_JS) if boxes else None
            if png is not None:
                try:
                    page.screenshot(path=str(png), full_page=True)
                except Exception:  # noqa: BLE001 - very tall/wide pages exceed Chromium's capture limit
                    size = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
                    page.screenshot(path=str(png), clip={"x": 0, "y": 0, "width": min(size[0], 4000),
                                                         "height": min(size[1], 12000)}, full_page=True)
            if pdf is not None:
                try:
                    page.pdf(path=str(pdf), print_background=True, width="1100px")
                except Exception as exc:  # noqa: BLE001
                    log(f"pdf failed for {html_path}: {exc}")
            return result
        finally:
            page.close()

    def close(self):
        self.context.close()
        self.browser.close()
        self._pw.stop()


def composite(panes: list[tuple[str, Path]], out: Path) -> None:
    from PIL import Image, ImageDraw  # noqa: PLC0415

    Image.MAX_IMAGE_PIXELS = None
    images = []
    for label, path in panes:
        try:
            image = Image.open(path).convert("RGB")
        except Exception:  # noqa: BLE001
            image = Image.new("RGB", (PANE_WIDTH, 200), "white")
        if image.width > PANE_WIDTH:
            image = image.resize((PANE_WIDTH, max(1, round(image.height * PANE_WIDTH / image.width))))
        if image.height > MAX_PANE_HEIGHT:
            image = image.crop((0, 0, image.width, MAX_PANE_HEIGHT))
        images.append((label, image))
    header = 28
    width = PANE_WIDTH * len(images) + 8 * (len(images) - 1)
    height = header + max(image.height for _, image in images)
    canvas = Image.new("RGB", (width, height), (200, 200, 200))
    draw = ImageDraw.Draw(canvas)
    for index, (label, image) in enumerate(images):
        x = index * (PANE_WIDTH + 8)
        draw.rectangle((x, 0, x + PANE_WIDTH, header - 1), fill=(40, 40, 40))
        draw.text((x + 8, 8), label, fill=(255, 255, 255))
        canvas.paste(Image.new("RGB", (PANE_WIDTH, height - header), "white"), (x, header))
        canvas.paste(image, (x, header))
    canvas.save(out, optimize=True)


# --- one unit ----------------------------------------------------------------------------------


def process_unit(unit: Path, out: Path, args, renderer: Renderer) -> dict:
    name = slug(unit.stem) + "-" + hashlib.sha1(str(unit).encode()).hexdigest()[:6]
    work = out / "units" / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    log(f"== {unit} -> {work}")
    result: dict = {"unit": str(unit), "name": name, "pages": [], "errors": []}

    # Inputs for one2html.
    if unit.is_file() and unit.suffix.lower() == ".zip":
        extracted = work / "input"
        safe_extract(unit, extracted)
        sources = section_files(extracted)
    elif unit.is_dir():
        sources = section_files(unit)
    else:
        sources = [unit]
    ref_dir = work / "ref"
    result["one2html"] = run_one2html(args.one2html, sources, ref_dir)
    for item in result["one2html"]:
        if not item["ok"]:
            result["errors"].append(f"one2html failed on {Path(item['file']).name}: {item['error'][-300:]}")
    ref_pages = reference_pages(ref_dir)

    # Converter + importer.
    manifest_dir = work / "manifest"
    proc = subprocess.run([args.converter, str(unit), "--out", str(manifest_dir), "-q"], capture_output=True, text=True)
    if proc.returncode != 0:
        result["errors"].append(f"converter failed: {proc.stderr[-1000:]}")
        manifest = {"notebooks": []}
    else:
        manifest = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8"))
    notes = fortress.FortressNotes(work / "fortress")
    try:
        report = notes.import_manifest(manifest_dir) if proc.returncode == 0 else {"notebooks": []}
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"import failed: {exc}\n{traceback.format_exc()[-1500:]}")
        report = {"notebooks": []}
    (work / "import-report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    for notebook in manifest.get("notebooks") or []:
        for failed in (notebook.get("report") or {}).get("failed_sections") or []:
            result["errors"].append(f"converter could not read section {' › '.join(failed.get('path') or [])}: "
                                    f"{failed.get('reason')} [{str(failed.get('error'))[:160]}]")
    docs = notes.documents_by_page()
    imported_pages = set(docs)
    manifest_pages = {}
    for notebook in manifest.get("notebooks") or []:
        for page, path in _manifest_pages(notebook.get("children") or [], [notebook.get("name") or ""]):
            manifest_pages[str(page["link_target_id"]).upper()] = (page, path)

    media_prefix = notes.settings.assets_path.resolve().as_uri() + "/"
    all_ids = list(dict.fromkeys([*ref_pages, *manifest_pages]))
    for index, pid in enumerate(all_ids):
        page_dir = work / "pages" / f"{index:03d}"
        page_dir.mkdir(parents=True, exist_ok=True)
        page_info, section_path = manifest_pages.get(pid, (None, []))
        entry: dict = {"page_id": pid, "dir": str(page_dir.relative_to(out)),
                       "title": (page_info or {}).get("title") or "", "section": " › ".join(section_path)}
        result["pages"].append(entry)
        ref_path = ref_pages.get(pid)
        doc_id = docs.get(pid)
        try:
            ref_model, boxes = Model(), None
            if ref_path is not None:
                _, ref_model, _ = refmod.load(ref_path)
                entry["title"] = entry["title"] or ref_model.title
                boxes = renderer.render(ref_path, None, boxes=True)
                entry["ref_html"] = os.path.relpath(ref_path, out)
            if doc_id is not None:
                tree = notes.tree(doc_id)
                (page_dir / "tree.json").write_text(json.dumps(tree, indent=1, ensure_ascii=False), encoding="utf-8")
                markdown = notes.markdown(doc_id)
                (page_dir / "note.md").write_text(markdown, encoding="utf-8")
                renderer_html = fortress.HtmlRenderer(notes, media_prefix)
                (page_dir / "tree.html").write_text(renderer_html.document(tree), encoding="utf-8")
                (page_dir / "md.html").write_text(fortress.markdown_html(markdown, tree.get("title") or "", media_prefix),
                                                  encoding="utf-8")
                our_model = fortress.tree_model(notes, tree)
                export = fortress.export_summary((page_dir / "md.html").read_text(encoding="utf-8"))
                tree_info = fortress.tree_summary(tree)
            else:
                our_model, export, tree_info = Model(), None, None
            if ref_path is None:
                entry["checks"] = [checks._result("page", True, "page not rendered by one2html (no reference to compare)",
                                                  applicable=False)]
                entry["status"] = "no-reference"
            elif doc_id is None:
                entry["checks"] = [checks._result("page", False, "page missing in Fortress")]
                entry["status"] = "missing"
            else:
                if ref_model.title in {"", "Untitled Page"} and (page_info or {}).get("title_derived"):
                    # Untitled in OneNote: Fortress names the note after its first line.
                    our_model.notes.append(f"untitled page named {our_model.title!r}")
                    ref_model.title = our_model.title
                entry["checks"] = checks.run_all(ref_model, our_model, boxes, imported_pages, export, tree_info)
                entry["status"] = "ok"
            entry["words"] = len(ref_model.tokens)
            failing = any(c["status"] == "fail" for c in entry["checks"])
            if args.shots == "all" or failing or (args.sample and index % args.sample == 0):
                if ref_path is not None:
                    renderer.render(ref_path, page_dir / "ref.png", page_dir / "ref.pdf")
                if doc_id is not None:
                    renderer.render(page_dir / "tree.html", page_dir / "tree.png", page_dir / "tree.pdf")
                    renderer.render(page_dir / "md.html", page_dir / "md.png", page_dir / "md.pdf")
                composite([("OneNote (one2html reference)", page_dir / "ref.png"),
                           ("Fortress block tree", page_dir / "tree.png"),
                           ("Fortress Markdown export", page_dir / "md.png")], page_dir / "side.png")
                entry["side"] = True
            entry["notes"] = our_model.notes[:20]
        except Exception as exc:  # noqa: BLE001
            entry["checks"] = [checks._result("page", False, f"pipeline error: {exc}", [traceback.format_exc()[-1500:]])]
            entry["status"] = "error"
        write_detail(out, entry)
    notes.close()
    if args.prune:
        # Keep the per-page results and images; drop converted files, notes and reference assets.
        for name in ("input", "manifest", "fortress", "ref"):
            shutil.rmtree(work / name, ignore_errors=True)
    return result


def _manifest_pages(entries, trail):
    for entry in entries:
        if entry.get("kind") == "section":
            for page in entry.get("pages") or []:
                yield page, [*trail, entry.get("name") or ""]
        else:
            yield from _manifest_pages(entry.get("children") or [], [*trail, entry.get("name") or ""])


# --- report --------------------------------------------------------------------------------------

CSS = """
body { font: 13px/1.4 system-ui, sans-serif; margin: 16px; color: #111; background: #fff; }
table { border-collapse: collapse; } td, th { border: 1px solid #ccc; padding: 2px 6px; vertical-align: top; }
.pass { background: #d9f2d9; } .fail { background: #f9d0d0; } .na { color: #aaa; }
.small { font-size: 11px; color: #555; } img.side { max-width: 100%; border: 1px solid #999; }
code, pre { white-space: pre-wrap; }
"""


def write_detail(out: Path, entry: dict) -> None:
    page_dir = out / entry["dir"]
    rows = []
    for check in entry.get("checks") or []:
        cls = {"pass": "pass", "fail": "fail"}.get(check["status"], "na")
        details = "".join(f"<li>{html.escape(d)}</li>" for d in check["details"])
        more = f"<li>… {check['detail_count'] - len(check['details'])} more</li>" if check.get("detail_count", 0) > len(check["details"]) else ""
        rows.append(f"<tr class='{cls}'><td>{check['name']}</td><td>{check['status']}</td>"
                    f"<td>{html.escape(check['summary'])}<ul>{details}{more}</ul></td></tr>")
    links = " · ".join(f"<a href='{name}'>{name}</a>" for name in
                       ("ref.pdf", "tree.html", "tree.pdf", "md.html", "md.pdf", "note.md", "tree.json") if (page_dir / name).exists())
    ref_link = f" · <a href='{os.path.relpath(out / entry['ref_html'], page_dir)}'>reference HTML</a>" if entry.get("ref_html") else ""
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in entry.get("notes") or [])
    (page_dir / "detail.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(entry['title'])}</title><style>{CSS}</style>"
        f"<p><a href='{os.path.relpath(out / 'index.html', page_dir)}'>← index</a></p>"
        f"<h2>{html.escape(entry['title'])}</h2><p class=small>{html.escape(entry['section'])} · {entry['page_id']}</p>"
        f"<p>{links}{ref_link}</p><table>{''.join(rows)}</table><ul class=small>{notes}</ul>"
        + ("<p><img class=side src='side.png'></p>" if entry.get("side") else "<p class=small>(no images: all checks passed)</p>"),
        encoding="utf-8")


def write_index(out: Path, results: list[dict], started: float) -> dict:
    pages = [p for unit in results for p in unit["pages"]]
    summary: dict[str, dict[str, int]] = {}
    for page in pages:
        for check in page.get("checks") or []:
            stats = summary.setdefault(check["name"], {"pass": 0, "fail": 0, "n/a": 0})
            stats[check["status"]] += 1
    names = [n for n in ("page", *checks.CHECKS) if n in summary]
    head = "".join(f"<th>{n}</th>" for n in names)
    rows = []
    for unit in results:
        for page in unit["pages"]:
            by_name = {c["name"]: c for c in page.get("checks") or []}
            cells = []
            for n in names:
                c = by_name.get(n)
                if c is None:
                    cells.append("<td class=na></td>")
                    continue
                cls = {"pass": "pass", "fail": "fail"}.get(c["status"], "na")
                cells.append(f"<td class={cls} title='{html.escape(c['summary'], quote=True)}'>{c['status']}</td>")
            rows.append(f"<tr><td><a href='{page['dir']}/detail.html'>{html.escape(page['title'] or '(untitled)')}</a>"
                        f"<div class=small>{html.escape(unit['name'])} · {html.escape(page['section'])}</div></td>{''.join(cells)}</tr>")
    rates = []
    for n in names:
        s = summary[n]
        applicable = s["pass"] + s["fail"]
        rate = f"{100 * s['pass'] / applicable:.1f}%" if applicable else "–"
        rates.append(f"<tr><td>{n}</td><td>{s['pass']}</td><td>{s['fail']}</td><td>{s['n/a']}</td><td>{rate}</td></tr>")
    errors = "".join(f"<li>{html.escape(unit['name'])}: {html.escape(e[:400])}</li>" for unit in results for e in unit["errors"])
    failing_pages = sum(1 for p in pages if any(c["status"] == "fail" for c in p.get("checks") or []))
    (out / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>OneNote import comparison</title><style>{CSS}</style>"
        f"<h1>OneNote → Fortress comparison</h1><p>{len(results)} inputs, {len(pages)} pages, "
        f"{failing_pages} pages with a failing check · {time.strftime('%Y-%m-%d %H:%M')} · {time.time() - started:.0f}s</p>"
        f"<h2>Pass rate per check</h2><table><tr><th>check</th><th>pass</th><th>fail</th><th>n/a</th><th>rate</th></tr>{''.join(rates)}</table>"
        f"<h2>Errors</h2><ul>{errors or '<li>none</li>'}</ul>"
        f"<h2>Pages</h2><table><tr><th>page</th>{head}</tr>{''.join(rows)}</table>", encoding="utf-8")
    data = {"inputs": len(results), "pages": len(pages), "failing_pages": failing_pages, "summary": summary,
            "units": results}
    (out / "results.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--each", type=Path, action="append", default=[],
                        help="treat every .one/.onepkg/.zip under this folder as its own input (duplicates once)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--converter", required=True)
    parser.add_argument("--one2html", required=True)
    parser.add_argument("--shots", choices=("all", "failing"), default="all",
                        help="screenshots/PDFs/composites for every page (default) or only pages with a failing check")
    parser.add_argument("--sample", type=int, default=0,
                        help="with --shots failing: also shoot every Nth page")
    parser.add_argument("--prune", action="store_true",
                        help="after each input, delete its converted files, notes and reference HTML (saves disk)")
    parser.add_argument("--skip", action="append", default=[],
                        help="with --each: skip inputs whose path contains this text")
    args = parser.parse_args()
    units = list(args.inputs)
    seen: set[str] = set()
    for folder in args.each:
        units += [u for u in units_from_each(folder, seen) if not any(text in str(u) for text in args.skip)]
    if not units:
        parser.error("no inputs")
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    renderer = Renderer()
    results = []
    try:
        for unit in units:
            try:
                results.append(process_unit(unit, args.out, args, renderer))
            except Exception as exc:  # noqa: BLE001
                results.append({"unit": str(unit), "name": slug(unit.stem), "pages": [],
                                "errors": [f"{exc}\n{traceback.format_exc()[-1500:]}"]})
            write_index(args.out, results, started)
    finally:
        renderer.close()
    data = write_index(args.out, results, started)
    log(f"report: {args.out / 'index.html'}")
    for name, stats in data["summary"].items():
        applicable = stats["pass"] + stats["fail"]
        log(f"  {name:10s} pass {stats['pass']:4d} fail {stats['fail']:4d} n/a {stats['n/a']:4d}"
            + (f"  ({100 * stats['pass'] / applicable:.1f}%)" if applicable else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
