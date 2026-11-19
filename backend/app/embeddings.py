"""ColBERT late-interaction search via PyLate, with a keyword fallback.

Fast enough to run on every keystroke over tens of thousands of paragraphs, on CPU:

* Paragraph token embeddings live in a few contiguous tensors, one per padded
  length bucket ([n_docs, L, dim]). A query is scored against every paragraph
  with one matrix multiply per bucket followed by a max over L and a sum over
  query tokens: exact MaxSim, no Python loop over paragraphs. Short paragraphs
  are padded by repeating their own first token, which can't change a maximum.
* Embeddings are persisted next to the notes database
  (``.fortress-embeddings.sqlite3``, keyed by block ID and a hash of the text),
  so a restart loads them in about a second instead of re-encoding everything.
* On CPUs with bfloat16 support (AVX512-BF16/AMX) the model and the index run
  in bfloat16: encoding is several times faster and scores match float32 to
  about three decimals. Elsewhere everything stays float32.
* On Apple Silicon a second encoder may run on the GPU (MLX, else torch MPS), for
  queries, for indexing or both: only where it measured faster than the CPU on this
  machine at load time, and only if its encodings match the CPU's (token cosine
  >= ``ACCEL_MIN_COSINE``). Any error on the GPU moves everything back to the CPU
  model (always kept loaded) for good. ``EMBEDDINGS_DEVICE=cpu`` turns it off.
  Encodings always come back to the CPU: the index and every score live there.
* Query encodings are cached (LRU), so refetching the same related list or
  search costs only the scoring. Typed queries, paragraph texts (related to a
  paragraph or a note) and whole notes each have their own cache, so opening a
  long note can't evict what was just searched.
* One thread runs the model (``_Encoder``): searches go ahead of background
  indexing, which is fed to it a batch at a time, and PyLate's tokenizer (which
  sets ``max_seq_length`` on every call) is never used from two threads at once.
* Requests never wait for the model to load: until the background indexer has
  loaded it (``load_model``), every scorer falls back to keyword overlap.
* A whole note is queried with at most ``NOTE_QUERY_VECTORS`` weighted vectors
  (its paragraphs' query vectors, clustered), so related-to-a-note costs about
  the same for a 1000-paragraph note as for a 50-paragraph one.

Edits are applied incrementally: ``warm_index()`` (run on a short interval in the
background) encodes changed paragraphs in batches and drops removed ones;
``score_documents`` itself encodes at most a handful of stale paragraphs inline
and scores paragraphs that are still waiting for the background indexer as 0.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import closing
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import background, block_query, textutil
from . import corpus as corpus_store
from .config import Settings, get_settings

_log = logging.getLogger("fortress")

# Paragraphs encoded inline by a search when the background indexer is behind.
INLINE_ENCODE_LIMIT = 8
# Paragraphs per model batch and per commit while indexing in the background.
ENCODE_BATCH = 16
# Background batches hold about this much text (~250 tokens), so a search queued
# behind one waits a few tens of milliseconds, not seconds.
BACKGROUND_BATCH_CHARS = 1000
INDEX_CHUNK = 64
QUERY_CACHE_SIZE = 256
# Query encodings of paragraph texts (focused related, whole-note queries) and of
# whole notes (clustered), kept apart from typed queries.
PARAGRAPH_CACHE_SIZE = 4096
NOTE_CACHE_SIZE = 32
# A whole-note query is clustered down to this many weighted vectors.
NOTE_QUERY_VECTORS = 256
NOTE_CLUSTER_ROUNDS = 4
# Upper bound on the similarity matrix built at once (elements; 4 bytes each).
SIM_CHUNK_ELEMENTS = 1 << 24
# Long queries (a whole note) are scored in two stages: a strided subset of their
# tokens ranks every paragraph, then the best candidates get exact MaxSim.
LONG_QUERY_TOKENS = 96
PREFILTER_TOKENS = 32
PREFILTER_CANDIDATES = 600
CALIBRATE = True
CALIBRATION_SAMPLE = 256
# Padded lengths: fine steps where most paragraphs are, so little compute is padding.
_BUCKETS = (
    *range(4, 65, 4),
    *range(72, 129, 8),
    *range(144, 257, 16),
    *(320, 384, 448, 512, 640, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192),
)
_STORE_NAME = ".fortress-embeddings.sqlite3"
_STORE_FORMAT = "colbert-v1"


@dataclass
class _Bucket:
    length: int
    rows: object  # torch tensor [capacity, length, dim]
    ids: list[str | None] = field(default_factory=list)
    free: list[int] = field(default_factory=list)


class _Index:
    """Token embeddings of every indexed paragraph, grouped by padded length."""

    def __init__(self) -> None:
        self.buckets: dict[int, _Bucket] = {}
        # block id -> (bucket length, row, text hash, version, token count)
        self.where: dict[str, tuple[int, int, str, str, int]] = {}
        # block id -> version, for a fast "anything changed?" pass per search
        self.versions: dict[str, str] = {}
        self.dim: int | None = None

    def __len__(self) -> int:
        return len(self.where)

    def tokens(self) -> int:
        return sum(entry[4] for entry in self.where.values())

    def put(self, block_id: str, text_hash: str, version: str, embedding) -> None:
        import torch

        tokens = int(embedding.shape[0])
        if tokens == 0:
            self.remove(block_id)
            return
        if self.dim is None:
            self.dim = int(embedding.shape[1])
        length = next((size for size in _BUCKETS if size >= tokens), None)
        if length is None:  # Longer than the model reads anyway; keep the start.
            length, embedding, tokens = _BUCKETS[-1], embedding[: _BUCKETS[-1]], _BUCKETS[-1]
        self.remove(block_id)
        bucket = self.buckets.get(length)
        if bucket is None:
            bucket = _Bucket(length, torch.empty((16, length, self.dim), dtype=embedding.dtype))
            self.buckets[length] = bucket
        if bucket.free:
            row = bucket.free.pop()
        else:
            row = len(bucket.ids)
            bucket.ids.append(None)
            if row >= bucket.rows.shape[0]:
                grown = torch.empty(
                    (max(16, 2 * bucket.rows.shape[0]), length, self.dim), dtype=bucket.rows.dtype
                )
                grown[:row] = bucket.rows[:row]
                bucket.rows = grown
        bucket.rows[row, :tokens] = embedding.to(bucket.rows.dtype)
        bucket.rows[row, tokens:] = embedding[0].to(bucket.rows.dtype)
        bucket.ids[row] = block_id
        self.where[block_id] = (length, row, text_hash, version, tokens)
        self.versions[block_id] = version

    def set_version(self, block_id: str, version: str) -> None:
        entry = self.where[block_id]
        self.where[block_id] = (*entry[:3], version, entry[4])
        self.versions[block_id] = version

    def trim(self) -> None:
        """Release spare capacity left by growing the buckets (after bulk loads)."""

        for bucket in self.buckets.values():
            used = len(bucket.ids)
            if bucket.rows.shape[0] > used + 16:
                bucket.rows = bucket.rows[: used + 16].clone()

    def remove(self, block_id: str) -> None:
        entry = self.where.pop(block_id, None)
        self.versions.pop(block_id, None)
        if entry is None:
            return
        bucket = self.buckets[entry[0]]
        bucket.ids[entry[1]] = None
        bucket.free.append(entry[1])

    def vectors(self, block_id: str):
        length, row, _hash, _version, tokens = self.where[block_id]
        return self.buckets[length].rows[row, :tokens]

    def maxsim_all(self, query, weights=None) -> tuple[list[str | None], object]:
        """Exact MaxSim of ``query`` [nq, dim] against every row: (ids, scores).

        ``weights`` [nq] (float32) weighs each query vector's best match (a
        clustered note query); None sums them as usual.
        """

        import torch

        ids: list[str | None] = []
        parts = []
        query_t = query.T.contiguous()
        for bucket in self.buckets.values():
            used = len(bucket.ids)
            if not used:
                continue
            parts.append(_maxsim_rows(bucket.rows[:used], query_t, weights))
            ids.extend(bucket.ids)
        if not parts:
            return [], torch.empty(0)
        return ids, torch.cat(parts)

    def maxsim_some(self, query, block_ids: list[str], weights=None) -> list[float]:
        """Exact MaxSim for some paragraphs, batched per bucket (see ``maxsim_all``)."""

        import torch

        query_t = query.T.contiguous()
        by_bucket: dict[int, list[int]] = {}
        for position, block_id in enumerate(block_ids):
            by_bucket.setdefault(self.where[block_id][0], []).append(position)
        out = [0.0] * len(block_ids)
        for length, positions in by_bucket.items():
            rows = self.buckets[length].rows[
                torch.tensor([self.where[block_ids[p]][1] for p in positions])
            ]
            for position, score in zip(positions, _maxsim_rows(rows, query_t, weights).tolist()):
                out[position] = score
        return out


def _maxsim_rows(rows, query_t, weights=None):
    """MaxSim of rows [n, L, dim] against query_t [dim, nq], chunked so the
    similarity matrix stays under ``SIM_CHUNK_ELEMENTS`` (a 1000-paragraph note
    against 600 candidates used to allocate gigabytes at once)."""

    import torch

    count, length, dim = rows.shape
    step = max(1, SIM_CHUNK_ELEMENTS // max(1, length * query_t.shape[1]))
    parts = []
    for start in range(0, count, step):
        chunk = rows[start : start + step]
        sims = (chunk.reshape(-1, dim) @ query_t).view(chunk.shape[0], length, -1)
        best = sims.amax(dim=1)
        if weights is None:
            parts.append(best.sum(dim=1, dtype=torch.float32))
        else:
            parts.append(best.float() @ weights)
    return parts[0] if len(parts) == 1 else torch.cat(parts)


@dataclass
class _Routes:
    """Which model encodes queries and which encodes documents (paragraph batches)."""

    query: object
    documents: object
    devices: dict[str, str] = field(default_factory=lambda: {"query": "cpu", "documents": "cpu"})


@dataclass
class _Cache:
    model: object | None = None
    routes: _Routes | None = None
    loaded: bool = False
    failed: bool = False
    index: _Index = field(default_factory=_Index)
    store_path: Path | None = None
    store_loaded: bool = False
    synced_corpus: object | None = None
    queries: OrderedDict = field(default_factory=OrderedDict)
    paragraph_queries: OrderedDict = field(default_factory=OrderedDict)
    note_queries: OrderedDict = field(default_factory=OrderedDict)
    query_hashes: set[str] = field(default_factory=set)
    dtype: object | None = None


_cache = _Cache()
# Only the background loader takes this; requests never wait on it.
_model_lock = threading.Lock()
# Guards the index and the persistent store (scoring takes a few tens of ms).
_index_lock = threading.RLock()
# Guards the query caches.
_queries_lock = threading.Lock()

_FOREGROUND, _BACKGROUND = 0, 1


class _Encoder:
    """The one thread that runs the model, foreground work first.

    PyLate's ``tokenize`` sets the tokenizer's ``max_seq_length`` (query or
    document length) on every call, so two threads encoding at once can cut a
    paragraph to query length and save it that way. Every encode and tokenize
    runs here instead. Background indexing is submitted a batch at a time, so a
    search waits for at most one batch.
    """

    def __init__(self) -> None:
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._order = itertools.count()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def run(self, fn, priority: int):
        if threading.current_thread() is self._thread:
            return fn()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="colbert-encoder", daemon=True
                )
                self._thread.start()
        done: Future = Future()
        self._queue.put((priority, next(self._order), fn, done))
        return done.result()

    def _loop(self) -> None:
        while True:
            _priority, _order, fn, done = self._queue.get()
            if not done.set_running_or_notify_cancel():
                continue
            try:
                done.set_result(fn())
            except BaseException as error:  # handed to the waiting caller
                done.set_exception(error)


_encoder = _Encoder()


def model_loaded() -> bool:
    return _cache.loaded


# --- Apple Silicon GPU (MLX, torch MPS) ---------------------------------------------------
#
# Chosen per machine at load time (an M1 Air and an M4 Max differ, and a GPU's fixed
# cost per call could lose to the CPU on a short query). Candidates, best first: MLX
# (app/colbert_mlx.py: 4-16x faster than the CPU on GitHub's virtual M1, see
# scripts/bench_accel.py), then torch MPS. A candidate is used only if its encodings
# of the probe texts match the CPU model's (mean token cosine >= ACCEL_MIN_COSINE per
# text, same token count; float16 first, then float32), and only for the roles
# (queries, document batches) where it is ACCEL_MIN_SPEEDUP times faster. The CPU
# model always stays loaded: any GPU error routes everything back to it for good.

ACCEL_MIN_COSINE = 0.999
ACCEL_MIN_SPEEDUP = 1.15
ACCELERATORS = ("mlx", "mps")
_PROBE_QUERIES = ["quarterly budget review", "notes from the design meeting about search latency"]
_PROBE_DOCUMENTS = [
    "The release checklist is owned by the platform team; the database backup runs nightly "
    "at two and is copied to cold storage every Sunday.",
    "Flight and hotel for the Kyoto conference are booked. Receipts go to the travel folder "
    "so the tax refund can be claimed in April.",
    "Search latency regressed after the index change: related paragraphs took 400 ms on a "
    "cold cache. Precomputing paragraph queries fixed most of it.",
    "Invoice 2231 is still pending; payment terms are thirty days and the customer asked "
    "for a summary of the shipping costs by order.",
    # Long enough for local attention windows and padding far from any real token.
    " ".join(f"Step {i}: check the {word} before the review." for i, word in enumerate(
        ("budget", "schedule", "owner", "server", "backup", "contract", "receipt", "policy") * 3
    )),
]


def _mps_available() -> bool:
    import sys

    if sys.platform != "darwin":
        return False
    try:
        import torch

        return bool(torch.backends.mps.is_available())
    except Exception:  # noqa: BLE001 - a torch without MPS
        return False


def _mlx_available() -> bool:
    import sys

    if sys.platform != "darwin":
        return False
    from . import colbert_mlx

    return colbert_mlx.available()


def _accelerator_available(kind: str) -> bool:
    return _mlx_available() if kind == "mlx" else _mps_available()


def _load_accelerator(kind: str, cpu_model, settings: Settings, precision: str):
    """Another encoder for ``kind`` ("mlx" or "mps") in ``precision`` (float16/float32)."""

    if kind == "mlx":
        from . import colbert_mlx

        return colbert_mlx.load(cpu_model, settings.colbert_model, precision)
    import torch
    from pylate import models

    model = models.ColBERT(model_name_or_path=settings.colbert_model, device="mps")
    model.eval()
    return model.to(getattr(torch, precision))


def _encode_now(model, texts: list[str], *, is_query: bool) -> list:
    """Encode with ``model`` on the calling thread; CPU tensors back."""

    import torch

    with torch.inference_mode():
        out = model.encode(
            texts,
            is_query=is_query,
            batch_size=ENCODE_BATCH,
            show_progress_bar=False,
            convert_to_numpy=False,
        )
    return [_tensor(embedding) for embedding in out]


def _agreement(reference, candidate) -> float:
    """Lowest mean token cosine between two encoders' encodings of the probe texts
    (0 if any text gets a different number of tokens)."""

    import torch

    worst = 1.0
    for is_query, texts in ((True, _PROBE_QUERIES), (False, _PROBE_DOCUMENTS)):
        for a, b in zip(
            _encode_now(reference, texts, is_query=is_query),
            _encode_now(candidate, texts, is_query=is_query),
        ):
            if a.shape != b.shape:
                return 0.0
            cosine = torch.nn.functional.cosine_similarity(a.float(), b.float(), dim=-1)
            worst = min(worst, float(cosine.mean()))
    return worst


def _timings(model) -> tuple[float, float]:
    """Median seconds for one short query and for one background-sized document batch."""

    import statistics

    def timed(fn) -> float:
        started = time.perf_counter()
        fn()
        return time.perf_counter() - started

    query = statistics.median(
        timed(lambda: _encode_now(model, [_PROBE_QUERIES[0]], is_query=True)) for _ in range(3)
    )
    # Two rounds: the probe batch takes ~1-2 s on a slow CPU, and loading waits for this.
    documents = min(
        timed(lambda: _encode_now(model, _PROBE_DOCUMENTS, is_query=False)) for _ in range(2)
    )
    return query, documents


def _accelerate(cpu_model, settings: Settings, load=_load_accelerator) -> None:
    """Route queries and/or document batches to a GPU encoder where it wins here.

    ``load(kind, cpu_model, settings, precision)`` builds a candidate. Never raises:
    any problem leaves everything on the CPU.
    """

    mode = (settings.embeddings_device or "auto").lower()
    if mode == "cpu":
        return
    if mode not in ("auto", *ACCELERATORS):
        _log.warning("Unknown EMBEDDINGS_DEVICE=%s; ColBERT runs on the CPU", mode)
        return
    kinds = ACCELERATORS if mode == "auto" else (mode,)
    precisions = ["float32"] if settings.embeddings_precision == "float32" else ["float16", "float32"]
    cpu_timings = None
    for kind in kinds:
        if not _accelerator_available(kind):
            if mode != "auto":
                _log.warning("EMBEDDINGS_DEVICE=%s is unavailable; ColBERT runs on the CPU", kind)
            continue
        try:
            for precision in precisions:
                candidate = load(kind, cpu_model, settings, precision)
                _encode_now(candidate, ["warm up"], is_query=True)
                _encode_now(candidate, ["warm up the documents"], is_query=False)
                cosine = _agreement(cpu_model, candidate)
                if cosine >= ACCEL_MIN_COSINE:
                    break
                candidate = None
            if candidate is None:
                _log.warning("ColBERT on %s disagrees with the CPU (cosine %.4f); not used", kind, cosine)
                continue
            if mode == "auto":
                _timings(candidate)  # one more round of set-up for these shapes
                cpu_timings = cpu_timings or _timings(cpu_model)
                gpu_timings = _timings(candidate)
                roles = {
                    role
                    for role, cpu_s, gpu_s in zip(("query", "documents"), cpu_timings, gpu_timings)
                    if gpu_s * ACCEL_MIN_SPEEDUP <= cpu_s
                }
            else:
                gpu_timings, roles = None, {"query", "documents"}
            _log.info(
                "ColBERT %s %s: cosine %.5f, seconds (query, 4 paragraphs) CPU %s / GPU %s; "
                "used for %s",
                kind, precision, cosine, cpu_timings, gpu_timings, sorted(roles) or "nothing",
            )
            if not roles:
                continue
            devices = {role: kind if role in roles else "cpu" for role in ("query", "documents")}
            _cache.routes = _Routes(
                candidate if "query" in roles else cpu_model,
                candidate if "documents" in roles else cpu_model,
                devices,
            )
            return
        except Exception:  # noqa: BLE001 - the CPU always works
            _log.warning("ColBERT on %s unavailable; trying the next option", kind, exc_info=True)
        finally:
            _empty_mps_cache()


def _empty_mps_cache() -> None:
    try:
        import torch

        if _mps_available():
            torch.mps.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _fall_back_to_cpu(error: BaseException) -> None:
    """A GPU encoder failed mid-run: everything goes to the CPU model, for good."""

    if _cache.routes is None:
        return
    _log.warning("ColBERT on the GPU failed (%s: %s); using the CPU from now on",
                 type(error).__name__, error)
    _cache.routes = None
    _empty_mps_cache()


def device_info() -> dict[str, str] | None:
    """Where queries and documents are encoded (None until the model is loaded)."""

    if not _cache.loaded:
        return None
    if _cache.routes is None:
        return {"query": "cpu", "documents": "cpu"}
    return dict(_cache.routes.devices)


def _bf16_supported() -> bool:
    try:
        flags = Path("/proc/cpuinfo").read_text()
    except OSError:
        return False
    return "avx512_bf16" in flags or "amx_bf16" in flags


def _get_model(wait: bool = False):
    """The ColBERT model, or None if disabled, unavailable or (``wait=False``) not loaded yet.

    Request paths call it without ``wait`` and never block: until the model is
    ready they score with keywords. Only the background indexer (``load_model``)
    loads it.
    """
    settings = get_settings()
    if not settings.embeddings_enabled or _cache.failed:
        return None
    if _cache.model is not None or not wait:
        return _cache.model
    with _model_lock:
        if _cache.model is not None or _cache.failed:
            return _cache.model
        try:
            import torch
            from pylate import models  # heavy import; do it lazily

            # One encode at a time runs (the encoder thread); more threads than
            # this oversubscribe the cores that requests and OCR also need.
            torch.set_num_threads(Settings.default_threads(settings.embeddings_threads))
            # Explicitly on the CPU: PyLate would otherwise pick MPS by itself on a Mac
            # (with no check that it is faster or right). _accelerate decides that.
            model = models.ColBERT(model_name_or_path=settings.colbert_model, device="cpu")
            _cache.dtype = torch.float32
            if isinstance(model, torch.nn.Module):
                model.eval()
                precision = settings.embeddings_precision
                if precision == "bfloat16" or (precision == "auto" and _bf16_supported()):
                    model = model.to(torch.bfloat16)
                    _cache.dtype = torch.bfloat16
                with torch.inference_mode():  # First calls pay one-off kernel set-up.
                    model.encode(["warm up"], is_query=True, show_progress_bar=False)
                    model.encode(["warm up the documents"], is_query=False, show_progress_bar=False)
            _cache.model = model
            _cache.routes = None
            if isinstance(model, torch.nn.Module):
                # On the model thread: MLX state stays on the thread that uses it.
                _encoder.run(lambda: _accelerate(model, settings), _FOREGROUND)
            _cache.loaded = True
            return _cache.model
        except Exception:
            _log.warning("ColBERT model unavailable; using keyword search", exc_info=True)
            _cache.failed = True
            return None


def load_model():
    """Load the model (blocking). For the background indexer and scripts only."""

    return _get_model(wait=True)


def model_if_ready():
    """The model if it is loaded, else None (never blocks)."""

    return _get_model()


def available() -> bool:
    """Whether scoring uses the model right now (False while it is still loading)."""

    return _get_model() is not None


def scoring_state(corpus) -> object | None:
    """A token that stays equal while scores against ``corpus`` cannot change.

    None while they may (the model is loading, or paragraphs are still being
    encoded): results are then not worth caching. Brings the index up to date
    with ``corpus`` first when that is cheap: only the notes changed since the
    last sync are compared, and a few edited paragraphs are encoded right away
    (ahead of background work) rather than left for the indexer.
    """

    if not get_settings().embeddings_enabled or _cache.failed:
        return ("keywords",)
    model = _get_model()
    if model is None:
        return None
    if _cache.synced_corpus is not corpus:
        synced = _cache.synced_corpus
        if not (
            isinstance(corpus, corpus_store.Corpus)
            and isinstance(synced, corpus_store.Corpus)
            and corpus_store.changed_between(synced, corpus) is not None
        ):
            return None  # a full pass (cold start): the background indexer's job
        with _index_lock:
            _load_store()
            missing = _sync(corpus)
        if missing and len(missing) <= INLINE_ENCODE_LIMIT:
            _add(model, missing, background=False)
            with _index_lock:
                missing = _sync(corpus)
        if missing:
            return None
    return ("model",)


def _tensor(embedding):
    import numpy as np
    import torch

    if isinstance(embedding, torch.Tensor):
        tensor = embedding.detach().cpu()
    else:
        tensor = torch.from_numpy(np.asarray(embedding, dtype=np.float32))
    tensor = tensor.reshape(-1, tensor.shape[-1]) if tensor.dim() != 2 else tensor
    dtype = _cache.dtype
    if dtype is None:
        dtype = torch.bfloat16 if tensor.dtype == torch.bfloat16 else torch.float32
        _cache.dtype = dtype
    return tensor.to(dtype).contiguous()


def _encode(model, texts: list[str], *, is_query: bool, background: bool | None = None) -> list:
    """Encode on the model thread; background work (default: documents) goes a batch
    at a time behind any waiting search."""

    if background is None:
        background = not is_query
    priority = _BACKGROUND if background else _FOREGROUND

    def encode_with(target, batch: list[str]) -> list:
        import torch

        with torch.inference_mode():
            return list(
                target.encode(
                    batch,
                    is_query=is_query,
                    batch_size=ENCODE_BATCH,
                    show_progress_bar=False,
                    convert_to_numpy=False,
                )
            )

    def run(batch: list[str]) -> list:
        routes = _cache.routes
        if routes is None or model is not _cache.model:
            return encode_with(model, batch)
        target = routes.query if is_query else routes.documents
        if target is model:
            return encode_with(model, batch)
        try:
            # Back on the CPU here, on the model thread, while the GPU encoder is in use.
            return [embedding.detach().cpu() for embedding in encode_with(target, batch)]
        except Exception as error:  # noqa: BLE001 - the CPU model takes over
            _fall_back_to_cpu(error)
            return encode_with(model, batch)

    out: list = []
    for batch in _batches(texts, BACKGROUND_BATCH_CHARS if background else None):
        out.extend(_encoder.run(lambda batch=batch: run(batch), priority))
    return [_tensor(embedding) for embedding in out]


def _batches(texts: list[str], max_chars: int | None):
    """Up to ``ENCODE_BATCH`` texts per batch and, with ``max_chars``, about that much
    text: a search waits for at most one background batch, so keep them short."""

    batch: list[str] = []
    size = 0
    for text in texts:
        if batch and (
            len(batch) >= ENCODE_BATCH or (max_chars is not None and size + len(text) > max_chars)
        ):
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        yield batch


def tokenized_lengths(texts: list[str], *, is_query: bool = True) -> list[int] | None:
    """Token counts of ``texts`` as the model reads them (no forward pass), or None
    while the model isn't loaded. Runs on the model thread like every encode."""

    model = _get_model()
    if model is None:
        return None

    def run() -> list[int]:
        return [len(ids) for ids in model.tokenize(texts, is_query=is_query)["input_ids"]]

    return _encoder.run(run, _FOREGROUND)


