"""A comparable content model of one page, built from either side.

Both the reference (one2html's HTML) and Fortress (the imported block tree)
are reduced to the same shape so ``checks.py`` can diff them:

``Model`` holds, in document order: text tokens (word + the inline marks on
it), headings, list items, check items, links, tables, images, files, math,
ink, note tags and top-level "containers" (OneNote outlines / Fortress
columns) for the layout check.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

WORD_RE = re.compile(r"\w+", re.UNICODE)


def norm_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace(" ", " ")
    return " ".join(text.split())


def words(text: str) -> list[str]:
    return WORD_RE.findall(unicodedata.normalize("NFC", text).casefold())


def norm_color(value: str | None) -> str | None:
    """``rgb(r,g,b)`` / ``#rrggbb`` / ``#rgb`` -> ``#RRGGBB`` (None for transparent/none)."""

    if not value:
        return None
    value = value.strip().lower()
    match = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)", value)
    if match:
        if match.group(4) is not None and float(match.group(4)) == 0:
            return None
        return "#{:02X}{:02X}{:02X}".format(*(int(match.group(i)) for i in (1, 2, 3)))
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return value.upper()
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(ch * 2 for ch in value[1:]).upper()
    if value in {"transparent", "none", "inherit", "initial", "default"}:
        return None
    return value


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Inline marks compared per word. Colours are compared as "color:#RRGGBB" etc.
MARK_NAMES = ("bold", "italic", "underline", "strike", "superscript", "subscript", "code")


@dataclass
class Token:
    word: str
    marks: frozenset[str]
    container: int = -1  # index into Model.containers (-1: none)


@dataclass
class Model:
    title: str = ""
    tokens: list[Token] = field(default_factory=list)
    text: str = ""  # readable text (for context in reports)
    headings: list[tuple[int, str]] = field(default_factory=list)
    list_items: list[tuple[str, bool, int, str]] = field(default_factory=list)  # (text, ordered, depth, style)
    checks: list[tuple[str, bool]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, href)
    tables: list[dict] = field(default_factory=list)  # {"rows","cols","cells":[[text]],"bg":[[color]],"depth"}
    images: list[dict] = field(default_factory=list)  # {"sha","alt","name","w","h"}
    files: list[dict] = field(default_factory=list)  # {"sha","name","size"}
    math: list[str] = field(default_factory=list)
    ink: list[dict] = field(default_factory=list)  # {"text"}
    tags: list[tuple[str, str]] = field(default_factory=list)  # (label/shape, text)
    aligned: list[tuple[str, str]] = field(default_factory=list)  # (align, text)
    containers: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def word_list(self) -> list[str]:
        return [token.word for token in self.tokens]


def tokens_from_pieces(pieces: list[tuple[str, frozenset[str]]], container: int = -1) -> list[Token]:
    """Words of consecutive inline text pieces (runs), each with the marks of its first character.

    Words are found in the joined text, so a word split across runs (``**bo**ld``,
    a link ending mid-word) is still one word.
    """

    texts = [unicodedata.normalize("NFC", text) for text, _ in pieces]
    joined = "".join(texts)
    starts, position = [], 0
    for text in texts:
        starts.append(position)
        position += len(text)
    tokens = []
    piece = 0
    for match in WORD_RE.finditer(joined):
        while piece + 1 < len(starts) and starts[piece + 1] <= match.start():
            piece += 1
        tokens.append(Token(match.group(0).casefold(), pieces[piece][1], container))
    return tokens
