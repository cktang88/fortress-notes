"""Answer questions from the user's own notes, fast, with every sentence cited.

The design follows what works for agentic search (grep-style tools the model can
steer) while keeping answers under ~3 seconds:

1. Before any model call, gather candidates locally (milliseconds): literal grep
   for the question's key words plus unified search for paraphrases.
2. One model call sees those passages and either answers or asks for one more
   look: ``grep`` a pattern across every note, or ``read`` a whole note.
3. A hard time budget: when time is short the model must answer with what it
   has, and if the model is too slow the closest passages are returned instead.

Only passages are sent to the model, never the whole workspace. Every sentence
must cite a verbatim quote; sentences whose quotes aren't really in the cited
passage are dropped, so the answer can't claim things the notes don't say.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from . import block_query, block_store, llm, search
from .config import get_settings

BUDGET_SECONDS = 3.0
# Leave room for one more model call after a tool round only if at least this much time is left.
MIN_CALL_SECONDS = 1.2
MAX_TOOL_ROUNDS = 2
PREFETCH_SEARCH = 8
PREFETCH_GREP = 8
TOOL_HITS = 8
SOURCE_CHARS = 1200
NOTE_BLOCKS = 40

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
    "with an exact quote. If the notes don't contain the answer, return found=false "
    "with no sentences."
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


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


class _Sources:
    """Numbered passages shown to the model, deduplicated by block."""

    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, object]] = {}
        self._block_ids: set[str] = set()

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
                "text": text[:SOURCE_CHARS],
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


def grep_blocks(pattern: str, limit: int = TOOL_HITS) -> list[dict[str, object]]:
    """Active paragraphs matching a case-insensitive regex (or plain text if invalid)."""

    pattern = pattern.strip()[:200]
    if not pattern:
        return []
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    db_path = get_settings().block_db_path
    hits = []
    for block_id, _document_id, _version, text in block_store.embedding_documents(db_path):
        if regex.search(text):
            hits.append(block_id)
            if len(hits) >= limit:
                break
    details = block_query.block_details(db_path, hits)
    return [details[block_id] for block_id in hits if block_id in details]


def grep_terms(terms: list[str], limit: int = PREFETCH_GREP) -> list[dict[str, object]]:
    """Paragraphs containing the most of ``terms`` (literal, case-insensitive)."""

    if not terms:
        return []
    db_path = get_settings().block_db_path
    scored = []
    for block_id, _document_id, _version, text in block_store.embedding_documents(db_path):
        lowered = text.lower()
        hits = sum(1 for term in terms if term in lowered)
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


def prefetch(question: str, sources: _Sources) -> list[str]:
    """Literal grep for key words, then meaning-based search, merged (no model call)."""

    literal = grep_terms(key_terms(question))
    hits = search.unified_search(question, limit=PREFETCH_SEARCH)
    semantic = block_query.block_details(
        get_settings().block_db_path, [str(hit["block_id"]) for hit in hits]
    )
    ordered = [*literal, *(semantic[str(hit["block_id"])] for hit in hits if str(hit["block_id"]) in semantic)]
    return sources.add(ordered)


async def ask(question: str) -> dict[str, object]:
    started = _clock()
    question = question.strip()
    sources = _Sources()
    steps: list[dict[str, str]] = []
    first = prefetch(question, sources)
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
        try:
            data = await asyncio.wait_for(
                llm.chat_json(_SYSTEM, prompt, timeout=max(remaining, 0.5)),
                timeout=max(remaining, 0.5),
            )
        except llm.LLMNotConfigured:
            return _result("not_configured", [], sources, steps, started)
        except Exception:  # Too slow or unreachable: show the closest passages instead.
            return _result("timeout", [], sources, steps, started)
        if not isinstance(data, dict):
            break
        action = data.get("action", "answer")
        if action == "answer" or must_answer:
            return _answer(data, sources, steps, started)
        if action == "grep" and isinstance(data.get("pattern"), str):
            pattern = data["pattern"]
            added = sources.add(grep_blocks(pattern))
            steps.append({"action": "grep", "detail": pattern})
            found = sources.payload(added) if added else "[] (no new matches)"
            transcript += f'\n\nYou searched for /{pattern}/. New sources:\n{found}'
        elif action == "read" and isinstance(data.get("note"), str):
            note = data["note"]
            added = sources.add(read_note(note))
            steps.append({"action": "read", "detail": note})
            found = sources.payload(added) if added else "[] (no such note, or nothing new)"
            transcript += f'\n\nYou read the note "{note}". New sources:\n{found}'
        else:
            break
    return _result("not_found", [], sources, steps, started)


def _answer(data: dict, sources: _Sources, steps: list, started: float) -> dict[str, object]:
    answer = []
    for sentence in data.get("sentences", []) or []:
        if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str):
            continue
        citations = []
        for citation in sentence.get("citations", []) or []:
            if not isinstance(citation, dict):
                continue
            source = sources.by_id.get(str(citation.get("id")))
            quote = citation.get("quote")
            if source is None or not isinstance(quote, str) or not quote.strip():
                continue
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
        if citations:
            answer.append({"text": sentence["text"].strip(), "citations": citations})
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