def _text_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def _remember(cache: OrderedDict, key, value, size: int) -> None:
    with _queries_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > size:
            cache.popitem(last=False)


def _recall(cache: OrderedDict, key):
    with _queries_lock:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
        return value


def _query_embedding(model, query: str, *, paragraph: bool = False):
    """Query encoding of ``query``: a typed query, or (``paragraph``) a paragraph's text."""

    cache, size = (
        (_cache.paragraph_queries, PARAGRAPH_CACHE_SIZE)
        if paragraph
        else (_cache.queries, QUERY_CACHE_SIZE)
    )
    cached = _recall(cache, query)
    if cached is not None:
        return cached
    embedding = _saved_query(query)
    if embedding is None:
        embedding = _encode(model, [query], is_query=True)[0]
    _remember(cache, query, embedding, size)
    return embedding


def query_token_count(query: str) -> int:
    """How many query vectors MaxSim sums over (divide a score by it for 0-1ish)."""

    model = _get_model()
    if model is None:
        return max(len(_tokens(query)), 1)
    return int(_query_embedding(model, query).shape[0])


# ---- persistent store ---------------------------------------------------------


def _store_path() -> Path:
    return get_settings().embeddings_path


def _store_key() -> str:
    dtype = str(_cache.dtype or "torch.float32")
    return f"{_STORE_FORMAT}|{get_settings().colbert_model}|{dtype}"


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS embeddings (
               block_id TEXT PRIMARY KEY, text_hash TEXT NOT NULL,
               tokens INTEGER NOT NULL, dim INTEGER NOT NULL, vectors BLOB NOT NULL)"""
    )
    # Query-mode encodings of paragraph texts, precomputed in the background so
    # "related to this paragraph" needs no model pass (keyed by text hash).
    connection.execute(
        """CREATE TABLE IF NOT EXISTS query_embeddings (
               text_hash TEXT PRIMARY KEY, tokens INTEGER NOT NULL, dim INTEGER NOT NULL,
               vectors BLOB NOT NULL)"""
    )
    connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return connection


def _saved_query(query: str):
    """The precomputed query encoding of ``query`` if it is a paragraph's text."""

    path = _cache.store_path
    if path is None or not _cache.query_hashes:
        return None
    text_hash = _text_hash(query)
    if text_hash not in _cache.query_hashes:
        return None
    try:
        with closing(sqlite3.connect(path, timeout=5)) as connection:
            row = connection.execute(
                "SELECT tokens, dim, vectors FROM query_embeddings WHERE text_hash = ?",
                (text_hash,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else _tensor(_from_bytes(row[2], row[0], row[1]))


def _to_bytes(embedding) -> bytes:
    import torch

    if embedding.dtype == torch.bfloat16:
        return embedding.contiguous().view(torch.int16).numpy().tobytes()
    return embedding.float().numpy().tobytes()


def _from_bytes(blob: bytes, tokens: int, dim: int):
    import numpy as np
    import torch

    if len(blob) == tokens * dim * 2:
        array = np.frombuffer(blob, dtype=np.int16).reshape(tokens, dim)
        return torch.from_numpy(array.copy()).view(torch.bfloat16)
    array = np.frombuffer(blob, dtype=np.float32).reshape(tokens, dim)
    return torch.from_numpy(array.copy())


def _load_store() -> None:
    """Load persisted embeddings once (keyed by text hash; versions filled on sync)."""

    path = _store_path()
    if _cache.store_loaded and _cache.store_path == path:
        return
    _cache.index = _Index()
    _cache.query_hashes = set()
    _cache.synced_corpus = None
    _cache.store_path = path
    _cache.store_loaded = True
    if path is None:
        return
    try:
        with closing(_connect(path)) as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
            if row is None or row[0] != _store_key():
                connection.execute("DELETE FROM embeddings")
                connection.execute("DELETE FROM query_embeddings")
                connection.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('format', ?)", (_store_key(),)
                )
                connection.commit()
                return
            for block_id, text_hash, tokens, dim, blob in connection.execute(
                "SELECT block_id, text_hash, tokens, dim, vectors FROM embeddings"
            ):
                _cache.index.put(block_id, text_hash, "", _tensor(_from_bytes(blob, tokens, dim)))
            _cache.index.trim()
            _cache.query_hashes = {
                row[0] for row in connection.execute("SELECT text_hash FROM query_embeddings")
            }
    except (sqlite3.Error, ValueError, RuntimeError):
        _log.warning("Embedding cache unreadable; rebuilding it", exc_info=True)
        _cache.index = _Index()


def _persist(rows: list[tuple[str, str, object]], removed: list[str]) -> None:
    path = _cache.store_path
    if path is None or (not rows and not removed):
        return
    try:
        with closing(_connect(path)) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO meta VALUES ('format', ?)", (_store_key(),)
            )
            connection.executemany(
                "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?, ?)",
                [
                    (block_id, text_hash, int(emb.shape[0]), int(emb.shape[1]), _to_bytes(emb))
                    for block_id, text_hash, emb in rows
                ],
            )
            connection.executemany(
                "DELETE FROM embeddings WHERE block_id = ?", [(block_id,) for block_id in removed]
            )
    except sqlite3.Error:
        _log.warning("Could not save embeddings; they'll be recomputed", exc_info=True)


