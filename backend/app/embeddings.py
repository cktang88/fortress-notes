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
* Query encodings are cached (LRU), so refetching the same related list or
  search costs only the scoring.

Edits are applied incrementally: ``warm_index()`` (run on a short interval in the
background) encodes changed paragraphs in batches and drops removed ones;
``score_documents`` itself encodes at most a handful of stale paragraphs inline
and scores paragraphs that are still waiting for the background indexer as 0.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from . import block_query
from .config import get_settings

_log = logging.getLogger("fortress")

# Paragraphs encoded inline by a search when the background indexer is behind.
INLINE_ENCODE_LIMIT = 8
# Paragraphs per model batch and per commit while indexing in the background.
ENCODE_BATCH = 16
INDEX_CHUNK = 64
QUERY_CACHE_SIZE = 256
# Long queries (a whole note) are scored in two stages: a strided subset of their
# tokens ranks every paragraph, then the best candidates get exact MaxSim.
LONG_QUERY_TOKENS = 96
PREFILTER_TOKENS = 32
PREFILTER_CANDIDATES = 600
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

    def maxsim_all(self, query) -> tuple[list[str | None], object]:
        """Exact MaxSim of ``query`` [nq, dim] against every row: (ids, scores)."""

        import torch

        ids: list[str | None] = []
        parts = []
        query_t = query.T.contiguous()
        for bucket in self.buckets.values():
            used = len(bucket.ids)
            if not used:
                continue
            # One multiply per bucket (chunking it into cache-sized blocks measured slower).
            rows = bucket.rows[:used]
            sims = (rows.reshape(-1, rows.shape[-1]) @ query_t).view(used, bucket.length, -1)
            parts.append(sims.amax(dim=1).sum(dim=1, dtype=torch.float32))
            ids.extend(bucket.ids)
        if not parts:
            return [], torch.empty(0)
        return ids, torch.cat(parts)

    def maxsim_some(self, query, block_ids: list[str]) -> list[float]:
        """Exact MaxSim for some paragraphs, batched per bucket."""

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
            sims = (rows.reshape(-1, rows.shape[-1]) @ query_t).view(len(positions), length, -1)
            for position, score in zip(
                positions, sims.amax(dim=1).sum(dim=1, dtype=torch.float32).tolist()
            ):
                out[position] = score
        return out


@dataclass
class _Cache:
    model: object | None = None
    loaded: bool = False
    failed: bool = False
    index: _Index = field(default_factory=_Index)
    store_path: Path | None = None
    store_loaded: bool = False
    synced_corpus: object | None = None
    queries: OrderedDict = field(default_factory=OrderedDict)
    query_hashes: set[str] = field(default_factory=set)
    dtype: object | None = None


_cache = _Cache()
# The background reindexer and request threads can all reach for the model first.
_model_lock = threading.Lock()
# Guards the index and the persistent store (scoring takes a few tens of ms).
_index_lock = threading.RLock()
# Searches in flight; the background indexer pauses between batches while any are.
_active_queries = 0
_active_lock = threading.Lock()


def model_loaded() -> bool:
    return _cache.loaded


def _bf16_supported() -> bool:
    try:
        flags = Path("/proc/cpuinfo").read_text()
    except OSError:
        return False
    return "avx512_bf16" in flags or "amx_bf16" in flags


def _get_model():
    """Lazily load the ColBERT model. Returns None if disabled or unavailable."""
    settings = get_settings()
    if not getattr(settings, "embeddings_enabled", True) or _cache.failed:
        return None
    if _cache.model is not None:
        return _cache.model
    with _model_lock:
        if _cache.model is not None or _cache.failed:
            return _cache.model
        try:
            import torch
            from pylate import models  # heavy import; do it lazily

            model = models.ColBERT(model_name_or_path=settings.colbert_model)
            _cache.dtype = torch.float32
            if isinstance(model, torch.nn.Module):
                model.eval()
                precision = getattr(settings, "embeddings_precision", "auto")
                if precision == "bfloat16" or (precision == "auto" and _bf16_supported()):
                    model = model.to(torch.bfloat16)
                    _cache.dtype = torch.bfloat16
                with torch.inference_mode():  # First calls pay one-off kernel set-up.
                    model.encode(["warm up"], is_query=True, show_progress_bar=False)
                    model.encode(["warm up the documents"], is_query=False, show_progress_bar=False)
            _cache.model = model
            _cache.loaded = True
            return _cache.model
        except Exception:
            _log.warning("ColBERT model unavailable; using keyword search", exc_info=True)
            _cache.failed = True
            return None


def available() -> bool:
    return _get_model() is not None


