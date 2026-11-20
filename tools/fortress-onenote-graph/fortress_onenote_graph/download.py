"""Download (or refresh) notebooks into the cache: hierarchy, page lists, page HTML,
resources. Re-runs fetch only pages whose ``lastModifiedDateTime`` changed and
resources not cached yet; an interrupted run resumes where it stopped."""

from __future__ import annotations

from typing import Any, Iterator

from .graph import GraphClient, GraphError, is_graph_url
from .htmlconv import resource_urls
from .progress import Progress
from .store import Store, resource_id

# Errors that stop the run (it can be resumed) instead of skipping one item.
FATAL_CODES = frozenset({"throttled", "network"})


def sections_of(container: dict[str, Any], path: list[str]) -> Iterator[tuple[list[str], dict[str, Any]]]:
    for section in container.get("sections") or []:
        if isinstance(section, dict) and section.get("id"):
            yield [*path, str(section.get("displayName") or "Section")], section
    for group in container.get("sectionGroups") or []:
        if isinstance(group, dict):
            yield from sections_of(group, [*path, str(group.get("displayName") or "Section group")])


def select(notebooks: list[dict[str, Any]], names: list[str] | None) -> list[dict[str, Any]]:
    if not names:
        return notebooks
    wanted = {name.casefold() for name in names}
    return [nb for nb in notebooks if str(nb.get("displayName", "")).casefold() in wanted]


def _fatal(error: GraphError) -> bool:
    return error.code in FATAL_CODES or error.status in (401, 429) or error.status >= 500


class Downloader:
    def __init__(self, client: GraphClient, store: Store, progress: Progress):
        self.client = client
        self.store = store
        self.progress = progress
        self.stats = {"sections": 0, "sections_failed": 0, "pages": 0, "pages_fetched": 0,
                      "pages_unchanged": 0, "pages_failed": 0, "resources_fetched": 0, "resources_missing": 0}

    def run(self, notebook_names: list[str] | None = None) -> dict[str, Any]:
        self.progress.phase("hierarchy", "Listing notebooks…")
        notebooks = self.client.notebooks()
        self.store.save_hierarchy(notebooks)
        selected = select(notebooks, notebook_names)
        missing = {name.casefold() for name in notebook_names or []} - {
            str(nb.get("displayName", "")).casefold() for nb in selected}
        if missing:
            available = ", ".join(sorted(str(nb.get("displayName")) for nb in notebooks)) or "none"
            raise SystemExit(f"No notebook named {', '.join(sorted(missing))}. Notebooks: {available}")
        sections = [(path, section) for nb in selected
                    for path, section in sections_of(nb, [str(nb.get("displayName") or "Notebook")])]

        self.progress.phase("sections", f"Listing pages of {len(sections)} section(s)…")
        todo: list[tuple[list[str], dict[str, Any]]] = []
        for done, (path, section) in enumerate(sections):
            self.progress.step("sections", done, len(sections), " › ".join(path))
            self.stats["sections"] += 1
            try:
                pages = self.client.section_pages(section["id"])
            except GraphError as error:
                if _fatal(error):
                    raise
                self.stats["sections_failed"] += 1
                self.store.save_section_error(section["id"], {"status": error.status, "code": error.code,
                                                              "message": str(error)})
                self.progress.warning(f"{' › '.join(path)}: pages could not be listed ({error})")
                continue
            self.store.save_section_pages(section["id"], pages)
            for page in pages:
                self.stats["pages"] += 1
                if self.store.page_is_current(page):
                    self.stats["pages_unchanged"] += 1
                else:
                    todo.append(([*path, str(page.get("title") or "Untitled Page")], page))
        self.progress.step("sections", len(sections), len(sections))

        self.progress.phase("pages", f"Downloading {len(todo)} new or changed page(s) "
                                     f"({self.stats['pages_unchanged']} unchanged)…")
        for done, (path, page) in enumerate(todo):
            self.progress.step("pages", done, len(todo), " › ".join(path))
            self.fetch_page(page, path)
        self.progress.step("pages", len(todo), len(todo))
        self.progress.event({"event": "requests", "sent": self.client.requests})
        return dict(self.stats, requests=self.client.requests)

    def fetch_page(self, page: dict[str, Any], path: list[str]) -> None:
        page_id = page["id"]
        meta = self.store.page_meta(page_id) or {}
        html = self.store.page_html(page_id)
        if html is None or meta.get("lastModifiedDateTime") != page.get("lastModifiedDateTime"):
            try:
                data = self.client.page_content(page_id)
            except GraphError as error:
                if _fatal(error):
                    raise
                self.stats["pages_failed"] += 1
                self.progress.warning(f"{' › '.join(path)}: page could not be downloaded ({error})")
                return
            self.store.save_page_html(page_id, data)
            meta = {"id": page_id, "lastModifiedDateTime": page.get("lastModifiedDateTime"), "complete": False}
            self.store.save_page_meta(page_id, meta)
            html = data.decode("utf-8", errors="replace")
        missing: list[str] = []
        resources: list[str] = []
        for url, _kind in resource_urls(html):
            rid = resource_id(url)
            if rid is None or not is_graph_url(url):
                missing.append(url[:300])
                continue
            resources.append(rid)
            if self.store.has_resource(rid):
                continue
            try:
                response = self.client.resource(url)
            except GraphError as error:
                if _fatal(error):
                    raise
                missing.append(rid)
                self.stats["resources_missing"] += 1
                self.progress.warning(f"{' › '.join(path)}: a picture or attachment could not be downloaded ({error})")
                continue
            self.store.save_resource(rid, response.body, response.headers.get("content-type"))
            self.stats["resources_fetched"] += 1
        meta.update(complete=True, resources=resources, missing=missing)
        self.store.save_page_meta(page_id, meta)
        self.stats["pages_fetched"] += 1
