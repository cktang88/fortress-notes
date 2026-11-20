"""Test doubles: a fake clock and an in-memory Microsoft Graph (no network)."""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from fortress_onenote_graph.graph import Response
from fortress_onenote_graph.throttle import Clock

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00fake png body"
PDF = b"%PDF-1.4 fake pdf"
G = "https://graph.microsoft.com/v1.0/users('me')/onenote/resources"


class FakeClock(Clock):
    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start
        self.sleeps: list[float] = []
        super().__init__(now=lambda: self.t, sleep=self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)


def client_url(section_guid: str, page_guid: str, title: str) -> str:
    return (f"onenote:https://d.docs.live.net/abc123/Documents/Work/Section.one#{urllib.parse.quote(title)}"
            f"&section-id={{{section_guid}}}&page-id={{{page_guid}}}&end")


class FakeGraph:
    """Answers the requests the tool makes, from dictionaries.

    ``notebooks``: Graph's hierarchy; ``pages``: section id -> page dicts;
    ``content``: page id -> HTML; ``resources``: resource id -> (bytes, type).
    ``fail``: list of (url substring, status, times) to inject errors.
    ``ignore_skip``: answer every page listing as if $skip were 0 (seen in the wild).
    """

    def __init__(self):
        self.notebooks: list[dict] = []
        self.pages: dict[str, list[dict]] = {}
        self.content: dict[str, str] = {}
        self.resources: dict[str, tuple[bytes, str]] = {}
        self.fail: list[list] = []
        self.ignore_skip = False
        self.log: list[str] = []
        self.tokens: list[str] = []

    def __call__(self, url: str, headers: dict[str, str]) -> Response:
        self.log.append(url)
        self.tokens.append(headers.get("Authorization", ""))
        assert url.startswith("https://graph.microsoft.com/"), url
        for rule in self.fail:
            if rule[0] in url and rule[2] > 0:
                rule[2] -= 1
                if rule[1] == "raise":
                    raise ConnectionError("connection reset")
                return Response(rule[1], {}, json.dumps({"error": {"code": str(rule[1]), "message": "injected"}}).encode())
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        if path.endswith("/me"):
            return self._json({"id": "user-1", "displayName": "Test User", "userPrincipalName": "test@example.com"})
        if path.endswith("/onenote/notebooks"):
            return self._json({"value": json.loads(json.dumps(self.notebooks))})
        match = re.search(r"/sections/([^/]+)/pages$", path)
        if match:
            pages = self.pages.get(match.group(1))
            if pages is None:
                return Response(403, {}, json.dumps({"error": {"code": "10002", "message": "denied"}}).encode())
            skip = 0 if self.ignore_skip else int(query.get("$skip", ["0"])[0])
            top = int(query.get("$top", ["20"])[0])
            return self._json({"value": pages[skip:skip + top]})
        match = re.search(r"/pages/([^/]+)/content$", path)
        if match:
            page_id = match.group(1)
            if page_id not in self.content:
                return Response(404, {}, b'{"error":{"code":"20102","message":"gone"}}')
            assert query.get("includeIDs") == ["true"], url
            assert "includeInkML" not in query
            return Response(200, {"content-type": "text/html"}, self.content[page_id].encode("utf-8"))
        match = re.search(r"/resources/([^/]+)/\$value$", path)
        if match:
            if match.group(1) not in self.resources:
                return Response(404, {}, b'{"error":{"code":"20102","message":"gone"}}')
            data, kind = self.resources[match.group(1)]
            return Response(200, {"content-type": kind}, data)
        return Response(400, {}, b'{"error":{"code":"badRequest","message":"unexpected"}}')

    @staticmethod
    def _json(value) -> Response:
        return Response(200, {"content-type": "application/json"}, json.dumps(value).encode())

    def count(self, fragment: str) -> int:
        return sum(1 for url in self.log if fragment in url)


SECTION_A_GUID = "8A9B0C1D-2E3F-4051-6273-8495A6B7C8D9"
PLAN_GUID = "11111111-1111-1111-1111-111111111111"
CHILD_GUID = "22222222-2222-2222-2222-222222222222"
STANDUP_GUID = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"


def sample_graph() -> FakeGraph:
    """Notebook "Work": sections Alpha (Plan + subpage Child) and, in group
    Projects, Meetings (Standup, with the printout/table page fixture), plus a
    section Graph refuses (password protected)."""

    graph = FakeGraph()
    graph.notebooks = [{
        "id": "0-NB1!1-ABC", "displayName": "Work",
        "sections": [{"id": "0-SA!1-ABC", "displayName": "Alpha"},
                     {"id": "0-SLOCK!1-ABC", "displayName": "Locked"}],
        "sectionGroups": [{"id": "0-G1!1-ABC", "displayName": "Projects", "sectionGroups": [],
                           "sections": [{"id": "0-SM!1-ABC", "displayName": "Meetings"}]}],
    }]
    graph.pages["0-SA!1-ABC"] = [
        {"id": "0-P1!1-ABC", "title": "Project plan", "createdDateTime": "2025-03-04T09:15:00Z",
         "lastModifiedDateTime": "2025-03-05T10:00:00Z", "level": 0, "order": 0,
         "links": {"oneNoteClientUrl": {"href": client_url(SECTION_A_GUID, PLAN_GUID, "Project plan")}}},
        {"id": "0-P2!1-ABC", "title": "Child", "createdDateTime": "2025-03-04T09:16:00Z",
         "lastModifiedDateTime": "2025-03-05T10:01:00Z", "level": 1, "order": 1,
         "links": {"oneNoteClientUrl": {"href": client_url(SECTION_A_GUID, CHILD_GUID, "Child")}}},
    ]
    graph.pages["0-SM!1-ABC"] = [
        {"id": "0-P3!1-ABC", "title": "Standup", "createdDateTime": "2025-03-01T08:00:00.1234567Z",
         "lastModifiedDateTime": "2025-03-02T08:00:00Z", "level": 0, "order": 0,
         "links": {"oneNoteClientUrl": {"href": client_url(SECTION_A_GUID, STANDUP_GUID, "Standup")}}},
    ]
    graph.content["0-P1!1-ABC"] = (FIXTURES / "page_layout.html").read_text(encoding="utf-8")
    graph.content["0-P2!1-ABC"] = ('<html><head><title>Child</title></head><body data-absolute-enabled="true">'
                                   '<div style="position:absolute;left:48px;top:120px;width:624px">'
                                   f'<p>Back to <a href="{client_url(SECTION_A_GUID, PLAN_GUID, "Project plan")}">'
                                   'the plan</a></p></div></body></html>')
    graph.content["0-P3!1-ABC"] = (FIXTURES / "page_objects.html").read_text(encoding="utf-8")
    graph.resources = {
        "0-aaa222!1-ABC!100": (PNG, "image/png"),
        "0-bbb111!1-ABC!200": (PDF, "application/pdf"),
        "0-bbb223!1-ABC!200": (PNG + b"page1", "image/png"),
        "0-bbb333!1-ABC!200": (b"not an image", "image/png"),
    }
    return graph
