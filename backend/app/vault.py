"""The notes folder as an Obsidian vault of plain Markdown (docs/open-standards.md).

Each note is ``<folder>/<subfolder>/<title>.md``, the folders being the note's
Fortress folders; stored files stay in ``assets/`` and are embedded with paths
relative to the note. Front matter uses only plain keys (``title``, ``aliases``,
``created``, ``updated``, ``id``, ``source``, ``picture_text``). The vault is a
one-way export: nothing reads it back as Fortress notes. Note links are wikilinks by file name, the
shortest name that is unique in the vault, as Obsidian writes them.

This module holds the naming (:func:`compute_layout`), the front matter written
(:func:`front_matter`) and read from any Markdown file (:func:`read_front_matter`),
and the link resolution of an import (:class:`ImportLinks`).
``documents.MirrorWriter`` writes the vault; the upload API reads Markdown with these.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml

from .block_markdown import (
    BlockSpec,
    ParseLinks,
    RenderLinks,
    heading_key,
    new_block_id,
)

# Bump to rewrite every mirror in the new format (the mirror writer compares it).
FORMAT_VERSION = 3

# Characters no file name may hold on macOS/Windows/Linux, plus those Obsidian
# refuses in note names because links use them (# ^ [ ] |).
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f\x7f]+')
_NAME_LIMIT = 100
# File systems limit a name to 255 bytes (UTF-8 on APFS and ext4), not characters:
# 100 CJK characters or emoji are 300-400 bytes. Room is left for " 12", ".md"
# and the temporary name a mirror is written under (".<name>.md.XXXXXXXX").
_NAME_BYTES = 200
# Top-level names the vault itself uses.
RESERVED_TOP_LEVEL = frozenset({"assets"})


def file_stem(name: str, fallback: str = "Untitled") -> str:
    """A note or folder name as a file name that every OS and Obsidian accept."""

    cleaned = " ".join(_UNSAFE_NAME.sub(" ", name).split()).strip(". ")
    cleaned = cleaned[:_NAME_LIMIT]
    while len(cleaned.encode("utf-8")) > _NAME_BYTES:
        cleaned = cleaned[:-1]
    cleaned = cleaned.rstrip(". ")
    return cleaned or fallback


def _unique(stem: str, used: set[str]) -> str:
    candidate, number = stem, 1
    while candidate.casefold() in used:
        number += 1
        candidate = f"{stem} {number}"
    used.add(candidate.casefold())
    return candidate


@dataclass
class Layout:
    """Where every active note lives in the vault and how links name it."""

    paths: dict[str, str] = field(default_factory=dict)  # note id -> "Folder/Title.md"
    titles: dict[str, str] = field(default_factory=dict)
    targets: dict[str, str] = field(default_factory=dict)  # note id -> wikilink target

    def note(self, document_id: str) -> tuple[str, str] | None:
        if document_id not in self.targets:
            return None
        return self.targets[document_id], self.titles[document_id]

    def media_prefix(self, document_id: str) -> str:
        depth = self.paths.get(document_id, "").count("/")
        return "../" * depth + "assets/"

    def render_links(self, document_id: str, anchors: Iterable[str],
                     blocks: Callable[[str], dict[str, Any] | None]) -> RenderLinks:
        return RenderLinks(
            document_id=document_id, anchors=set(anchors), notes=self.note, blocks=blocks,
            media_prefix=self.media_prefix(document_id),
        )


def compute_layout(
    documents: Iterable[dict[str, Any]],
    folders: Iterable[dict[str, Any]],
    taken: Callable[[str, str], bool] | None = None,
) -> Layout:
    """Vault paths for ``documents`` (id, title, folder_id, created_at) in ``folders``.

    Names that clash (case-insensitively, as on macOS) get " 2", " 3", ... in
    order of creation, so a note keeps its name when a newer one takes the same
    title. ``taken(path, document_id)`` says a path holds a file that isn't that
    note's mirror (someone else's file): it is never overwritten.
    """

    folder_rows = sorted(folders, key=lambda row: (row.get("position") or 0, str(row["id"])))
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for row in folder_rows:
        by_parent.setdefault(row.get("parent_id"), []).append(row)
    directories: dict[str, str] = {}
    used_names: dict[str, set[str]] = {"": {name for name in RESERVED_TOP_LEVEL}}

    def place(parent_id: str | None, prefix: str, depth: int) -> None:
        if depth > 64:
            return
        used = used_names.setdefault(prefix, set())
        for row in by_parent.get(parent_id, []):
            name = _unique(file_stem(str(row.get("name") or ""), "Folder"), used)
            path = f"{prefix}{name}/"
            directories[str(row["id"])] = path
            used_names.setdefault(path, set())
            place(str(row["id"]), path, depth + 1)

    place(None, "", 0)
    layout = Layout()
    note_names: dict[str, set[str]] = {}
    ordered = sorted(documents, key=lambda row: (str(row.get("created_at") or ""), str(row["id"])))
    for row in ordered:
        document_id = str(row["id"])
        directory = directories.get(str(row.get("folder_id") or ""), "")
        used = note_names.setdefault(directory, set())
        stem = file_stem(str(row.get("title") or ""))
        candidate, number = stem, 1
        while candidate.casefold() in used or (
            taken is not None and taken(f"{directory}{candidate}.md", document_id)
        ):
            number += 1
            candidate = f"{stem} {number}"
        used.add(candidate.casefold())
        layout.paths[document_id] = f"{directory}{candidate}.md"
        layout.titles[document_id] = str(row.get("title") or "")
    stems: dict[str, int] = {}
    for path in layout.paths.values():
        key = posixpath.basename(path)[:-3].casefold()
        stems[key] = stems.get(key, 0) + 1
    for document_id, path in layout.paths.items():
        stem = posixpath.basename(path)[:-3]
        # Obsidian's "shortest path when possible".
        layout.targets[document_id] = stem if stems[stem.casefold()] == 1 else path[:-3]
    return layout


# --- Front matter -------------------------------------------------------------------------------

def front_matter(tree: dict[str, Any], stem: str | None,
                 picture_text: dict[str, str] | None,
                 source_url: str | None = None) -> dict[str, Any]:
    """The YAML front matter of a note's Markdown (key order as written): plain keys
    other tools know, nothing Fortress-only."""

    title = str(tree["title"])
    meta: dict[str, Any] = {"title": title}
    if stem is not None and stem != title:
        meta["aliases"] = [title]
    meta["created"] = _plain(tree["created_at"])
    meta["updated"] = _plain(tree["updated_at"])
    meta["id"] = tree["id"]
    if source_url:
        # What web clippers (Obsidian's included) call the page a note came from.
        meta["source"] = source_url
    if picture_text:
        # Text read from the pictures and PDFs the note shows, so other tools find it too.
        meta["picture_text"] = picture_text
    return meta


def dump(meta: dict[str, Any], body: str) -> str:
    """A Markdown file: YAML front matter, then the body."""

    head = yaml.safe_dump(_yaml_safe(meta), sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=1000)
    return f"---\n{head}---\n\n{body}\n" if body else f"---\n{head}---\n"


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _yaml_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _yaml_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_yaml_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _plain(value) if isinstance(value, (datetime, date)) else str(value)


@dataclass
class NoteMetadata:
    """What a Markdown file's front matter says about its note."""

    title: str | None
    created: Any
    updated: Any
    aliases: list[str]


