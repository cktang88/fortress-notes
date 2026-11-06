"""Answer questions from the user's own notes, fast, with every sentence cited.

The design follows what works for agentic search (grep-style tools the model can
steer) while keeping answers under ~3 seconds:

1. Before any model call, gather candidates locally (tens of milliseconds): literal
   grep for the question's key words and unified search for paraphrases, in
   parallel.
2. One model call sees those passages and either answers or asks for one more
   look: ``grep`` a pattern across every note, or ``read`` a whole note.
3. The reply is streamed: each answer sentence is checked and sent on as soon as
   it is complete, so the first sentence shows well before the whole answer.
4. A hard time budget: when time is short the model must answer with what it
   has, and if the model is too slow the closest passages (plus any sentences
   already checked) are returned instead.

Only passages are sent to the model, never the whole workspace, and only the part
of a long paragraph around the matched words. Every sentence must cite a verbatim
quote; sentences whose quotes aren't really in the cited passage are dropped, so
the answer can't claim things the notes don't say.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import regex

from . import block_query, block_store, llm, search
from .config import get_settings

BUDGET_SECONDS = 3.0
# Leave room for one more model call after a tool round only if at least this much time is left.
MIN_CALL_SECONDS = 1.2
MAX_TOOL_ROUNDS = 2
PREFETCH_SEARCH = 6
PREFETCH_GREP = 6
TOOL_HITS = 8
# Characters of a paragraph shown to the model: a window around the matched words.
SOURCE_CHARS = 700
NOTE_BLOCKS = 40
# Total time one grep over every note may take (the model's regex can backtrack badly).
GREP_SECONDS = 0.3

_clock = time.monotonic

_STOPWORDS = frozenset(
    """the and for that with this from have are was were but not you your our they them
    their there then than what when where which who whom whose why how did does do done
    will would could should about into over also just like some more most only very been
    being has had its can all any each other such these those out get got one may might
    must ever last first tell me my mine i we us is it of on in at to a an or as by""".split()
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

_SYSTEM = (
    "You answer questions using ONLY the person's own notes. Sources are numbered "
    "passages (S1, S2, ...) from their notes, each with its note title and edit date. "
    "Never use outside knowledge and never guess.\n"
    "Reply with exactly one JSON object, one of:\n"
    '1. {"action": "answer", "found": true|false, "sentences": [{"text": "...", '
    '"citations": [{"id": "S1", "quote": "<text copied exactly from that source>"}]}]}\n'
    '2. {"action": "grep", "pattern": "<case-insensitive regex>"} to search every note '
    "for exact words, names, dates or numbers not covered by the sources.\n"
    '3. {"action": "read", "note": "<note title>"} to read a whole note when a source '
    "is cut off or the answer needs its surrounding context.\n"
    "Prefer answering immediately: only search or read when the sources clearly cannot "
    "answer. Answers are 1-5 short sentences; every sentence cites at least one source "
    "with an exact quote. If the sources don't contain the answer, return found=false "
    'with no sentences AND a "try" field: a case-insensitive regex of other wording the '
    "person may have used for it (synonyms, names, translations), e.g. "
    '{"action": "answer", "found": false, "sentences": [], '
    '"try": "custard tart|pastel de nata|nata"}.'
)


_TYPOGRAPHY = str.maketrans(
    {
        **dict.fromkeys("‘’‚‛′", "'"),
        **dict.fromkeys("“”„‟″", '"'),
        **dict.fromkeys("‐‑‒–—―−", "-"),
    }
)


def _normalize(text: str) -> str:
    """Text as compared for quotes: ignores case, spacing, typographic quotes and
    dashes, Markdown emphasis, and quote marks wrapped around the whole quote."""

    text = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHY)
    text = re.sub(r"[*_`]+", "", text)
    return re.sub(r"\s+", " ", text).strip().strip("\"'").strip().lower()


def key_terms(question: str) -> list[str]:
    """Distinctive words of a question, longest first (names, dates, rare words)."""

    terms = []
    for word in _WORD.findall(question):
        lowered = word.lower()
        if lowered in _STOPWORDS or (len(lowered) < 3 and not lowered.isdigit()):
            continue
        if lowered not in terms:
            terms.append(lowered)
    return sorted(terms, key=len, reverse=True)[:6]


def _window(text: str, terms: list[str]) -> str:
    """At most ``SOURCE_CHARS`` of ``text``, centred on the first matched term.

    The window is a verbatim slice, so a quote found in it is in the note too.
    """

    if len(text) <= SOURCE_CHARS:
        return text
    lowered = text.lower()
    first = min((i for i in (lowered.find(term) for term in terms) if i >= 0), default=0)
    start = max(0, min(first - SOURCE_CHARS // 3, len(text) - SOURCE_CHARS))
    if start > 0:  # Start on a word boundary.
        space = text.find(" ", start, first if first > start else start + 40)
        start = space + 1 if space >= 0 else start
    return text[start : start + SOURCE_CHARS]


class _Sources:
    """Numbered passages shown to the model, deduplicated by block."""

    def __init__(self, terms: list[str] | None = None) -> None:
        self.by_id: dict[str, dict[str, object]] = {}
        self._block_ids: set[str] = set()
        self.terms = terms or []

    def add(self, rows: list[dict[str, object]]) -> list[str]:
        added = []
        for row in rows:
            block_id = str(row["id"])
            text = str(row.get("text") or "")
            if block_id in self._block_ids or not text.strip():
                continue
            self._block_ids.add(block_id)
            source_id = f"S{len(self.by_id) + 1}"
            self.by_id[source_id] = {
                "block_id": block_id,
                "document_id": row["document_id"],
                "document_title": row["document_title"],
                "updated_at": row.get("updated_at", ""),
                "text": _window(text, self.terms),
            }
            added.append(source_id)
        return added

    def payload(self, source_ids: list[str]) -> str:
        return json.dumps(
            [
                {
                    "id": source_id,
                    "note": self.by_id[source_id]["document_title"],
                    "edited": str(self.by_id[source_id]["updated_at"])[:10],
                    "text": self.by_id[source_id]["text"],
                }
                for source_id in source_ids
            ],
            ensure_ascii=False,
        )


def _compile_grep(pattern: str):
    """The model's case-insensitive regex, or its ``|`` alternatives as plain text."""

    try:
        return regex.compile(pattern, regex.IGNORECASE)
    except regex.error:
        alternatives = [part.strip() for part in pattern.split("|") if part.strip()]
        if not alternatives:
            return None
        return regex.compile("|".join(map(regex.escape, alternatives)), regex.IGNORECASE)


