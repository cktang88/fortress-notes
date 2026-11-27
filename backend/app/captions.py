"""Picture descriptions: a short sentence about what each picture shows, made locally
by a small vision-language model, so search finds photos by their content ("dog on
the beach", "receipt from the café"). OCR (app/ocr.py) still reads the words in them;
this adds what they show. Findings and numbers: docs/picture-descriptions.md.

Optional and off by default. The Mac app turns it on in Settings
(``PUT /api/descriptions``, :func:`set_enabled`); ``PICTURE_DESCRIPTIONS=true`` turns it
on for a backend run without the app. On: the pinned model is downloaded once (in a
child process, so turning it off stops a download too), then the ``captions`` lane of
app/background.py describes pictures one by one, newest first, on the idle lane: it
runs only while no search scores and no other lane (OCR, indexing, mirrors) has work.
Off: the work stops and the model leaves memory; descriptions already made stay
searchable.

Each picture is described once per content hash and :data:`MODEL`, like OCR's queue:
a picture is pending while ``picture_descriptions`` has no finished row for its hash
and the current MODEL; one that keeps failing is given up after :data:`MAX_ATTEMPTS`;
bumping MODEL re-describes everything lazily while the old descriptions stay
searchable. Tiny pictures (icons) are not described.

A description is kept apart from the text read from the picture (``assets.text``):
``textutil.ASSET_TEXT_EXTRAS`` appends it to the asset's searchable text, so it reaches
full-text search, ColBERT and Ask the way OCR text does, and either can be
recomputed alone. It is never part of a note's body or its Markdown mirror. The note's
info shows it next to the picture's text (``description``), labelled as generated.

To remove the feature: delete this module, its line in app/main.py and the Mac app's
PictureDescriptions.swift (and its two uses).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from . import background, block_store, colbert_mlx, textutil
from .config import Settings, get_settings
from .ocr import IMAGE_TYPES, open_image

log = logging.getLogger("fortress.captions")

# The model and the exact commit of its Hugging Face repository (docs/dependencies.md):
# chosen in docs/picture-descriptions.md. Apache-2.0.
MODEL_REPO = "HuggingFaceTB/SmolVLM-500M-Instruct"
MODEL_REVISION = "753c1752602b5e739af7a7fa6947a635051897a3"  # 2026-11-13
# Only what transformers loads (the repository also has ONNX exports).
MODEL_FILES = ("*.json", "*.safetensors", "*.txt", "*.jinja")
DOWNLOAD_BYTES = 1_020_000_000
# Bump the suffix when the output changes (model, prompt, length, image size):
# every picture is then described again, lazily, in the background.
MODEL = "smolvlm-500m/1"
PROMPT = "Caption this image in a few words."
MAX_NEW_TOKENS = 24
IMAGE_SIDE = 512
MIN_IMAGE_SIDE = 64
MAX_ATTEMPTS = 3
MAX_CHARS = 500
POLL_SECONDS = 60.0

# --- Storage ------------------------------------------------------------------------------

block_store.extend_schema("""
-- Picture descriptions (app/captions.py), one row per content and model; image
-- assets lacking a finished row for the current model are its queue.
CREATE TABLE IF NOT EXISTS picture_descriptions (
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    text TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    duration_ms INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, model)
);
""")

# An asset's description: the newest finished one (the current model's once it has
# described the picture; an older model's meanwhile).
DESCRIPTION_SQL = """(SELECT NULLIF(text, '') FROM picture_descriptions
                       WHERE content_hash = assets.content_hash AND status = 'done'
                       ORDER BY updated_at DESC LIMIT 1)"""
textutil.ASSET_TEXT_EXTRAS["description"] = DESCRIPTION_SQL

_IMAGE_TYPES_SQL = ",".join(f"'{t}'" for t in sorted(IMAGE_TYPES))
_PENDING_SQL = f"""
SELECT assets.content_hash, MIN(assets.file_path) AS file_path,
       MAX(assets.created_at) AS created_at,
       COALESCE(MAX(picture_descriptions.attempts), 0) AS attempts
  FROM assets
  LEFT JOIN picture_descriptions
    ON picture_descriptions.content_hash = assets.content_hash
   AND picture_descriptions.model = ?
 WHERE assets.mime_type IN ({_IMAGE_TYPES_SQL})
   AND (picture_descriptions.status IS NULL
        OR (picture_descriptions.status = 'running'
            AND picture_descriptions.attempts < {MAX_ATTEMPTS}))
 GROUP BY assets.content_hash
 ORDER BY created_at DESC
 LIMIT ?
