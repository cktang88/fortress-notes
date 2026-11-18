"""Text in pictures and PDFs, read locally in the background so it is searchable.

Every image and PDF asset (pasted, uploaded, imported from OneNote) is read once
per content hash and OCR model:

* Images go through RapidOCR with PaddleOCR's PP-OCRv6 small models (ONNX, CPU,
  no torch; ~30 MB of models shipped inside the ``rapidocr`` wheel).
* PDFs use their embedded text layer (pypdfium2); only pages without one
  (scans, printouts) are rendered and OCR'd.

The queue is SQLite itself: an asset is pending while ``ocr_results`` has no
finished row for its hash and the current :data:`MODEL`. The ``ocr`` lane of
app/background.py works through it, one asset per unit, newest assets first (a fresh paste before an old backfill),
images whose block already has OneNote's own OCR text (``alt``) last. A crash
or restart loses nothing: the work is simply still pending, and an asset that
keeps failing is given up after :data:`MAX_ATTEMPTS`. Changing :data:`MODEL`
re-reads everything lazily while the old text stays searchable meanwhile.

The text is stored in ``assets.text``; from there it is part of each owning
block's full-text row, its ColBERT document and Ask's grep corpus, without
being part of the note body.

The Mac app reads the same assets with Apple Vision and sends its text here; see
"Which reading wins" below for how the two share the work and whose text is used.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import background, block_store
from .config import Settings, get_settings

log = logging.getLogger("fortress.ocr")

# Bump the suffix when the pipeline's output changes (models, pre/post-processing):
# every asset is then read again, lazily, in the background.
MODEL = "rapidocr-ppocrv6-small/1"
MAX_ATTEMPTS = 3
# A PDF page is OCR'd when its text layer has almost no text, or little text and
# a picture covering a good part of the page (a scan with a stamped header).
PDF_TEXT_MIN_CHARS = 16
PDF_SCAN_TEXT_CHARS = 200
PDF_SCAN_IMAGE_SHARE = 0.25
PDF_MAX_OCR_PAGES = 100
PDF_RENDER_MAX_SIDE = 2000
MAX_TEXT_CHARS = 200_000
MIN_IMAGE_SIDE = 12
POLL_SECONDS = 60.0

IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
)
PDF_TYPE = "application/pdf"
OCR_TYPES = IMAGE_TYPES | {PDF_TYPE}


@dataclass
class Result:
    text: str
    method: str  # "ocr", "pdf-text", "pdf-text+ocr"
    pages: int | None = None


# --- Reading ------------------------------------------------------------------------------


_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """The RapidOCR engine, loaded once (≈0.5 s, ≈150 MB)."""

    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import ModelType, OCRVersion, RapidOCR

            threads = Settings.default_threads(get_settings().ocr_threads)
            _engine = RapidOCR(
                params={
                    "Global.log_level": "error",
                    "Det.ocr_version": OCRVersion.PPOCRV6,
                    "Det.model_type": ModelType.SMALL,
                    "Rec.ocr_version": OCRVersion.PPOCRV6,
                    "Rec.model_type": ModelType.SMALL,
                    # Detect on the image scaled down to ≤1280 px (recognition still
                    # reads crops at full resolution). rapidocr's default instead
                    # scales the short side up to 736 px: 2-3x slower on pages and
                    # screenshots, and no more accurate in our benchmark.
                    "Det.limit_type": "max",
                    "Det.limit_side_len": 1280,
                    "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                }
            )
        return _engine


def loaded() -> bool:
    return _engine is not None


def ocr_image(image) -> str:
    """Text lines of a PIL image, top to bottom, words on one line joined by spaces."""

    import numpy as np

    if min(image.size) < MIN_IMAGE_SIDE:
        return ""
    output = _get_engine()(np.asarray(image.convert("RGB"))[:, :, ::-1])
    if output.boxes is None or not output.txts:
        return ""
    return _lines(output.boxes, output.txts)


def _lines(boxes, texts) -> str:
    """Group detected boxes into reading lines (a table row reads as one line)."""

    items = []
    for box, text in zip(boxes, texts):
        ys = [point[1] for point in box]
        xs = [point[0] for point in box]
        items.append(((min(ys) + max(ys)) / 2, max(4.0, max(ys) - min(ys)), min(xs), text))
    items.sort(key=lambda item: (item[0], item[2]))
    lines: list[list[tuple]] = []
    for item in items:
        line = lines[-1] if lines else None
        if line is not None and abs(item[0] - line[0][0]) <= 0.5 * min(item[1], line[0][1]):
            line.append(item)
        else:
            lines.append([item])
    return "\n".join(
        " ".join(text for *_rest, text in sorted(line, key=lambda item: item[2])) for line in lines
    )


def _open_image(path: Path):
    from PIL import Image, ImageOps

    image = Image.open(path)
    image.seek(0)  # first frame of a GIF/WebP animation
    image = ImageOps.exif_transpose(image)  # phone photos are stored sideways
    if image.mode in {"RGBA", "LA", "P", "PA"}:
        # Transparent screenshots: dark text on a transparent background must not
        # become dark text on black.
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, rgba)
    return image.convert("RGB")


def read_pdf(path: Path) -> Result:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        parts: list[str] = []
        ocr_pages = 0
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range().replace("\r\n", "\n").strip()
                finally:
                    textpage.close()
                if ocr_pages < PDF_MAX_OCR_PAGES and _needs_ocr(page, text):
                    width, height = page.get_size()
                    scale = min(PDF_RENDER_MAX_SIDE / max(width, height, 1), 300 / 72)
                    bitmap = page.render(scale=scale)
                    try:
                        ocr_text = ocr_image(bitmap.to_pil())
                    finally:
                        bitmap.close()
                    ocr_pages += 1
                    text = "\n".join(part for part in (text, ocr_text) if part)
            finally:
                page.close()
            if text:
                parts.append(text)
        method = "pdf-text+ocr" if ocr_pages else "pdf-text"
        return Result("\n\n".join(parts), method, len(pdf))
    finally:
        pdf.close()


def _needs_ocr(page, text: str) -> bool:
    import pypdfium2.raw as pdfium_c

    characters = len("".join(text.split()))
    if characters < PDF_TEXT_MIN_CHARS:
        return True
    if characters >= PDF_SCAN_TEXT_CHARS:
        return False
    width, height = page.get_size()
    covered = 0.0
    for image in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2):
        left, bottom, right, top = image.get_pos()
        covered += max(0.0, min(right, width) - max(left, 0)) * max(0.0, min(top, height) - max(bottom, 0))
    return covered >= PDF_SCAN_IMAGE_SHARE * width * height


def read_asset(path: Path, mime_type: str) -> Result:
    """Read the text of one image or PDF (slow: never on a request path)."""

    if mime_type == PDF_TYPE:
        return read_pdf(path)
    return Result(ocr_image(_open_image(path)), "ocr")


# --- Which reading wins -------------------------------------------------------------------
#
# One rule: an asset's text is that of its best-ranked finished ("done") result. The
# Mac app reads pictures and PDFs with Apple Vision (more scripts, handwriting) and
# sends its results here (:func:`store_external`); they outrank this process's own
# model, which outranks results of earlier versions of it. Results of one external
# family replace each other (a newer macOS's Vision replaces an older one's).
#
# No asset is read twice for nothing:
# * The CPU queue never reads an asset a preferred reader has finished.
# * While an external reader is active (it asked for work within
#   :data:`EXTERNAL_LEASE_SECONDS`), the CPU queue only reads what that reader gave up
#   on (a "failed" row): the Mac, which is faster and better, takes the backlog.
#   When the app quits the lease lapses and the CPU queue carries on by itself.
# * The external reader re-reads assets the CPU model already read (once per external
#   model id): that is the upgrade, e.g. Cyrillic the CPU model can't read.

EXTERNAL_FAMILIES = {"apple-vision": 2}
EXTERNAL_LEASE_SECONDS = 180.0
MAX_MODEL_CHARS = 64

_external_seen: dict[str, float] = {}
_external_lock = threading.Lock()


def family(model: str) -> str:
    return model.split("/", 1)[0]


def rank(model: str) -> int:
    """How much a model's text is preferred: external readers > this process's > older."""

    if model == MODEL:
        return 1
    return EXTERNAL_FAMILIES.get(family(model), 0)


def is_external_model(model: str) -> bool:
    return (
        0 < len(model) <= MAX_MODEL_CHARS
        and family(model) in EXTERNAL_FAMILIES
        and len(model.split("/", 1)) == 2
        and model.split("/", 1)[1].strip() != ""
        and model.isprintable()
    )


def _preferred_sql(column: str) -> str:
    """SQL truth value: ``column`` names a model preferred over this process's own."""

    return "(" + " OR ".join(
        f"{column} LIKE '{name}/%'" for name in sorted(EXTERNAL_FAMILIES)
    ) + ")"


