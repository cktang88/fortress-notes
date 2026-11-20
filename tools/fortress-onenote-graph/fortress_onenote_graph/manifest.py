"""The download cache -> a fortress-onenote manifest (tools/fortress-onenote/SCHEMA.md,
format version 1) + content-addressed ``assets/``, as a folder or a zip.

Graph specifics (also in the README):

* **Ids**: Graph ids are not OneNote GUIDs, so notebooks, section groups, sections
  and pages get ``id``/``file_identity`` = ``"graph:" + <Graph id>``. The Fortress
  importer keys notes by page ``id``, so pages downloaded through Graph never
  collide with pages imported from ``.one`` files (and re-downloads update the
  same notes). ``link_target_id`` is the page GUID OneNote puts in ``onenote:``
  links (``page-id={…}``, read from the page's ``links.oneNoteClientUrl``), so
  links between downloaded pages are rewritten like in a file import.
* **Notebook** ``source_kind`` is ``"graph"`` (a new enum value; allowed without a
  version bump).
* Section order: Graph has no table-of-contents order; sections come in the
  order Graph lists them, then section groups.
* No author, page height, ink or handwriting, section colours.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .htmlconv import PageConverter, clean_text, onenote_link
from .layout import compute as compute_layout
from .store import Store, resource_id

FORMAT = "fortress-onenote-manifest"
FORMAT_VERSION = 1
GENERATOR = "fortress-onenote-graph"
ID_PREFIX = "graph:"
_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")
INK_NOTE = ("Drawings and handwriting (ink) are not downloaded through Microsoft Graph; "
            "pages that had ink are imported without it.")


def sniff_image(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data.startswith(b"BM"):
        return "bmp", "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff", "image/tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:4] == b"\xd7\xcd\xc6\x9a" or data[40:44] == b" EMF":
        return "emf", "image/emf"
    return None


def _mime_ext(mime: str | None) -> str | None:
    return {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
            "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mp4": "m4a",
            "video/mp4": "mp4", "text/plain": "txt"}.get((mime or "").split(";")[0].strip().lower())


def iso(value: Any) -> str | None:
    """Graph timestamp -> ISO 8601 UTC with milliseconds and ``Z``."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    parsed = parsed.astimezone(dt.UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def link_target(page: dict[str, Any]) -> str:
    """The page GUID of ``onenote:`` links to this page, else the Graph id."""

    links = page.get("links") if isinstance(page.get("links"), dict) else {}
    for name in ("oneNoteClientUrl", "oneNoteWebUrl"):
        href = (links.get(name) or {}).get("href") if isinstance(links.get(name), dict) else None
        if isinstance(href, str):
            target = onenote_link(href)
            if target and target.get("page_id"):
                return target["page_id"]
    return ID_PREFIX + str(page["id"])


class _Assets:
    def __init__(self, store: Store, out_dir: Path):
        self.store = store
        self.out_dir = out_dir
        self.assets: dict[str, dict[str, Any]] = {}
        self.bytes = 0

    def resolve(self, url: str, kind: str, filename: str | None) -> dict[str, Any]:
        rid = resource_id(url)
        found = self.store.resource(rid) if rid else None
        if found is None:
            return {"asset": None, "data_status": "missing"}
        info, path = found
        data = path.read_bytes()
        mime = str(info.get("content_type") or "application/octet-stream").split(";")[0].strip()
        if not data:
            return {"asset": None, "data_status": "empty", "mime": mime, "size": 0}
        status = "available"
        if kind == "image":
            sniffed = sniff_image(data)
            if sniffed is None:
                ext, mime, status = "bin", "application/octet-stream", "invalid"
            else:
                ext, mime = sniffed
        else:
            ext = filename.rsplit(".", 1)[1].lower() if filename and "." in filename else ""
            if not _EXT_RE.match(ext):
                ext = _mime_ext(mime) or "bin"
        digest = hashlib.sha256(data).hexdigest()
        name = f"{digest}.{ext}"
        if name not in self.assets:
            target = self.out_dir / "assets" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                shutil.copyfile(path, target)
            self.assets[name] = {"path": f"assets/{name}", "sha256": digest, "size": len(data),
                                 "mime": mime, "ext": ext}
            self.bytes += len(data)
        return {"asset": name, "data_status": status, "mime": mime, "size": len(data)}


def _walk_blocks(blocks: list[dict[str, Any]]):
    for block in blocks:
        yield block
        if block.get("type") == "table":
            for row in block.get("rows") or []:
                for cell in row.get("cells") or []:
                    yield from _walk_blocks(cell.get("blocks") or [])


_COUNT_KEYS = ("section_groups", "sections", "sections_failed", "pages", "subpages", "blocks", "paragraphs",
               "headings", "list_items", "check_items", "code_blocks", "quotes", "tables", "images",
               "printout_pages", "attachments", "attachments_missing", "ink", "math", "embeds", "links_web",
               "links_onenote", "tags", "column_layouts", "text_chars")
