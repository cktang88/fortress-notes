"""OneNote importer: exported notebooks -> living Fortress notes.

Pipeline (docs/onenote-import.md, F3):

1. **convert** -- the Rust converter ``fortress-onenote`` (tools/fortress-onenote)
   reads the export (.zip, folder, .one, .onetoc2, .onepkg) and writes
   ``manifest.json`` + content-addressed ``assets/`` (contract:
   tools/fortress-onenote/SCHEMA.md, format version 1).
2. **import** -- notebooks, section groups and sections become folders
   (``sources.ensure_folder_path``, keyed by their OneNote file identities);
   every page becomes one note through ``sources.upsert_document_from_source``
   keyed by the page GUID, so re-importing the same export updates notes in
   place, keeps local edits, and reports untouched pages as ``unchanged``.
3. **link** -- ``onenote:`` links become ``[[document_id|Title]]`` links. A link
   whose target page was not imported yet is kept as plain text in the first
   pass; once every page exists, the pages that had such links are upserted again
   with the links resolved (an ordinary "untouched blocks follow the source"
   update). On a re-import every target already exists, so nothing changes.

Design choices:

* **Subpages** stay in their section folder, in OneNote's order (a subpage comes
  right after its parent, as in OneNote's page list). The parent page gets a
  trailing "Subpages" list linking to its direct subpages, and every page keeps
  ``level`` / ``parent_page_id`` in ``provenance.extra.onenote`` so a future
  nested-page tree can be rebuilt without re-importing. (Folders always list
  before notes in the sidebar, so a folder per parent page would separate a
  parent from its subpages.)
* **Layout**: the converter's reading-order rows are followed; a ``columns`` row
  becomes a ``columns`` block with one ``column`` per OneNote column (relative
  ``width``), nested columns included. "Scattered" bands (more than 4 columns)
  are stacked instead.
* **Block ids**: a block with a OneNote object id (manifest ``object_id``, the
  content's ExGUID, stable across edits) gets the source id
  ``<page GUID>/obj:<object_id>``, so a paragraph edited in OneNote updates the
  same Fortress block (same id, links and backlinks; a block edited here keeps
  the local text and records a conflict). Blocks without one (format-1
  manifests, ambiguous content) fall back to ``<page GUID>/<type>:<hash of its
  content>`` (duplicates get ``#2``, ``#3``); the hash id is also passed as the
  block's previous id, so notes imported before object ids existed are matched
  and migrated in place. The ids go to ``provenance.extra.onenote``
  (``object_id``, ``element_id``; a table lists its cells' ids as ``nested``).
* **Paragraph links** (``onenote:…&object-id={GUID}&N``): when the target page
  is imported and has a block whose ``element_id``/``object_id`` equals the
  link's ``object_ref``, the link becomes a block reference ``(( block_id ))``
  (it previews the paragraph's current text and records a backlink); otherwise
  it links to the page.
* **Removed in OneNote**: on a re-import, notes previously imported from a
  notebook in this export whose page is no longer there are kept and marked
  ``provenance.source_removed`` (+ ``source_removed_at``), and listed in the
  report with section folders that disappeared (sections that failed to read
  are never counted as removed).
* **Assets** are copied once into ``NOTES_DIR/assets/<sha256>.<ext>`` (deduped by
  hash). Extensions a browser would render as active content (html, svg from
  attachments, ...) are stored as ``.bin`` (served as a download).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import assets, block_store, documents, provenance, sources

SOURCE = "onenote_import"
IMPORTER_NAME = "fortress-onenote-import"
IMPORTER_VERSION = "1"
MANIFEST_FORMAT = "fortress-onenote-manifest"
SUPPORTED_FORMAT_VERSION = 1
PX_PER_UNIT = 48.0  # OneNote half-inch geometry units -> CSS px at 96 dpi
CONVERTER_TIMEOUT_S = float(os.environ.get("FORTRESS_ONENOTE_TIMEOUT_S", "3600"))
MAX_WARNINGS = 300
IMPORTS_DIRNAME = ".fortress-imports"

_GUID_RE = re.compile(r"^\{?[0-9A-Fa-f-]{36}\}?$")
_ASSET_NAME_RE = re.compile(r"^([0-9a-f]{64})\.([A-Za-z0-9]{1,10})$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MONOSPACE = ("consolas", "courier", "courier new", "lucida console", "menlo", "monaco",
              "source code pro", "cascadia", "fira code", "fira mono", "jetbrains mono",
              "sf mono", "dejavu sans mono", "andale mono", "monospace")
_NUMBER_STYLES = frozenset({"upper_roman", "lower_roman", "upper_alpha", "lower_alpha"})
_BAD_LINK_SCHEMES = ("javascript:", "vbscript:", "data:")
# Link targets kept as links; anything else (file:, UNC paths, ms-word:, javascript:, ...)
# stays plain text and is listed in the block's provenance (``dropped_links``).
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel", "ftp", "ftps", "sftp", "news", "sms", "geo"})
MAX_LINK_LENGTH = 2048
MAX_TAG_LABEL = 100
# C0/C1 control characters other than tab and newline (OneNote files can carry them;
# they are invisible there and break Markdown/terminals here).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_SPACE_CONTROL_RE = re.compile(r"[\x0c\x1c-\x1f\x85]")
_LINK_JUNK_RE = re.compile(r"[\x00-\x20\x7f-\x9f]")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,31}):")
_WWW_RE = re.compile(r"^www\.[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+([/?#]\S*)?$", re.IGNORECASE)


def _clean(text: str) -> str:
    """``text`` without control characters (tab and newline kept; CR becomes newline)."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Separators (form feed, file/group/record/unit separators, NEL) read as spaces.
    return _CONTROL_RE.sub("", _SPACE_CONTROL_RE.sub(" ", text))


def _clean_manifest(value: Any) -> Any:
    """Every string of the manifest cleaned, except rich ``text`` (its marks index into it;
    runs are cleaned once split)."""

    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, list):
        return [_clean_manifest(item) for item in value]
    if isinstance(value, dict):
        rich = "marks" in value
        return {key: (item if rich and key == "text" else _clean_manifest(item)) for key, item in value.items()}
    return value


def safe_link(href: object) -> str | None:
    """``href`` when it is safe to keep as a clickable link, else None."""

    if not isinstance(href, str):
        return None
    href = href.strip()
    if not href or len(href) > MAX_LINK_LENGTH:
        return None
    # Browsers ignore tabs/newlines/control characters inside a scheme ("java\nscript:").
    probe = _LINK_JUNK_RE.sub("", href)
    if _SCHEME_RE.match(probe) is None and _WWW_RE.match(href):
        # OneNote stores "www.example.com" links without a scheme and opens them as web pages.
        href = probe = "http://" + href
    match = _SCHEME_RE.match(probe)
    if match is None or match.group(1).lower() not in _SAFE_LINK_SCHEMES:
        return None
    if _CONTROL_RE.search(href) or any(ch in href for ch in "\n\t"):
        return None
    return href
# Image types the local OCR reads (images.extract_and_store).
_OCR_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
# Images shown as images; any other image asset (.bin, ...) is kept as a file.
_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif",
                               "emf", "wmf", "svg"})
# Pages whose imported text covers less of the manifest's text are reported.
COVERAGE_THRESHOLD = 0.98
# Extensions served as themselves from /media; anything else is stored as .bin.
_SAFE_EXTENSIONS = frozenset(
    """png jpg jpeg gif webp bmp tif tiff heic heif emf wmf pdf xps oxps
    mp3 wav m4a aac ogg oga opus wma amr flac mp4 m4v mov webm avi wmv 3gp mkv
    txt csv md rtf doc docx dot dotx xls xlsx xlsm ppt pptx pps ppsx odt ods odp
    vsd vsdx pub one zip 7z rar gz tar epub pages numbers key msg""".split()
)


class OneNoteImportError(RuntimeError):
    """A job-level failure with a message meant for the user."""


# --- Converter ---------------------------------------------------------------------------


def converter_path() -> Path | None:
    """Where the ``fortress-onenote`` binary is, or None.

    Order: ``FORTRESS_ONENOTE_BIN``; the repository build
    (``tools/fortress-onenote/target/release``); next to the running Python
    (an app bundle ships them side by side, also ``../Resources/bin``); ``PATH``.
    """

    candidates: list[Path] = []
    configured = os.environ.get("FORTRESS_ONENOTE_BIN", "").strip()
    if configured:
        return Path(configured).expanduser() if Path(configured).expanduser().is_file() else None
    repo = Path(__file__).resolve().parents[2]
    candidates.append(repo / "tools" / "fortress-onenote" / "target" / "release" / "fortress-onenote")
    executable = Path(sys.executable).resolve().parent
    candidates += [executable / "fortress-onenote", executable.parent / "Resources" / "bin" / "fortress-onenote"]
    found = shutil.which("fortress-onenote")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


_converters: set[subprocess.Popen] = set()
_converters_lock = threading.Lock()


def terminate_converters() -> None:
    """Stops every running converter (the backend is shutting down)."""

    with _converters_lock:
        running = list(_converters)
    for process in running:
        with contextlib.suppress(OSError):
            process.kill()