def grep_blocks(pattern: str, limit: int = TOOL_HITS) -> list[dict[str, object]]:
    """Active paragraphs matching a case-insensitive regex (or plain text if invalid).

    Runs under a time limit so a pathological pattern can't stall Ask, and only a
    non-empty match counts, so a stray ``|`` doesn't match every paragraph.
    """

    pattern = pattern.strip()[:200]
    if not pattern:
        return []
    compiled = _compile_grep(pattern)
    if compiled is None:
        return []
    db_path = get_settings().block_db_path
    deadline = time.monotonic() + GREP_SECONDS
    hits = []
    for block_id, _document_id, _version, text in block_store.embedding_documents(db_path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            matched = any(match.group() for match in compiled.finditer(text, timeout=remaining))
        except TimeoutError:
            break
        if matched:
            hits.append(block_id)
            if len(hits) >= limit:
                break
    details = block_query.block_details(db_path, hits)
    return [details[block_id] for block_id in hits if block_id in details]


# The corpus lower-cased once per change, so a term grep is one pass of `in` checks.
_lowered: tuple[object, list[tuple[str, str]]] | None = None


def _lowered_corpus(corpus) -> list[tuple[str, str]]:
    global _lowered
    cached = _lowered
    if cached is not None and cached[0] is corpus:
        return cached[1]
    lowered = [(block_id, text.lower()) for block_id, _document_id, _version, text in corpus]
    _lowered = (corpus, lowered)
    return lowered


def grep_terms(terms: list[str], limit: int = PREFETCH_GREP) -> list[dict[str, object]]:
    """Paragraphs containing the most of ``terms`` (literal, case-insensitive)."""

    if not terms:
        return []
    db_path = get_settings().block_db_path
    corpus = _lowered_corpus(block_store.embedding_documents(db_path))
    scored = []
    for block_id, lowered in corpus:
        hits = 0
        for term in terms:
            if term in lowered:
                hits += 1
        if hits:
            scored.append((hits, block_id))
    scored.sort(key=lambda item: -item[0])
    ids = [block_id for _hits, block_id in scored[:limit]]
    details = block_query.block_details(db_path, ids)
    return [details[block_id] for block_id in ids if block_id in details]


def read_note(name: str) -> list[dict[str, object]]:
    """Every paragraph of the note whose title (or ID) best matches ``name``."""

    db_path = get_settings().block_db_path
    wanted = name.strip().lower()
    documents = block_store.list_documents(db_path)
    match = next(
        (d for d in documents if str(d["id"]) == name.strip() or str(d["title"]).lower() == wanted),
        None,
    ) or next((d for d in documents if wanted and wanted in str(d["title"]).lower()), None)
    if match is None:
        return []
    tree = block_store.document_tree(db_path, str(match["id"]))
    if tree is None:
        return []
    rows: list[dict[str, object]] = []

    def visit(nodes: list[dict]) -> None:
        for node in nodes:
            if len(rows) >= NOTE_BLOCKS:
                return
            rows.append(
                {
                    "id": node["id"],
                    "document_id": tree["id"],
                    "document_title": tree["title"],
                    "updated_at": node["updated_at"],
                    "text": node["text"],
                }
            )
            visit(node["children"])

    visit(tree["children"])
    return rows


def _semantic(question: str) -> list[dict[str, object]]:
    hits = search.unified_search(question, limit=PREFETCH_SEARCH)
    details = block_query.block_details(
        get_settings().block_db_path, [str(hit["block_id"]) for hit in hits]
    )
    return [details[str(hit["block_id"])] for hit in hits if str(hit["block_id"]) in details]


def prefetch(question: str, sources: _Sources) -> list[str]:
    """Literal grep for key words, then meaning-based search, merged (no model call)."""

    literal = grep_terms(key_terms(question))
    return sources.add([*literal, *_semantic(question)])


async def _prefetch_parallel(question: str, sources: _Sources) -> list[str]:
    # Both are synchronous (SQLite, ColBERT); run them side by side off the event
    # loop so other requests aren't stalled while Ask looks things up.
    literal, semantic = await asyncio.gather(
        asyncio.to_thread(grep_terms, key_terms(question)),
        asyncio.to_thread(_semantic, question),
    )
    return sources.add([*literal, *semantic])


class _SentenceStream:
    """Pulls complete ``sentences`` items out of a JSON reply as it streams in.

    A tiny incremental scanner (strings, escapes, nesting): every time an object
    directly inside the top-level ``"sentences"`` array closes, it is parsed and
    returned. Anything unexpected is simply left for the final full parse.
    """

    def __init__(self) -> None:
        self.text = ""
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._string_start = 0
        self._last_string: str | None = None
        self._last_key: str | None = None
        self._array_depth: int | None = None
        self._object_start: int | None = None

    def feed(self, delta: str) -> list[dict]:
        start = len(self.text)
        self.text += delta
        text = self.text
        found = []
        for i in range(start, len(text)):
            char = text[i]
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif char == "\\":
                    self._escape = True
                elif char == '"':
                    self._in_string = False
                    self._last_string = text[self._string_start : i + 1]
                continue
            if char == '"':
                self._in_string = True
                self._string_start = i
            elif char == ":":
                try:
                    self._last_key = json.loads(self._last_string) if self._last_string else None
                except json.JSONDecodeError:
                    self._last_key = None
            elif char in "{[":
                if char == "[" and self._depth == 1 and self._last_key == "sentences":
                    self._array_depth = self._depth + 1
                elif char == "{" and self._array_depth == self._depth:
                    self._object_start = i
                self._depth += 1
            elif char in "}]":
                self._depth -= 1
                if char == "}" and self._object_start is not None and self._depth == self._array_depth:
                    try:
                        item = json.loads(text[self._object_start : i + 1])
                    except json.JSONDecodeError:
                        item = None
                    if isinstance(item, dict):
                        found.append(item)
                    self._object_start = None
                elif char == "]" and self._array_depth is not None and self._depth < self._array_depth:
                    self._array_depth = None
        return found


def _checked(sentence: object, sources: _Sources) -> dict | None:
    """The sentence with only its genuine citations, or None if none hold up."""

    if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str):
        return None
    citations = []
    for citation in sentence.get("citations", []) or []:
        if not isinstance(citation, dict):
            continue
        source = sources.by_id.get(str(citation.get("id")))
        quote = citation.get("quote")
        if source is None or not isinstance(quote, str):
            continue
        if not any(character.isalnum() for character in quote):
            continue  # Punctuation alone "matches" almost any passage.
        if _normalize(quote) not in _normalize(str(source["text"])):
            continue  # The model "quoted" something the note doesn't say.
        citations.append(
            {
                "block_id": source["block_id"],
                "document_id": source["document_id"],
                "document_title": source["document_title"],
                "quote": quote.strip(),
            }
        )
    if not citations or not sentence["text"].strip():
        return None
    return {"text": sentence["text"].strip(), "citations": citations}


