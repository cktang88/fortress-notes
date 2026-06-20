"""Agentic passes over notes: cross-note inconsistency detection + self-healing."""

import asyncio
import re

import httpx

from . import llm, notes_store, search
from .models import (
    ConsistencyIssue,
    ConsistencyReport,
    DeadLink,
    HealReport,
    StaleFact,
)

# ---- cross-note inconsistency detection ---------------------------------------

_CONSISTENCY_SYSTEM = (
    "You are doing a 'code review' across someone's notes. You are given a TARGET note "
    "and several RELATED notes retrieved by semantic search. Find places where the "
    "TARGET note CONTRADICTS a RELATED note (conflicting facts, dates, numbers, claims, "
    "or recommendations). Only report genuine contradictions, not mere differences in "
    "topic. Respond ONLY with JSON: "
    '{"summary": "<1-2 sentences>", "issues": [{"related_note_id": "<id>", '
    '"related_note_title": "<title>", "claim": "<what the target says>", '
    '"conflict": "<what the related note says>", "severity": "low|medium|high"}]}. '
    "Return an empty issues list if there are no contradictions."
)


async def check_consistency(note_id: str, k: int = 5) -> ConsistencyReport:
    target = notes_store.get_note(note_id)
    if target is None:
        return ConsistencyReport(summary="Note not found.", issues=[])

    related_results = search.related_notes(note_id, k)
    related_notes = [notes_store.get_note(r.note.id) for r in related_results]
    related_notes = [n for n in related_notes if n is not None]

    if not related_notes:
        return ConsistencyReport(
            summary="No related notes found to compare against.",
            issues=[],
            checked_against=[],
        )

    related_block = "\n\n".join(
        f"### RELATED NOTE (id={n.id}, title={n.title})\n{n.body}" for n in related_notes
    )
    user = (
        f"## TARGET NOTE (id={target.id}, title={target.title})\n{target.body}\n\n"
        f"## RELATED NOTES\n{related_block}"
    )

    try:
        data = await llm.chat_json(_CONSISTENCY_SYSTEM, user)
    except llm.LLMNotConfigured:
        return ConsistencyReport(
            summary="OPENROUTER_API_KEY is not set. Add it to backend/.env.",
            issues=[],
            checked_against=[n.title for n in related_notes],
        )

    checked = [n.title for n in related_notes]
    if "_raw" in data:
        return ConsistencyReport(summary=data["_raw"], issues=[], checked_against=checked)

    issues = [
        ConsistencyIssue(
            related_note_id=i.get("related_note_id", ""),
            related_note_title=i.get("related_note_title", ""),
            claim=i.get("claim", ""),
            conflict=i.get("conflict", ""),
            severity=i.get("severity", "medium"),
        )
        for i in data.get("issues", [])
    ]
    return ConsistencyReport(
        summary=data.get("summary", ""), issues=issues, checked_against=checked
    )


# ---- self-healing: dead links -------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\)\]\>\"'`]+")


def _extract_urls(text: str) -> list[str]:
    seen: list[str] = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:")
        if url not in seen:
            seen.append(url)
    return seen


async def _check_url(client: httpx.AsyncClient, url: str) -> DeadLink | None:
    try:
        resp = await client.get(url, follow_redirects=True, timeout=10)
        if resp.status_code >= 400:
            return DeadLink(url=url, status=str(resp.status_code))
        return None
    except httpx.HTTPError as e:
        return DeadLink(url=url, status=type(e).__name__)


async def _check_links(urls: list[str]) -> list[DeadLink]:
    if not urls:
        return []
    headers = {"User-Agent": "Mozilla/5.0 (FortressNotes link checker)"}
    async with httpx.AsyncClient(headers=headers) as client:
        results = await asyncio.gather(*(_check_url(client, u) for u in urls))
    return [r for r in results if r is not None]


# ---- self-healing: stale facts (web search) -----------------------------------

_HEAL_SYSTEM = (
    "You refresh personal notes for accuracy. Using web search, identify claims in the "
    "note that are likely OUT OF DATE or no longer true as of now, and propose updates. "
    "Focus on time-sensitive facts (versions, prices, leadership, statistics, 'latest' "
    "claims, dates). Ignore timeless statements. Respond ONLY with JSON: "
    '{"summary": "<1-2 sentences>", "stale_facts": [{"claim": "<outdated text>", '
    '"finding": "<what current sources say>", "suggestion": "<how to update it>"}]}. '
    "Return an empty list if nothing seems stale."
)


async def _check_stale_facts(title: str, body: str) -> tuple[str, list[StaleFact]]:
    try:
        data = await llm.chat_json(_HEAL_SYSTEM, f"# {title}\n\n{body}", web=True)
    except llm.LLMNotConfigured:
        return ("OPENROUTER_API_KEY is not set; skipped staleness check.", [])
    if "_raw" in data:
        return (data["_raw"], [])
    facts = [
        StaleFact(
            claim=f.get("claim", ""),
            finding=f.get("finding", ""),
            suggestion=f.get("suggestion", ""),
        )
        for f in data.get("stale_facts", [])
    ]
    return (data.get("summary", ""), facts)


async def heal_note(note_id: str) -> HealReport:
    note = notes_store.get_note(note_id)
    if note is None:
        return HealReport(summary="Note not found.")

    urls = _extract_urls(note.body)
    dead_links, (stale_summary, stale_facts) = await asyncio.gather(
        _check_links(urls),
        _check_stale_facts(note.title, note.body),
    )

    parts = []
    if urls:
        parts.append(f"Checked {len(urls)} link(s), {len(dead_links)} broken.")
    else:
        parts.append("No links found.")
    if stale_summary:
        parts.append(stale_summary)
    return HealReport(
        summary=" ".join(parts),
        dead_links=dead_links,
        stale_facts=stale_facts,
    )