def run_converter(input_path: Path, out_dir: Path, *, timeout: float | None = None,
                  cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Run the converter; returns its JSON summary line. Raises OneNoteImportError
    (or :class:`ImportCancelled` when ``cancelled()`` turns true while it runs)."""

    binary = converter_path()
    if binary is None:
        raise OneNoteImportError(
            "The OneNote converter (fortress-onenote) was not found. Set FORTRESS_ONENOTE_BIN "
            "or build it: cd tools/fortress-onenote && cargo build --release"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = timeout or CONVERTER_TIMEOUT_S
    try:
        process = subprocess.Popen(
            [str(binary), str(input_path), "--out", str(out_dir), "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise OneNoteImportError(f"The OneNote converter could not be started: {exc}") from exc
    with _converters_lock:
        _converters.add(process)
    try:
        stdout, stderr = _wait_for_converter(process, limit, cancelled)
    finally:
        with _converters_lock:
            _converters.discard(process)
    if process.returncode != 0:
        detail = (stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {process.returncode}"
        raise OneNoteImportError(f"The OneNote export could not be converted: {message}")
    summary: dict[str, Any] = {}
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            summary = json.loads(line)
            break
        except ValueError:
            continue
    if not (out_dir / "manifest.json").is_file():
        raise OneNoteImportError("The OneNote converter did not write a manifest.")
    return summary


def _wait_for_converter(process: subprocess.Popen, limit: float,
                        cancelled: Callable[[], bool] | None) -> tuple[str, str]:
    deadline = time.monotonic() + limit
    while True:
        try:
            return process.communicate(timeout=0.25)
        except subprocess.TimeoutExpired:
            stopped_by_user = cancelled is not None and cancelled()
            if stopped_by_user or time.monotonic() > deadline:
                process.kill()
                process.communicate()
                if stopped_by_user:
                    raise ImportCancelled() from None
                raise OneNoteImportError(
                    f"The OneNote converter took longer than {int(limit)} s and was stopped."
                ) from None


# --- Manifest -> source blocks -----------------------------------------------------------


def _norm_guid(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().upper()
    if _GUID_RE.match(value) and not value.startswith("{"):
        value = "{" + value + "}"
    return value


def _page_key(page: dict[str, Any]) -> str | None:
    """A page's import key: its manifest ``id`` (the GUID; copies carry ``~hash``)."""

    raw = page.get("id") or page.get("link_target_id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    guid, tilde, suffix = raw.strip().partition("~")
    return (_norm_guid(guid) or guid) + (tilde + suffix.lower() if tilde else "")


def _short_hash(*parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8"))
    return digest.hexdigest()[:16]


def _link_label(title: str) -> str:
    label = " ".join(str(title).split()).replace("]", ")").replace("[", "(").replace("|", "/")
    return label or "Untitled"


def _px(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(float(value) * PX_PER_UNIT)


@dataclass
class _Target:
    document_id: str
    title: str


@dataclass
class _PageContext:
    """Per-page conversion state: link resolution, assets, counts."""

    importer: "_Importer"
    page_guid: str
    page_label: str
    stats: dict[str, int] = field(default_factory=dict)
    unresolved: list[dict[str, str]] = field(default_factory=list)
    pending: set[str] = field(default_factory=set)  # targets that may resolve later
    rewritten: int = 0
    block_level_links: int = 0  # paragraph links that could only point at their page
    block_references: int = 0  # paragraph links that became block references
    ocr_assets: list[str] = field(default_factory=list)
    warned: set[str] = field(default_factory=set)  # image asset names without text
    dropped_links: list[str] = field(default_factory=list)  # unsafe/unsupported targets kept as text

    def count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    def warn(self, message: str) -> None:
        self.importer.warn(f"{self.page_label}: {message}")

    def warn_once(self, message: str) -> None:
        if message not in self.warned:
            self.warned.add(message)
            self.warn(message)


def _runs(text: str, marks: list[dict[str, Any]] | None, ctx: _PageContext,
          block_links: list[str]) -> list[dict[str, Any]]:
    """Compact runs (sources.source_block's form) from text + code-point mark spans."""

    if not text:
        return []
    length = len(text)
    usable: list[tuple[int, int, dict[str, Any]]] = []
    for mark in marks or []:
        if not isinstance(mark, dict):
            continue
        try:
            start, end = max(0, int(mark.get("start", 0))), min(length, int(mark.get("end", 0)))
        except (TypeError, ValueError):
            continue
        if start < end:
            usable.append((start, end, mark))
    # Resolve each link mark once (counts and the unresolved list are per link).
    hrefs: dict[int, str | None] = {}
    for index, (start, end, mark) in enumerate(usable):
        if mark.get("type") == "link":
            hrefs[index] = _link_href(mark, text[start:end], ctx, block_links)
    bounds = sorted({0, length, *(s for s, _, _ in usable), *(e for _, e, _ in usable)})
    equations = {index: latex for index, (_, _, mark) in enumerate(usable)
                 if mark.get("type") == "math" and (latex := _inline_latex(mark)) is not None}
    runs: list[dict[str, Any]] = []
    emitted: set[int] = set()
    for a, b in zip(bounds, bounds[1:]):
        equation = next((index for index in equations if usable[index][0] <= a and usable[index][1] >= b), None)
        if equation is not None:
            # OneNote's linear text of an equation loses its structure (a/b, x², ∮…):
            # keep the LaTeX instead, as inline code ``$…$`` (there is no inline math run).
            if equation not in emitted:
                emitted.add(equation)
                runs.append({"text": f"${equations[equation]}$", "code": 1})
            continue
        run: dict[str, Any] = {"text": _clean(text[a:b])}
        for index, (start, end, mark) in enumerate(usable):
            if start > a or end < b:
                continue
            kind = mark.get("type")
            if kind == "bold":
                run["b"] = 1
            elif kind == "italic":
                run["i"] = 1
            elif kind == "underline":
                run["u"] = 1
            elif kind in {"strikethrough", "strike"}:
                run["s"] = 1
            elif kind == "superscript":
                run["sup"] = 1
            elif kind == "subscript":
                run["sub"] = 1
            elif kind in {"color", "highlight"}:
                color = mark.get("color")
                if isinstance(color, str) and _COLOR_RE.match(color):
                    run[kind] = color.upper()
            elif kind == "font":
                family = str(mark.get("family") or "").strip().lower()
                if family and any(family.startswith(name) for name in _MONOSPACE):
                    run["code"] = 1
            elif kind == "link":
                href = hrefs.get(index)
                if href:
                    run["link"] = href
            # font_size is dropped; equations with LaTeX were written above.
        # Monospace runs keep their other marks (colour, highlight, bold...): the Markdown
        # export writes them as HTML around the code span and reads them back.
        runs.append(run)
    return runs


def _link_href(mark: dict[str, Any], label: str, ctx: _PageContext,
               block_links: list[str]) -> str | None:
    href = str(mark.get("href") or "").strip()
    onenote = mark.get("onenote") if isinstance(mark.get("onenote"), dict) else None
    if onenote is None and href.lower().startswith("onenote:"):
        onenote = {}
    if onenote is not None:
        page_id = _norm_guid(onenote.get("page_id"))
        target = ctx.importer.targets.get(page_id) if page_id else None
        if target is not None:
            ctx.rewritten += 1
            if onenote.get("object_id") or onenote.get("object_ref"):
                block_id = ctx.importer.find_block(target.document_id, onenote.get("object_ref"))
                if block_id is not None:
                    ctx.block_references += 1
                    return f"(( {block_id} ))"
                ctx.block_level_links += 1
                if (
                    onenote.get("object_ref") and page_id in ctx.importer.manifest_pages
                    and page_id not in ctx.importer._linked_this_run
                ):
                    # The paragraph may be new in a page imported later in this run.
                    ctx.pending.add(page_id)
            return f"[[{target.document_id}|{_link_label(target.title)}]]"
        if page_id and page_id in ctx.importer.manifest_pages:
            ctx.pending.add(page_id)
        ctx.unresolved.append({
            "page": ctx.page_label,
            "target": href or label,
            "title": str(onenote.get("page_title") or label),
        })
        block_links.append(href)
        return None
    safe = safe_link(href)
    if safe is None and href:
        ctx.dropped_links.append(href[:MAX_LINK_LENGTH])
    return safe


def _inline_latex(mark: dict[str, Any]) -> str | None:
    latex = mark.get("latex")
    if not isinstance(latex, str) or not latex.strip():
        return None
    return " ".join(_strip_math_delimiters(latex).split()) or None


def _text_without_equations(block: dict[str, Any]) -> str:
    """A text block's text minus the inline equations the importer writes as LaTeX."""

    text = str(block.get("text") or "")
    spans = sorted(
        (mark["start"], mark["end"]) for mark in block.get("marks") or []
        if isinstance(mark, dict) and mark.get("type") == "math" and _inline_latex(mark) is not None
        and isinstance(mark.get("start"), int) and isinstance(mark.get("end"), int)
    )
    for start, end in reversed(spans):
        text = text[:start] + " " + text[end:]
    return text


def _strip_math_delimiters(latex: str) -> str:
    latex = latex.strip()
    for opening, closing in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)")):
        if latex.startswith(opening) and latex.endswith(closing) and len(latex) >= len(opening) + len(closing):
            return latex[len(opening):-len(closing)].strip()
    return latex


def _apply_tag_colors(runs: list[dict[str, Any]], tags: object) -> None:
    """Note tags such as "Highlight" or "Critical" colour their paragraph in OneNote
    (highlight / font colour): give that colour to runs without their own."""

    highlight = color = None
    for tag in tags if isinstance(tags, list) else []:
        if not isinstance(tag, dict):
            continue
        if highlight is None and isinstance(tag.get("highlight_color"), str) and _COLOR_RE.match(tag["highlight_color"]):
            highlight = tag["highlight_color"].upper()
        if color is None and isinstance(tag.get("text_color"), str) and _COLOR_RE.match(tag["text_color"]):
            color = tag["text_color"].upper()
    if highlight is None and color is None:
        return
    for run in runs:
        if "code" in run:
            continue
        if highlight and "highlight" not in run:
            run["highlight"] = highlight
        if color and "color" not in run:
            run["color"] = color


def _text_block(block: dict[str, Any], ctx: _PageContext) -> tuple[dict[str, Any], list[str]]:
    block_links: list[str] = []
    runs = _runs(str(block.get("text") or ""), block.get("marks"), ctx, block_links)
    _apply_tag_colors(runs, block.get("tags"))
    kind = block.get("type")
    result: dict[str, Any] = {"inline": runs}
    if kind == "heading":
        level = block.get("level")
        result.update(type="heading", level=level if isinstance(level, int) and 1 <= level <= 6 else 1)
    elif kind in {"list_item", "check_item"}:
        # A to-do may also sit in a numbered / bulleted list (``list`` is then set).
        info = block.get("list") if isinstance(block.get("list"), dict) else {}
        result.update(type="list", ordered=bool(info.get("ordered")))
        if kind == "check_item":
            result["checked"] = bool(block.get("checked"))
        if info.get("marker"):
            result["list_format"] = str(info["marker"])
        if info.get("ordered") and info.get("style") in _NUMBER_STYLES:
            # a. b. c. / i. ii. iii. numbering (the marker alone does not say which).
            result.setdefault("attrs", {})["list_style"] = info["style"]
        if isinstance(info.get("restart"), int) and not isinstance(info["restart"], bool):
            result.setdefault("attrs", {})["list_start"] = info["restart"]
        if block.get("checked_at"):
            result["checked_at"] = str(block["checked_at"])
    elif kind == "quote":
        result["type"] = "quote"
    else:
        result["type"] = "paragraph"
    if block.get("align") in {"center", "right"}:
        # BlockNote's paragraph prop (what the web editor reads and writes).
        result.setdefault("attrs", {})["textAlignment"] = block["align"]
    ctx.count("text_blocks")
    return result, block_links


def _note_tags(block: dict[str, Any]) -> list[str]:
    labels = []
    for tag in block.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        if block.get("type") == "check_item" and tag.get("checkable"):
            continue  # it is the checkbox
        # Labels can be empty (third-party writers), padded or very long.
        label = " ".join(str(tag.get("label") or "").split())[:MAX_TAG_LABEL]
        if not label:
            shape = str(tag.get("shape") or "")
            label = "" if shape in {"", "no_icon"} else shape.replace("_", " ").capitalize()
        if label and label not in labels:
            labels.append(label)
    return labels


def _convert_block(block: dict[str, Any], ctx: _PageContext, nested: bool = False) -> dict[str, Any] | None:
    """One manifest block -> a source block dict (no children). None to drop it."""

    kind = block.get("type")
    block_links: list[str] = []
    dropped_before = len(ctx.dropped_links)
    result: dict[str, Any]
    if kind in {"paragraph", "heading", "list_item", "check_item", "quote"}:
        result, block_links = _text_block(block, ctx)
        if kind == "check_item":
            ctx.count("check_items")
    elif kind == "code":
        result = {"type": "code", "text": str(block.get("text") or "")}
    elif kind == "math":
        latex = block.get("latex")
        if isinstance(latex, str) and latex.strip():
            latex = _strip_math_delimiters(latex)
        else:
            latex = str(block.get("text") or "")
            if not (isinstance(block.get("mathml"), str) and block["mathml"]):
                # Third-party writers store equations without LaTeX/MathML: only the
                # flattened text is left (x=−b±b2−4ac2a), so say the structure is lost.
                ctx.warn_once("an equation had no LaTeX or MathML; its plain text was kept "
                              "(fractions, powers and roots may read flattened)")
        result = {"type": "math", "latex": latex}
        if isinstance(block.get("mathml"), str) and block["mathml"]:
            result["mathml"] = block["mathml"]
        ctx.count("math")
    elif kind == "table":
        result = _table(block, ctx)
        if not nested:
            result = _fit_table(result, 0, ctx)
        ctx.count("tables")
    elif kind == "image":
        result = _image(block, ctx)
    elif kind == "ink":
        result = _ink(block, ctx)
    elif kind == "file":
        result = _file(block, ctx)
    elif kind == "embed":
        url = safe_link(block.get("url"))
        raw = str(block.get("url") or "").strip()
        if url is None and not raw:
            return None
        thumbnail = ctx.importer.asset_url(block.get("thumbnail_asset")) if block.get("thumbnail_asset") else None
        if url is None:
            # Not a web address (file:, javascript:, ...): keep it as text, not a link.
            ctx.dropped_links.append(raw[:MAX_LINK_LENGTH])
            result = {"type": "paragraph", "text": raw}
        elif thumbnail:
            # The video's poster frame (what OneNote shows), linked to the video.
            result = {"type": "image", "url": thumbnail, "caption": url, "name": "Video",
                      "attrs": {"link": url, "embed": True}}
            alt = str(block.get("alt_text") or "").strip()
            if alt:
                result["alt"] = alt
        else:
            result = {"type": "paragraph", "inline": [{"text": url, "link": url}]}
        ctx.count("embeds")
    else:
        text = str(block.get("text") or "")
        ctx.warn(f"unsupported OneNote content ({kind}) kept as text" if text else f"unsupported OneNote content ({kind}) skipped")
        if not text:
            return None
        result = {"type": "paragraph", "text": text}
    tags = _note_tags(block)
    if tags:
        result.setdefault("attrs", {})["onenote_tags"] = tags
    if not nested:
        dropped = ctx.dropped_links[dropped_before:]
        result["id"], previous = _block_external_id(ctx.page_guid, block)
        if previous:
            result["previous_ids"] = previous
        extra: dict[str, Any] = {}
        if block_links:
            extra["unresolved_links"] = block_links
        if dropped:
            extra["dropped_links"] = dropped
        ids = _onenote_ids(block)
        if kind == "table":
            nested_ids = sorted(_nested_object_ids(block))[:200]
            if nested_ids:
                ids["nested"] = nested_ids
        if ids:
            extra["onenote"] = ids
        if block.get("source_url") and str(block["source_url"]).lower().startswith(("http://", "https://")):
            result["provenance"] = {"source_url": str(block["source_url"])}
        if extra:
            result.setdefault("provenance", {})["extra"] = extra
    return result


_ID_KEYS = ("object_id", "element_id")
_OBJECT_ID_RE = re.compile(r"^\{[0-9A-Fa-f-]{36}\}&[0-9A-Fa-f]{1,8}$")


def _object_id(value: object) -> str | None:
    """A manifest ExGUID (``{GUID}&N``) in canonical upper case, or None."""

    if not isinstance(value, str) or not _OBJECT_ID_RE.match(value.strip()):
        return None
    guid, _, counter = value.strip().partition("&")
    return f"{guid.upper()}&{int(counter, 16):X}"


def _onenote_ids(block: dict[str, Any]) -> dict[str, str]:
    return {key: oid for key in _ID_KEYS if (oid := _object_id(block.get(key)))}


def _nested_object_ids(block: dict[str, Any]) -> set[str]:
    """Object/element ids of the blocks inside a table's cells (any depth)."""

    found: set[str] = set()
    for row in block.get("rows") or []:
        for cell in (row or {}).get("cells") or []:
            for inner in (cell or {}).get("blocks") or []:
                if isinstance(inner, dict):
                    found.update(_onenote_ids(inner).values())
                    if inner.get("type") == "table":
                        found |= _nested_object_ids(inner)
    return found


def _legacy_external_id(page_guid: str, block: dict[str, Any]) -> str:
    """The content-hash id (all that format-1 manifests allow)."""

    kind = block.get("type")
    if kind in {"paragraph", "heading", "list_item", "check_item", "quote", "code"}:
        digest = _short_hash(kind, block.get("text") or "")
    elif kind == "image":
        digest = _short_hash(kind, block.get("asset") or block.get("original_name") or "", block.get("alt_text") or "")
    else:
        # Ids added by newer converters must not change the hash of old blocks.
        digest = _short_hash(kind, json.dumps(_without_ids(block), sort_keys=True, ensure_ascii=False))
    return f"{page_guid}/{kind}:{digest}"


def _without_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_ids(item) for key, item in value.items()
            if key not in {"element_id", "object_ref"}
            and not (key == "object_id" and "&" in str(item))
        }
    if isinstance(value, list):
        return [_without_ids(item) for item in value]
    return value


def _block_external_id(page_guid: str, block: dict[str, Any]) -> tuple[str, list[str]]:
    """(source id, earlier source ids) for a top-level manifest block."""

    legacy = _legacy_external_id(page_guid, block)
    oid = _object_id(block.get("object_id"))
    if oid is None:
        return legacy, []
    return f"{page_guid}/obj:{oid}", [legacy]


def _tree(blocks: list[dict[str, Any]], ctx: _PageContext, nested: bool = False) -> list[dict[str, Any]]:
    """Flat manifest blocks (with ``indent``) -> nested source block dicts."""

    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, list[dict[str, Any]]]] = [(-1, roots)]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        converted = _convert_block(block, ctx, nested=nested)
        if converted is None:
            continue
        indent = block.get("indent") if isinstance(block.get("indent"), int) else 0
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        stack[-1][1].append(converted)
        converted.setdefault("children", [])
        stack.append((indent, converted["children"]))
    return roots


