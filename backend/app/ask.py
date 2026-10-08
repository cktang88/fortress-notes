"""Answer questions from the user's own notes, with every sentence cited.

Only the best-matching paragraphs (from unified search) are sent to the model,
never whole notes. The model must cite a verbatim quote for each sentence; any
sentence whose quote isn't really in the cited paragraph is dropped, so the
answer can't claim things the notes don't say.
"""

from __future__ import annotations

import json
import re

from . import block_query, llm, search
from .config import get_settings

SOURCE_LIMIT = 10
SOURCE_CHARS = 1200

_SYSTEM = (
    "You answer questions using ONLY the person's own notes, given as numbered sources. "
    "Never use outside knowledge and never guess. Write a short, direct answer of 1-5 "
    "sentences. Every sentence must cite at least one source by its id together with a "
    "quote copied character-for-character from that source's text that supports it. "
    "If the sources don't answer the question, set found to false and return no sentences. "
    'Respond ONLY with JSON: {"found": true|false, "sentences": [{"text": "...", '
    '"citations": [{"id": "<source id>", "quote": "<exact text from that source>"}]}]}'
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def retrieve(question: str) -> list[dict[str, object]]:
    """The best-matching paragraphs for a question, with their full text."""

    hits = search.unified_search(question, limit=SOURCE_LIMIT)
    details = block_query.block_details(
        get_settings().block_db_path, [str(hit["block_id"]) for hit in hits]
    )
    sources = []
    for hit in hits:
        row = details.get(str(hit["block_id"]))
        if row is None or not str(row["text"]).strip():
            continue
        sources.append(
            {
                "block_id": row["id"],
                "document_id": row["document_id"],
                "document_title": row["document_title"],
                "text": str(row["text"])[:SOURCE_CHARS],
                "updated_at": row["updated_at"],
            }
        )
    return sources


async def ask(question: str) -> dict[str, object]:
    question = question.strip()
    sources = retrieve(question)
    if not sources:
        return {"status": "not_found", "answer": [], "sources": []}

    numbered = {f"S{index + 1}": source for index, source in enumerate(sources)}
    payload = [
        {
            "id": source_id,
            "note": source["document_title"],
            "edited": str(source["updated_at"])[:10],
            "text": source["text"],
        }
        for source_id, source in numbered.items()
    ]
    try:
        data = await llm.chat_json(
            _SYSTEM,
            f"Question: {question}\n\nSources:\n{json.dumps(payload, ensure_ascii=False)}",
        )
    except llm.LLMNotConfigured:
        return {"status": "not_configured", "answer": [], "sources": _public(sources)}

    answer = []
    for sentence in data.get("sentences", []) if isinstance(data, dict) else []:
        if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str):
            continue
        citations = []
        for citation in sentence.get("citations", []) or []:
            if not isinstance(citation, dict):
                continue
            source = numbered.get(str(citation.get("id")))
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

    status = "answered" if answer else "not_found"
    return {"status": status, "answer": answer, "sources": _public(sources)}


def _public(sources: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {key: source[key] for key in ("block_id", "document_id", "document_title", "text")}
        for source in sources
    ]