def read_front_matter(metadata: dict[str, Any]) -> NoteMetadata:
    """Front matter written by Fortress, Obsidian or anything else."""

    aliases = metadata.get("aliases", metadata.get("alias"))
    if isinstance(aliases, str):
        aliases = [aliases]
    return NoteMetadata(
        title=str(metadata["title"]).strip() or None if metadata.get("title") is not None else None,
        created=_first(metadata, "created", "created_at", "date created", "date"),
        updated=_first(metadata, "updated", "updated_at", "modified", "date modified", "lastmod"),
        aliases=[str(alias) for alias in aliases if alias] if isinstance(aliases, list) else [],
    )


def _first(metadata: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, (str, datetime, date)) and str(value).strip():
            return value.isoformat() if isinstance(value, (datetime, date)) else value
    return None


# --- Links of an import ---------------------------------------------------------------------------


class ImportLinks(ParseLinks):
    """Wikilinks across a set of Markdown files read together (a vault, an upload).

    ``[[Name]]`` resolves by file name, path, title or alias among the files
    (then ``fallback``: notes already in the workspace); ``[[Name#^id]]`` and
    ``[[Name#Heading]]`` resolve to the block with that ``^id`` / heading in
    that file, whichever file is read first. Set ``document_id`` to the note
    being read before parsing each file, and call :meth:`finish` on all of them.
    """

    def __init__(self, fallback: Callable[[str], str | None] | None = None) -> None:
        super().__init__()
        self._names: dict[str, str] = {}
        self._fallback = fallback
        self._headings: dict[tuple[str | None, str], str] = {}
        self._claimed: set[tuple[str | None, str]] = set()
        self._aliases: dict[str, str] = {}

    def add(self, document_id: str, *names: str | None) -> None:
        for name in names:
            if name:
                key = _name_key(name)
                if key:
                    self._names.setdefault(key, document_id)

    def note(self, name: str) -> str | None:
        key = _name_key(name)
        found = self._names.get(key) or self._names.get(key.rsplit("/", 1)[-1])
        if found is None and self._fallback is not None:
            found = self._fallback(name.strip())
        return found

    def heading(self, document_id: str | None, text: str) -> str | None:
        key = (document_id or self.document_id, heading_key(text))
        if key not in self._headings:
            self._headings[key] = new_block_id()
        return self._headings[key]

    def own_heading(self, text: str, block_id: str | None) -> str | None:
        key = (self.document_id, heading_key(text))
        if key in self._claimed:
            return block_id  # a later heading with the same name: links go to the first
        self._claimed.add(key)
        known = self._headings.get(key)
        if block_id is None:
            block_id = known or new_block_id()
        elif known is not None and known != block_id:
            self._aliases[known] = block_id
        self._headings[key] = block_id
        return block_id

    def finish(self, specs: list[BlockSpec]) -> list[BlockSpec]:
        """Point references made before their heading was read at the heading's own id."""

        if self._aliases:
            _rewrite_refs(specs, self._aliases)
        return specs


