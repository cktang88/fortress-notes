"""HTTP benchmark of the endpoints the native app calls constantly.

Usage (from ``backend/``)::

    uv run --frozen python scripts/bench_api.py --dir /tmp/bench-api
    uv run --frozen python scripts/bench_api.py --dir /tmp/bench-api --kind onenote --out results.json

The first run builds the workspace in ``--dir/<kind>`` (``plain``: 2,000 notes of
paragraphs from ``bench_workspace``; ``onenote``: OneNote-shaped pages with
headings, tables with nested cell blocks, columns, images, note and block links
and provenance on every block, plus one 2,000-block page). Every run copies it,
starts a real ``uvicorn`` on the copy (no embeddings or vision models, so no
model downloads and no AI calls; the server never reads ``backend/.env``), and
reports per endpoint: p50/p95 latency, the identity payload size, the gzip wire
size, and the ``If-None-Match`` revalidation status and latency. It also reports
time to the first ``/api/health`` and the first navigation, and reader latency
while a writer applies transactions.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# BENCH_APP_DIR: benchmark another copy of the backend (e.g. a baseline checkout).
BACKEND = Path(os.environ.get("BENCH_APP_DIR") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

# A 1x1 PNG; images are content-addressed by their SHA-256 like OneNote imports.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


# --- Workspaces -----------------------------------------------------------------


def build_plain(directory: Path, notes: int) -> None:
    import bench_workspace

    bench_workspace.build(directory, notes)


def build_onenote(directory: Path, pages: int) -> None:
    """OneNote-shaped pages: tables, columns, images, links, provenance on every block."""

    import hashlib

    os.environ["NOTES_DIR"] = str(directory)
    from app import block_store, sources
    from app import config
    from app.models import BlockOperation

    config.activate(None)  # read NOTES_DIR again
    db = directory / ".fortress.sqlite3"
    assets = directory / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    rng = random.Random(11)
    words = (
        "budget roadmap kyoto lisbon sourdough marathon invoice design review sprint "
        "museum ticket garden tomato physio knee habit compounding latency index cache "
        "migration contract renewal passport lease dentist insurance itinerary"
    ).split()

    def sentence() -> str:
        return " ".join(rng.choice(words) for _ in range(rng.randint(8, 20))).capitalize() + "."

    # Big attachments for Range/caching checks, content-addressed like imports.
    media: list[str] = []
    for index in range(40):
        data = _PNG + index.to_bytes(4, "big")
        digest = hashlib.sha256(data).hexdigest()
        path = assets / f"{digest}.png"
        path.write_bytes(data)
        block_store.register_asset(db, path, digest, "image/png")
        media.append(f"/media/{path.name}")
    audio = os.urandom(4 * 1024 * 1024)
    digest = hashlib.sha256(audio).hexdigest()
    (assets / f"{digest}.m4a").write_bytes(audio)
    block_store.register_asset(db, assets / f"{digest}.m4a", digest, "audio/mp4")

    def runs(text: str) -> list[dict]:
        return [{"type": "text", "text": text}]

    def table(page: int, number: int) -> sources.SourceBlock:
        rows = []
        for r in range(5):
            cells = []
            for c in range(4):
                blocks = [sources.SourceBlock(type="paragraph", content=runs(f"r{r}c{c} {rng.choice(words)}"))]
                if r == 2 and c == 1:
                    blocks += [sources.SourceBlock(type="paragraph", content=runs(sentence())),
                               sources.SourceBlock(type="bullet_item", content=runs(sentence()))]
                cells.append(sources.SourceBlock(type="table_cell", children=blocks))
            rows.append(sources.SourceBlock(type="table_row", children=cells))
        return sources.SourceBlock(
            type="table", external_id=f"{{P{page}}}/table{number}", attrs={"header_rows": 1},
            children=rows, provenance={"extra": {"onenote": {"object_id": f"T{page}-{number}"}}},
        )

    def paragraph(page: int, key: str, text: str | None = None) -> sources.SourceBlock:
        return sources.SourceBlock(
            type="paragraph", external_id=f"{{P{page}}}/{key}", content=runs(text or sentence()),
            provenance={"extra": {"onenote": {"object_id": f"{page}-{key}", "element_id": key}}},
        )

    def page_blocks(page: int, size: int) -> list[sources.SourceBlock]:
        blocks: list[sources.SourceBlock] = [
            sources.SourceBlock(markdown=f"## Section {page}", external_id=f"{{P{page}}}/h"),
        ]
        index = 0
        while len(blocks) < size:
            index += 1
            kind = index % 10
            if kind == 3:
                blocks.append(table(page, index))
            elif kind == 6:
                blocks.append(sources.SourceBlock(
                    type="columns", external_id=f"{{P{page}}}/cols{index}",
                    attrs={"onenote_y": index},
                    children=[
                        sources.SourceBlock(
                            type="column", external_id=f"{{P{page}}}/col{index}-{side}",
                            attrs={"width": 0.5},
                            children=[paragraph(page, f"c{index}-{side}-{n}") for n in range(3)],
                        )
                        for side in range(2)
                    ],
                ))
            elif kind == 8:
                url = rng.choice(media)
                blocks.append(sources.SourceBlock(
                    type="image", external_id=f"{{P{page}}}/img{index}",
                    attrs={"url": url, "alt": sentence(), "width": 480, "height": 320},
                ))
            else:
                blocks.append(paragraph(page, f"p{index}"))
        return blocks

    started = time.perf_counter()
    created: list[str] = []
    for page in range(pages):
        size = 2000 if page == 0 else rng.randint(20, 60)
        result = sources.upsert_document_from_source(
            db, source="onenote_import", external_id=f"{{P{page}}}",
            title=f"Page {page} {rng.choice(words)}", blocks=page_blocks(page, size),
            folder_path=[(f"nb{page % 4}", f"Notebook {page % 4}"),
                         (f"sec{page % 24}", f"Section {page % 24}")],
            importer={"name": "onenote", "version": "bench"},
        )
        created.append(result.document_id)
    # Links: each page links to two earlier pages and references a block of one.
    for number, document_id in enumerate(created[1:], start=1):
        tree = block_store.document_tree(db, document_id)
        targets = rng.sample(created[:number], min(2, number))
        target_tree = block_store.document_tree(db, targets[0])
        target_block = next(
            node["id"] for node in target_tree["children"] if node["type"] == "paragraph"
        )
        content = [node for target in targets
                   for node in ({"type": "docLink", "id": target}, {"type": "text", "text": " "})]
        content += [{"type": "text", "text": "see "}, {"type": "blockRef", "id": target_block}]
        block_store.apply_transaction(db, document_id, [BlockOperation(
            operation="insert", block_id=block_store.new_id(), parent_id=None,
            type="paragraph", content=content,
        )], tree["revision"])
    print(f"built onenote workspace: {pages} pages in {time.perf_counter() - started:.0f} s",
          file=sys.stderr)


def ensure_workspace(root: Path, kind: str, size: int) -> Path:
    directory = root / kind
    if not (directory / ".fortress.sqlite3").exists():
        directory.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "NOTES_DIR": str(directory), "EMBEDDINGS_ENABLED": "false",
               "VISION_ENABLED": "false", "VLM_CAPTION_ENABLED": "false"}
        # Build in a child process so this one stays free of app imports.
        subprocess.run(
            [sys.executable, __file__, "--build", kind, "--dir", str(directory),
             "--size", str(size)],
            check=True, env=env, cwd=tempfile.gettempdir(),
        )
    return directory


# --- Server ---------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, notes_dir: Path):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {
            **os.environ,
            "NOTES_DIR": str(notes_dir),
            "EMBEDDINGS_ENABLED": "false",
            "VISION_ENABLED": "false",
            "VLM_CAPTION_ENABLED": "false",
            "AUTO_BACKUP_ENABLED": "false",
            "HF_HUB_OFFLINE": "1",
            "OPENROUTER_API_KEY": "",
        }
        self.started = time.perf_counter()
        # cwd outside backend/: Settings reads ".env" from the working directory.
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND),
             "--port", str(self.port), "--log-level", "warning"],
            env=env, cwd=notes_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

    def wait(self, client) -> dict[str, float]:
        import httpx

        while True:
            try:
                if client.get(f"{self.base}/api/health").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if self.process.poll() is not None:
                raise RuntimeError(self.process.stderr.read().decode())
            time.sleep(0.01)
        health = time.perf_counter() - self.started
        client.get(f"{self.base}/api/navigation").raise_for_status()
        navigation = time.perf_counter() - self.started
        return {"health_s": round(health, 2), "first_navigation_s": round(navigation, 2)}

    def rss_mb(self) -> float:
        try:
            for line in Path(f"/proc/{self.process.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024)
        except OSError:
            pass
        return 0.0

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()


# --- Measuring ------------------------------------------------------------------


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"p50_ms": round(1000 * statistics.median(ordered), 2), "p95_ms": round(1000 * p95, 2)}


def measure(client, name: str, requests: list[tuple[str, str, object]], rounds: int) -> dict:
    """Time ``rounds`` passes over ``requests`` (method, url, json body)."""

    samples: list[float] = []
    sizes: list[int] = []
    wire: list[int] = []
    revalidate: list[float] = []
    statuses: set[int] = set()
    etags = 0
    for round_number in range(rounds):
        for method, url, body in requests:
            headers = {"Accept-Encoding": "identity"}
            started = time.perf_counter()
            response = client.request(method, url, json=body, headers=headers)
            samples.append(time.perf_counter() - started)
            if response.status_code >= 400:
                raise RuntimeError(f"{name}: {method} {url} -> {response.status_code} {response.text[:200]}")
            if round_number == 0:
                sizes.append(len(response.content))
                gz = client.request(method, url, json=body, headers={"Accept-Encoding": "gzip"})
                encoded = gz.headers.get("content-encoding") == "gzip"
                # httpx decodes gzip; Content-Length is what crossed the wire.
                wire.append(int(gz.headers.get("content-length") or 0) if encoded
                            else len(gz.content))
            etag = response.headers.get("etag")
            if method == "GET" and etag:
                etags += 1
                started = time.perf_counter()
                again = client.request(method, url, headers={"If-None-Match": etag,
                                                             "Accept-Encoding": "identity"})
                revalidate.append(time.perf_counter() - started)
                statuses.add(again.status_code)
    result = {"name": name, "n": len(samples), **_stats(samples),
              "bytes": round(statistics.median(sizes)) if sizes else 0,
              "gzip_bytes": round(statistics.median(wire)) if wire else 0}
    if revalidate:
        result["revalidate"] = {**_stats(revalidate), "status": sorted(statuses)}
    return result


def mixed(client_factory, base: str, docs: list[str], writer_doc: str, seconds: float) -> dict:
    """Four readers open notes while one writer types into another note."""

    stop = time.perf_counter() + seconds
    reads: list[float] = []
    writes: list[float] = []
    errors: list[str] = []

    def reader(seed: int) -> None:
        rng = random.Random(seed)
        with client_factory() as client:
            while time.perf_counter() < stop:
                started = time.perf_counter()
                response = client.get(f"{base}/api/block-documents/{rng.choice(docs)}")
                reads.append(time.perf_counter() - started)
                if response.status_code != 200:
                    errors.append(f"read {response.status_code}")

    def writer() -> None:
        with client_factory() as client:
            tree = client.get(f"{base}/api/block-documents/{writer_doc}").json()
            block = next(node for node in tree["children"] if node["type"] == "paragraph")
            revision = tree["revision"]
            count = 0
            while time.perf_counter() < stop:
                count += 1
                text = f"typing {count}"
                started = time.perf_counter()
                response = client.post(
                    f"{base}/api/block-documents/{writer_doc}/transactions",
                    json={"base_revision": revision, "operations": [{
                        "operation": "update", "block_id": block["id"],
                        "content": [{"type": "text", "text": text}]}]},
                )
                writes.append(time.perf_counter() - started)
                if response.status_code != 200:
                    errors.append(f"write {response.status_code} {response.text[:120]}")
                    break
                revision = response.json()["revision"]

    threads = [threading.Thread(target=reader, args=(n,)) for n in range(4)]
    threads.append(threading.Thread(target=writer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {"reads": {"n": len(reads), **_stats(reads)},
            "writes": {"n": len(writes), **_stats(writes)}, "errors": errors[:5]}


def typing(client, base: str, document_id: str, count: int, query: str = "") -> dict:
    tree = client.get(f"{base}/api/block-documents/{document_id}").json()
    block = next(node for node in tree["children"] if node["type"] == "paragraph")
    revision = tree["revision"]
    samples: list[float] = []
    size = 0
    for number in range(count):
        text = f"typed {number} " + "x" * number
        started = time.perf_counter()
        response = client.post(
            f"{base}/api/block-documents/{document_id}/transactions{query}",
            json={"base_revision": revision, "operations": [{
                "operation": "update", "block_id": block["id"],
                "content": [{"type": "text", "text": text}]}]},
            headers={"Accept-Encoding": "identity"},
        )
        samples.append(time.perf_counter() - started)
        response.raise_for_status()
        size = len(response.content)
        revision = response.json()["revision"]
    return {"n": count, **_stats(samples), "bytes": size}


def run(directory: Path, kind: str, rounds: int) -> dict:
    import httpx

    work = Path(tempfile.mkdtemp(prefix=f"bench-api-{kind}-"))
    try:
        shutil.copytree(directory, work, dirs_exist_ok=True)
        server = Server(work)
        client = httpx.Client(timeout=60)
        try:
            results: dict[str, object] = {"kind": kind, "startup": server.wait(client)}
            base = server.base
            navigation = client.get(f"{base}/api/navigation").json()
            docs: list[str] = []

            def walk(items):
                for item in items:
                    if item["kind"] == "document":
                        docs.append(item["id"])
                    else:
                        walk(item["children"])

            walk(navigation["items"])
            rng = random.Random(3)
            sample = rng.sample(docs, min(30, len(docs)))
            sizes = {}
            # The largest note (the onenote workspace has a 2,000-block page).
            for document_id in docs[:5] + sample[:5]:
                tree = client.get(f"{base}/api/block-documents/{document_id}").json()
                sizes[document_id] = len(json.dumps(tree))
            largest = max(sizes, key=sizes.get)
            backup = client.post(f"{base}/api/backups")
            media = sorted((work / "assets").glob("*.*"))
            endpoints: list[tuple[str, list[tuple[str, str, object]]]] = [
                ("navigation", [("GET", f"{base}/api/navigation", None)]),
                ("document", [("GET", f"{base}/api/block-documents/{d}", None) for d in sample]),
                ("document (largest)", [("GET", f"{base}/api/block-documents/{largest}", None)]),
                ("info", [("GET", f"{base}/api/block-documents/{d}/info", None) for d in sample]),
                ("backlinks", [("GET", f"{base}/api/block-documents/{d}/backlinks", None)
                               for d in sample]),
                ("related (no model)", [("GET", f"{base}/api/notes/{d}/related", None)
                                        for d in sample[:10]]),
                ("unified-search", [("GET", f"{base}/api/unified-search?q={q}", None) for q in
                                    ("budget review", "kyoto museum", "train castle", "cache")]),
                ("block-link-targets", [("GET", f"{base}/api/block-link-targets?q={q}", None)
                                        for q in ("budget", "Page 1", "kyo", "zzz")]),
                ("trash", [("GET", f"{base}/api/trash", None)]),
                ("backups", [("GET", f"{base}/api/backups", None)]),
            ]
            if media:
                endpoints.append(("media", [("GET", f"{base}/media/{p.name}", None)
                                            for p in media[:5] if p.suffix != ".txt"]))
            batch = client.post(f"{base}/api/batch/block-documents", json={"ids": sample[:20]})
            if batch.status_code == 200:
                endpoints.append(("batch (20 documents)", [
                    ("POST", f"{base}/api/batch/block-documents", {"ids": sample[:20]})]))
            results["endpoints"] = [measure(client, name, requests, rounds)
                                    for name, requests in endpoints]
            if batch.status_code != 200:
                results["endpoints"].append({"name": "batch (20 documents)", "missing": True})
            results["backup_created"] = backup.status_code
            results["typing (normal note)"] = typing(client, base, sample[0], 40)
            results["typing (largest note)"] = typing(client, base, largest, 40)
            meta = client.post(f"{base}/api/block-documents/{largest}/transactions?response=meta",
                               json={"base_revision": 0, "operations": [{"operation": "delete",
                                                                         "block_id": "x"}]})
            if meta.status_code != 422:  # the option exists (409: stale revision)
                results["typing (largest, ?response=meta)"] = typing(
                    client, base, largest, 40, "?response=meta")
            results["mixed (4 readers + 1 writer, 5 s)"] = mixed(
                lambda: httpx.Client(timeout=60), base, sample[1:], sample[0], 5.0
            )
            results["rss_mb"] = server.rss_mb()
            return results
        finally:
            client.close()
            server.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def print_table(results: dict) -> None:
    print(f"\n== {results['kind']} workspace ==  startup {results['startup']}  rss {results['rss_mb']} MB")
    print(f"{'endpoint':28} {'p50 ms':>8} {'p95 ms':>8} {'bytes':>9} {'gzip':>8}  revalidate")
    for row in results["endpoints"]:
        if row.get("missing"):
            print(f"{row['name']:28} {'(not available)':>8}")
            continue
        reval = row.get("revalidate")
        reval_text = f"{reval['status']} p50 {reval['p50_ms']} ms" if reval else "-"
        print(f"{row['name']:28} {row['p50_ms']:8} {row['p95_ms']:8} {row['bytes']:9} "
              f"{row['gzip_bytes']:8}  {reval_text}")
    for key in ("typing (normal note)", "typing (largest note)",
                "typing (largest, ?response=meta)"):
        if key not in results:
            continue
        row = results[key]
        print(f"{key:28} {row['p50_ms']:8} {row['p95_ms']:8} {row['bytes']:9}")
    row = results["mixed (4 readers + 1 writer, 5 s)"]
    print(f"mixed reads  n={row['reads']['n']} p50 {row['reads']['p50_ms']} p95 {row['reads']['p95_ms']}"
          f" | writes n={row['writes']['n']} p50 {row['writes']['p50_ms']} p95 {row['writes']['p95_ms']}"
          f" {row['errors']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--kind", choices=["plain", "onenote", "both"], default="both")
    parser.add_argument("--size", type=int, default=None, help="notes (plain) or pages (onenote)")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--label", default="")
    parser.add_argument("--build", choices=["plain", "onenote"], default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.build:
        (build_plain if args.build == "plain" else build_onenote)(args.dir, args.size)
        return
    kinds = ["plain", "onenote"] if args.kind == "both" else [args.kind]
    all_results = []
    for kind in kinds:
        size = args.size or (2000 if kind == "plain" else 600)
        directory = ensure_workspace(args.dir, kind, size)
        results = run(directory, kind, args.rounds)
        results["label"] = args.label
        print_table(results)
        all_results.append(results)
    if args.out:
        with args.out.open("a", encoding="utf-8") as handle:
            for results in all_results:
                handle.write(json.dumps(results) + "\n")


if __name__ == "__main__":
    main()
