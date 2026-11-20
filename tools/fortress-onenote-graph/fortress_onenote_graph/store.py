"""The persistent download cache (one folder per signed-in account).

```
<cache>/accounts/<account key>/
  account.json                 who this is (display name, user principal name)
  hierarchy.json               notebooks, section groups, sections (last listing)
  sections/<key>.json          a section's pages, in order, with level (last listing)
  sections/<key>.error.json    why the last listing of this section failed
  pages/<key>/content.html     the page's output HTML (includeIDs=true)
  pages/<key>/meta.json        {id, lastModifiedDateTime, complete, resources, missing}
  resources/<key>.bin|.json    a resource's bytes and {id, content_type, size, sha256}
  throttle.json                requests sent in the last hour (shared by runs)
```

Keys are hashes of Graph ids (never ids as paths). Every file is written to a
temporary name and renamed, and a page's ``meta.json`` is written last, so an
interrupted run leaves either a complete page or one that is fetched again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_RESOURCE_ID_RE = re.compile(r"/resources/([^/?#]+)/(?:\$value|content)", re.IGNORECASE)


def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "fortress-onenote-graph"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "fortress-onenote-graph"


def default_config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "fortress-onenote-graph"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fortress-onenote-graph"


def key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def resource_id(url: str) -> str | None:
    match = _RESOURCE_ID_RE.search(url)
    return match.group(1) if match else None


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    write_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass

    @classmethod
    def for_account(cls, cache_dir: Path, account_id: str) -> "Store":
        return cls(cache_dir / "accounts" / key(account_id))

    # hierarchy / sections

    def save_hierarchy(self, notebooks: list[dict[str, Any]]) -> None:
        write_json(self.root / "hierarchy.json", notebooks)

    def hierarchy(self) -> list[dict[str, Any]]:
        return read_json(self.root / "hierarchy.json", []) or []

    def save_section_pages(self, section_id: str, pages: list[dict[str, Any]]) -> None:
        write_json(self.root / "sections" / f"{key(section_id)}.json", {"id": section_id, "pages": pages})
        (self.root / "sections" / f"{key(section_id)}.error.json").unlink(missing_ok=True)

    def section_pages(self, section_id: str) -> list[dict[str, Any]] | None:
        data = read_json(self.root / "sections" / f"{key(section_id)}.json")
        return data.get("pages") if isinstance(data, dict) else None

    def save_section_error(self, section_id: str, error: dict[str, Any]) -> None:
        write_json(self.root / "sections" / f"{key(section_id)}.error.json", error)

    def section_error(self, section_id: str) -> dict[str, Any] | None:
        return read_json(self.root / "sections" / f"{key(section_id)}.error.json")

    # pages

    def page_dir(self, page_id: str) -> Path:
        return self.root / "pages" / key(page_id)

    def page_meta(self, page_id: str) -> dict[str, Any] | None:
        return read_json(self.page_dir(page_id) / "meta.json")

    def save_page_meta(self, page_id: str, meta: dict[str, Any]) -> None:
        write_json(self.page_dir(page_id) / "meta.json", meta)

    def page_html(self, page_id: str) -> str | None:
        try:
            return (self.page_dir(page_id) / "content.html").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def save_page_html(self, page_id: str, html: bytes) -> None:
        write_bytes(self.page_dir(page_id) / "content.html", html)

    def page_is_current(self, page: dict[str, Any]) -> bool:
        meta = self.page_meta(page["id"])
        return bool(meta and meta.get("complete") and meta.get("lastModifiedDateTime") == page.get("lastModifiedDateTime")
                    and (self.page_dir(page["id"]) / "content.html").is_file())

    # resources

    def has_resource(self, rid: str) -> bool:
        return (self.root / "resources" / f"{key(rid)}.json").is_file()

    def save_resource(self, rid: str, data: bytes, content_type: str | None) -> None:
        base = self.root / "resources" / key(rid)
        write_bytes(base.with_suffix(".bin"), data)
        write_json(base.with_suffix(".json"), {"id": rid, "content_type": content_type, "size": len(data),
                                               "sha256": hashlib.sha256(data).hexdigest()})

    def resource(self, rid: str) -> tuple[dict[str, Any], Path] | None:
        base = self.root / "resources" / key(rid)
        info = read_json(base.with_suffix(".json"))
        if not isinstance(info, dict) or not base.with_suffix(".bin").is_file():
            return None
        return info, base.with_suffix(".bin")

    @property
    def throttle_path(self) -> Path:
        return self.root / "throttle.json"