def _sync(corpus) -> list[tuple[str, str, str]]:
    """Match the index to ``corpus``; returns paragraphs that still need encoding.

    After the first full pass only the notes that changed since the last synced
    snapshot are compared (``corpus.changed_between``), so an edit costs a look
    at one note, not at every paragraph.
    """

    index = _cache.index
    missing: list[tuple[str, str, str]] = []
    synced = _cache.synced_corpus
    if corpus is synced:
        return missing
    changed = None
    if isinstance(corpus, corpus_store.Corpus) and isinstance(synced, corpus_store.Corpus):
        changed = corpus_store.changed_between(synced, corpus)
    if changed is None:
        rows = corpus
        candidates = None  # every indexed paragraph may be gone
    else:
        rows = [row for note in changed if note in corpus.notes for row in corpus.notes[note].rows]
        candidates = [
            row[0] for note in changed if note in synced.notes for row in synced.notes[note].rows
        ]
    live: set[str] = set()
    for block_id, _document_id, version, text in rows:
        if not textutil.long_enough_to_compare(text):
            continue  # too short to match by meaning; dropped from the index below
        live.add(block_id)
        entry = index.where.get(block_id)
        if entry is not None and entry[3] == version:
            continue
        text_hash = _text_hash(text)
        if entry is not None and entry[2] == text_hash:
            index.set_version(block_id, version)
            continue
        missing.append((block_id, version, text))
    stale = [
        block_id
        for block_id in (index.where if candidates is None else candidates)
        if block_id not in live and block_id in index.where
    ]
    for block_id in stale:
        index.remove(block_id)
    _persist([], stale)
    if not missing:
        _cache.synced_corpus = corpus
    return missing