"""


def pending(db_path: Path, limit: int = 1) -> list[dict]:
    """Pictures the current model has yet to describe, newest first."""

    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        return [dict(row) for row in connection.execute(_PENDING_SQL, (MODEL, limit))]


def _mark(db_path: Path, content_hash: str, **fields) -> None:
    fields["updated_at"] = block_store.utc_now()
    columns = ", ".join(fields)
    updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
    with block_store.connection_scope(db_path) as connection:
        connection.execute(
            f"""INSERT INTO picture_descriptions(content_hash, model, {columns})
                VALUES (?, ?, {", ".join("?" for _ in fields)})
                ON CONFLICT(content_hash, model) DO UPDATE SET {updates}""",
            (content_hash, MODEL, *fields.values()),
        )


def process_one(db_path: Path) -> bool:
    """Describe the next pending picture; False when nothing is pending."""

    rows = pending(db_path)
    if not rows:
        return False
    row = rows[0]
    content_hash, path = row["content_hash"], Path(row["file_path"])
    attempts = int(row["attempts"]) + 1
    # Recorded before the work: a picture that crashes the process is not retried forever.
    _mark(db_path, content_hash, status="running", attempts=attempts)
    started = time.perf_counter()
    try:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(path.name)
        text = describe_file(path)
    except Exception as exc:  # a broken file must not stop the queue
        log.warning("Describing %s failed: %s", path.name, exc)
        _mark(db_path, content_hash, status="failed" if attempts >= MAX_ATTEMPTS else "running",
              error=f"{type(exc).__name__}: {exc}"[:500])
        return True
    elapsed = round((time.perf_counter() - started) * 1000)
    _mark(db_path, content_hash, status="done", text=text[:MAX_CHARS], duration_ms=elapsed,
          error=None)
    with block_store.connection_scope(db_path) as connection:
        # Other models' descriptions are only needed until this one has described it.
        connection.execute(
            "DELETE FROM picture_descriptions WHERE content_hash = ? AND model <> ?",
            (content_hash, MODEL),
        )
    block_store.asset_text_changed(db_path, content_hash)
    return True


def counts(db_path: Path) -> dict[str, int]:
    block_store.initialize(db_path)
    with block_store.connection_scope(db_path) as connection:
        done = dict(connection.execute(
            "SELECT status, COUNT(*) FROM picture_descriptions WHERE model = ? GROUP BY status",
            (MODEL,),
        ).fetchall())
        queued = connection.execute(
            f"SELECT COUNT(*) FROM ({_PENDING_SQL})", (MODEL, -1)
        ).fetchone()[0]
    return {"queued": queued, "done": done.get("done", 0), "failed": done.get("failed", 0)}


# --- The model ----------------------------------------------------------------------------

class _Loaded:
    """The processor (both runtimes use transformers' prompt and pixels) and the model:
    MLX on Apple Silicon's GPU when it works, else torch on the CPU."""

    def __init__(self, path: Path) -> None:
        from transformers import AutoProcessor

        self.path = path
        self.processor = AutoProcessor.from_pretrained(path)
        # One tile at IMAGE_SIDE: SmolVLM's default splits a picture into up to 16
        # tiles of 512 px, which is 5-10x slower and described no better here.
        self.processor.image_processor.do_image_splitting = False
        self.processor.image_processor.size = {"longest_edge": IMAGE_SIDE}
        eos = json.loads((path / "generation_config.json").read_text()).get("eos_token_id")
        self.eos = set(eos if isinstance(eos, list) else [eos])
        self.gpu = self._mlx()
        self.cpu = None if self.gpu is not None else self._torch()

    def _mlx(self):
        if get_settings().embeddings_device.strip().lower() == "cpu" or not colbert_mlx.available():
            return None
        try:
            from .captions_mlx import MlxSmolVLM

            return MlxSmolVLM(self.path)
        except Exception:  # noqa: BLE001 - the CPU describes instead
            log.warning("MLX picture describer unavailable; using the CPU", exc_info=True)
            return None

    def _torch(self):
        import torch
        from transformers import AutoModelForImageTextToText

        torch.set_num_threads(Settings.default_threads(get_settings().embeddings_threads))
        # float32: bfloat16 halves the memory and is faster on CPUs with bf16 units, but
        # was several times slower on GitHub's M1 (docs/picture-descriptions.md).
        return AutoModelForImageTextToText.from_pretrained(self.path, dtype=torch.float32).eval()

    @property
    def device(self) -> str:
        return "mlx" if self.gpu is not None else "cpu"

    def describe(self, image) -> str:
        messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": PROMPT}]}]
        prompt = self.processor.apply_chat_template(messages, add_generation_prompt=True)
        if self.gpu is not None:
            try:
                inputs = self.processor(text=prompt, images=[image], return_tensors="np")
                tokens = self.gpu.generate(inputs["input_ids"], inputs["pixel_values"],
                                           MAX_NEW_TOKENS, self.eos)
                return self.processor.decode(tokens, skip_special_tokens=True)
            except Exception:  # noqa: BLE001 - any MLX error: the CPU from now on
                log.warning("MLX picture describer failed; using the CPU from now on", exc_info=True)
                self.gpu = None
        if self.cpu is None:
            self.cpu = self._torch()
        import torch

        inputs = self.processor(text=prompt, images=[image], return_tensors="pt")
        with torch.inference_mode():
            output = self.cpu.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)
        return self.processor.decode(output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


_model: _Loaded | None = None
_model_lock = threading.Lock()


def _load() -> _Loaded:
    """The model, loaded once (a few seconds)."""

    global _model
    with _model_lock:
        if _model is None:
            _model = _Loaded(snapshot_path())
        return _model


def unload() -> None:
    global _model
    with _model_lock:
        _model = None


def describe_image(image) -> str:
    """A few words about a PIL image (slow: never on a request path)."""

    return _tidy(_load().describe(image))


def _tidy(text: str) -> str:
    """The first sentence, without a dangling cut-off end."""

    text = " ".join(text.split())
    end = text.find(". ")
    if end >= 0:
        text = text[: end + 1]
    return text


def describe_file(path: Path) -> str:
    image = open_image(path)
    if min(image.size) < MIN_IMAGE_SIDE:
        return ""  # an icon or a bullet: nothing to describe
    image.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
    return describe_image(image)


# --- Download -----------------------------------------------------------------------------


def _repo_dir() -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE) / ("models--" + MODEL_REPO.replace("/", "--"))