def note_external_reader(model: str) -> None:
    """An external reader asked for work: the CPU queue defers to it for a while."""

    with _external_lock:
        _external_seen[model] = time.monotonic()


def external_reader() -> str | None:
    """The external reader currently active (asked for work recently), if any."""

    now = time.monotonic()
    with _external_lock:
        active = [(seen, model) for model, seen in _external_seen.items()
                  if now - seen < EXTERNAL_LEASE_SECONDS]
    return max(active)[1] if active else None


def _forget_external_readers() -> None:
    with _external_lock:
        _external_seen.clear()


# --- Queue --------------------------------------------------------------------------------

_OCR_TYPES_SQL = ",".join(f"'{t}'" for t in sorted(OCR_TYPES))
# Whether each of an asset's blocks already has OneNote's OCR text (read those last).
_HAS_ALT_SQL = """EXISTS (
           SELECT 1 FROM block_assets JOIN blocks ON blocks.id = block_assets.block_id
            WHERE block_assets.content_hash = assets.content_hash
       ) AND NOT EXISTS (
           SELECT 1 FROM block_assets JOIN blocks ON blocks.id = block_assets.block_id
            WHERE block_assets.content_hash = assets.content_hash
              AND trim(COALESCE(CASE WHEN json_valid(blocks.attrs_json)
                                     THEN json_extract(blocks.attrs_json, '$.alt') END, '')) = ''
       )"""