def _table(block: dict[str, Any], ctx: _PageContext) -> dict[str, Any]:
    rows = []
    for row in block.get("rows") or []:
        cells = []
        for cell in (row or {}).get("cells") or []:
            nested = _tree((cell or {}).get("blocks") or [], ctx, nested=True)
            entry: dict[str, Any] = {}
            first = nested[0] if nested else None
            if (
                first is not None and first.get("type") == "paragraph"
                and not first.get("children") and "inline" in first
                # A cell's first line holds only runs: a tagged paragraph stays a block.
                and set(first.get("attrs") or {}) <= {"textAlignment"}
            ):
                entry["inline"] = first["inline"]
                align = (first.get("attrs") or {}).get("textAlignment")
                if align:
                    entry["align"] = align  # the cell's first line is centred / right-aligned
                nested = nested[1:]
            if nested:
                entry["blocks"] = [_nested_safe(item) for item in nested]
            background = (cell or {}).get("background")
            if isinstance(background, str) and _COLOR_RE.match(background):
                entry["background"] = background.upper()
            cells.append(entry)
        if not cells:
            cells = [""]
        rows.append({"cells": cells})
    if not rows:
        rows = [{"cells": [""]}]
    widths = [_px(width) for width in block.get("column_widths") or []]
    return {
        "type": "table",
        "rows": rows,
        "column_widths": widths,
        "borders": bool(block.get("borders_visible", True)),
    }