def snapshot_path() -> Path:
    """The downloaded model's folder (raises when it is not complete)."""

    from huggingface_hub import snapshot_download

    return Path(snapshot_download(MODEL_REPO, revision=MODEL_REVISION,
                                  allow_patterns=list(MODEL_FILES), local_files_only=True))


def downloaded() -> bool:
    try:
        path = snapshot_path()
    except Exception:
        return False
    return any(path.glob("*.safetensors"))


def downloaded_bytes() -> int:
    """Bytes of the model on disk, finished files and partial ones."""

    repo = _repo_dir()
    total = 0
    for folder, pattern in ((repo / "snapshots" / MODEL_REVISION, "*"), (repo / "blobs", "*.incomplete")):
        try:
            total += sum(path.stat().st_size for path in folder.glob(pattern) if path.is_file())
        except OSError:
            pass
    return total


# --- Worker -------------------------------------------------------------------------------


class Describer(background.Service):
    """The ``captions`` lane: download the model, then one picture per unit."""

    name = "captions"
    priority = background.IDLE
    poll = POLL_SECONDS

    def __init__(self) -> None:
        self.db_path: Path | None = None
        self.state = "off"  # off, downloading, loading, describing, idle, failed
        self.error: str | None = None
        self._download: subprocess.Popen | None = None
        self._download_failures = 0

    def step(self) -> float | None:
        if self.db_path is None:
            return None
        if not downloaded():
            return self._fetch()
        if _model is None:
            self.state = "loading"
            try:
                _load()
            except Exception as exc:
                self.state, self.error = "failed", f"The model couldn't be loaded: {exc}"[:300]
                log.exception("loading the picture-description model failed")
                return None
        self.state = "describing"
        if process_one(self.db_path):
            return 0
        self.state = "idle"
        return None

    def _fetch(self) -> float | None:
        """Download the pinned model in a child process (stoppable)."""

        self.state, self.error = "downloading", None
        code = (
            "import sys; from huggingface_hub import snapshot_download; "
            "snapshot_download(sys.argv[1], revision=sys.argv[2], allow_patterns=sys.argv[3:])"
        )
        env = {**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
        self._download = subprocess.Popen(
            [sys.executable, "-c", code, MODEL_REPO, MODEL_REVISION, *MODEL_FILES],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
        )
        _, stderr = self._download.communicate()
        failed = self._download.returncode
        self._download = None
        if failed == 0 and downloaded():
            self._download_failures = 0
            return 0
        if failed and failed < 0:  # stopped (turned off or shutting down)
            self.state = "off"
            return None
        self._download_failures += 1
        lines = (stderr or b"").decode(errors="replace").strip().splitlines()
        self.state = "failed"
        self.error = f"The model couldn't be downloaded: {lines[-1] if lines else failed}"[:300]
        log.warning("%s", self.error)
        # Offline, or Hugging Face is down: try again later, less and less often.
        return min(3600.0, 60.0 * 2 ** min(self._download_failures, 6))

    def stop(self) -> None:
        download = self._download
        if download is not None and download.poll() is None:
            download.terminate()


_describer = background.register(Describer())
_enabled: bool | None = None


def enabled() -> bool:
    return get_settings().picture_descriptions if _enabled is None else _enabled


def start(db_path: Path) -> None:
    """Start the lane if descriptions are on (backend startup)."""

    _describer.db_path = db_path
    if enabled():
        background.ensure(_describer)


def set_enabled(on: bool) -> None:
    """Turn the descriptions on (download, then describe in the background) or off."""

    global _enabled
    _enabled = on
    if on:
        _describer.db_path = get_settings().block_db_path
        background.ensure(_describer)
        background.wake(Describer.name)
    else:
        _describer.stop()  # a download in progress
        background.halt(Describer.name)
        _describer.state = "off"
        unload()


def wake(_paths: object = None) -> None:
    """New pictures were registered: describe them when the lane is next idle."""

    background.wake(Describer.name)


def wait_idle(timeout: float = 30.0) -> bool:
    db_path = _describer.db_path
    return background.runner.wait_idle(
        Describer.name, lambda: db_path is None or not pending(db_path), timeout
    )


def status() -> dict[str, object]:
    on = enabled()
    state = _describer.state if on else "off"
    if on and state == "off":
        state = "describing" if background.running(Describer.name) else "off"
    return {
        "enabled": on,
        "state": state,
        "model": MODEL,
        "repository": MODEL_REPO,
        "revision": MODEL_REVISION,
        "device": _model.device if _model is not None else None,  # mlx or cpu, once loaded
        "download_bytes": DOWNLOAD_BYTES,
        "downloaded_bytes": DOWNLOAD_BYTES if downloaded() else downloaded_bytes(),
        "error": _describer.error if state == "failed" else None,
        **counts(get_settings().block_db_path),
    }


# --- API ----------------------------------------------------------------------------------

router = APIRouter()


class DescriptionSettings(BaseModel):
    enabled: bool


@router.get("/api/descriptions")
def get_descriptions() -> dict:
    """Whether pictures are described, the model's download, and how far along it is."""

    return status()


@router.put("/api/descriptions")
def put_descriptions(settings: DescriptionSettings) -> dict:
    """Turn picture descriptions on or off (the Mac app's Settings)."""

    if settings.enabled != enabled() or (
        settings.enabled and not background.running(Describer.name)
    ):
        set_enabled(settings.enabled)
    return status()