def _name_key(name: str) -> str:
    key = name.strip().replace("\\", "/")
    if key.lower().endswith(".md"):
        key = key[:-3]
    return " ".join(key.split()).casefold()


def _rewrite_refs(specs: list[BlockSpec], aliases: dict[str, str]) -> None:
    for item in specs:
        if item.content:
            item.content = [
                {**node, "id": aliases[node["id"]]}
                if node.get("type") == "blockRef" and node.get("id") in aliases else node
                for node in item.content
            ]
        _rewrite_refs(item.children, aliases)


# --- Reading the workspace -----------------------------------------------------------------------


def workspace_layout(connection) -> Layout:
    """The vault layout of every active note (no files consulted)."""

    rows = [dict(row) for row in connection.execute(
        "SELECT id, title, folder_id, created_at FROM documents WHERE deleted_at IS NULL"
    )]
    folders = [dict(row) for row in connection.execute(
        "SELECT id, parent_id, name, position FROM folders"
    )]
    return compute_layout(rows, folders)


def referenced_blocks(connection, document_id: str | None = None) -> dict[str, set[str]]:
    """Note id -> ids of its blocks that a block of an active note refers to."""

    sql = """SELECT DISTINCT target.document_id, refs.target_block_id
               FROM block_refs AS refs
               JOIN blocks AS source ON source.id = refs.source_block_id
               JOIN documents AS source_doc ON source_doc.id = source.document_id
               JOIN blocks AS target ON target.id = refs.target_block_id
              WHERE source_doc.deleted_at IS NULL"""
    params: tuple = ()
    if document_id is not None:
        sql += " AND target.document_id = ?"
        params = (document_id,)
    result: dict[str, set[str]] = {}
    for row in connection.execute(sql, params):
        result.setdefault(row[0], set()).add(row[1])
    return result


def workspace_names(connection) -> Callable[[str], str | None]:
    """Finds an active note by the names a wikilink may use: vault path, file name, title, id."""

    layout = workspace_layout(connection)
    names: dict[str, str] = {}
    for document_id, path in sorted(layout.paths.items(), key=lambda item: item[1]):
        for name in (path, posixpath.basename(path), layout.titles[document_id]):
            names.setdefault(_name_key(name), document_id)
    ids = set(layout.paths)

    def find(name: str) -> str | None:
        key = _name_key(name)
        return names.get(key) or (name.strip() if name.strip() in ids else None)

    return find