Emit = Callable[[dict], Awaitable[None]]


async def _call(prompt: str, timeout: float, sources: _Sources, emit: Emit, shown: list) -> dict:
    """One streamed model call; checked sentences are emitted as they complete."""

    stream = _SentenceStream()
    async with asyncio.timeout(timeout):
        async for delta in llm.chat_json_stream(
            _SYSTEM, prompt, timeout=timeout, model=get_settings().ask_model, fast=True
        ):
            for sentence in stream.feed(delta):
                checked = _checked(sentence, sources)
                if checked is not None:
                    shown.append(checked)
                    await emit({"type": "sentence", "sentence": checked})
    return llm._parse_json_object(stream.text)


async def _run(question: str, emit: Emit) -> dict[str, object]:
    started = _clock()
    question = question.strip()
    sources = _Sources(key_terms(question))
    steps: list[dict[str, str]] = []
    first = await _prefetch_parallel(question, sources)
    if not first:
        return _result("not_found", [], sources, steps, started)

    message = f"Question: {question}\n\nSources:\n{sources.payload(first)}"
    transcript = message
    for round_number in range(MAX_TOOL_ROUNDS + 1):
        remaining = BUDGET_SECONDS - (_clock() - started)
        must_answer = round_number == MAX_TOOL_ROUNDS or remaining < MIN_CALL_SECONDS * 2
        prompt = transcript + (
            "\n\nYou are out of time: answer now with action \"answer\"." if must_answer else ""
        )
        shown: list[dict] = []
        try:
            data = await _call(prompt, max(remaining, 0.5), sources, emit, shown)
        except llm.LLMNotConfigured:
            return _result("not_configured", [], sources, steps, started)
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
            # Too slow: keep what was already checked, else show the closest passages.
            return _result("answered" if shown else "timeout", shown, sources, steps, started)
        except Exception:
            logging.getLogger("fortress").warning("Ask could not reach the model", exc_info=True)
            return _result("answered" if shown else "unavailable", shown, sources, steps, started)
        if not isinstance(data, dict):
            break
        action = data.get("action", "answer")
        retry = data.get("try")
        if (
            action == "answer"
            and not must_answer
            and not data.get("sentences")
            and isinstance(retry, str)
            and retry.strip()
            and not steps
        ):
            # "Not found" with other wording to try: grep it locally (milliseconds) and,
            # only if that turns up new passages, give the model one last look.
            added = sources.add(await asyncio.to_thread(grep_blocks, retry))
            steps.append({"action": "grep", "detail": retry})
            await emit({"type": "step", "step": steps[-1]})
            if added:
                transcript += (
                    f"\n\nYou searched for /{retry}/. New sources:\n{sources.payload(added)}"
                )
                continue
            return _answer(data, sources, steps, started, shown)
        if action == "answer" or must_answer or data.get("sentences"):
            return _answer(data, sources, steps, started, shown)
        if action == "grep" and isinstance(data.get("pattern"), str):
            pattern = data["pattern"]
            added = sources.add(await asyncio.to_thread(grep_blocks, pattern))
            steps.append({"action": "grep", "detail": pattern})
            await emit({"type": "step", "step": steps[-1]})
            found = sources.payload(added) if added else "[] (no new matches)"
            transcript += f'\n\nYou searched for /{pattern}/. New sources:\n{found}'
        elif action == "read" and isinstance(data.get("note"), str):
            note = data["note"]
            added = sources.add(await asyncio.to_thread(read_note, note))
            steps.append({"action": "read", "detail": note})
            await emit({"type": "step", "step": steps[-1]})
            found = sources.payload(added) if added else "[] (no such note, or nothing new)"
            transcript += f'\n\nYou read the note "{note}". New sources:\n{found}'
        else:
            break
    return _result("not_found", [], sources, steps, started)


