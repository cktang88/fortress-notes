"""Seeds a running Fortress backend with folders and notes for the integration tests.

Usage: python3 seed_backend.py http://127.0.0.1:8000 300
Standard library only, so it runs before (or without) the backend's virtualenv.
"""

import json
import os
import sys
import urllib.request


def post(base: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        # The backend's token (backend/app/auth.py), when it was started with one.
        headers={"Content-Type": "application/json", "X-Fortress-Token": os.environ.get("FORTRESS_TOKEN", "")},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read() or b"{}")


def main() -> None:
    base = sys.argv[1].rstrip("/")
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    folders = [post(base, "/api/folders", {"name": f"Folder {i}"})["id"] for i in range(8)]
    for i in range(count):
        paragraphs = [
            f"Paragraph {p} of note {i}: the quick brown fox jumps over the lazy dog, "
            f"and keeps a list of things to remember about topic {i % 17}."
            for p in range(30 + i % 40)
        ]
        body = "\n\n".join([f"- item {k} for note {i}" for k in range(5)] + paragraphs)
        post(
            base,
            "/api/block-documents",
            {
                "title": f"Seeded note {i:04d}",
                "body": body,
                "folder_id": folders[i % len(folders)] if i % 5 else None,
            },
        )
    print(f"seeded {count} notes in {len(folders)} folders")


if __name__ == "__main__":
    main()