_TYPE_COUNTS = {"paragraph": "paragraphs", "heading": "headings", "list_item": "list_items",
                "check_item": "check_items", "code": "code_blocks", "quote": "quotes", "table": "tables",
                "embed": "embeds"}


def build(store: Store, out: Path, *, notebook_names: list[str] | None = None,
          progress: Callable[[int, int, str], None] | None = None,
          warn: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Write ``manifest.json`` + ``assets/`` into ``out`` (a directory, or a ``.zip``
    path: the folder is built next to it and zipped). Returns the summary line."""

    zip_path = out if out.suffix.lower() == ".zip" else None
    out_dir = out.with_name(out.name + ".partial") if zip_path else out
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    assets = _Assets(store, out_dir)
    hierarchy = [nb for nb in store.hierarchy() if isinstance(nb, dict)]
    if notebook_names:
        wanted = {name.casefold() for name in notebook_names}
        hierarchy = [nb for nb in hierarchy if str(nb.get("displayName", "")).casefold() in wanted]
    work: list[tuple[dict[str, Any], dict[str, Any]]] = []
    notebooks = []
    totals = {"notebooks": 0, "sections": 0, "sections_failed": 0, "pages": 0}

    for nb in hierarchy:
        name = clean_text(str(nb.get("displayName") or "Notebook"))
        report = {"counts": {key: 0 for key in _COUNT_KEYS}, "tags_by_label": {}, "warnings": [],
                  "failed_sections": [], "missing_data": [], "layout_notes": []}
        entry = {"id": ID_PREFIX + str(nb["id"]), "file_identity": ID_PREFIX + str(nb["id"]), "name": name,
                 "color": None, "source_kind": "graph", "source_path": name,
                 "children": _entries(nb, [name], report, store, work), "report": report}
        notebooks.append(entry)
        totals["notebooks"] += 1

    total_pages = len(work)
    for done, (page_entry, context) in enumerate(work):
        if progress:
            progress(done, total_pages, " › ".join(page_entry["source_path"]))
        _fill_page(page_entry, context, store, assets, warn)
    if progress:
        progress(total_pages, total_pages, None)

    for entry in notebooks:
        _count(entry)
        counts = entry["report"]["counts"]
        totals["sections"] += counts["sections"]
        totals["sections_failed"] += counts["sections_failed"]
        totals["pages"] += counts["pages"]

    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generator": {"name": GENERATOR, "version": __version__, "parser": "Microsoft Graph OneNote API v1.0"},
        "units": {"text_offsets": "code_points", "geometry": "half_inch", "px_per_geometry_unit": 48.0},
        "input": {"kind": "graph", "name": "Microsoft Graph"},
        "notebooks": notebooks,
        "assets": dict(sorted(assets.assets.items())),
        "report": {"export_errors": [], "input_warnings": [INK_NOTE], "skipped": [],
                   **totals, "assets": len(assets.assets), "assets_bytes": assets.bytes},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    final = out_dir
    if zip_path is not None:
        temporary = zip_path.with_name(zip_path.name + ".tmp")
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(out_dir / "manifest.json", "manifest.json")
            for name in sorted(assets.assets):
                archive.write(out_dir / "assets" / name, f"assets/{name}", compress_type=zipfile.ZIP_STORED)
        temporary.replace(zip_path)
        shutil.rmtree(out_dir)
        final = zip_path
    return {"manifest": str(final if zip_path else final / "manifest.json"), "format_version": FORMAT_VERSION,
            **totals, "assets": len(assets.assets)}


def _entries(container: dict[str, Any], path: list[str], report: dict[str, Any], store: Store,
             work: list) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for section in container.get("sections") or []:
        if not isinstance(section, dict) or "id" not in section:
            continue
        name = clean_text(str(section.get("displayName") or "Section"))
        section_path = [*path, name]
        pages = store.section_pages(section["id"])
        if pages is None:
            error = store.section_error(section["id"]) or {}
            report["failed_sections"].append({
                "path": section_path, "file": None,
                "reason": _failure_reason(error),
                "hint": ("Open the section in OneNote, remove its password if it has one, let it sync, "
                         "then run the download again."),
                "error": str(error.get("message") or "not downloaded"),
            })
            continue
        entry = {"kind": "section", "id": ID_PREFIX + str(section["id"]),
                 "file_identity": ID_PREFIX + str(section["id"]), "name": name, "color": None,
                 "path": section_path, "in_toc": True, "pages": [], "warnings": []}
        stack: list[tuple[int, str]] = []
        for order, page in enumerate(page for page in pages if isinstance(page, dict) and page.get("id")):
            level = page.get("level") if isinstance(page.get("level"), int) and page["level"] >= 0 else 0
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack and level > 0 else None
            page_id = ID_PREFIX + str(page["id"])
            title = " ".join(clean_text(str(page.get("title") or "")).split())
            page_entry = {
                "id": page_id, "link_target_id": link_target(page), "title": title, "title_derived": False,
                "level": level, "parent_id": parent, "order": order,
                "created": iso(page.get("createdDateTime")) or iso(page.get("lastModifiedDateTime")),
                "updated": iso(page.get("lastModifiedDateTime")) or iso(page.get("createdDateTime")),
                "author": None, "source_path": [*section_path, title or "Untitled Page"], "source_url": None,
                "height": None, "ink_text": None, "items": [], "layout": [],
                "stats": {"blocks": 0, "text_chars": 0},
            }
            stack.append((level, page_id))
            entry["pages"].append(page_entry)
            work.append((page_entry, {"graph_page": page, "section": entry, "report": report}))
        children.append(entry)
    for group in container.get("sectionGroups") or []:
        if not isinstance(group, dict) or "id" not in group:
            continue
        name = clean_text(str(group.get("displayName") or "Section group"))
        children.append({"kind": "section_group", "id": ID_PREFIX + str(group["id"]),
                         "file_identity": ID_PREFIX + str(group["id"]), "name": name, "path": [*path, name],
                         "children": _entries(group, [*path, name], report, store, work)})
    return children


def _failure_reason(error: dict[str, Any]) -> str:
    status = error.get("status")
    if status == 403:
        return "Microsoft refused access to this section (password-protected or no permission)."
    if status == 404:
        return "The section no longer exists."
    if error:
        return f"The section could not be listed (HTTP {status} {error.get('code') or ''})".strip() + "."
    return "The section has not been downloaded yet."


def _fill_page(page: dict[str, Any], context: dict[str, Any], store: Store, assets: _Assets,
               warn: Callable[[str], None] | None) -> None:
    graph_page = context["graph_page"]
    report = context["report"]
    html = store.page_html(graph_page["id"])
    meta = store.page_meta(graph_page["id"]) or {}

    def note(message: str) -> None:
        report["warnings"].append({"message": message, "page_id": page["id"], "page_title": page["title"]})
        if warn:
            warn(f"{' › '.join(page['source_path'])}: {message}")

    if html is None:
        note("page content was not downloaded (run the download again); imported empty")
        html = ""
    elif meta.get("lastModifiedDateTime") != graph_page.get("lastModifiedDateTime") or not meta.get("complete"):
        note("page changed in OneNote after this copy was downloaded, or its download did not finish; "
             "run the download again to refresh it")
    converter = PageConverter(assets.resolve)
    result = converter.convert(html)
    for message in result.warnings:
        note(message)
    page["items"] = result.items
    page["layout"], scattered = compute_layout(result.items)
    if scattered:
        report["layout_notes"].append({"page_id": page["id"], "page_title": page["title"],
                                       "message": f"{scattered} band(s) of scattered notes"})
    if not page["title"]:
        page["title"] = result.first_line or "Untitled Page"
        page["title_derived"] = True
        page["source_path"][-1] = page["title"]
    page["source_url"] = result.source_url
    blocks = [block for item in result.items for block in _walk_blocks(item["blocks"])]
    page["stats"] = {"blocks": len(blocks), "text_chars": sum(len(block.get("text") or "") for block in blocks)}
    for block in blocks:
        if block["type"] in ("image", "file") and block.get("data_status") != "available":
            report["missing_data"].append({
                "page_id": page["id"], "page_title": page["title"], "section_path": context["section"]["path"],
                "kind": "image" if block["type"] == "image" else "attachment",
                "name": block.get("filename") or block.get("alt_text"), "data_status": block["data_status"]})


def _count(notebook: dict[str, Any]) -> None:
    report = notebook["report"]
    counts = report["counts"]
    counts["sections_failed"] = len(report["failed_sections"])

    def walk(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            if entry["kind"] == "section_group":
                counts["section_groups"] += 1
                walk(entry["children"])
                continue
            counts["sections"] += 1
            for page in entry["pages"]:
                counts["pages"] += 1
                counts["subpages"] += 1 if page["level"] > 0 else 0
                if any(row["kind"] == "columns" for row in page["layout"]):
                    counts["column_layouts"] += 1
                for item in page["items"]:
                    for block in _walk_blocks(item["blocks"]):
                        counts["blocks"] += 1
                        kind = block["type"]
                        if kind in _TYPE_COUNTS:
                            counts[_TYPE_COUNTS[kind]] += 1
                        if kind == "image":
                            counts["printout_pages" if block.get("is_printout_page") else "images"] += 1
                        if kind == "file":
                            counts["attachments"] += 1
                            counts["attachments_missing"] += block["data_status"] != "available"
                        for tag in block.get("tags") or []:
                            counts["tags"] += 1
                            label = tag["label"] or tag["shape"]
                            report["tags_by_label"][label] = report["tags_by_label"].get(label, 0) + 1
                        for mark in block.get("marks") or []:
                            if mark["type"] == "link":
                                counts["links_onenote" if "onenote" in mark else "links_web"] += 1
                        counts["text_chars"] += len(block.get("text") or "")
    walk(notebook["children"])