# Assets still to read for a model (bound first): one row per content hash, the newest
# first, those whose every block already has OneNote's OCR text last. A row "running"
# with attempts left is pending too (a retry, or a read a crash interrupted).
_PENDING_SQL = f"""
SELECT assets.content_hash, assets.file_path, assets.mime_type,
       MIN(assets.media_name) AS media_name,
       MAX(assets.created_at) AS created_at,
       COALESCE(MAX(ocr_results.attempts), 0) AS attempts,
       {_HAS_ALT_SQL} AS has_alt
  FROM assets
  LEFT JOIN ocr_results
    ON ocr_results.content_hash = assets.content_hash AND ocr_results.model = ?
 WHERE assets.mime_type IN ({_OCR_TYPES_SQL})
   AND (ocr_results.status IS NULL
        OR (ocr_results.status = 'running' AND ocr_results.attempts < {MAX_ATTEMPTS}))
   AND (? = 0 OR NOT EXISTS (
        SELECT 1 FROM ocr_results AS better
         WHERE better.content_hash = assets.content_hash AND better.status = 'done'
           AND {_preferred_sql("better.model")}))
   AND (? = 0 OR EXISTS (
        SELECT 1 FROM ocr_results AS given_up
         WHERE given_up.content_hash = assets.content_hash AND given_up.status = 'failed'
           AND {_preferred_sql("given_up.model")}))
 GROUP BY assets.content_hash
 ORDER BY has_alt, created_at DESC
 LIMIT ?
"""


def pending(db_path: Path, limit: int = 1, *, defer: bool = False) -> list[dict]:
    """The CPU queue: assets this process's model has yet to read and no preferred
    reader has read. ``defer``: an external reader is active, so only what it gave up on."""

    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        return [dict(row) for row in connection.execute(
            _PENDING_SQL, (MODEL, 1, int(defer), limit))]


def external_pending(db_path: Path, model: str, limit: int) -> tuple[int, list[dict]]:
    """An external reader's queue: (how many, the first ``limit``) assets ``model``
    hasn't finished, including those the CPU model already read (it reads better)."""

    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        total = connection.execute(
            f"SELECT COUNT(*) FROM ({_PENDING_SQL})", (model, 0, 0, -1)
        ).fetchone()[0]
        rows = [dict(row) for row in connection.execute(_PENDING_SQL, (model, 0, 0, limit))]
    return total, rows


def _mark(db_path: Path, content_hash: str, model: str | None = None, **fields) -> None:
    model = model or MODEL
    fields["updated_at"] = block_store.utc_now()
    columns = ", ".join(fields)
    updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
    with block_store.connection_scope(db_path) as connection:
        connection.execute(
            f"""INSERT INTO ocr_results(content_hash, model, {columns})
                VALUES (?, ?, {", ".join("?" for _ in fields)})
                ON CONFLICT(content_hash, model) DO UPDATE SET {updates}""",
            (content_hash, model, *fields.values()),
        )


