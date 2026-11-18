"""Regenerates the block-document test fixtures from the current backend.

The fixtures in Tests/FortressKitTests/Fixtures are real backend responses. Their
pre-block-model versions (at LEGACY_REVISION in git) are written into a database of the
last schema before the change (backend/tests/fixtures/legacy_v13.sql), the backend's
own migration brings them to the current shape, and `GET /api/block-documents/{id}` is
saved, so ids, titles and timestamps stay as they were.

Run from the repository root with the backend's virtualenv:

    backend/.venv/bin/python mac/Scripts/regenerate_block_fixtures.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "mac/Tests/FortressKitTests/Fixtures"
LEGACY = ROOT / "backend/tests/fixtures/legacy_v13.sql"
DOCUMENTS = ["document.json", "editor-rich.json"]
# The last commit with the fixtures in the legacy (BlockNote / Markdown) shape.
LEGACY_REVISION = "c6c170f"


def legacy_rows(document: dict) -> list[tuple]:
    rows = []

    def add(nodes: list[dict], parent: str | None) -> None:
        for position, node in enumerate(nodes):
            rows.append((
                node["id"], document["id"], parent, position, node["type"],
                json.dumps(node.get("attrs") or {}), json.dumps(node.get("content") or {}),
                node.get("text") or "", node.get("created_at") or document["created_at"],
                node.get("updated_at") or document["updated_at"],
                json.dumps(node["provenance"]) if node.get("provenance") and not node["provenance"].get("inherited") else None,
                node.get("revision"),
            ))
            add(node.get("children") or [], node["id"])

    add(document["children"], None)
    return rows


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        notes = Path(directory)
        database = notes / ".fortress.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(LEGACY.read_text(encoding="utf-8"))
        connection.execute("PRAGMA foreign_keys = ON")
        for table in ("block_refs", "document_refs", "block_assets", "block_user_attrs", "block_attrs", "blocks", "documents"):
            connection.execute(f"DELETE FROM {table}")
        documents = {}
        for name in DOCUMENTS:
            path = (FIXTURES / name).relative_to(ROOT)
            document = json.loads(subprocess.check_output(["git", "-C", str(ROOT), "show", f"{LEGACY_REVISION}:{path}"]))
            documents[name] = document
            connection.execute(
                "INSERT INTO documents (id, title, status, tags_json, folder_id, position, deleted_at, created_at, updated_at,"
                " revision, title_auto, provenance_json, source_kind, source_external_id, sync_json, last_opened_at, change_seq)"
                " VALUES (?, ?, ?, '[]', NULL, 0, NULL, ?, ?, ?, ?, ?, 'manual', NULL, '{}', NULL, 0)",
                (document["id"], document["title"], document["status"], document["created_at"], document["updated_at"],
                 document["revision"], int(bool(document.get("title_auto"))),
                 json.dumps(document.get("provenance") or {"source": "manual"})),
            )
            connection.executemany("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", legacy_rows(document))
            # The notes it links to (`[[id|Title]]`), so `resolved_links` names them.
            for target, title in re.findall(r"\[\[([0-9A-Z]{26})\|([^\]]+)\]\]", json.dumps(document, ensure_ascii=False)):
                connection.execute(
                    "INSERT OR IGNORE INTO documents (id, title, status, created_at, updated_at) VALUES (?, ?, 'rough', ?, ?)",
                    (target, title, document["created_at"], document["created_at"]))
        connection.commit()
        connection.close()

        os.environ["NOTES_DIR"] = str(notes)
        os.environ["EMBEDDINGS_ENABLED"] = "false"
        os.environ["VISION_ENABLED"] = "false"
        sys.path.insert(0, str(ROOT / "backend"))
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            for name, document in documents.items():
                response = client.get(f"/api/block-documents/{document['id']}")
                response.raise_for_status()
                (FIXTURES / name).write_text(json.dumps(response.json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                print("wrote", name)


if __name__ == "__main__":
    main()
