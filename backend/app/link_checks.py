"""Monthly broken-link checks for links in edited block documents."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from markdown_it import MarkdownIt

from . import block_store

_URL_RE = re.compile(r"https?://[^\s\)\]\>\"'`]+", re.IGNORECASE)
_CHECK_LOCK = asyncio.Lock()
_CHECK_INTERVAL = timedelta(days=30)
_MARKDOWN = MarkdownIt("commonmark")


def extract_urls(text: str) -> list[str]:
    """Extract distinct HTTP(S) URLs and trim punctuation around prose links."""

    urls: list[str] = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(".,;:")
        if url and url not in urls:
            urls.append(url)
    return urls


def _markdown_urls(text: str) -> set[str]:
    """Keep Markdown link destinations whole; scan only text outside links as prose."""

    urls: set[str] = set()
    for token in _MARKDOWN.parse(text):
        if token.type != "inline" or not token.children:
            continue
        inside_link = False
        for child in token.children:
            if child.type == "link_open":
                inside_link = True
                href = child.attrGet("href")
                if isinstance(href, str):
                    urls.update(_href_urls({"href": href}))
            elif child.type == "link_close":
                inside_link = False
            elif child.type == "text" and not inside_link:
                urls.update(extract_urls(child.content))
    return urls


async def _request_status(client: httpx.AsyncClient, url: str) -> str:
    try:
        async with client.stream(
            "GET", url, follow_redirects=True, timeout=10
        ) as response:
            return "ok" if response.status_code < 400 else str(response.status_code)
    except httpx.HTTPError as error:
        return type(error).__name__


async def _check_urls(
    urls: list[str], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, str]:
    """Check a batch of URLs and return each URL's valid or failure status."""

    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        return {}
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (FortressNotes link checker)"},
        transport=transport,
    ) as client:
        statuses = await asyncio.gather(
            *(_request_status(client, url) for url in unique_urls)
        )
    return dict(zip(unique_urls, statuses, strict=True))


async def check_document_links(
    database_path: Path,
    document_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Return known link statuses and check old blocks whose valid cache expired."""

    async with _CHECK_LOCK:
        tree = block_store.document_tree(database_path, document_id)
        if tree is None:
            raise KeyError("document not found")

        now = datetime.now(timezone.utc)
        cutoff = now - _CHECK_INTERVAL
        links: dict[str, dict[str, object]] = {}
        _collect_links(tree.get("children", []), cutoff, links)
        if not links:
            return {"links": []}

        cached = block_store.get_link_check_cache(database_path, list(links))
        due: list[str] = []
        for url, link in links.items():
            entry = cached.get(url)
            if link["has_old_block"] and not _has_recent_success(entry, cutoff):
                due.append(url)

        if due:
            statuses = await _check_urls(due, transport=transport)
            checked_at = now.isoformat()
            for url, status in statuses.items():
                block_store.store_link_check(database_path, url, status, checked_at)
                cached[url] = {"status": status, "checked_at": checked_at}

        results: list[dict[str, object]] = []
        for url, link in links.items():
            entry = cached.get(url)
            if entry is None:
                continue
            results.append(
                {
                    "url": url,
                    "status": entry["status"],
                    "checked_at": entry["checked_at"],
                    "block_ids": link["block_ids"],
                }
            )
        return {"links": results}


def _collect_links(
    nodes: list[dict], cutoff: datetime, links: dict[str, dict[str, object]]
) -> None:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        href_urls = _href_urls(node.get("content"))
        text_urls = _markdown_urls(str(node.get("text", "")))
        block_urls = text_urls.union(href_urls)
        old_block = _is_old(node.get("updated_at"), cutoff)
        for url in block_urls:
            link = links.setdefault(url, {"block_ids": [], "has_old_block": False})
            block_ids = link["block_ids"]
            if node.get("id") not in block_ids:
                block_ids.append(node["id"])
            if old_block:
                link["has_old_block"] = True
        _collect_links(node.get("children", []), cutoff, links)


def _href_urls(value: object) -> set[str]:
    urls: set[str] = set()
    if isinstance(value, dict):
        href = value.get("href")
        if isinstance(href, str):
            href = href.strip()
            parsed = urlsplit(href)
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
                urls.add(href)
        for nested in value.values():
            urls.update(_href_urls(nested))
    elif isinstance(value, list):
        for nested in value:
            urls.update(_href_urls(nested))
    return urls


def _is_old(updated_at: object, cutoff: datetime) -> bool:
    if not isinstance(updated_at, str):
        return False
    try:
        value = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= cutoff


def _has_recent_success(entry: dict[str, str] | None, cutoff: datetime) -> bool:
    if entry is None or entry["status"] != "ok":
        return False
    try:
        checked_at = datetime.fromisoformat(entry["checked_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return checked_at >= cutoff