def _tensor(embedding):
    import numpy as np
    import torch

    if isinstance(embedding, torch.Tensor):
        tensor = embedding.detach()
    else:
        tensor = torch.from_numpy(np.asarray(embedding, dtype=np.float32))
    tensor = tensor.reshape(-1, tensor.shape[-1]) if tensor.dim() != 2 else tensor
    dtype = _cache.dtype
    if dtype is None:
        dtype = torch.bfloat16 if tensor.dtype == torch.bfloat16 else torch.float32
        _cache.dtype = dtype
    return tensor.to(dtype).contiguous()


def _encode(model, texts: list[str], *, is_query: bool) -> list:
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


def _text_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def _query_embedding(model, query: str):
    cached = _cache.queries.get(query)
    if cached is not None:
        _cache.queries.move_to_end(query)
        return cached
    embedding = _saved_query(query)
    if embedding is None:
        embedding = _encode(model, [query], is_query=True)[0]
    _cache.queries[query] = embedding
    while len(_cache.queries) > QUERY_CACHE_SIZE:
        _cache.queries.popitem(last=False)
    return embedding


def query_token_count(query: str) -> int:
    """How many query vectors MaxSim sums over (divide a score by it for 0-1ish)."""

    model = _get_model()
    if model is None:
        return max(len(_tokens(query)), 1)
    return int(_query_embedding(model, query).shape[0])


# ---- persistent store ---------------------------------------------------------


def _store_path() -> Path | None:
    db_path = getattr(get_settings(), "block_db_path", None)
    return Path(db_path).with_name(_STORE_NAME) if db_path else None


def _store_key() -> str:
    settings = get_settings()
    dtype = str(_cache.dtype or "torch.float32")
    return f"{_STORE_FORMAT}|{getattr(settings, 'colbert_model', '')}|{dtype}"


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
    """Match the index to ``corpus``; returns paragraphs that still need encoding."""

    index = _cache.index
    missing: list[tuple[str, str, str]] = []
    if corpus is _cache.synced_corpus:
        return missing
    live: set[str] = set()
    for block_id, _document_id, version, text in corpus:
        live.add(block_id)
        entry = index.where.get(block_id)
        if entry is not None and entry[3] == version:
            continue
        text_hash = _text_hash(text)
        if entry is not None and entry[2] == text_hash:
            index.set_version(block_id, version)
            continue
        missing.append((block_id, version, text))
    stale = [block_id for block_id in index.where if block_id not in live]
    for block_id in stale:
        index.remove(block_id)
    _persist([], stale)
    if not missing:
        _cache.synced_corpus = corpus
    return missing


def _add(model, docs: list[tuple[str, str, str]]) -> None:
    embeddings = _encode(model, [text for _id, _version, text in docs], is_query=False)
    rows = []
    with _index_lock:
        for (block_id, version, text), embedding in zip(docs, embeddings):
            text_hash = _text_hash(text)
            _cache.index.put(block_id, text_hash, version, embedding)
            rows.append((block_id, text_hash, embedding))
        _persist(rows, [])


# ---- keyword fallback ---------------------------------------------------------

def _tokens(text: str) -> set[str]:
    # Same words as the full-text search: any script, case and accents ignored.
    return set(block_query.query_tokens(text))


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
    model = _get_model()
    if model is None:
        return 0
    from . import block_store  # local import to avoid an import cycle

    corpus = block_store.embedding_documents(get_settings().block_db_path)
    with _index_lock:
        _load_store()
        missing = _sync(corpus)
    if not missing:
        if queries and getattr(get_settings(), "precompute_query_embeddings", True):
            _warm_queries(model, time_limit)
        return 0
    started = time.monotonic()
    missing.sort(key=lambda doc: len(doc[2]))
    encoded = 0
    for start in range(0, len(missing), INDEX_CHUNK):
        while _active_queries:
            time.sleep(0.005)
        _add(model, missing[start : start + INDEX_CHUNK])
        encoded += len(missing[start : start + INDEX_CHUNK])
        if time_limit is not None and time.monotonic() - started > time_limit:
            break
    if encoded > INDEX_CHUNK:
        with _index_lock:
            _cache.index.trim()
    return encoded


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
            from . import block_store

            for _block_id, _document_id, _version, text in block_store.embedding_documents(
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
        while _active_queries:
            time.sleep(0.005)
        chunk = pending[start : start + INDEX_CHUNK]
        encoded = _encode(model, [text for _hash, text in chunk], is_query=True)
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
    query: str, docs: list[tuple[str, str, str]], *, normalized: bool = False
) -> dict[str, float]:
    """Score `query` against docs = [(id, updated_at_iso, text)]. Returns {id: score}.

    Uses ColBERT MaxSim (the sum over query tokens of each one's best match in the
    paragraph) when available, otherwise keyword overlap (share of query words
    found, 0-1). With ``normalized=True`` MaxSim is divided by the number of query
    tokens, i.e. the mean best cosine similarity (about 0-1), so a fixed relevance
    threshold works for short and long queries alike.
    """
    model = _get_model()
    if model is None:
        query_words = _tokens(query)
        return {d[0]: _keyword_score(query_words, d[2]) for d in docs}
    if not docs:
        return {}

    query_emb = _query_embedding(model, query)
    scores = _score_with(model, query_emb, docs)
    if normalized:
        count = max(int(query_emb.shape[0]), 1)
        scores = {block_id: score / count for block_id, score in scores.items()}
    return scores


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
    found: dict[str, object] = {}
    missing = []
    for text in dict.fromkeys(texts):
        cached = _cache.queries.get(text)
        if cached is None:
            cached = _saved_query(text)
        if cached is None:
            missing.append(text)
        else:
            found[text] = cached
    if missing:
        found.update(zip(missing, _encode(model, missing, is_query=True)))
    for text, embedding in found.items():
        _cache.queries[text] = embedding
        _cache.queries.move_to_end(text)
    while len(_cache.queries) > QUERY_CACHE_SIZE:
        _cache.queries.popitem(last=False)
    return torch.cat([found[text] for text in texts], dim=0)