def _add(model, docs: list[tuple[str, str, str]], *, background: bool = True) -> None:
    embeddings = _encode(
        model, [text for _id, _version, text in docs], is_query=False, background=background
    )
    rows = []
    with _index_lock:
        for (block_id, version, text), embedding in zip(docs, embeddings):
            text_hash = _text_hash(text)
            _cache.index.put(block_id, text_hash, version, embedding)
            rows.append((block_id, text_hash, embedding))
        _persist(rows, [])


# ---- keyword fallback ---------------------------------------------------------

@lru_cache(maxsize=1 << 16)
def _tokens(text: str) -> frozenset[str]:
    # Same words as the full-text search: any script, case and accents ignored.
    # Cached: without the model every search tokenized every paragraph again.
    return frozenset(block_query.query_tokens(text))


def _keyword_score(q: set[str], text: str) -> float:
    if not q:
        return 0.0
    overlap = q & _tokens(text)
    return len(overlap) / len(q)


# ---- public scoring API -------------------------------------------------------


def warm_index(time_limit: float | None = None, *, queries: bool = True) -> int:
    """Encode any paragraphs whose embedding is missing or stale; drop deleted ones.

    Returns the number of paragraphs (re)encoded. Loads persisted embeddings on the
    first call, so after a restart only paragraphs edited meanwhile are encoded.
    Cheap when nothing changed, so it's fine to call on a short interval. Work is
    committed in small chunks and pauses while searches run. Once every paragraph
    is indexed (and with ``queries``), paragraphs also get their query encoding
    precomputed, so asking what relates to a paragraph needs no model pass.
    """
    model = load_model()
    if model is None:
        return 0
    corpus = corpus_store.snapshot(get_settings().block_db_path)
    with _index_lock:
        _load_store()
        missing = _sync(corpus)
    if not missing:
        if queries and get_settings().precompute_query_embeddings:
            _warm_queries(model, time_limit)
        return 0
    started = time.monotonic()
    missing.sort(key=lambda doc: len(doc[2]))
    encoded = 0
    for start in range(0, len(missing), INDEX_CHUNK):
        while background.foreground_active():
            time.sleep(0.005)
        _add(model, missing[start : start + INDEX_CHUNK])
        encoded += len(missing[start : start + INDEX_CHUNK])
        if time_limit is not None and time.monotonic() - started > time_limit:
            break
    if encoded > INDEX_CHUNK:
        with _index_lock:
            _cache.index.trim()
    return encoded


