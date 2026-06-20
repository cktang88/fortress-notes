"""OpenRouter chat completions: note review, consistency check, self-healing."""

import json

import httpx

from .config import get_settings
from .models import ReviewItem, ReviewKind, ReviewResponse

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM_PROMPTS: dict[ReviewKind, str] = {
    "factcheck": (
        "You are a rigorous fact-checker reviewing a personal note. Identify specific "
        "claims that are factually wrong, unsupported, or out of date. Be concrete and "
        "flag your own uncertainty. Do not praise; only surface problems."
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
    '"items": [{"label": "<short tag>", "detail": "<the point>"}]}. '
    "Return 1-7 items."
)


class LLMNotConfigured(Exception):
    """Raised when OPENROUTER_API_KEY is missing."""


async def chat_json(system: str, user: str, *, web: bool = False) -> dict:
    """Call OpenRouter and parse the JSON object response.

    Set web=True to enable OpenRouter's web search plugin (for fact-freshness).
    Returns the parsed dict, or {"_raw": <text>} if the model didn't return JSON.
    """
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise LLMNotConfigured

    payload: dict = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    if web:
        payload["plugins"] = [{"id": "web"}]

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": "Fortress Notes",
    }

    async with httpx.AsyncClient(timeout=180) as client:
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
            f"# {title}\n\n{body}",
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
        ReviewItem(label=i.get("label", ""), detail=i.get("detail", ""))
        for i in data.get("items", [])
    ]
    return ReviewResponse(kind=kind, summary=data.get("summary", ""), items=items)