# How deep blocks may nest inside table cells (block_types: cell blocks start at 1;
# children and nested tables' cells each add one).
MAX_CELL_DEPTH = 8


def _fit_table(table: dict[str, Any], depth: int, ctx: _PageContext) -> dict[str, Any]:
    """``table`` (source form, at ``depth``) with its cells' blocks fitted to MAX_CELL_DEPTH:
    deeper list levels are lifted to the deepest allowed level and tables that would
    nest too deeply become one paragraph per row, so no content is refused."""

    rows = []
    for row in table.get("rows") or []:
        cells = []
        for cell in row.get("cells") or []:
            if isinstance(cell, dict) and cell.get("blocks"):
                cell = {**cell, "blocks": _fit_blocks(cell["blocks"], depth + 1, ctx)}
            cells.append(cell)
        rows.append({**row, "cells": cells})
    return {**table, "rows": rows}


def _fit_blocks(blocks: list[dict[str, Any]], depth: int, ctx: _PageContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        block = dict(block)
        children = block.pop("children", None) or []
        if block.get("type") == "table":
            if depth + 1 > MAX_CELL_DEPTH:
                ctx.warn("a table nested too deeply was imported as text, one paragraph per row")
                out.extend({"type": "paragraph", "text": " | ".join(_source_text(cell) for cell in row.get("cells") or [])}
                           for row in block.get("rows") or [])
            else:
                out.append(_fit_table(block, depth, ctx))
            out.extend(_fit_blocks(children, depth, ctx))
            continue
        out.append(block)
        if children:
            if depth + 1 > MAX_CELL_DEPTH:
                out.extend(_fit_blocks(children, depth, ctx))  # lifted to this level
            else:
                block["children"] = _fit_blocks(children, depth + 1, ctx)
    return out


def _source_text(item: object) -> str:
    """Plain text of a source-form block or cell (for flattening)."""

    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    parts = []
    if isinstance(item.get("inline"), list):
        parts.append("".join(str(run.get("text") or "") for run in item["inline"] if isinstance(run, dict)))
    elif isinstance(item.get("text"), str):
        parts.append(item["text"])
    for row in item.get("rows") or []:
        parts.append(" | ".join(_source_text(cell) for cell in (row or {}).get("cells") or []))
    for child in [*(item.get("blocks") or []), *(item.get("children") or [])]:
        parts.append(_source_text(child))
    return " ".join(part for part in parts if part)


def _nested_safe(block: dict[str, Any]) -> dict[str, Any]:
    """Cells cannot hold layout blocks; drop source-only keys for in-cell blocks."""

    block = {key: value for key, value in block.items() if key not in {"id", "previous_ids", "provenance"}}
    if block.get("type") in {"columns", "column"}:
        block = {"type": "paragraph", "text": ""}
    if block.get("children"):
        block["children"] = [_nested_safe(child) for child in block["children"]]
    else:
        block.pop("children", None)
    return block


def _image(block: dict[str, Any], ctx: _PageContext) -> dict[str, Any] | None:
    ctx.count("images")
    if block.get("is_printout_page"):
        ctx.count("printout_pages")
    url = ctx.importer.asset_url(block.get("asset"))
    alt = str(block.get("alt_text") or "").strip()
    name = block.get("original_name")
    if url is None:
        ctx.count("images_missing")
        status = block.get("data_status")
        what = {"empty": "Empty image (no data) in the OneNote export",
                "invalid": "Image OneNote marked as damaged"}.get(status, "Image missing from the OneNote export")
        label = f"{what}{f' ({name})' if name else ''}"
        return {"type": "paragraph", "inline": [{"text": label, "i": 1}] + ([{"text": f"\n{alt}"}] if alt else [])}
    if url.rsplit(".", 1)[-1].lower() not in _IMAGE_EXTENSIONS:
        # The bytes are not a picture (damaged data, or a format browsers cannot show):
        # keep them as a downloadable file instead of a broken image.
        ctx.warn_once("image data that is not a picture was kept as a file attachment")
        result = {"type": "file", "url": url, "name": _file_name(name or "image.bin"),
                  "attrs": {"caption": "Image data that cannot be shown" + (f": {alt}" if alt else "")}}
        return result
    result: dict[str, Any] = {"type": "image", "url": url}
    if alt:
        result["alt"] = alt
    elif str(block.get("asset")).rsplit(".", 1)[-1].lower() in _OCR_EXTENSIONS:
        # No OneNote OCR text: the app's own OCR makes it searchable (background).
        ctx.ocr_assets.append(str(block["asset"]))
    for key in ("width", "height"):
        value = _px(block.get(key))
        if value:
            result[key] = value
    if name:
        result["name"] = _file_name(name)
    attrs: dict[str, Any] = {}
    if block.get("hyperlink"):
        link = safe_link(block["hyperlink"])
        if link:
            attrs["link"] = link
        else:
            ctx.dropped_links.append(str(block["hyperlink"])[:MAX_LINK_LENGTH])
    if block.get("is_printout_page"):
        attrs["printout_page"] = block.get("printout_page_number")
    if block.get("is_background"):
        attrs["background"] = True
    original = ctx.importer.asset_url(block.get("original_asset")) if block.get("original_asset") else None
    if original:
        attrs["original_url"] = original
    if attrs:
        result["attrs"] = attrs
    return result


def _ink(block: dict[str, Any], ctx: _PageContext) -> dict[str, Any] | None:
    ctx.count("ink")
    url = ctx.importer.asset_url(block.get("asset"), allow_svg=True)
    text = str(block.get("recognized_text") or "").strip()
    if url is None:
        return {"type": "paragraph", "text": text} if text else None
    result: dict[str, Any] = {"type": "image", "url": url, "name": "Ink drawing"}
    if text:
        result["alt"] = text
        ctx.count("ink_text")
    for key in ("width", "height"):
        value = _px(block.get(key))
        if value:
            result[key] = value
    result["attrs"] = {"ink": True}
    return result


def _file(block: dict[str, Any], ctx: _PageContext) -> dict[str, Any]:
    ctx.count("files")
    name = _file_name(block.get("filename"))
    result: dict[str, Any] = {"type": "file", "name": name}
    url = ctx.importer.asset_url(block.get("asset"))
    if url is None:
        ctx.count("files_missing")
        result["caption"] = {"empty": "Empty file (no data) in the OneNote export",
                             "invalid": "File OneNote marked as damaged"}.get(
            block.get("data_status"), "File missing from the OneNote export")
    else:
        result["url"] = url
    if isinstance(block.get("size"), int) and block["size"] > 0:
        result["size"] = block["size"]
    mime = block.get("mime")
    if isinstance(mime, str) and mime:
        result["mime"] = mime
    if block.get("media_kind") in {"audio", "video"}:
        result.setdefault("attrs", {})["media_kind"] = block["media_kind"]
    if "caption" in result:
        result.setdefault("attrs", {})["caption"] = result.pop("caption")
    return result


MAX_FILE_NAME = 200
# provenance.source_path / source_url limit (provenance._STRING_FIELDS).
MAX_SOURCE_PATH = 2000
MAX_PATH_PART = 200


def _source_path(parts: list[Any], fallback: str) -> str:
    """"Notebook › Group › Section › Page" within MAX_SOURCE_PATH: very long names are cut,
    and with very deep section groups the middle groups give way to "…"."""

    names = [name if len(name) <= MAX_PATH_PART else name[: MAX_PATH_PART - 1] + "…"
             for name in (str(part) for part in parts)]
    path = " › ".join(names) or fallback
    while len(path) > MAX_SOURCE_PATH and len(names) > 3:
        names = [names[0], "…", *names[3:]] if names[1] == "…" else [names[0], "…", *names[2:]]
        path = " › ".join(names)
    return path[: MAX_SOURCE_PATH - 1] + "…" if len(path) > MAX_SOURCE_PATH else path


def _file_name(value: object) -> str:
    """A display/download name for an attachment: no folders (``..\\..\\x.exe`` is
    ``x.exe``), no control characters, at most MAX_FILE_NAME characters (extension kept)."""

    name = assets.safe_filename(str(value or "")) if value else "attachment"
    if name == "file":
        name = "attachment"
    if len(name) > MAX_FILE_NAME:
        stem, dot, extension = name.rpartition(".")
        if dot and stem and len(extension) <= 16:
            name = stem[: MAX_FILE_NAME - len(extension) - 2] + "…." + extension
        else:
            name = name[: MAX_FILE_NAME - 1] + "…"
    return name


def _layout(rows: list[dict[str, Any]], items: dict[str, dict[str, Any]], used: set[str],
            ctx: _PageContext) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "item":
            item = items.get(str(row.get("item")))
            if item is None or row.get("item") in used:
                continue
            used.add(row["item"])
            blocks.extend(_tree(item.get("blocks") or [], ctx))
        elif row.get("kind") == "columns":
            columns = []
            for column in row.get("columns") or []:
                content = _layout((column or {}).get("rows") or [], items, used, ctx)
                if content:
                    columns.append((column, content))
            if row.get("scattered") or len(columns) < 2:
                for _, content in columns:
                    blocks.extend(content)
                continue
            total = sum(float(c.get("width") or 0) for c, _ in columns) or 1.0
            entries = []
            for column, content in columns:
                width = float(column.get("width") or 0) / total or 1.0 / len(columns)
                entries.append({
                    "id": f"{ctx.page_guid}/column:{content[0]['id']}",
                    "previous_ids": [f"{ctx.page_guid}/column:{old}"
                                     for old in content[0].get("previous_ids") or []],
                    "width": round(width, 4),
                    "blocks": content,
                })
            first = entries[0]["blocks"][0]
            blocks.append({
                "id": f"{ctx.page_guid}/columns:{first['id']}",
                "previous_ids": [f"{ctx.page_guid}/columns:{old}" for old in first.get("previous_ids") or []],
                "type": "columns",
                "attrs": {"onenote": True},
                "columns": entries,
            })
            ctx.count("columns")
    return blocks


def page_blocks(page: dict[str, Any], ctx: _PageContext) -> list[dict[str, Any]]:
    items = {str(item.get("id")): item for item in page.get("items") or [] if isinstance(item, dict)}
    used: set[str] = set()
    blocks = _layout(page.get("layout") or [], items, used, ctx)
    for item_id, item in items.items():  # never lose content the layout missed
        if item_id not in used:
            used.add(item_id)
            blocks.extend(_tree(item.get("blocks") or [], ctx))
    ink_text = str(page.get("ink_text") or "").strip()
    if ink_text and not ctx.stats.get("ink_text"):
        first_ink = _find_ink(blocks)
        if first_ink is not None:
            first_ink["alt"] = ink_text
        else:
            blocks.append({"id": f"{ctx.page_guid}/ink_text", "type": "paragraph", "text": ink_text})
    return blocks


def _find_ink(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for block in blocks:
        if block.get("type") == "image" and (block.get("attrs") or {}).get("ink") and not block.get("alt"):
            return block
        for nested in [block.get("children") or [], *[c.get("blocks") or [] for c in block.get("columns") or []]]:
            found = _find_ink(nested)
            if found is not None:
                return found
    return None


def _subpage_blocks(page_guid: str, children: list[dict[str, Any]], ctx: _PageContext) -> list[dict[str, Any]]:
    if not children:
        return []
    items = []
    for child in children:
        guid = _page_key(child) or ""
        target = ctx.importer.page_docs.get(guid)
        title = str(child.get("title") or "Untitled Page")
        if target is not None:
            run = {"text": title, "link": f"[[{target.document_id}|{_link_label(target.title)}]]"}
        else:
            ctx.pending.add(guid)
            run = {"text": title}
        items.append({"id": f"{page_guid}/subpage:{guid}", "type": "list", "inline": [run]})
    return [{"id": f"{page_guid}/subpages", "type": "paragraph",
             "inline": [{"text": "Subpages", "b": 1}], "children": items}]


# --- Import ----------------------------------------------------------------------------------


def _sections(entries: list[dict[str, Any]], trail: list[tuple[str, str]]):
    """(folder segments, section entry) pairs in table-of-contents order, plus groups."""

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("id") or entry.get("file_identity")
        name = str(entry.get("name") or "Untitled")
        if entry.get("kind") == "section_group":
            segments = trail + [(f"group:{key}", name)]
            yield segments, None
            yield from _sections(entry.get("children") or [], segments)
        elif entry.get("kind") == "section":
            yield trail + [(f"section:{key}", name)], entry


class ImportCancelled(Exception):
    """Raised between pages when the job was cancelled."""


def _manifest_text(blocks: list[Any]) -> list[str]:
    """The text a reader sees in manifest blocks (tables included; math excluded,
    since it is imported as LaTeX)."""

    parts: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"paragraph", "heading", "list_item", "check_item", "quote", "code"}:
            if not block.get("is_source_citation"):
                parts.append(_text_without_equations(block))
        elif kind == "image":
            parts.append(str(block.get("alt_text") or ""))
        elif kind == "ink":
            parts.append(str(block.get("recognized_text") or ""))
        elif kind == "table":
            for row in block.get("rows") or []:
                for cell in (row or {}).get("cells") or []:
                    parts.extend(_manifest_text((cell or {}).get("blocks") or []))
    return parts


_WORD_RE = re.compile(r"[^\W_]+")


def _word_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for word in _WORD_RE.findall(text.casefold()):
        counts[word] = counts.get(word, 0) + 1
    return counts


def text_coverage(expected: str, actual: str) -> tuple[float, int, int]:
    """(share of ``expected``'s characters found in ``actual``, chars expected, chars found).

    Both texts are split into case-folded words (letters/digits); a word counts
    with its length, as often as it occurs in both. Formatting, order,
    whitespace, punctuation and link syntax do not matter; lost words do.
    """

    want, have = _word_counts(expected), _word_counts(actual)
    total = sum(len(word) * count for word, count in want.items())
    found = sum(len(word) * count for word, count in have.items())
    if total == 0:
        return 1.0, 0, found
    covered = sum(len(word) * min(count, have.get(word, 0)) for word, count in want.items())
    return covered / total, total, found


class _Importer:
    def __init__(self, manifest_dir: Path, db_path: Path, notes_path: Path, assets_path: Path,
                 folder_id: str | None, progress: Callable[..., None] | None,
                 cancelled: Callable[[], bool] | None = None,
                 ocr: Callable[[list[Path]], None] | None = None):
        self.manifest_dir = manifest_dir
        self.db_path = db_path
        self.notes_path = notes_path
        self.assets_path = assets_path
        self.folder_id = folder_id
        self.progress = progress or (lambda **_: None)
        self.cancelled = cancelled or (lambda: False)
        self.ocr = ocr
        self._asset_paths: dict[str, Path] = {}
        self._block_index: dict[str, dict[str, str]] = {}
        manifest_file = manifest_dir / "manifest.json"
        try:
            self.manifest = _clean_manifest(json.loads(manifest_file.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise OneNoteImportError(f"The converter's manifest could not be read: {exc}") from exc
        if self.manifest.get("format") != MANIFEST_FORMAT:
            raise OneNoteImportError("Not a fortress-onenote manifest.")
        if self.manifest.get("format_version") != SUPPORTED_FORMAT_VERSION:
            raise OneNoteImportError(
                f"Unsupported manifest format version {self.manifest.get('format_version')!r}; "
                "update Fortress Notes or use a matching converter."
            )
        self.assets: dict[str, Any] = self.manifest.get("assets") or {}
        self._copied: dict[tuple[str, bool], str | None] = {}
        self.warnings: list[str] = []
        self.targets: dict[str, _Target] = {}
        self.manifest_pages: set[str] = set()
        self.page_docs: dict[str, _Target] = {}
        self._linked_this_run: set[str] = set()
        generator = self.manifest.get("generator") or {}
        self.importer_info = {
            "name": IMPORTER_NAME,
            "version": f"{IMPORTER_VERSION} ({generator.get('name', 'fortress-onenote')} "
                       f"{generator.get('version', '?')})",
        }

    # assets

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message)

    def asset_url(self, name: object, allow_svg: bool = False) -> str | None:
        if not isinstance(name, str) or not name:
            return None
        key = (name, allow_svg)
        if key in self._copied:
            return self._copied[key]
        url = self._copy_asset(name, allow_svg)
        self._copied[key] = url
        return url

    def _copy_asset(self, name: str, allow_svg: bool) -> str | None:
        info = self.assets.get(name)
        match = _ASSET_NAME_RE.match(name)
        if not isinstance(info, dict) or match is None:
            self.warn(f"asset {name} is not in the manifest")
            return None
        source = (self.manifest_dir / str(info.get("path") or f"assets/{name}")).resolve()
        if self.manifest_dir.resolve() not in source.parents or not source.is_file():
            self.warn(f"asset {name} is missing from the converter output")
            return None
        digest, extension = match.group(1), match.group(2).lower()
        if extension == "svg" and not allow_svg or extension != "svg" and extension not in _SAFE_EXTENSIONS:
            extension = "bin"
        target = self.assets_path / f"{digest}.{extension}"
        if not target.is_file():
            temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        mime = str(info.get("mime") or "application/octet-stream")
        if extension == "bin":
            mime = "application/octet-stream"
        block_store.register_asset(self.db_path, target, digest, mime)
        self._asset_paths[name] = target
        return f"/media/{target.name}"

    # blocks

    def find_block(self, document_id: str, object_ref: object) -> str | None:
        """The Fortress block of ``document_id`` imported from OneNote object ``object_ref``."""

        oid = _object_id(object_ref)
        if oid is None:
            return None
        index = self._block_index.get(document_id)
        if index is None:
            index = {}
            with block_store.connection_scope(self.db_path) as connection:
                rows = connection.execute(
                    "SELECT id, provenance_json FROM blocks WHERE document_id = ?", (document_id,)
                ).fetchall()
            # Element ids (what links name) first, then content and nested ids.
            for key in ("element_id", "object_id", "nested"):
                for row in rows:
                    ids = ((provenance.loads(row["provenance_json"]).get("extra") or {}).get("onenote") or {})
                    values = ids.get(key)
                    for value in values if isinstance(values, list) else [values]:
                        canonical = _object_id(value)
                        if canonical and canonical not in index:
                            index[canonical] = row["id"]
            self._block_index[document_id] = index
        return index.get(oid)

    # pages

    def _existing_targets(self) -> None:
        block_store.initialize(self.db_path)
        with block_store.connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """SELECT id, title, source_external_id FROM documents
                    WHERE source_kind = ? AND deleted_at IS NULL""",
                (SOURCE,),
            ).fetchall()
        for row in rows:
            self.page_docs[str(row["source_external_id"])] = _Target(row["id"], row["title"])
            guid = _norm_guid(row["source_external_id"])
            if guid and "~" not in guid:
                self.targets[guid] = _Target(row["id"], row["title"])

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        notebooks = [nb for nb in self.manifest.get("notebooks") or [] if isinstance(nb, dict)]
        plan: list[tuple[int, list[tuple[str, str]], dict[str, Any], dict[str, Any]]] = []
        results: list[dict[str, Any]] = []
        notebook_info: list[dict[str, Any]] = []
        for index, notebook in enumerate(notebooks):
            nb_key = notebook.get("id") or notebook.get("file_identity") or notebook.get("name")
            nb_segment = (f"notebook:{nb_key}", str(notebook.get("name") or "OneNote"))
            nb_folder = sources.ensure_folder_path(self.db_path, SOURCE, [nb_segment], parent_id=self.folder_id)
            report = notebook.get("report") or {}
            entry = {
                "name": nb_segment[1], "folder_id": nb_folder, "sections": 0,
                "pages_created": 0, "pages_updated": 0, "pages_unchanged": 0, "pages_skipped": 0,
                "images": 0, "files": 0, "tables": 0, "columns_pages": 0,
                "ink": 0, "math": 0, "check_items": 0, "conflicts": 0,
                "images_ocr_queued": 0, "low_coverage_pages": [],
                "pages_removed_in_source": [], "sections_removed_in_source": [],
                "warnings": [], "failed_sections": [
                    {
                        "path": " › ".join(str(part) for part in (failed.get("path") or [])),
                        "reason": str(failed.get("reason") or ""),
                        "hint": str(failed.get("hint") or ""),
                    }
                    for failed in report.get("failed_sections") or [] if isinstance(failed, dict)
                ],
            }
            for warning in report.get("warnings") or []:
                if isinstance(warning, dict):
                    where = warning.get("page_title")
                    entry["warnings"].append(f"{where}: {warning.get('message')}" if where else str(warning.get("message")))
            for note in report.get("layout_notes") or []:
                if isinstance(note, dict):
                    entry["warnings"].append(f"{note.get('page_title')}: {note.get('message')}")
            for missing in report.get("missing_data") or []:
                if isinstance(missing, dict):
                    entry["warnings"].append(
                        f"{missing.get('page_title')}: {missing.get('kind')} "
                        f"{missing.get('name') or ''} is {missing.get('data_status')} in the export".replace("  ", " ")
                    )
            results.append(entry)
            info = {
                "key": str(nb_key), "folder_id": nb_folder, "page_keys": set(), "folder_keys": set(),
                "failed_paths": {
                    tuple(str(part) for part in failed.get("path") or [])
                    for failed in report.get("failed_sections") or [] if isinstance(failed, dict)
                },
            }
            notebook_info.append(info)
            planned = len(plan)
            for segments, section in _sections(notebook.get("children") or [], [nb_segment]):
                folder = sources.ensure_folder_path(self.db_path, SOURCE, segments, parent_id=self.folder_id)
                info["folder_keys"].update(key for key, _ in segments)
                if section is None:
                    continue
                entry["sections"] += 1
                for page in section.get("pages") or []:
                    if isinstance(page, dict) and _page_key(page):
                        info["page_keys"].add(_page_key(page))
                        plan.append((index, segments, {"folder_id": folder, "section": section}, page))
                        link = _norm_guid(page.get("link_target_id"))
                        if link:
                            self.manifest_pages.add(link)
            if len(plan) == planned and not entry["failed_sections"]:
                # The converter reported nothing wrong but found no page (a package of
                # OneNote 2007 sections, a table of contents naming missing sections...).
                entry["warnings"].append(
                    "No pages were found in this notebook although no section was reported as failed. "
                    "If it has pages in OneNote, open it in OneNote 2016 or later (which upgrades old "
                    "sections), export it again, or import its folder of .one files.")

        self._existing_targets()
        total = len(plan)
        children: dict[str, list[dict[str, Any]]] = {}
        for _, _, _, page in plan:
            parent = _page_key({"id": page.get("parent_id")})
            if parent:
                children.setdefault(parent, []).append(page)
        page_state: dict[str, dict[str, Any]] = {}
        self.progress(status="importing", pages_total=total, pages_done=0, current=None)

        for done, (index, segments, where, page) in enumerate(plan):
            if self.cancelled():
                raise ImportCancelled()
            label = " › ".join([name for _, name in segments] + [str(page.get("title") or "Untitled Page")])
            self.progress(status="importing", pages_total=total, pages_done=done, current=label)
            entry = results[index]
            try:
                state = self._import_page(page, segments, where["folder_id"], label, children)
            except Exception as exc:  # one bad page must not stop the import
                entry["pages_skipped"] += 1
                entry["warnings"].append(f"{label}: page could not be imported ({exc})")
                traceback.print_exc()
                continue
            state["notebook"] = index
            page_state[state["guid"]] = state
            entry[f"pages_{state['status']}"] += 1
            for key in ("images", "files", "tables", "ink", "math", "check_items"):
                entry[key] += state["ctx"].stats.get(key, 0)
            if state["ctx"].stats.get("columns"):
                entry["columns_pages"] += 1
            entry["conflicts"] += state["conflicts"]

        # Pass 2: pages whose links/subpage lists point at pages created after them.
        self.progress(status="linking", pages_total=total, pages_done=total, current=None)
        for guid, state in page_state.items():
            if not any(target in self.targets or target in self.page_docs
                       for target in state["ctx"].pending):
                continue
            if self.cancelled():
                raise ImportCancelled()
            try:
                refreshed = self._import_page(state["page"], state["segments"], state["folder_id"],
                                              state["label"], children)
            except Exception as exc:
                results[state["notebook"]]["warnings"].append(f"{state['label']}: links could not be updated ({exc})")
                continue
            refreshed["notebook"] = state["notebook"]
            refreshed["status"] = state["status"]
            page_state[guid] = refreshed

        self._check_coverage(page_state, results)
        for info, entry in zip(notebook_info, results):
            self._find_removed(info, entry)
        self._queue_ocr(page_state, results)

        rewritten = sum(state["ctx"].rewritten for state in page_state.values())
        block_level = sum(state["ctx"].block_level_links for state in page_state.values())
        block_refs = sum(state["ctx"].block_references for state in page_state.values())
        unresolved = [item for state in page_state.values() for item in state["ctx"].unresolved]
        for message in self.warnings:
            (results[0]["warnings"] if results else []).append(message)
        for entry in results:
            if len(entry["warnings"]) > MAX_WARNINGS:
                extra = len(entry["warnings"]) - MAX_WARNINGS
                entry["warnings"] = entry["warnings"][:MAX_WARNINGS] + [f"… and {extra} more"]
        global_report = self.manifest.get("report") or {}
        errors_txt = "\n\n".join(
            str(item.get("text") or "") for item in global_report.get("export_errors") or []
            if isinstance(item, dict)
        )
        return {
            "notebooks": results,
            "links": {
                "rewritten": rewritten,
                "block_references": block_refs,
                "block_links_as_page_links": block_level,
                "unresolved": unresolved,
            },
            "errors_txt": errors_txt,
            "input_warnings": [str(item) for item in global_report.get("input_warnings") or []],
            "skipped_files": [str(item) for item in global_report.get("skipped") or []],
            "pages_total": total,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def _import_page(self, page: dict[str, Any], segments: list[tuple[str, str]], folder_id: str | None,
                     label: str, children: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        guid = _page_key(page)
        assert guid is not None
        link_guid = _norm_guid(page.get("link_target_id"))
        ctx = _PageContext(self, guid, label)
        blocks = page_blocks(page, ctx)
        blocks += _subpage_blocks(guid, children.get(guid, []), ctx)
        if ctx.dropped_links:
            ctx.warn(f"{len(ctx.dropped_links)} link(s) kept as plain text (not a web address, or too long): "
                     + ", ".join(repr(link[:80]) for link in ctx.dropped_links[:3]))
        source_url = page.get("source_url")
        title = str(page.get("title") or "Untitled Page")
        onenote_extra: dict[str, Any] = {
            "level": page.get("level") or 0,
            "order": page.get("order"),
            "section_path": [name for _, name in segments],
            # Which notebook/section folder (by source key) the page belongs to,
            # so a re-import can tell which notes it should still find.
            "notebook_id": segments[0][0].removeprefix("notebook:"),
            "section_id": segments[-1][0].removeprefix("section:"),
        }
        if page.get("parent_id"):
            onenote_extra["parent_page_id"] = _page_key({"id": page["parent_id"]})
        if page.get("title_derived"):
            onenote_extra["title_derived"] = True
        if page.get("author"):
            onenote_extra["author"] = str(page["author"])[:200]
        provenance: dict[str, Any] = {
            "source_path": _source_path(page.get("source_path") or [], label),
            "extra": {"onenote": onenote_extra},
        }
        if (isinstance(source_url, str) and source_url.lower().startswith(("http://", "https://"))
                and len(source_url) <= MAX_SOURCE_PATH):
            provenance["source_url"] = source_url
        result = sources.upsert_document_from_source(
            self.db_path,
            source=SOURCE,
            external_id=guid,
            title=title,
            blocks=sources.source_blocks(blocks),
            folder_id=folder_id,
            created_at=page.get("created"),
            modified_at=page.get("updated") or page.get("created"),
            provenance=provenance,
            importer=self.importer_info,
        )
        self._block_index.pop(result.document_id, None)
        if result.status in {"created", "updated"}:
            documents.mirror_document(self.db_path, self.notes_path, result.document_id)
        if result.status != "skipped_deleted":
            target = _Target(result.document_id, title)
            self.page_docs[guid] = target
            if link_guid and link_guid not in self._linked_this_run:
                # The first copy of a page in this export is the link target.
                self._linked_this_run.add(link_guid)
                self.targets[link_guid] = target
        status = "skipped" if result.status == "skipped_deleted" else result.status
        return {
            "guid": guid, "status": status, "document_id": result.document_id, "ctx": ctx,
            "page": page, "segments": segments, "folder_id": folder_id, "label": label,
            "conflicts": len(result.conflicts),
        }

    # checks after the import

    def _check_coverage(self, page_state: dict[str, dict[str, Any]], results: list[dict[str, Any]]) -> None:
        """List pages whose imported text lost more than 2 % of the manifest's letters."""

        for state in page_state.values():
            if state["status"] == "skipped":
                continue
            page = state["page"]
            expected = "\n".join(
                part for item in page.get("items") or [] if isinstance(item, dict)
                for part in _manifest_text(item.get("blocks") or [])
            )
            with block_store.connection_scope(self.db_path) as connection:
                rows = connection.execute(
                    "SELECT text, attrs_json, provenance_json FROM blocks WHERE document_id = ?",
                    (state["document_id"],),
                ).fetchall()
            imported = []
            for row in rows:
                external = str(provenance.loads(row["provenance_json"]).get("external_id") or "")
                if "/subpage" in external or "/ink_text" in external:
                    continue  # added by the importer, not page text
                imported.append(str(row["text"] or ""))
                try:
                    attrs = json.loads(row["attrs_json"] or "{}")
                except ValueError:
                    attrs = {}
                if isinstance(attrs, dict) and isinstance(attrs.get("alt"), str):
                    imported.append(attrs["alt"])
            coverage, wanted, found = text_coverage(expected, "\n".join(imported))
            if coverage < COVERAGE_THRESHOLD:
                results[state["notebook"]]["low_coverage_pages"].append({
                    "page": state["label"],
                    "document_id": state["document_id"],
                    "coverage": round(coverage, 4),
                    "manifest_chars": wanted,
                    "imported_chars": found,
                })

    def _find_removed(self, info: dict[str, Any], entry: dict[str, Any]) -> None:
        """Mark notes whose OneNote page is gone from a notebook of this export."""

        notebook_key = f"notebook:{info['key']}"
        with block_store.connection_scope(self.db_path) as connection:
            folders = {
                row["id"]: row for row in connection.execute(
                    "SELECT id, parent_id, name, source_kind, source_external_id FROM folders"
                ).fetchall()
            }
            docs = connection.execute(
                """SELECT id, folder_id, source_external_id, provenance_json FROM documents
                    WHERE source_kind = ? AND deleted_at IS NULL""",
                (SOURCE,),
            ).fetchall()

        def chain(folder_id: str | None) -> list[sqlite3.Row]:
            rows: list[sqlite3.Row] = []
            seen: set[str] = set()
            while folder_id and folder_id in folders and folder_id not in seen:
                seen.add(folder_id)
                rows.append(folders[folder_id])
                folder_id = folders[folder_id]["parent_id"]
            return list(reversed(rows))

        def source_keys(rows: list[sqlite3.Row]) -> list[str]:
            return [str(row["source_external_id"]) for row in rows
                    if row["source_kind"] == SOURCE and row["source_external_id"]]

        failed = info["failed_paths"]
        removed_ids: list[str] = []
        for doc in docs:
            if doc["source_external_id"] in info["page_keys"]:
                continue
            extra = (provenance.loads(doc["provenance_json"]).get("extra") or {}).get("onenote") or {}
            keys = source_keys(chain(doc["folder_id"]))
            in_notebook = (
                extra.get("notebook_id") == info["key"] if extra.get("notebook_id") is not None
                else notebook_key in keys
            )
            if not in_notebook:
                continue
            section_path = tuple(str(part) for part in extra.get("section_path") or [])
            if section_path and section_path in failed:
                continue  # its section could not be read this time: unknown, not removed
            removed_ids.append(doc["id"])
        if removed_ids:
            entry["pages_removed_in_source"] = [
                {key: item[key] for key in ("document_id", "title", "source_path",
                                            "source_removed_at", "newly_removed")}
                for item in sources.mark_documents_source_removed(self.db_path, SOURCE, removed_ids)
            ]
        # Section (group) folders of this notebook that the export no longer has.
        for folder_id, row in folders.items():
            key = str(row["source_external_id"] or "")
            if row["source_kind"] != SOURCE or not key.startswith(("section:", "group:")):
                continue
            rows = chain(folder_id)
            if notebook_key not in source_keys(rows) or key in info["folder_keys"]:
                continue
            path = [str(item["name"]) for item in rows]
            if tuple(path) in failed:
                continue
            entry["sections_removed_in_source"].append({
                "folder_id": folder_id, "name": row["name"], "path": " › ".join(path),
                "kind": "section_group" if key.startswith("group:") else "section",
            })

    def _queue_ocr(self, page_state: dict[str, dict[str, Any]], results: list[dict[str, Any]]) -> None:
        """Hand images without OneNote OCR text to the app's OCR (in the background)."""

        if self.ocr is None:
            return
        paths: list[Path] = []
        seen: set[Path] = set()
        for state in page_state.values():
            for name in state["ctx"].ocr_assets:
                path = self._asset_paths.get(name)
                if path is None or path in seen:
                    continue
                seen.add(path)
                with block_store.connection_scope(self.db_path) as connection:
                    row = connection.execute(
                        "SELECT text FROM assets WHERE content_hash = ? AND media_name = ?",
                        (path.stem, path.name),
                    ).fetchone()
                if row is not None and str(row["text"] or "").strip():
                    continue  # already read
                paths.append(path)
                results[state["notebook"]]["images_ocr_queued"] += 1
        if paths:
            try:
                self.ocr(paths)
            except Exception:  # OCR is a bonus; never fail the import for it
                traceback.print_exc()


def import_manifest(
    manifest_dir: Path,
    *,
    db_path: Path,
    notes_path: Path,
    assets_path: Path,
    folder_id: str | None = None,
    progress: Callable[..., None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    ocr: Callable[[list[Path]], None] | None = None,
) -> dict[str, Any]:
    """Import a converted export (``manifest_dir/manifest.json``); returns the report.

    ``cancelled`` is polled between pages (raises :class:`ImportCancelled`);
    ``ocr`` receives image files that have no OneNote OCR text and must return
    quickly (queue the work).
    """

    assets_path.mkdir(parents=True, exist_ok=True)
    if folder_id is not None and not block_store.folder_exists(db_path, folder_id):
        raise OneNoteImportError("The folder to import into does not exist.")
    return _Importer(manifest_dir, db_path, notes_path, assets_path, folder_id, progress,
                     cancelled, ocr).run()


_ocr_queue: "queue.Queue[Path]" = queue.Queue()
_ocr_worker: threading.Thread | None = None
_ocr_lock = threading.Lock()


def background_ocr(paths: list[Path]) -> None:
    """Queue images for the app's caption + OCR (``images.extract_and_store``), one
    at a time on a daemon thread; the text lands in the assets text cache and
    the notes' search index."""

    global _ocr_worker
    for path in paths:
        _ocr_queue.put(path)
    with _ocr_lock:
        if _ocr_worker is None or not _ocr_worker.is_alive():
            _ocr_worker = threading.Thread(target=_ocr_work, name="onenote-ocr", daemon=True)
            _ocr_worker.start()


def _ocr_work() -> None:
    from . import images  # heavy model imports stay out of the import path

    while True:
        path = _ocr_queue.get()
        try:
            if path.is_file():
                images.extract_and_store(path)
        except Exception:
            traceback.print_exc()
        finally:
            _ocr_queue.task_done()


# --- Jobs ------------------------------------------------------------------------------------


def imports_dir(notes_path: Path) -> Path:
    path = notes_path / IMPORTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
INTERRUPTED_MESSAGE = (
    "The import was interrupted by a restart. Import the export again; "
    "pages that were already imported are kept and updated in place."
)
CANCELLED_MESSAGE = "Import cancelled. Pages imported before cancelling are kept."
_SAVE_INTERVAL_S = 1.0


class ImportJobs:
    """One-at-a-time background import jobs with an in-memory registry.

    Every job's record is written to ``NOTES_DIR/.fortress-imports/<job_id>.json``
    when it is queued, at each status change, about once a second while it runs,
    and when it ends. A stored record that is still "running" but unknown to
    this process belongs to a job a restart interrupted: it reads as ``failed``
    with :data:`INTERRUPTED_MESSAGE` (re-running an import is idempotent).
    """

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._notes: dict[str, Path] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._saved_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self.ocr: Callable[[list[Path]], None] | None = background_ocr

    @staticmethod
    def new_id() -> str:
        return block_store._new_ulid()

    def submit(self, job_id: str, *, input_path: Path, input_name: str, owned_input: Path | None,
               db_path: Path, notes_path: Path, assets_path: Path,
               folder_id: str | None = None) -> dict[str, Any]:
        record = {
            "job_id": job_id,
            "status": "queued",
            "progress": {"pages_total": 0, "pages_done": 0, "current": None},
            "result": None,
            "error": None,
            "input_name": input_name,
            "folder_id": folder_id,
            "created_at": block_store._now(),
            "finished_at": None,
        }
        snapshot = json.loads(json.dumps(record))
        with self._lock:
            self._jobs[job_id] = record
            self._notes[job_id] = notes_path
            self._cancel[job_id] = threading.Event()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="onenote-import", daemon=True)
                self._worker.start()
        self._save(job_id, notes_path)
        self._queue.put((job_id, {
            "input_path": input_path, "owned_input": owned_input, "db_path": db_path,
            "notes_path": notes_path, "assets_path": assets_path, "folder_id": folder_id,
        }))
        return snapshot

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._jobs[job_id]
            progress = changes.pop("progress", None)
            if progress:
                record["progress"] = {**record["progress"], **progress}
            status_changed = "status" in changes and changes["status"] != record["status"]
            record.update(changes)
            notes_path = self._notes.get(job_id)
            due = time.monotonic() - self._saved_at.get(job_id, 0.0) >= _SAVE_INTERVAL_S
        if notes_path is not None and (status_changed or due):
            self._save(job_id, notes_path)

    def cancel(self, job_id: str, notes_path: Path) -> tuple[dict[str, Any] | None, bool]:
        """Ask a job to stop: ``(record, accepted)``. A queued job is cancelled at
        once; a running one stops between pages (or stops the converter) and
        then reads ``cancelled``. ``record`` is None for an unknown job;
        ``accepted`` is False when the job had already ended."""

        accepted = False
        with self._lock:
            record = self._jobs.get(job_id)
            event = self._cancel.get(job_id)
            if record is not None and record["status"] not in TERMINAL_STATUSES and event is not None:
                accepted = True
                event.set()
                if record["status"] == "queued":
                    record.update(status="cancelled", error=CANCELLED_MESSAGE, finished_at=block_store._now())
                else:
                    record["cancel_requested"] = True
        if accepted:
            self._save(job_id, self._notes.get(job_id, notes_path))
        return self.get(job_id, notes_path), accepted

    def _work(self) -> None:
        while True:
            job_id, options = self._queue.get()
            try:
                with self._lock:
                    skip = self._jobs[job_id]["status"] == "cancelled"
                if skip:
                    for leftover in (options.get("owned_input"),):
                        if leftover is not None:
                            shutil.rmtree(leftover, ignore_errors=True)
                else:
                    self._run(job_id, **options)
            except Exception:  # pragma: no cover - _run records its own failures
                traceback.print_exc()
            finally:
                self._queue.task_done()

    def _run(self, job_id: str, *, input_path: Path, owned_input: Path | None, db_path: Path,
             notes_path: Path, assets_path: Path, folder_id: str | None) -> None:
        started = time.monotonic()
        work = imports_dir(notes_path) / job_id
        event = self._cancel.get(job_id) or threading.Event()
        try:
            self._update(job_id, status="converting")
            out_dir = work / "out"
            if out_dir.exists():
                shutil.rmtree(out_dir)
            run_converter(input_path, out_dir, cancelled=event.is_set)

            def progress(*, status: str, pages_total: int, pages_done: int, current: str | None) -> None:
                self._update(job_id, status=status, progress={
                    "pages_total": pages_total, "pages_done": pages_done, "current": current,
                })

            result = import_manifest(out_dir, db_path=db_path, notes_path=notes_path,
                                     assets_path=assets_path, folder_id=folder_id, progress=progress,
                                     cancelled=event.is_set, ocr=self.ocr)
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
            self._update(job_id, status="done", result=result, finished_at=block_store._now(),
                         progress={"current": None})
        except ImportCancelled:
            self._update(job_id, status="cancelled", error=CANCELLED_MESSAGE,
                         finished_at=block_store._now(), progress={"current": None})
        except OneNoteImportError as exc:
            self._update(job_id, status="failed", error=str(exc), finished_at=block_store._now())
        except Exception as exc:
            traceback.print_exc()
            self._update(job_id, status="failed", error=f"Import failed: {exc}",
                         finished_at=block_store._now())
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if owned_input is not None:
                shutil.rmtree(owned_input, ignore_errors=True)
            self._save(job_id, notes_path)

    def _save(self, job_id: str, notes_path: Path) -> None:
        with self._lock:
            record = json.loads(json.dumps(self._jobs[job_id]))
            self._saved_at[job_id] = time.monotonic()
        _write_record(notes_path, record)

    def _stored(self, notes_path: Path, job_id: str) -> dict[str, Any] | None:
        """A record from disk; one left running by an earlier process is failed."""

        stored = imports_dir(notes_path) / f"{job_id}.json"
        try:
            value = json.loads(stored.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("job_id") != job_id:
            return None
        if value.get("status") not in TERMINAL_STATUSES:
            value.update(status="failed", error=INTERRUPTED_MESSAGE, interrupted=True,
                         finished_at=value.get("finished_at") or block_store._now())
            value["progress"] = {**(value.get("progress") or {}), "current": None}
            try:
                _write_record(notes_path, value)
            except OSError:
                pass
            base = imports_dir(notes_path)
            shutil.rmtree(base / job_id, ignore_errors=True)
            shutil.rmtree(base / f"{job_id}-upload", ignore_errors=True)
        return value

    def get(self, job_id: str, notes_path: Path) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                return json.loads(json.dumps(record))
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", job_id):
            return None
        return self._stored(notes_path, job_id)

    def list(self, notes_path: Path, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            records = {job_id: json.loads(json.dumps(record)) for job_id, record in self._jobs.items()}
        for stored in imports_dir(notes_path).glob("*.json"):
            if stored.stem in records or not re.fullmatch(r"[A-Za-z0-9]{1,64}", stored.stem):
                continue
            value = self._stored(notes_path, stored.stem)
            if value is not None:
                records[stored.stem] = value
        ordered = sorted(records.values(), key=lambda item: (str(item.get("created_at") or ""), item["job_id"]),
                         reverse=True)
        return ordered[:limit]

    def wait(self, timeout: float | None = None) -> None:
        """Block until every queued job finished (tests)."""

        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("import jobs still running")
            time.sleep(0.02)


def _write_record(notes_path: Path, record: dict[str, Any]) -> None:
    target = imports_dir(notes_path) / f"{record['job_id']}.json"
    temporary = target.with_name(f".{target.name}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, target)


jobs = ImportJobs()