class Reindexer(background.Service):
    """The ``index`` lane: keeps embeddings current so searches stay instant.

    Runs from startup: the first unit loads the model and the saved embeddings
    (and, on a first run, starts encoding everything). Each unit encodes for
    about :data:`SLICE_S`, so pausing (a restore) and searches wait little.
    """

    name = "index"
    priority = background.LOW
    SLICE_S = 2.0

    def step(self) -> float | None:
        settings = get_settings()
        model = load_model()
        if model is None:
            return None  # disabled or unavailable: nothing to do until restarted
        if warm_index(self.SLICE_S, queries=False):
            return 0
        if settings.precompute_query_embeddings and _warm_queries(model, self.SLICE_S):
            return 0
        return settings.reindex_interval_s


background.register(Reindexer())


def _warm_queries(model, time_limit: float | None = None) -> int:
    """Precompute query encodings for indexed paragraphs; forget those of old texts."""

    path = _cache.store_path
    if path is None:
        return 0
    with _index_lock:
        live = {entry[2] for entry in _cache.index.where.values()}
        gone = _cache.query_hashes - live
        todo: dict[str, str] = {}
        if len(_cache.query_hashes) - len(gone) < len(live):
            for _block_id, _document_id, _version, text in corpus_store.snapshot(
                get_settings().block_db_path
            ):
                text_hash = _text_hash(text)
                if text_hash in live and text_hash not in _cache.query_hashes and text.strip():
                    todo[text_hash] = text
    if gone:
        try:
            with closing(_connect(path)) as connection, connection:
                connection.executemany(
                    "DELETE FROM query_embeddings WHERE text_hash = ?", [(h,) for h in gone]
                )
            _cache.query_hashes -= gone
        except sqlite3.Error:
            pass
    pending = sorted(todo.items(), key=lambda item: len(item[1]))
    started = time.monotonic()
    done = 0
    for start in range(0, len(pending), INDEX_CHUNK):
        while background.foreground_active():
            time.sleep(0.005)
        chunk = pending[start : start + INDEX_CHUNK]
        encoded = _encode(model, [text for _hash, text in chunk], is_query=True, background=True)
        try:
            with closing(_connect(path)) as connection, connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO query_embeddings VALUES (?, ?, ?, ?)",
                    [
                        (text_hash, int(emb.shape[0]), int(emb.shape[1]), _to_bytes(emb))
                        for (text_hash, _text), emb in zip(chunk, encoded)
                    ],
                )
        except sqlite3.Error:
            return done
        _cache.query_hashes.update(text_hash for text_hash, _text in chunk)
        done += len(chunk)
        if time_limit is not None and time.monotonic() - started > time_limit:
            break
    return done


