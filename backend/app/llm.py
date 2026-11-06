"""OpenRouter chat completions for note review."""

import json

import httpx

from . import images
from .config import get_settings
from .models import (
    BlockReviewContextKind,
    BlockReviewContextResponse,
    BlockReviewItem,
    BlockReviewResponse,
    ReviewItem,
    ReviewKind,
    ReviewResponse,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM_PROMPTS: dict[ReviewKind, str] = {
    "factcheck": (
        "You are a rigorous fact-checker reviewing a personal note. FIRST steelman the "
        "note: read every claim in the most reasonable, charitable way a knowledgeable "
        "person would mean it. Only after steelmanning, flag genuine problems. "
        "Classify each flagged item by severity: "
        "'high' = obviously, provably incorrect; "
        "'medium' = misleading, or very incomplete / out of context; "
        "'low' = a minor revision. "
        "Be sparing with 'low' items — do NOT nitpick wording, style, or things that are "
        "fine under a charitable reading. If a claim is reasonable, do not flag it. "
        "Do not praise; only surface real problems."
    ),
    "clarify": (
        "You are a sharp editor reviewing a personal note. Ask the clarifying questions "
        "needed to make the note unambiguous and complete. Focus on gaps, undefined "
        "terms, and missing context."
    ),
    "object": (
        "You are doing a 'code review' for someone's reasoning. Surface objections, "
        "inconsistencies, and faulty or missing steps in the reasoning chain. Steelman "
        "the strongest counterarguments. Be direct."
    ),
}

_RESPONSE_INSTRUCTION = (
    "Respond ONLY with JSON of the form "
    '{"summary": "<1-2 sentence overview>", '
    '"items": [{"label": "<short tag>", "detail": "<the point>", '
    '"quote": "<the exact verbatim span of text from the note this point refers to>", '
    '"severity": "high|medium|low"}]}. '
    "The quote MUST be copied character-for-character from the note so it can be located. "
    "Use severity high for provably incorrect, medium for misleading/incomplete, low for "
    "minor revisions. Return 0-7 items (return none if the note holds up)."
)


class LLMNotConfigured(Exception):
    """Raised when OPENROUTER_API_KEY is missing."""


async def chat_json(
    system: str,
    user: str,
    *,
    web: bool = False,
    timeout: float = 180,
    model: str | None = None,
    fast: bool = False,
) -> dict:
    """Call OpenRouter and parse the JSON object response.

    Set web=True to enable OpenRouter's web search plugin (for fact-freshness).
    Returns the parsed dict, or {"_raw": <text>} if the model didn't return JSON.
    """
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise LLMNotConfigured

    payload: dict = {
        "model": model or settings.openrouter_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    if web:
        payload["plugins"] = [{"id": "web"}]
    if fast:
        # Route to the quickest provider and keep answers short.
        payload["provider"] = {"sort": "latency"}
        payload["max_tokens"] = 600

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": "Fortress Notes",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": content.strip()}


async def review_note(kind: ReviewKind, title: str, body: str) -> ReviewResponse:
    try:
        data = await chat_json(
            f"{_SYSTEM_PROMPTS[kind]} {_RESPONSE_INSTRUCTION}",
            f"# {title}\n\n{images.inline_for_llm(body)}",
        )
    except LLMNotConfigured:
        return ReviewResponse(
            kind=kind,
            summary="OPENROUTER_API_KEY is not set. Add it to backend/.env to enable AI review.",
            items=[],
        )
    if "_raw" in data:
        return ReviewResponse(kind=kind, summary=data["_raw"], items=[])
    items = [
        ReviewItem(
            label=i.get("label", ""),
            detail=i.get("detail", ""),
            quote=i.get("quote", ""),
            severity=i.get("severity", "medium"),
        )
        for i in data.get("items", [])
    ]
    return ReviewResponse(kind=kind, summary=data.get("summary", ""), items=items)


async def review_block(
    kind: ReviewKind, block_id: str, block_text: str, content: dict
) -> BlockReviewResponse:
    """Review one persisted block; findings are retained only with exact source quotes."""
    try:
        data = await chat_json(
            f"{_SYSTEM_PROMPTS[kind]} {_RESPONSE_INSTRUCTION.replace('note', 'block')}",
            "Review this single canonical note block. Do not use surrounding document context.\n"
            f"block_id: {block_id}\n"
            f"block_text: {block_text}\n"
            f"block_content: {json.dumps(content, ensure_ascii=False)}",
        )
    except LLMNotConfigured:
        return BlockReviewResponse(
            kind=kind,
            summary="OPENROUTER_API_KEY is not set. Add it to backend/.env to enable AI review.",
            block_id=block_id,
            items=[],
        )
    if "_raw" in data:
        return BlockReviewResponse(
            kind=kind, summary=data["_raw"], block_id=block_id, items=[]
        )
    items = []
    for item in data.get("items", []):
        quote = item.get("quote", "")
        if not isinstance(quote, str) or not quote or quote not in block_text:
            continue
        items.append(
            BlockReviewItem(
                block_id=block_id,
                label=item.get("label", ""),
                detail=item.get("detail", ""),
                quote=quote,
                severity=item.get("severity", "medium"),
            )
        )
    return BlockReviewResponse(
        kind=kind, summary=data.get("summary", ""), block_id=block_id, items=items
    )


async def review_blocks(
    kind: ReviewKind,
    context: BlockReviewContextKind,
    targets: list[dict],
    linked_context: list[dict],
) -> BlockReviewContextResponse:
    """Review canonical blocks, retaining only findings quoted from their target block."""
    target_sources = {
        block["id"]: block["text"] for block in targets if isinstance(block.get("text"), str)
    }
    target_payload = [
        {key: block[key] for key in ("id", "text", "content")} for block in targets
    ]
    linked_payload = [
        {key: block[key] for key in ("id", "document_id", "type", "text", "content")}
        for block in linked_context
    ]
    try:
        data = await chat_json(
            _SYSTEM_PROMPTS[kind],
            "Review the target blocks using the supplied context. Return findings only for "
            "target blocks, never for linked context blocks. Each finding must include the "
            "target block_id and a quote copied exactly from that block's text. "
            "Return JSON with a summary and an items array. Each item must have block_id, "
            "label, detail, quote, and severity (high, medium, or low). Return no more than "
            "7 findings.\n"
            f"target_blocks: {json.dumps(target_payload, ensure_ascii=False)}\n"
            f"linked_context_blocks: {json.dumps(linked_payload, ensure_ascii=False)}",
        )
    except LLMNotConfigured:
        return BlockReviewContextResponse(
            kind=kind,
            context=context,
            summary="OPENROUTER_API_KEY is not set. Add it to backend/.env to enable AI review.",
            items=[],
        )
    if "_raw" in data:
        return BlockReviewContextResponse(
            kind=kind, context=context, summary=data["_raw"], items=[]
        )

    items = []
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        block_id = item.get("block_id")
        quote = item.get("quote")
        source = target_sources.get(block_id) if isinstance(block_id, str) else None
        if source is None or not isinstance(quote, str) or not quote or quote not in source:
            continue
        items.append(
            BlockReviewItem(
                block_id=block_id,
                label=item.get("label", ""),
                detail=item.get("detail", ""),
                quote=quote,
                severity=item.get("severity", "medium"),
            )
        )
    return BlockReviewContextResponse(
        kind=kind, context=context, summary=data.get("summary", ""), items=items
    )