def _best_done(connection, content_hash: str) -> dict | None:
    rows = connection.execute(
        "SELECT * FROM ocr_results WHERE content_hash = ? AND status = 'done'", (content_hash,)
    ).fetchall()
    if not rows:
        return None
    return dict(max(rows, key=lambda row: (rank(row["model"]), row["updated_at"])))


def _publish(db_path: Path, content_hash: str) -> None:
    """Give the asset the text of its best finished reading (search, ColBERT, Ask).

    Only the blocks showing this asset are re-indexed, and only when the text changed.
    """

    with block_store.connection_scope(db_path) as connection:
        best = _best_done(connection, content_hash)
    if best is not None:
        block_store.store_asset_text(db_path, content_hash, best["text"])


def process_one(db_path: Path, *, defer: bool = False) -> bool:
    """Read the next pending asset; False when nothing is pending."""

    rows = pending(db_path, defer=defer)
    if not rows:
        return False
    row = rows[0]
    content_hash, path = row["content_hash"], Path(row["file_path"])
    attempts = int(row["attempts"]) + 1
    # Recorded before the work: an asset that crashes the process is not retried forever.
    _mark(db_path, content_hash, status="running", attempts=attempts)
    started = time.perf_counter()
    try:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(path.name)
        result = read_asset(path, row["mime_type"])
    except Exception as exc:  # a broken file must not stop the queue
        log.warning("OCR failed for %s: %s", path.name, exc)
        _mark(db_path, content_hash, status="failed" if attempts >= MAX_ATTEMPTS else "running",
              error=f"{type(exc).__name__}: {exc}"[:500])
        return True
    text = result.text.strip()[:MAX_TEXT_CHARS]
    elapsed = round((time.perf_counter() - started) * 1000)
    _mark(db_path, content_hash, status="done", text=text, method=result.method,
          pages=result.pages, duration_ms=elapsed, error=None)
    with block_store.connection_scope(db_path) as connection:
        # Results of earlier versions of this model are only needed until it has read
        # the asset; external readers' results stay (they may outrank it).
        connection.execute(
            f"""DELETE FROM ocr_results WHERE content_hash = ? AND model <> ?
                  AND NOT {_preferred_sql("model")}""",
            (content_hash, MODEL),
        )
    _publish(db_path, content_hash)
    return True


def store_external(
    db_path: Path,
    content_hash: str,
    model: str,
    *,
    status: str,
    text: str = "",
    method: str = "ocr",
    pages: int | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, object]:
    """Record an external reader's result for an asset (``status`` done or failed).

    Raises KeyError for a hash that is no registered picture or PDF, ValueError for a
    model that isn't an external reader's.
    """

    if not is_external_model(model):
        raise ValueError(f"not an external OCR model: {model!r}")
    if status not in {"done", "failed"}:
        raise ValueError(f"status must be done or failed, not {status!r}")
    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        known = connection.execute(
            f"SELECT 1 FROM assets WHERE content_hash = ? AND mime_type IN ({_OCR_TYPES_SQL}) LIMIT 1",
            (content_hash,),
        ).fetchone()
        previous = connection.execute(
            "SELECT attempts FROM ocr_results WHERE content_hash = ? AND model = ?",
            (content_hash, model),
        ).fetchone()
    if known is None:
        raise KeyError(content_hash)
    attempts = (previous["attempts"] if previous else 0) + 1
    if status == "failed":
        # Given up at once: the reader's errors are about the file, not a passing state.
        # The CPU model then reads it, even while this reader stays active.
        _mark(db_path, content_hash, model, status="failed", attempts=attempts,
              error=(error or "failed")[:500], duration_ms=duration_ms)
        wake()
        return {"status": "failed", "model": model, "text_chars": 0}
    text = (text or "").strip()[:MAX_TEXT_CHARS]
    _mark(db_path, content_hash, model, status="done", attempts=attempts, text=text,
          method=(method or "ocr")[:32], pages=pages, duration_ms=duration_ms, error=None)
    with block_store.connection_scope(db_path) as connection:
        # Another version of the same reader (an older macOS's Vision) is replaced.
        connection.execute(
            "DELETE FROM ocr_results WHERE content_hash = ? AND model <> ? AND model LIKE ?",
            (content_hash, model, f"{family(model)}/%"),
        )
    _publish(db_path, content_hash)
    return {"status": "done", "model": model, "text_chars": len(text)}