def score_documents(
    query: str,
    docs: list[tuple[str, str, str]],
    *,
    normalized: bool = False,
    paragraph: bool = False,
) -> dict[str, float]:
    """Score `query` against docs = [(id, updated_at_iso, text)]. Returns {id: score}.

    Uses ColBERT MaxSim (the sum over query tokens of each one's best match in the
    paragraph) when available, otherwise keyword overlap (share of query words
    found, 0-1). With ``normalized=True`` MaxSim is divided by the number of query
    tokens, i.e. the mean best cosine similarity (about 0-1), so a fixed relevance
    threshold works for short and long queries alike. ``paragraph`` marks the
    query as a paragraph's text (cached apart from typed queries).
    """
    docs = [doc for doc in docs if textutil.long_enough_to_compare(doc[2])]
    model = _get_model()
    if model is None:
        query_words = _tokens(query)
        return {d[0]: _keyword_score(query_words, d[2]) for d in docs}
    if not docs:
        return {}

    query_emb = _query_embedding(model, query, paragraph=paragraph)
    scores = _score_with(model, query_emb, docs)
    if normalized:
        count = max(int(query_emb.shape[0]), 1)
        scores = {block_id: score / count for block_id, score in scores.items()}
    return scores


def _paragraph_encodings(model, texts: list[str]) -> dict[str, object]:
    """Query encodings of paragraph texts: cached, saved, or encoded in one batch."""

    found: dict[str, object] = {}
    missing = []
    for text in dict.fromkeys(texts):
        cached = _recall(_cache.paragraph_queries, text)
        if cached is None:
            cached = _saved_query(text)
        if cached is None:
            missing.append(text)
        else:
            found[text] = cached
    if missing:
        found.update(zip(missing, _encode(model, missing, is_query=True)))
    for text, embedding in found.items():
        _remember(_cache.paragraph_queries, text, embedding, PARAGRAPH_CACHE_SIZE)
    return found


