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
* **Block ids**: the manifest has no per-paragraph object ids, so a block's
  source id is ``<page GUID>/<type>:<hash of its content>`` (duplicates get
  ``#2``, ``#3`` from the upsert). Unchanged blocks therefore match across
  re-imports wherever they moved; a paragraph edited in OneNote becomes a new
  block (the old one is removed, or kept and marked ``source_removed`` if it was
  edited in Fortress). Paragraph-level ``onenote:`` links (``object-id``) point at
  their page.
* **Assets** are copied once into ``NOTES_DIR/assets/<sha256>.<ext>`` (deduped by
  hash). Extensions a browser would render as active content (html, svg from
  attachments, ...) are stored as ``.bin`` (served as a download).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import block_store, documents, sources

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
_BAD_LINK_SCHEMES = ("javascript:", "vbscript:", "data:")
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


def run_converter(input_path: Path, out_dir: Path, *, timeout: float | None = None) -> dict[str, Any]:
    """Run the converter; returns its JSON summary line. Raises OneNoteImportError."""

    binary = converter_path()
    if binary is None:
        raise OneNoteImportError(
            "The OneNote converter (fortress-onenote) was not found. Set FORTRESS_ONENOTE_BIN "
            "or build it: cd tools/fortress-onenote && cargo build --release"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(binary), str(input_path), "--out", str(out_dir), "-q"],
            capture_output=True,
            text=True,
            timeout=timeout or CONVERTER_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OneNoteImportError(
            f"The OneNote converter took longer than {int(exc.timeout)} s and was stopped."
        ) from exc
    except OSError as exc:
        raise OneNoteImportError(f"The OneNote converter could not be started: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {completed.returncode}"
        raise OneNoteImportError(f"The OneNote export could not be converted: {message}")
    summary: dict[str, Any] = {}
    for line in reversed((completed.stdout or "").strip().splitlines()):
        try:
            summary = json.loads(line)
            break
        except ValueError:
            continue
    if not (out_dir / "manifest.json").is_file():
        raise OneNoteImportError("The OneNote converter did not write a manifest.")
    return summary


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
    block_level_links: int = 0

    def count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    def warn(self, message: str) -> None:
        self.importer.warn(f"{self.page_label}: {message}")


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
    runs: list[dict[str, Any]] = []
    for a, b in zip(bounds, bounds[1:]):
        run: dict[str, Any] = {"text": text[a:b]}
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
            # "math" (inline equations) keeps OneNote's linear text; font_size is dropped.
        if "code" in run:
            # Inline code cannot carry other marks in Markdown; keep it plain code.
            run = {key: value for key, value in run.items() if key in {"text", "code", "link"}}
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
            if onenote.get("object_id"):
                ctx.block_level_links += 1
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
    if not href or href.lower().startswith(_BAD_LINK_SCHEMES):
        return None
    return href


def _strip_math_delimiters(latex: str) -> str:
    latex = latex.strip()
    for opening, closing in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)")):
        if latex.startswith(opening) and latex.endswith(closing) and len(latex) >= len(opening) + len(closing):
            return latex[len(opening):-len(closing)].strip()
    return latex


def _text_block(block: dict[str, Any], ctx: _PageContext) -> tuple[dict[str, Any], list[str]]:
    block_links: list[str] = []
    runs = _runs(str(block.get("text") or ""), block.get("marks"), ctx, block_links)
    kind = block.get("type")
    result: dict[str, Any] = {"inline": runs}
    if kind == "heading":
        level = block.get("level")
        result.update(type="heading", level=level if isinstance(level, int) and 1 <= level <= 6 else 1)
    elif kind == "list_item":
        info = block.get("list") or {}
        result.update(type="list", ordered=bool(info.get("ordered")))
        if info.get("marker"):
            result["list_format"] = str(info["marker"])
    elif kind == "check_item":
        info = block.get("list") or {}
        result.update(type="list", ordered=bool(info.get("ordered")), checked=bool(block.get("checked")))
        if block.get("checked_at"):
            result["checked_at"] = str(block["checked_at"])
    elif kind == "quote":
        result["type"] = "quote"
    else:
        result["type"] = "paragraph"
    ctx.count("text_blocks")
    return result, block_links


def _note_tags(block: dict[str, Any]) -> list[str]:
    labels = []
    for tag in block.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        if block.get("type") == "check_item" and tag.get("checkable"):
            continue  # it is the checkbox
        label = str(tag.get("label") or tag.get("shape") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _convert_block(block: dict[str, Any], ctx: _PageContext, nested: bool = False) -> dict[str, Any] | None:
    """One manifest block -> a source block dict (no children). None to drop it."""

    kind = block.get("type")
    block_links: list[str] = []
    result: dict[str, Any]
    if kind in {"paragraph", "heading", "list_item", "check_item", "quote"}:
        result, block_links = _text_block(block, ctx)
        if kind == "check_item":
            ctx.count("check_items")
    elif kind == "code":
        result = {"type": "code", "text": str(block.get("text") or "")}
    elif kind == "math":
        latex = block.get("latex")
        latex = _strip_math_delimiters(latex) if isinstance(latex, str) and latex.strip() else str(block.get("text") or "")
        result = {"type": "math", "latex": latex}
        if isinstance(block.get("mathml"), str) and block["mathml"]:
            result["mathml"] = block["mathml"]
        ctx.count("math")
    elif kind == "table":
        result = _table(block, ctx)
        ctx.count("tables")
    elif kind == "image":
        result = _image(block, ctx)
    elif kind == "ink":
        result = _ink(block, ctx)
    elif kind == "file":
        result = _file(block, ctx)
    elif kind == "embed":
        url = str(block.get("url") or "").strip()
        if not url or url.lower().startswith(_BAD_LINK_SCHEMES):
            return None
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
        result["id"] = _block_external_id(ctx.page_guid, block)
        extra: dict[str, Any] = {}
        if block_links:
            extra["unresolved_links"] = block_links
        if block.get("source_url") and str(block["source_url"]).lower().startswith(("http://", "https://")):
            result["provenance"] = {"source_url": str(block["source_url"])}
        if extra:
            result.setdefault("provenance", {})["extra"] = extra
    return result


def _block_external_id(page_guid: str, block: dict[str, Any]) -> str:
    kind = block.get("type")
    if kind in {"paragraph", "heading", "list_item", "check_item", "quote", "code"}:
        digest = _short_hash(kind, block.get("text") or "")
    elif kind == "image":
        digest = _short_hash(kind, block.get("asset") or block.get("original_name") or "", block.get("alt_text") or "")
    else:
        digest = _short_hash(kind, json.dumps(block, sort_keys=True, ensure_ascii=False))
    return f"{page_guid}/{kind}:{digest}"


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
            ):
                entry["inline"] = first["inline"]
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


def _nested_safe(block: dict[str, Any]) -> dict[str, Any]:
    """Cells cannot hold layout blocks; drop source-only keys for in-cell blocks."""

    block = {key: value for key, value in block.items() if key not in {"id", "provenance"}}
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
        label = f"Image missing from the OneNote export{f' ({name})' if name else ''}"
        return {"type": "paragraph", "inline": [{"text": label, "i": 1}] + ([{"text": f"\n{alt}"}] if alt else [])}
    result: dict[str, Any] = {"type": "image", "url": url}
    if alt:
        result["alt"] = alt
    for key in ("width", "height"):
        value = _px(block.get(key))
        if value:
            result[key] = value
    if name:
        result["name"] = str(name)
    attrs: dict[str, Any] = {}
    if block.get("hyperlink") and not str(block["hyperlink"]).lower().startswith(_BAD_LINK_SCHEMES):
        attrs["link"] = str(block["hyperlink"])
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
    name = str(block.get("filename") or "attachment")
    result: dict[str, Any] = {"type": "file", "name": name}
    url = ctx.importer.asset_url(block.get("asset"))
    if url is None:
        ctx.count("files_missing")
        result["caption"] = "File missing from the OneNote export"
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
                    "width": round(width, 4),
                    "blocks": content,
                })
            blocks.append({
                "id": f"{ctx.page_guid}/columns:{entries[0]['blocks'][0]['id']}",
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


class _Importer:
    def __init__(self, manifest_dir: Path, db_path: Path, notes_path: Path, assets_path: Path,
                 folder_id: str | None, progress: Callable[..., None] | None):
        self.manifest_dir = manifest_dir
        self.db_path = db_path
        self.notes_path = notes_path
        self.assets_path = assets_path
        self.folder_id = folder_id
        self.progress = progress or (lambda **_: None)
        manifest_file = manifest_dir / "manifest.json"
        try:
            self.manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
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
        return f"/media/{target.name}"

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
            for segments, section in _sections(notebook.get("children") or [], [nb_segment]):
                folder = sources.ensure_folder_path(self.db_path, SOURCE, segments, parent_id=self.folder_id)
                if section is None:
                    continue
                entry["sections"] += 1
                for page in section.get("pages") or []:
                    if isinstance(page, dict) and _page_key(page):
                        plan.append((index, segments, {"folder_id": folder, "section": section}, page))
                        link = _norm_guid(page.get("link_target_id"))
                        if link:
                            self.manifest_pages.add(link)

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
            try:
                refreshed = self._import_page(state["page"], state["segments"], state["folder_id"],
                                              state["label"], children)
            except Exception as exc:
                results[state["notebook"]]["warnings"].append(f"{state['label']}: links could not be updated ({exc})")
                continue
            refreshed["notebook"] = state["notebook"]
            refreshed["status"] = state["status"]
            page_state[guid] = refreshed

        rewritten = sum(state["ctx"].rewritten for state in page_state.values())
        block_level = sum(state["ctx"].block_level_links for state in page_state.values())
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
        source_url = page.get("source_url")
        title = str(page.get("title") or "Untitled Page")
        onenote_extra: dict[str, Any] = {
            "level": page.get("level") or 0,
            "order": page.get("order"),
            "section_path": [name for _, name in segments],
        }
        if page.get("parent_id"):
            onenote_extra["parent_page_id"] = _page_key({"id": page["parent_id"]})
        if page.get("title_derived"):
            onenote_extra["title_derived"] = True
        if page.get("author"):
            onenote_extra["author"] = str(page["author"])[:200]
        provenance: dict[str, Any] = {
            "source_path": " › ".join(str(part) for part in (page.get("source_path") or [])) or label,
            "extra": {"onenote": onenote_extra},
        }
        if isinstance(source_url, str) and source_url.lower().startswith(("http://", "https://")):
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


def import_manifest(
    manifest_dir: Path,
    *,
    db_path: Path,
    notes_path: Path,
    assets_path: Path,
    folder_id: str | None = None,
    progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Import a converted export (``manifest_dir/manifest.json``); returns the report."""

    assets_path.mkdir(parents=True, exist_ok=True)
    if folder_id is not None and not block_store.folder_exists(db_path, folder_id):
        raise OneNoteImportError("The folder to import into does not exist.")
    return _Importer(manifest_dir, db_path, notes_path, assets_path, folder_id, progress).run()


# --- Jobs ------------------------------------------------------------------------------------


def imports_dir(notes_path: Path) -> Path:
    path = notes_path / IMPORTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class ImportJobs:
    """One-at-a-time background import jobs with an in-memory registry.

    Each finished job's record is also written to
    ``NOTES_DIR/.fortress-imports/<job_id>.json`` so the history survives restarts.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._worker: threading.Thread | None = None

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
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="onenote-import", daemon=True)
                self._worker.start()
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
            record.update(changes)

    def _work(self) -> None:
        while True:
            job_id, options = self._queue.get()
            try:
                self._run(job_id, **options)
            except Exception:  # pragma: no cover - _run records its own failures
                traceback.print_exc()
            finally:
                self._queue.task_done()

    def _run(self, job_id: str, *, input_path: Path, owned_input: Path | None, db_path: Path,
             notes_path: Path, assets_path: Path, folder_id: str | None) -> None:
        started = time.monotonic()
        work = imports_dir(notes_path) / job_id
        try:
            self._update(job_id, status="converting")
            out_dir = work / "out"
            if out_dir.exists():
                shutil.rmtree(out_dir)
            run_converter(input_path, out_dir)

            def progress(*, status: str, pages_total: int, pages_done: int, current: str | None) -> None:
                self._update(job_id, status=status, progress={
                    "pages_total": pages_total, "pages_done": pages_done, "current": current,
                })

            result = import_manifest(out_dir, db_path=db_path, notes_path=notes_path,
                                     assets_path=assets_path, folder_id=folder_id, progress=progress)
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
            self._update(job_id, status="done", result=result, finished_at=block_store._now(),
                         progress={"current": None})
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
        target = imports_dir(notes_path) / f"{job_id}.json"
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, target)

    def get(self, job_id: str, notes_path: Path) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                return json.loads(json.dumps(record))
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", job_id):
            return None
        stored = imports_dir(notes_path) / f"{job_id}.json"
        try:
            return json.loads(stored.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self, notes_path: Path, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            records = {job_id: json.loads(json.dumps(record)) for job_id, record in self._jobs.items()}
        for stored in imports_dir(notes_path).glob("*.json"):
            if stored.stem in records:
                continue
            try:
                value = json.loads(stored.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict) and value.get("job_id") == stored.stem:
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


jobs = ImportJobs()