def reading(db_path: Path, content_hash: str) -> dict[str, object] | None:
    """The reading whose text an asset uses (all of it), or None before any."""

    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        best = _best_done(connection, content_hash)
    if best is None:
        return None
    return {key: best[key] for key in ("content_hash", "model", "status", "text", "method", "pages", "updated_at")}


def status_counts(db_path: Path) -> dict[str, object]:
    block_store.initialize(db_path)
    reader = external_reader()
    with block_store.connection_scope(db_path) as connection:
        counts = dict(
            connection.execute(
                "SELECT status, COUNT(*) FROM ocr_results WHERE model = ? GROUP BY status",
                (MODEL,),
            ).fetchall()
        )
        # Not yet readable by anyone: no result of this model nor a preferred one.
        queued = connection.execute(
            f"SELECT COUNT(*) FROM ({_PENDING_SQL})", (MODEL, 1, 0, -1)
        ).fetchone()[0]
        external = None
        if reader is not None:
            external_counts = dict(
                connection.execute(
                    "SELECT status, COUNT(*) FROM ocr_results WHERE model = ? GROUP BY status",
                    (reader,),
                ).fetchall()
            )
            external = {
                "model": reader,
                "queued": connection.execute(
                    f"SELECT COUNT(*) FROM ({_PENDING_SQL})", (reader, 0, 0, -1)
                ).fetchone()[0],
                "done": external_counts.get("done", 0),
                "failed": external_counts.get("failed", 0),
            }
    return {
        "model": MODEL,
        "queued": queued,
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "running": background.busy(OcrQueue.name),
        # The Mac app's Apple Vision reader, while it is working (else null).
        "external": external,
    }


def asset_status(db_path: Path, content_hashes: list[str]) -> dict[str, dict[str, object]]:
    """OCR state of each content hash: its best finished reading (and that reading's
    model); else this process's model's state (running, failed); else queued."""

    if not content_hashes:
        return {}
    placeholders = ",".join("?" for _ in content_hashes)
    with block_store.connection_scope(db_path) as connection:
        rows = connection.execute(
            f"""SELECT content_hash, model, status, method, pages, error, updated_at
                  FROM ocr_results WHERE content_hash IN ({placeholders})""",
            content_hashes,
        ).fetchall()
    by_hash: dict[str, list[dict]] = {}
    for row in rows:
        by_hash.setdefault(row["content_hash"], []).append(dict(row))
    result = {}
    for content_hash in content_hashes:
        found = by_hash.get(content_hash, [])
        done = [row for row in found if row["status"] == "done" and rank(row["model"]) > 0]
        own = next((row for row in found if row["model"] == MODEL), None)
        if done:
            result[content_hash] = max(done, key=lambda row: (rank(row["model"]), row["updated_at"]))
        elif own is not None:
            result[content_hash] = own
        else:
            result[content_hash] = {"status": "queued", "model": MODEL}
    return result


# --- Worker -------------------------------------------------------------------------------


class OcrQueue(background.Service):
    """The ``ocr`` lane: one pending asset per unit; :func:`wake` after registering new ones."""

    name = "ocr"
    priority = background.LOW
    # The poll also finds assets registered any other way (an import's PDFs, a
    # restored backup).
    poll = POLL_SECONDS

    def __init__(self) -> None:
        self.db_path: Path | None = None

    def step(self) -> float | None:
        if self.db_path is None or not process_one(
            self.db_path, defer=external_reader() is not None
        ):
            return None
        return 0


_queue = background.register(OcrQueue())


def start(db_path: Path) -> None:
    """Start reading in the background (it backfills everything pending)."""

    _queue.db_path = db_path
    background.ensure(_queue)


def stop() -> None:
    background.halt(OcrQueue.name)


def wake(_paths: object = None) -> None:
    """New assets were registered: read them soon. Returns at once.

    Takes (and ignores) a list of paths so it can serve as an importer's OCR hook:
    the queue finds pending assets in SQLite by itself.
    """

    background.wake(OcrQueue.name)


def wait_idle(timeout: float = 30.0) -> bool:
    """Block until the queue is drained (tests and scripts)."""

    db_path = _queue.db_path
    return background.runner.wait_idle(
        OcrQueue.name,
        lambda: db_path is None or not pending(db_path, defer=external_reader() is not None),
        timeout,
    )
