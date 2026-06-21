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
    "You compare a TARGET note against several RELATED notes and report only places "
    "where they DIRECTLY CONTRADICT EACH OTHER — the same subject stated with "
    "incompatible facts, dates, numbers, names, or recommendations across the notes. "
    "This is purely about internal agreement between the notes. "
    "DO NOT judge whether anything is factually true in the real world, and DO NOT "
    "flag grammar, style, or claims that merely differ in topic or detail without "
    "conflicting. If the notes do not disagree with each other, return no issues. "
    "Respond ONLY with JSON: "
    '{"summary": "<1-2 sentences>", "issues": [{"related_note_id": "<id>", '
    '"related_note_title": "<title>", "claim": "<what the target note says>", '
    '"conflict": "<the contradicting statement in the related note>", '
    '"severity": "low|medium|high"}]}. '
    "Return an empty issues list if there are no contradictions between the notes."
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
    "You detect TEMPORAL STALENESS in a personal note: facts that were TRUE when the "
    "note was last edited but have since BECOME FALSE because the world changed. The "
    "note's created and last-edited dates are given. Using web search, find only claims "
    "whose correctness has CHANGED since the last-edited date (e.g. 'current' leader, "
    "latest version, price, record, status, count). "
    "DO NOT report: grammar, spelling, style, opinions, timeless facts, or claims that "
    "were simply wrong all along (that is fact-checking, not healing). If a fact was "
    "already correct and is still correct, ignore it. "
    "The 'suggestion' field MUST be ONLY the corrected replacement text that should "
    "directly replace 'claim' in the note — a clean drop-in sentence/phrase with NO "
    "commentary, NO 'consider updating to', NO alternatives, NO surrounding quotes, and "
    "no 'or remove…'. It must read naturally if pasted in place of 'claim'. "
    "Respond ONLY with JSON: "
    '{"summary": "<1-2 sentences>", "stale_facts": [{"claim": "<the now-outdated text>", '
    '"finding": "<what is true now and roughly when it changed>", '
    '"suggestion": "<corrected replacement text only>"}]}. '
    "Return an empty list if nothing is stale."
)


async def _check_stale_facts(
    title: str, body: str, created: str, edited: str
) -> tuple[str, list[StaleFact]]:
    user = (
        f"Created: {created}\nLast edited: {edited}\n\n# {title}\n\n{body}\n\n"
        "Find facts that were correct as of the last-edited date but are no longer "
        "correct today."
    )
    try:
        data = await llm.chat_json(_HEAL_SYSTEM, user, web=True)
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
        _check_stale_facts(
            note.title,
            note.body,
            note.created_at.isoformat(),
            note.updated_at.isoformat(),
        ),
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
