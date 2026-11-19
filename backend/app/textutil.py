"""The shared text rules: what a word is, which words carry no meaning, how case and
accents are ignored, which text of a block is searchable, and how a passage is cut
around a match.

Search, related notes, duplicate detection and Ask all use these, so "cafe" finds
"café" everywhere and a stopword is a stopword everywhere. No imports from the rest
of the app (the block store uses ``searchable_text`` while writing).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache

# A word: a run of letters and digits in any script (what the FTS ``unicode61``
# tokenizer treats as a token, so word search, highlights and Ask agree).
WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Words that say nothing about what a passage is about (English; questions and notes).
STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing done down
    during each ever few first for from further get got had has have having he her
    here hers herself him himself his how i if in into is it its itself just last let
    like may me might mine more most must my myself next no nor not now of off on once
    one only or other our ours ourselves out over own same she should so some still
    such tell than that the their theirs them themselves then there these they this
    those through to too two under until up us use using very was we were what when
    where which while who whom whose why will with would you your yours yourself
    yourselves""".split()
)


def fold(text: str) -> str:
    """Lowercase ``text`` and strip accents, keeping exactly one character per character.

    Offsets into the result are offsets into ``text``, so matches found in the folded
    text can be highlighted in the original. Mirrors the FTS ``unicode61`` tokenizer's
    case and diacritic folding closely enough for highlighting.
    """

    if text.isascii():
        return text.lower()
    return text.translate(_FOLD_TABLE)


def fold_accents(pattern: str) -> str:
    """``pattern`` with accents stripped from non-ASCII letters only.

    For regular expressions matched (case-insensitively) against ``fold``-ed text:
    ASCII is left alone, so escapes such as ``\\D`` or ``\\W`` keep their meaning.
    """

    if pattern.isascii():
        return pattern
    return "".join(c if c.isascii() else _fold_char(c) for c in pattern)


class _FoldTable(dict):
    """code point -> folded character, filled on first use (for ``str.translate``)."""

    def __missing__(self, code: int) -> str:
        folded = self[code] = _fold_char(chr(code))
        return folded


_FOLD_TABLE = _FoldTable()


@lru_cache(maxsize=4096)
def _fold_char(character: str) -> str:
    if character.isascii():
        return character.lower()
    base = "".join(
        part
        for part in unicodedata.normalize("NFKD", character)
        if not unicodedata.combining(part)
    ).lower()
    if len(base) == 1:
        return base
    lowered = character.lower()
    return lowered if len(lowered) == 1 else character


def words(text: str) -> list[str]:
    """The words of ``text`` as search sees them: lowercase, without accents."""

    return WORD.findall(fold(text))


def word_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, folded word) for each word of ``text``, in ``text`` offsets."""

    return [(match.start(), match.end(), match.group()) for match in WORD.finditer(fold(text))]


def content_words(text: str, *, numbers: bool = True) -> list[str]:
    """The distinctive words of ``text``, folded, unique, in order of appearance.

    Stopwords and ASCII words under three letters are left out (words in other
    scripts are often short and still distinctive). Numbers ("2024", "42") are
    kept whatever their length when ``numbers`` is true, and dropped otherwise.
    """

    out: dict[str, None] = {}
    for word in words(text):
        if word in STOPWORDS:
            continue
        if word.isdigit():
            if numbers:
                out[word] = None
        elif len(word) >= 3 or not word.isascii():
            out[word] = None
    return list(out)


# --- What is searchable ------------------------------------------------------


# Similarity matching (meaning search, related notes, "seen before") ignores
# paragraphs shorter than this many non-space characters: "ok", "-", "a" match
# everything a little and nothing well. Exact word search still finds them.
MIN_SIMILARITY_CHARS = 3


def long_enough_to_compare(text: str) -> bool:
    """Whether ``text`` has enough to it for similarity matching (see above)."""

    return len("".join(text.split())) >= MIN_SIMILARITY_CHARS


def searchable_text(text: str, asset_text: str | Iterable[str] = "") -> str:
    """What full-text search, embeddings, related notes and Ask see of one block.

    ``text`` is the block's own plain text; ``asset_text`` the text extracted from
    the images and files it shows (OCR, a PDF's text layer), either joined with
    newlines in ``asset_text_sql`` order or as a list in that order.
    """

    parts = [asset_text] if isinstance(asset_text, str) else list(asset_text)
    extracted = "\n".join(part for part in parts if part.strip())
    return "\n\n".join(part for part in (text, extracted) if part)


def asset_text_sql(block_id: str) -> str:
    """SQL for one block's extracted asset text, ``block_id`` being an SQL expression.

    The non-empty texts joined with newlines, in media-name order, or NULL.
    """

    return f"""(SELECT group_concat(text, char(10)) FROM (
                  SELECT assets.text FROM block_assets
                    JOIN assets USING (content_hash, media_name)
                   WHERE block_assets.block_id = {block_id} AND trim(assets.text) <> ''
                   ORDER BY block_assets.media_name))"""


# --- Passages around a match -------------------------------------------------


def window(text: str, first: int, size: int) -> tuple[int, int]:
    """(start, stop) of at most ``size`` characters of ``text`` around offset ``first``.

    The match sits about a third of the way in, and the window starts on a word
    boundary when it does not start the text.
    """

    if len(text) <= size:
        return 0, len(text)
    start = max(0, min(first - size // 3, len(text) - size))
    if start > 0:
        space = text.find(" ", start, max(first, start + 1))
        start = space + 1 if space >= 0 else start
    return start, min(start + size, len(text))


def first_match(folded: str, terms: Iterable[str]) -> int:
    """Offset of the earliest of ``terms`` (already folded) in ``folded`` text, or 0."""

    return min((i for i in (folded.find(term) for term in terms if term) if i >= 0), default=0)
