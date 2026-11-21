"""OpenRouter chat completions (used by Ask)."""

import json

import httpx

from .config import get_settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMNotConfigured(Exception):
    """Raised when OPENROUTER_API_KEY is missing."""


_clients: dict[int, httpx.AsyncClient] = {}


def _client() -> httpx.AsyncClient:
    """One pooled client per event loop: reusing the TLS connection saves ~250 ms a call."""

    import asyncio

    loop = asyncio.get_running_loop()
    client = _clients.get(id(loop))
    if client is None or client.is_closed:
        # Drop clients of loops that have gone away (tests make a loop per request).
        for key, old in list(_clients.items()):
            if old.is_closed:
                del _clients[key]
        client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=90),
        )
        _clients[id(loop)] = client
    return client


_last_preconnect = 0.0


async def preconnect() -> None:
    """Open (or keep warm) the connection to OpenRouter before a question arrives.

    At most one cheap request every 30 seconds, and only with a key configured.
    """

    import time

    global _last_preconnect
    settings = get_settings()
    if not settings.openrouter_api_key or time.monotonic() - _last_preconnect < 30:
        return
    _last_preconnect = time.monotonic()
    try:
        await _client().get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            timeout=5,
        )
    except httpx.HTTPError:
        pass


async def chat_json_stream(
    system: str,
    user: str,
    *,
    timeout: float = 30,
    model: str,
    fast: bool = False,
    reasoning_effort: str = "",
):
    """Stream a JSON-object reply from OpenRouter, yielding text deltas as they arrive.

    Uses a pooled connection. Raises ``LLMNotConfigured`` without a key and
    ``httpx`` errors on HTTP failures.
    """
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise LLMNotConfigured

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "stream": True,
    }
    if fast:
        payload["provider"] = {"sort": "latency"}
        payload["max_tokens"] = 600
        payload["temperature"] = 0
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort, "exclude": True}
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": "Fortress Notes",
    }
    async with _client().stream(
        "POST", OPENROUTER_URL, json=payload, headers=headers, timeout=timeout
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue  # SSE comments (": OPENROUTER PROCESSING") and blank lines
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if "error" in event:
                raise httpx.HTTPError(str(event["error"])[:200])
            for choice in event.get("choices") or []:
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    yield delta


def parse_json_object(content: object) -> dict:
    """The JSON object in a model reply, even inside a code fence or after prose."""

    text = content.strip() if isinstance(content, str) else ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
        decoder = json.JSONDecoder()
        start = text.find("{")
        while start != -1 and not isinstance(data, dict):
            try:
                data, _end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                pass
            start = text.find("{", start + 1)
    return data if isinstance(data, dict) else {"_raw": text}