def score_note(texts: list[str], docs: list[tuple[str, str, str]]) -> tuple[dict[str, float], int]:
    """Raw MaxSim of a whole note (see ``note_query_embedding``) and its query token count."""

    model = _get_model()
    query_emb = note_query_embedding(texts)
    if model is None or query_emb is None or not docs:
        return {}, 0
    return _score_with(model, query_emb, docs), int(query_emb.shape[0])


def _score_with(model, query_emb, docs: list[tuple[str, str, str]]) -> dict[str, float]:
    """MaxSim of a ready query embedding against ``docs``, indexing stale ones as needed."""

    global _active_queries
    with _active_lock:
        _active_queries += 1
    try:
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
            _add(model, stale)
            stale = []
        blank.update(block_id for block_id, _version, _text in stale)
        with _index_lock:
            scores = _score_indexed(query_emb, docs, blank)
    finally:
        with _active_lock:
            _active_queries -= 1
    return scores


def _score_indexed(query_emb, docs, unscored: set[str]) -> dict[str, float]:
    """MaxSim for ``docs``; 0 for those in ``unscored`` (blank or not encoded yet)."""

    index = _cache.index
    if not len(index):
        return dict.fromkeys((doc[0] for doc in docs), 0.0)
    if len(docs) * 8 < len(index):
        where = index.where
        wanted = [doc[0] for doc in docs if doc[0] in where and doc[0] not in unscored]
        # A handful of paragraphs (filtered search, tests): score them one by one.
        scores = dict.fromkeys((doc[0] for doc in docs), 0.0)
        scores.update(zip(wanted, index.maxsim_some(query_emb, wanted)))
        return scores
    tokens = int(query_emb.shape[0])
    if tokens > LONG_QUERY_TOKENS:
        where = index.where
        wanted = [doc[0] for doc in docs if doc[0] in where and doc[0] not in unscored]
        scores = dict.fromkeys((doc[0] for doc in docs), 0.0)
        step = -(-tokens // PREFILTER_TOKENS)
        ids, rough = index.maxsim_all(query_emb[::step])
        rough = rough * (tokens / query_emb[::step].shape[0])
        estimate = dict(zip(ids, rough.tolist()))
        candidates = sorted(wanted, key=lambda block_id: -estimate[block_id])
        exact = candidates[:PREFILTER_CANDIDATES]
        # Paragraphs outside the shortlist keep the (scaled) prefilter estimate,
        # capped below the shortlist so the ranking stays exact at the top.
        floor = None
        for block_id, score in zip(exact, index.maxsim_some(query_emb, exact)):
            scores[block_id] = score
            floor = score if floor is None else min(floor, score)
        for block_id in candidates[PREFILTER_CANDIDATES:]:
            scores[block_id] = min(estimate[block_id], floor) if floor is not None else 0.0
        return scores
    ids, values = index.maxsim_all(query_emb)
    all_scores = dict(zip(ids, values.tolist()))
    get = all_scores.get
    scores = {doc[0]: get(doc[0], 0.0) for doc in docs}
    for block_id in unscored:
        if block_id in scores:
            scores[block_id] = 0.0
    return scores
