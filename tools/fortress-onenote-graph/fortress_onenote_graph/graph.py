"""Read-only Microsoft Graph client for OneNote: retries, throttling, pagination.

Every request goes through :class:`~fortress_onenote_graph.throttle.Throttle`
(one at a time). The access token is only ever sent to ``graph.microsoft.com``
over HTTPS: resource URLs come from page HTML and are checked first.
The HTTP layer is a plain function so tests swap in a fake Graph.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .throttle import Throttle

GRAPH_HOST = "graph.microsoft.com"
GRAPH_ROOT = f"https://{GRAPH_HOST}/v1.0"
ONENOTE_ROOT = f"{GRAPH_ROOT}/me/onenote"
PAGE_SIZE = 100  # Graph's maximum $top for pages
MAX_PAGE_BATCHES = 200  # 20,000 pages in one section: beyond that something is looping
MAX_429_RETRIES = 12
MAX_ERROR_RETRIES = 5
HIERARCHY_EXPAND = ("sections,sectionGroups($expand=sections,sectionGroups($levels=max;"
                    "$expand=sections))")
PAGE_SELECT = "id,title,createdDateTime,lastModifiedDateTime,level,order,links"


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


Transport = Callable[[str, dict[str, str]], Response]
TokenProvider = Callable[[bool], str]  # force_refresh -> access token


class GraphError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, url: str = ""):
        super().__init__(f"HTTP {status} {code}: {message}".strip())
        self.status = status
        self.code = code
        self.url = url


def urllib_transport(url: str, headers: dict[str, str], timeout: float = 120.0) -> Response:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only
            return Response(response.status, {k.lower(): v for k, v in response.headers.items()},
                            response.read())
    except urllib.error.HTTPError as error:
        body = error.read() if error.fp is not None else b""
        return Response(error.code, {k.lower(): v for k, v in (error.headers or {}).items()}, body)


def is_graph_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == GRAPH_HOST and not parsed.username


def _error_of(response: Response) -> tuple[str, str]:
    try:
        error = response.json().get("error") or {}
        return str(error.get("code") or ""), str(error.get("message") or "")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return "", response.body[:200].decode("utf-8", "replace")


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class GraphClient:
    def __init__(self, token: TokenProvider, throttle: Throttle, transport: Transport = urllib_transport,
                 log: Callable[[str], None] | None = None):
        self.token = token
        self.throttle = throttle
        self.transport = transport
        self.log = log or (lambda message: None)
        self.requests = 0

    def get(self, url: str, accept: str = "application/json") -> Response:
        if url.startswith("/"):
            url = ONENOTE_ROOT + url
        if not is_graph_url(url):
            raise GraphError(0, "refused", f"not a Microsoft Graph URL: {url[:200]}", url)
        refreshed = False
        throttled = errors = 0
        while True:
            self.throttle.acquire()
            self.requests += 1
            headers = {"Authorization": f"Bearer {self.token(refreshed)}", "Accept": accept,
                       "User-Agent": "fortress-onenote-graph"}
            try:
                response = self.transport(url, headers)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                errors += 1
                if errors > MAX_ERROR_RETRIES:
                    raise GraphError(0, "network", str(exc), url) from exc
                self.log(f"network error ({exc}); retrying")
                self.throttle.clock.sleep(min(60.0, 2.0 ** errors))
                continue
            if 200 <= response.status < 300:
                self.throttle.success(_float(response.headers.get("x-ms-throttle-limit-percentage")))
                return response
            if response.status == 429:
                throttled += 1
                if throttled > MAX_429_RETRIES:
                    raise GraphError(429, "throttled", "Microsoft kept refusing requests (429); "
                                     "run the same command again later to resume", url)
                pause = self.throttle.throttled(_float(response.headers.get("retry-after")))
                self.log(f"throttled by Microsoft (429); pausing {pause:.0f} s")
                continue
            if response.status == 401 and not refreshed:
                refreshed = True
                continue
            if response.status in (500, 502, 503, 504):
                errors += 1
                if errors > MAX_ERROR_RETRIES:
                    raise GraphError(response.status, *_error_of(response), url)
                retry_after = _float(response.headers.get("retry-after"))
                if retry_after:
                    self.throttle.throttled(retry_after)
                else:
                    self.throttle.clock.sleep(min(60.0, 2.0 ** errors))
                self.log(f"HTTP {response.status}; retrying")
                continue
            raise GraphError(response.status, *_error_of(response), url)

    def json(self, url: str) -> Any:
        return self.get(url).json()

    # --- OneNote ---------------------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self.json(f"{GRAPH_ROOT}/me?$select=id,displayName,userPrincipalName")

    def notebooks(self) -> list[dict[str, Any]]:
        """Every notebook with its section groups and sections (one call; walks the
        tree with separate calls if Graph refuses the nested ``$expand``)."""

        try:
            return list(self._collection(f"/notebooks?$expand={urllib.parse.quote(HIERARCHY_EXPAND, safe='(),;=$')}"))
        except GraphError as error:
            if error.status not in (400, 501):
                raise
            self.log("nested hierarchy request refused; listing notebooks one level at a time")
        notebooks = list(self._collection("/notebooks"))
        for notebook in notebooks:
            notebook["sections"] = list(self._collection(f"/notebooks/{_q(notebook['id'])}/sections"))
            notebook["sectionGroups"] = self._groups(f"/notebooks/{_q(notebook['id'])}/sectionGroups", 0)
        return notebooks

    def _groups(self, url: str, depth: int) -> list[dict[str, Any]]:
        groups = list(self._collection(url))
        for group in groups:
            group["sections"] = list(self._collection(f"/sectionGroups/{_q(group['id'])}/sections"))
            group["sectionGroups"] = ([] if depth > 20 else
                                      self._groups(f"/sectionGroups/{_q(group['id'])}/sectionGroups", depth + 1))
        return groups

    def _collection(self, url: str) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        for _ in range(MAX_PAGE_BATCHES):
            data = self.json(url)
            for item in data.get("value") or []:
                if isinstance(item, dict) and item.get("id") not in seen:
                    seen.add(item.get("id"))
                    yield item
            url = data.get("@odata.nextLink")
            if not url:
                return

    def section_pages(self, section_id: str) -> list[dict[str, Any]]:
        """A section's pages in section order, with their subpage ``level``.

        ``$top`` turns off ``@odata.nextLink``, so this pages with ``$skip`` and
        stops on a short batch -- or when Graph ignores ``$skip`` and repeats a
        batch it already sent (seen in the wild), so it never loops forever.
        """

        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        for batch_number in range(MAX_PAGE_BATCHES):
            url = (f"/sections/{_q(section_id)}/pages?pagelevel=true&$top={PAGE_SIZE}"
                   f"&$skip={batch_number * PAGE_SIZE}&$select={PAGE_SELECT}")
            batch = [item for item in self.json(url).get("value") or [] if isinstance(item, dict)]
            fresh = [item for item in batch if item.get("id") not in seen]
            if batch and not fresh:
                self.log(f"Graph repeated a batch of pages for section {section_id}; stopping there")
                break
            for item in fresh:
                seen.add(item.get("id"))
                pages.append(item)
            if len(batch) < PAGE_SIZE:
                break
        else:
            self.log(f"section {section_id}: stopped after {MAX_PAGE_BATCHES * PAGE_SIZE} pages")
        if all(isinstance(page.get("order"), int) for page in pages):
            pages.sort(key=lambda page: page["order"])
        return pages

    def page_content(self, page_id: str) -> bytes:
        return self.get(f"/pages/{_q(page_id)}/content?includeIDs=true", accept="text/html").body

    def resource(self, url: str) -> Response:
        return self.get(url, accept="*/*")


def _q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="!-_.")
