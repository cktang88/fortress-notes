"""Text in pictures and PDFs, read locally in the background so it is searchable.

Every image and PDF asset (pasted, uploaded, imported from OneNote) is read once
per content hash and OCR model:

* Images go through RapidOCR with PaddleOCR's PP-OCRv6 small models (ONNX, CPU,
  no torch; ~30 MB of models shipped inside the ``rapidocr`` wheel).
* PDFs use their embedded text layer (pypdfium2); only pages without one
  (scans, printouts) are rendered and OCR'd.

The queue is SQLite itself: an asset is pending while ``ocr_results`` has no
finished row for its hash and the current :data:`MODEL`. One daemon thread
works through it, newest assets first (a fresh paste before an old backfill),
images whose block already has OneNote's own OCR text (``alt``) last. A crash
or restart loses nothing: the work is simply still pending, and an asset that
keeps failing is given up after :data:`MAX_ATTEMPTS`. Changing :data:`MODEL`
re-reads everything lazily while the old text stays searchable meanwhile.

The text is stored in ``assets.text``; from there it is part of each owning
block's full-text row, its ColBERT document and Ask's grep corpus, without
being part of the note body.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import block_store
from .config import get_settings

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

            threads = getattr(get_settings(), "ocr_threads", 0) or max(1, min(4, (os.cpu_count() or 2) // 2))
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


# --- Queue --------------------------------------------------------------------------------

# Assets still to read for the current model: one row per content hash, the
# newest first, those whose every block already has OneNote's OCR text last.
_PENDING_SQL = f"""
SELECT assets.content_hash, assets.file_path, assets.mime_type,
       MAX(assets.created_at) AS created_at,
       COALESCE(MAX(ocr_results.attempts), 0) AS attempts,
       EXISTS (
           SELECT 1 FROM block_assets JOIN blocks ON blocks.id = block_assets.block_id
            WHERE block_assets.content_hash = assets.content_hash
       ) AND NOT EXISTS (
           SELECT 1 FROM block_assets JOIN blocks ON blocks.id = block_assets.block_id
            WHERE block_assets.content_hash = assets.content_hash
              AND trim(COALESCE(CASE WHEN json_valid(blocks.attrs_json)
                                     THEN json_extract(blocks.attrs_json, '$.alt') END, '')) = ''
       ) AS has_alt
  FROM assets
  LEFT JOIN ocr_results
    ON ocr_results.content_hash = assets.content_hash AND ocr_results.model = ?
 WHERE assets.mime_type IN ({",".join(f"'{t}'" for t in sorted(OCR_TYPES))})
   AND (ocr_results.status IS NULL
        OR (ocr_results.status = 'running' AND ocr_results.attempts < {MAX_ATTEMPTS}))
 GROUP BY assets.content_hash
 ORDER BY has_alt, created_at DESC
 LIMIT ?
"""


def pending(db_path: Path, limit: int = 1) -> list[dict]:
    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        return [dict(row) for row in connection.execute(_PENDING_SQL, (MODEL, limit))]


def _mark(db_path: Path, content_hash: str, **fields) -> None:
    fields["updated_at"] = block_store._now()
    columns = ", ".join(fields)
    updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
    with block_store.connection_scope(db_path) as connection:
        connection.execute(
            f"""INSERT INTO ocr_results(content_hash, model, {columns})
                VALUES (?, ?, {", ".join("?" for _ in fields)})
                ON CONFLICT(content_hash, model) DO UPDATE SET {updates}""",
            (content_hash, MODEL, *fields.values()),
        )


def process_one(db_path: Path) -> bool:
    """Read the next pending asset; False when nothing is pending."""

    rows = pending(db_path)
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
        # Results of earlier models are only needed until this one has read the asset.
        connection.execute(
            "DELETE FROM ocr_results WHERE content_hash = ? AND model <> ?", (content_hash, MODEL)
        )
    block_store.store_asset_text(db_path, content_hash, text)
    return True


def status_counts(db_path: Path) -> dict[str, object]:
    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        counts = dict(
            connection.execute(
                "SELECT status, COUNT(*) FROM ocr_results WHERE model = ? GROUP BY status",
                (MODEL,),
            ).fetchall()
        )
        queued = connection.execute(
            f"SELECT COUNT(*) FROM ({_PENDING_SQL})", (MODEL, -1)
        ).fetchone()[0]
    return {
        "model": MODEL,
        "queued": queued,
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "running": _worker.busy,
    }


def asset_status(db_path: Path, content_hashes: list[str]) -> dict[str, dict[str, object]]:
    """OCR state of each content hash for the current model (absent = not an OCR type)."""

    if not content_hashes:
        return {}
    placeholders = ",".join("?" for _ in content_hashes)
    with block_store.connection_scope(db_path) as connection:
        rows = connection.execute(
            f"""SELECT content_hash, status, method, pages, error, updated_at
                  FROM ocr_results WHERE model = ? AND content_hash IN ({placeholders})""",
            (MODEL, *content_hashes),
        ).fetchall()
    found = {row["content_hash"]: dict(row) for row in rows}
    return {
        content_hash: found.get(content_hash, {"status": "queued"})
        for content_hash in content_hashes
    }


# --- Worker -------------------------------------------------------------------------------


class _Worker:
    """One daemon thread; :func:`wake` after registering new assets."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.db_path: Path | None = None
        self.wakeup = threading.Event()
        self.stopping = threading.Event()
        self.busy = False
        self.lock = threading.Lock()

    def start(self, db_path: Path) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                if self.db_path == db_path:
                    self.wakeup.set()
                    return
                self.stop()
            self.db_path = db_path
            self.stopping.clear()
            self.wakeup.set()  # backfill whatever is pending from before
            self.thread = threading.Thread(
                target=self._run, args=(db_path,), name="fortress-ocr", daemon=True
            )
            self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.stopping.set()
        self.wakeup.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout)
        self.thread = None

    def _run(self, db_path: Path) -> None:
        while not self.stopping.is_set():
            # Woken by uploads and imports; the poll also finds assets registered
            # any other way (an import's PDFs, a restored backup).
            self.wakeup.wait(POLL_SECONDS)
            self.wakeup.clear()
            while not self.stopping.is_set():
                self.busy = True
                try:
                    if not process_one(db_path):
                        break
                except Exception:  # e.g. the database is locked for a long write
                    log.exception("OCR worker error")
                    self.stopping.wait(5.0)
                finally:
                    self.busy = False


_worker = _Worker()


def start(db_path: Path) -> None:
    """Start the background worker (it backfills everything pending)."""

    _worker.start(db_path)


def stop() -> None:
    _worker.stop()


def wake(_paths: object = None) -> None:
    """New assets were registered: read them soon. Returns at once.

    Takes (and ignores) a list of paths so it can serve as an importer's OCR hook:
    the queue finds pending assets in SQLite by itself.
    """

    _worker.wakeup.set()


def wait_idle(timeout: float = 30.0) -> bool:
    """Block until the queue is drained (tests and scripts)."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db_path = _worker.db_path
        if db_path is None or (
            not _worker.busy and not _worker.wakeup.is_set() and not pending(db_path)
        ):
            return True
        time.sleep(0.02)
    return False