async def ask_stream(question: str) -> AsyncIterator[dict]:
    """Ask, as events: ``sentence`` (checked, as soon as complete), ``step`` (a
    grep or read), then ``done`` with the same result ``ask`` returns."""

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def run() -> None:
        try:
            result = await _run(question, emit)
        except Exception:
            logging.getLogger("fortress").exception("Ask failed")
            result = _result("unavailable", [], _Sources(), [], _clock())
        await queue.put({"type": "done", "result": result})

    task = asyncio.create_task(run())
    try:
        while True:
            event = await queue.get()
            yield event
            if event["type"] == "done":
                return
    finally:
        if not task.done():
            task.cancel()


async def ask(question: str) -> dict[str, object]:
    async for event in ask_stream(question):
        if event["type"] == "done":
            return event["result"]
    raise RuntimeError("Ask ended without a result")  # pragma: no cover


def _answer(
    data: dict, sources: _Sources, steps: list, started: float, shown: list | None = None
) -> dict[str, object]:
    sentences = data.get("sentences")
    checked = (_checked(s, sources) for s in (sentences if isinstance(sentences, list) else []))
    answer = [sentence for sentence in checked if sentence is not None]
    if not answer and shown:
        answer = list(shown)  # The full reply didn't parse; keep what streamed.
    return _result("answered" if answer else "not_found", answer, sources, steps, started)


def _result(
    status: str, answer: list, sources: _Sources, steps: list, started: float
) -> dict[str, object]:
    return {
        "status": status,
        "answer": answer,
        "sources": [
            {key: source[key] for key in ("block_id", "document_id", "document_title", "text")}
            for source in list(sources.by_id.values())[:10]
        ],
        "steps": steps,
        "elapsed_ms": round((_clock() - started) * 1000),
    }