def note_query_embedding(texts: list[str]):
    """One query for a whole note: its paragraphs' query encodings, joined.

    Paragraph query encodings are precomputed and saved in the background, so this
    usually needs no model pass, where encoding the note's full text as one long
    query took hundreds of milliseconds. Missing ones are encoded in one batch.
    """

    import torch

    model = _get_model()
    texts = [text for text in texts if text.strip()]
    if model is None or not texts:
        return None
    found = _paragraph_encodings(model, texts)
    return torch.cat([found[text] for text in texts], dim=0)


def note_query(texts: list[str]):
    """A whole note as at most ``NOTE_QUERY_VECTORS`` weighted query vectors.

    Returns (vectors, weights or None, token count): the paragraphs' query
    vectors (``note_query_embedding``), clustered when there are more than
    ``NOTE_QUERY_VECTORS``. A cluster's vector is the mean of its members and its
    weight their number, so weighted MaxSim stays on the scale of the plain sum
    over every token (and is a lower bound of it). Cached per note text.
    """

    texts = [text for text in texts if text.strip()]
    if not texts or _get_model() is None:
        return None
    key = hashlib.blake2b("\x00".join(texts).encode("utf-8"), digest_size=16).digest()
    cached = _recall(_cache.note_queries, key)
    if cached is not None:
        return cached
    joined = note_query_embedding(texts)
    if joined is None:
        return None
    tokens = int(joined.shape[0])
    vectors, weights = _cluster(joined, NOTE_QUERY_VECTORS)
    result = (vectors, weights, tokens)
    _remember(_cache.note_queries, key, result, NOTE_CACHE_SIZE)
    return result


def _cluster(vectors, size: int):
    """(vectors, None) if there are at most ``size``, else (centroids, counts) of a
    few rounds of spherical k-means seeded evenly across the note."""

    import torch

    count = int(vectors.shape[0])
    if count <= size:
        return vectors, None
    points = vectors.float()
    seeds = torch.linspace(0, count - 1, size).round().long()
    centroids = torch.nn.functional.normalize(points[seeds], dim=-1)
    for _round in range(NOTE_CLUSTER_ROUNDS):
        assign = (points @ centroids.T).argmax(dim=1)
        sums = torch.zeros_like(centroids).index_add_(0, assign, points)
        filled = torch.bincount(assign, minlength=size) > 0
        centroids = torch.where(
            filled[:, None], torch.nn.functional.normalize(sums, dim=-1), centroids
        )
    assign = (points @ centroids.T).argmax(dim=1)
    counts = torch.bincount(assign, minlength=size)
    sums = torch.zeros_like(centroids).index_add_(0, assign, points)
    keep = counts > 0
    means = sums[keep] / counts[keep, None]
    return means.to(vectors.dtype).contiguous(), counts[keep].float()


def score_note(texts: list[str], docs: list[tuple[str, str, str]]) -> tuple[dict[str, float], int]:
    """Raw MaxSim of a whole note (see ``note_query``) and its query token count."""

    texts = [text for text in texts if textutil.long_enough_to_compare(text)]
    docs = [doc for doc in docs if textutil.long_enough_to_compare(doc[2])]
    model = _get_model()
    query = note_query(texts) if model is not None and texts else None
    if query is None or not docs:
        return {}, 0
    vectors, weights, tokens = query
    return _score_with(model, vectors, docs, weights), tokens


def _score_with(model, query_emb, docs: list[tuple[str, str, str]], weights=None) -> dict[str, float]:
    """MaxSim of a ready query embedding against ``docs``, indexing stale ones as needed."""

    with background.foreground():
        with _index_lock:
            _load_store()
            where = _cache.index.where
            versions = _cache.index.versions.get
            changed = [doc for doc in docs if versions(doc[0]) != doc[1]]
            stale, blank = [], set()
            for block_id, version, text in changed:
                entry = where.get(block_id)
                if not text.strip():
                    blank.add(block_id)
                elif entry is not None and entry[2] == _text_hash(text):
                    _cache.index.set_version(block_id, version)
                else:
                    stale.append((block_id, version, text))
        if stale and len(stale) <= INLINE_ENCODE_LIMIT:
            _add(model, stale, background=False)
            stale = []
        blank.update(block_id for block_id, _version, _text in stale)
        with _index_lock:
            scores = _score_indexed(query_emb, docs, blank, weights)
    return scores


def _score_indexed(query_emb, docs, unscored: set[str], weights=None) -> dict[str, float]:
    """MaxSim for ``docs``; 0 for those in ``unscored`` (blank or not encoded yet)."""

    index = _cache.index
    if not len(index):
        return dict.fromkeys((doc[0] for doc in docs), 0.0)
    if len(docs) * 8 < len(index):
        where = index.where
        wanted = [doc[0] for doc in docs if doc[0] in where and doc[0] not in unscored]
        # A handful of paragraphs (filtered search, tests): score them one by one.
        scores = dict.fromkeys((doc[0] for doc in docs), 0.0)
        scores.update(zip(wanted, index.maxsim_some(query_emb, wanted, weights)))
        return scores
    tokens = int(query_emb.shape[0])
    if tokens > LONG_QUERY_TOKENS:
        where = index.where
        wanted = [doc[0] for doc in docs if doc[0] in where and doc[0] not in unscored]
        scores = dict.fromkeys((doc[0] for doc in docs), 0.0)
        step = -(-tokens // PREFILTER_TOKENS)
        if weights is None:
            ids, rough = index.maxsim_all(query_emb[::step])
            rough = rough * (tokens / query_emb[::step].shape[0])
        else:
            ids, rough = index.maxsim_all(query_emb[::step], weights[::step])
            rough = rough * float(weights.sum() / weights[::step].sum())
        estimate = dict(zip(ids, rough.tolist()))
        candidates = sorted(wanted, key=lambda block_id: -estimate[block_id])
        exact = candidates[:PREFILTER_CANDIDATES]
        rest = candidates[PREFILTER_CANDIDATES:]
        # A sample of the rest, scored exactly too, puts the estimates on the exact
        # scale: the relevance cutoff compares against the median of all scores,
        # and raw estimates sit well below exact scores.
        sample = rest[:: max(1, len(rest) // CALIBRATION_SAMPLE)] if CALIBRATE else []
        exact_scores = index.maxsim_some(query_emb, exact + sample, weights)
        factor = 1.0
        if sample:
            ratios = sorted(
                score / estimate[block_id]
                for block_id, score in zip(sample, exact_scores[len(exact):])
                if estimate[block_id] > 0
            )
            if ratios:
                factor = ratios[len(ratios) // 2]
        # Paragraphs outside the shortlist keep the (scaled) prefilter estimate,
        # capped below the shortlist so the ranking stays exact at the top.
        floor = None
        for block_id, score in zip(exact, exact_scores):
            scores[block_id] = score
            floor = score if floor is None else min(floor, score)
        for block_id in rest:
            scores[block_id] = min(estimate[block_id] * factor, floor) if floor is not None else 0.0
        return scores
    ids, values = index.maxsim_all(query_emb, weights)
    all_scores = dict(zip(ids, values.tolist()))
    get = all_scores.get
    scores = {doc[0]: get(doc[0], 0.0) for doc in docs}
    for block_id in unscored:
        if block_id in scores:
            scores[block_id] = 0.0
    return scores
