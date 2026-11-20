# Fortress Notes — Spec

## 1. Goal

A local-first block-centric notes app where **SQLite is the live source of truth** for
documents, nested blocks, references, and indexes. Markdown remains the portable
import/export format. The native Mac app provides one search box (full text +
ColBERT late-interaction embedding search + titles, no mode toggle), live related
results, nested folders, a rough→polished workflow, Ask (cited answers from your
notes), text read from every picture and PDF, and lossless OneNote import.

Non-goals: multi-user, real-time collaboration, mobile, cloud sync, tags, pinned notes,
whole-workspace (zip) export. The backend serves one local user; its only access
control is the per-launch token described in §3 ("Local auth").

### Principle: open standards first

Wherever people or other tools see what Fortress writes or reads — the notes folder,
exports, imports, the API — it uses the open or de-facto standard (CommonMark, GFM,
Obsidian Flavored Markdown, YAML front matter, ISO 8601, OpenAPI, ...) instead of
inventing its own. Our own data goes only where no standard can say it, under a
`fortress` namespace that other readers don't see. Companion principle, **least
surprise**: things behave as in Apple's apps and Obsidian, and standard Mac behaviour
(⌘C/⌘V and pasteboard types, screenshots, system shortcuts, Quick Look, the share
sheet) is never overridden. The policy and the decision for every format are in
**[docs/open-standards.md](docs/open-standards.md)**.

## 2. Data model

SQLite (`NOTES_DIR/.fortress.sqlite3`) is the only source of truth: documents,
folders, nested blocks with stable IDs, references, history, assets, OCR text and the
FTS index. Every write goes to SQLite first. Each active note also gets a read-only
Markdown mirror, so notes stay readable outside the app: the notes folder is an
**Obsidian vault** of plain CommonMark/GFM/Obsidian Markdown (`vault.py`,
docs/open-standards.md). A note is `NOTES_DIR/<folder>/<subfolder>/<Title>.md`
(its Fortress folders; names other tools can't use are cleaned, clashes get " 2");
renames and moves rename the file and rewrite the wikilinks to it. Writers never
touch mirrors: the `mirrors` background lane (`documents.MirrorWriter`) follows
SQLite's change log, rewrites a changed note's mirror atomically (coalesced, about
0.5 s after the edit) and removes it when the note is trashed. The app never reads
the mirrors back; only the explicit Markdown importer reads `.md` files. A mirror
looks like this (extra front-matter keys added by hand are preserved):

```markdown
---
title: My note
aliases: [My note?]      # only when the file name differs from the title
created: '2026-06-20T10:00:00+00:00'
updated: '2026-06-20T10:05:00+00:00'
id: 01HZX...             # ULID
status: rough            # "rough" | "polished"
source: https://...      # only for a note that came from a web page
fortress:
  provenance:            # where the note came from (§7a)
    source: manual
  picture_text:          # text read from each picture/PDF it shows (see §6)
    01J....png: "Q1 revenue"
---

The note body: see [[Other note]], [[Other note#^01J...|that paragraph]].

![chart|300](assets/01J....png)
```

`.fortress-mirrors.json` (change-log position and vault format version) and
`.fortress-mirror-files.json` (each note's path, link name and referenced blocks)
let a restart rewrite only what changed; a new `vault.FORMAT_VERSION` rewrites every
mirror once (format 1, `NOTES_DIR/<id>.md`, is removed then).

Pasted images are stored as files in `NOTES_DIR/assets/<id>.<ext>` (imported ones as
`<sha256>.<ext>`); the API serves them at `/media/…`, and Markdown embeds them as
`![alt|width](assets/<name>)` relative to the note (docs/block-model.md §7).

- **id**: ULID (sortable, unique), in the mirror's front matter.
- **status**: `rough` (braindump/brainstorm) or `polished`. Drives UI badge + filter
  and the "promote to polished" action.
- **title**: if empty, derived from the first heading/line of the body.
- Timestamps are UTC ISO-8601. `updated_at` is set on every save.

On first start an empty database imports the Markdown files of `NOTES_DIR` once
(subfolders become folders; a note keeps its front-matter `id`), copying the
originals to a timestamped backup folder first.

## 2a. Block shapes

The block model is specified in **[docs/block-model.md](docs/block-model.md)**, the
contract between the backend and every client. In short: a block is a `type`
from one registry (`block_schema.py`, served at `GET /api/block-schema`), `attrs`,
inline `content` (text blocks: text/link/docLink/blockRef nodes with marks) and a
`payload` (math, html). Tables, columns, quotes and callouts are container blocks
with child blocks. Siblings are ordered by fractional `order` keys
(`order_keys.py`). The server derives `text` (plain, no Markdown: FTS, embeddings,
Ask and titles read it), Markdown (`block_markdown.py`), links, backlinks and asset
links; it rejects unknown types, illegal nesting and cross-category type changes
with **422**.

## 3. Backend (FastAPI, Python / uv)

### Modules (`backend/app/`)
- `config.py` — settings, read once from the environment and `backend/.env`
  (`pydantic-settings`).
- `auth.py` — local auth middleware (token, browser and host checks) and the
  parent-process watch (see "Local auth" below).
- `models.py` — Pydantic schemas (`NoteCreate`, `NoteUpdate`, `BlockTransaction`,
  `RelatedResult`, `ExternalOcrResult`, ...).
- `block_store.py` — SQLite schema, migrations, blocks, transactions, FTS, change log.
- `block_schema.py` — the block type registry and inline content rules (§2a).
- `block_query.py` — filtered block-level full-text queries.
- `order_keys.py` — fractional sibling order keys.
- `block_markdown.py` — Markdown (CommonMark/GFM/Obsidian) in and out of canonical blocks.
- `vault.py` — the notes folder as an Obsidian vault: file names, front matter,
  wikilink resolution across an import.
- `block_legacy.py`, `block_migration.py` — the schema-14 migration of stored
  data in older block shapes (the API accepts canonical shapes only).
- `documents.py` — note lifecycle (create/update/trash/restore/purge/capture)
  and the Markdown mirror writer.
- `workspace.py` — the Trash listing. `backups.py` — snapshots and restore.
- `portability.py` — Markdown import (files, uploads, first-start import) and export.
- `background.py` — the one owner of background work (lanes, below).
- `corpus.py` — the incremental search corpus every search reader shares.
- `textutil.py` — shared text rules (words, stopwords, case/accent folding, which
  text of a block is searchable, snippets).
- `embeddings.py` — ColBERT late-interaction via PyLate; encode + MaxSim scoring,
  with a keyword fallback when disabled/unavailable.
- `relevance.py` — the cutoff that decides which "related" paragraphs are related.
- `search.py` — unified search (words + meaning + titles) and related notes.
- `ask.py` / `llm.py` — Ask your notes over OpenRouter chat completions.
- `ocr.py`, `images.py`, `assets.py` — text in pictures and PDFs (§6), uploaded files.
- `provenance.py`, `sources.py` — provenance and living documents (§7a).
- `main.py` — FastAPI app and routes.
- `document_service.py` — rendered note bodies and the one document ETag, shared by
  the single GET, the batch read and the writes that answer with a tree.
- `http_cache.py` — ETags, `If-None-Match` → 304, rendered-body caches, API gzip.
- `media.py` — `/media` file serving (immutable caching, ranges, content types).
- `batch.py` — batch reads and moves for the native app.
- `onenote_import.py` — OneNote importer: runs `fortress-onenote`, imports its manifest as living documents, import jobs (§7b).

### API

| Method | Path | Body / Query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/health` | — | `{status, embeddings_enabled, model_loaded, vision_enabled, vision_loaded, block_db_ready}` (open without the token) |
| `GET` | `/api/block-schema` | — | the block type registry (docs/block-model.md) |
| `GET` | `/api/navigation` | `?recent_limit=` | folder tree + recent notes |
| `POST` · `PATCH` · `DELETE` | `/api/folders` · `/api/folders/{id}` | `{name, parent_id?, position?}` · `{name}` · — | folder (folders nest); `DELETE` answers 409 unless the folder is empty |
| `POST` | `/api/folders/{id}/move` · `/api/block-documents/{id}/move` | `{parent_id, position?}` · `{folder_id, position?}` | the moved item |
| `POST` | `/api/block-documents` | `NoteCreate` | `201` document tree; header `X-Document-ETag` |
| `GET` | `/api/block-documents/{id}` | `If-None-Match` | document tree (`ETag`), or `304`; never writes |
| `PATCH` | `/api/block-documents/{id}` | `{title?, status?}` (anything else: 422) | metadata payload (`children: []`); header `X-Document-ETag`. Title `""` hands it back to the first line |
| `DELETE` | `/api/block-documents/{id}` | — | `204` (to the Trash) |
| `POST` | `/api/block-documents/{id}/opened` | — | `204`; records `last_opened_at` (debounced, skipped while a writer is busy; not in the ETag) |
| `POST` | `/api/block-documents/{id}/undo` · `/redo` | `{base_revision}` | document tree; header `X-Document-ETag` |
| `GET` | `/api/block-documents/{id}/related` | `?k=5&block_id=` | `RelatedResult[]` |
| `GET` | `/api/block-documents/{id}/backlinks` | — | backlinks |
| `GET` | `/api/block-link-targets` | `?q=&limit=` | link autocomplete |
| `GET` | `/api/unified-search` | `?q=&limit=` + filters | ranked blocks (words, meaning, titles) |
| `POST` | `/api/ask` | `{question}` (`Accept: application/x-ndjson` streams) | cited answer |
| `POST` | `/api/capture` | `{text, provenance?}` | `201 {document_id, title, folder}` |
| `POST` | `/api/files` | multipart `file` | `{props: {url, name}}` (images and PDFs get OCR in the background) |
| `GET` | `/api/ocr` | — | `{enabled, model, queued, done, failed, running}` (background OCR state) |
| `GET` | `/api/ocr/pending` | `?model=apple-vision/…&limit=8` | pictures/PDFs an external reader has yet to read (§6) |
| `GET` · `PUT` | `/api/ocr/{content_hash}` | — · `{model, status, text, method, pages, error, duration_ms}` | all text read from one picture/PDF · store an external reader's result (§6) |
| `GET` | `/media/{file}` | — | stored file bytes (ranges, immutable caching; see below) |
| `POST` | `/api/batch/block-documents` | `{ids: [≤50], known: {id: ETag}}` | `{documents: [tree], etags: {id: ETag}, unchanged: [id], missing: [id]}`; never writes |
| `POST` | `/api/batch/move` | `{documents: [{id, folder_id}], folders: [{id, parent_id}]}` | `{folders, documents}` (counts moved) |
| `POST` | `/api/block-documents/{id}/transactions` | `BlockTransaction`, `?response=meta\|document` | metadata payload (`children: []`, the default) or the tree; header `X-Document-ETag` |
| `GET` | `/api/trash` | — | trashed documents, newest first |
| `POST` | `/api/trash/{id}/restore` | — | `{id, folder_id, position}` (also rewrites the Markdown mirror) |
| `DELETE` | `/api/trash/{id}` · `/api/trash` | — | delete one forever · empty the trash |
| `GET` · `POST` | `/api/backups` | — | list snapshots · take a manual snapshot |
| `POST` | `/api/backups/{name}/restore` | — | `{restored, safety_backup}` |
| `GET` | `/api/block-documents/{id}/markdown` | `?download=true` | one note as Markdown (with provenance, §7a) |
| `GET` | `/api/block-documents/{id}/info` | — | note metadata: dates, provenance, counts, conflicts (§7a) |
| `POST` | `/api/block-documents/{id}/blocks/{block_id}/source-conflict` | `{keep}` | document tree (§7a) |
| `POST` | `/api/markdown-import/files` | multipart `files[]`, `folder_id?` | `{imported, errors}` |
| `POST` | `/api/import/onenote` | multipart `file` (+ `folder_id?`) or JSON `{path, folder_id?}` | `202 {job_id, status: "queued"}` (§7b) |
| `GET` | `/api/import/onenote/{job_id}` | — | import job (§7b) |
| `GET` | `/api/import/onenote` | `?limit=20` | recent import jobs, newest first |
| `POST` | `/api/import/onenote/{job_id}/cancel` | — | `202` the job |

### HTTP caching, compression, errors

- **Validators.** These GETs carry a strong `ETag` and `Cache-Control: no-cache`
  (keep, but revalidate); a request whose `If-None-Match` matches gets `304` with no
  body:
  - `GET /api/block-documents/{id}` — changes with anything in the payload: the note's
    own rows (blocks, user attrs, links, history, title/status/provenance/...)
    and every linked note or block (titles and text in
    `resolved_links`, `[[id|Title]]` labels).
  - `GET /api/navigation` (per `recent_limit`), `GET /api/trash`,
    `GET /api/block-documents/{id}/info`, `GET /api/block-documents/{id}/backlinks`
    — change with any change in the workspace (they show other notes); `/info` also
    with `last_opened_at`.
  - One document validator everywhere: the batch read's `known`/`etags` are these
    ETags, and every write that answers with a note sends `X-Document-ETag`.
  - `GET /api/backups` — changes with the snapshot list.

  The server tracks changes with SQLite triggers (`change_log.seq`, an `epoch` renewed
  by every restore, and `documents.change_seq`), so writes from any code path — and
  restores — invalidate validators. Other API responses also get
  `Cache-Control: no-cache` unless they set their own (the Ask stream: `no-store`).
- **Reads never write.** `GET /api/block-documents/{id}` and the batch read change
  nothing (no `last_opened_at`, no repair): the app records an open with
  `POST .../opened`, and the empty paragraph an empty note needs is made at create,
  import and restore (and once at startup for older data). A write's
  `X-Document-ETag` is the ETag the next GET of that note answers 304 to.
- **Server caches.** Rendered bodies are cached per validator (documents: 256 entries
  / 64 MB; others: 256 / 32 MB), so repeated GETs skip SQLite and JSON encoding.
- **gzip.** `/api` responses of 1 KB or more are gzipped when the client sends
  `Accept-Encoding: gzip` (`Vary: Accept-Encoding`). A gzipped response's ETag gets a
  `-gzip` suffix (`"abc-gzip"`); `If-None-Match` accepts either form. Never
  compressed: the Ask NDJSON stream (stays unbuffered), media, octet-stream, PDF and
  SQLite downloads.
- **Media.** `/media/<name>` files named by a ULID (uploads) or SHA-256 (imports) never
  change: `Cache-Control: public, max-age=31536000, immutable`, `ETag` = the SHA-256
  for content-addressed names (else size/mtime), `Last-Modified`, `Accept-Ranges`
  (`Range` → 206), explicit `Content-Type` (e.g. `.m4a` `audio/mp4`, `.webm`
  `audio/webm`), `X-Content-Type-Options: nosniff`. Text sidecars (`<id>.txt`) are
  `no-cache`. Uploads (`/api/files`) stream to disk while hashing.
- **Errors** are JSON `{"detail": ...}`: unknown routes 404, a database still locked
  after the 5 s busy timeout `503` with `Retry-After: 1`, anything unexpected `500`
  `{"detail": "Internal server error"}`.
- **SQLite.** WAL, `synchronous=NORMAL`, 16 MB page cache, 256 MB mmap, 5 s busy
  timeout, one pooled connection per worker thread (prepared statements reused).
  FTS rows share their block's rowid, so an edit re-indexes only the blocks it wrote.
  Markdown mirrors of edited notes are written in the background (coalesced, 0.5 s).

### Local auth (`auth.py`)

The backend listens on loopback, where web pages can reach it too, so:

- Requests a browser made (an `Origin` header, or `Sec-Fetch-Site` other than `none`)
  are refused (403) everywhere.
- With `FORTRESS_TOKEN` in the environment (the Mac app makes a fresh random one per
  launch), every request needs it as `X-Fortress-Token: <token>` or
  `Authorization: Bearer <token>` (else 401); only `GET /api/health` is open.
- Without a token (tests, a backend started by hand) the `Host` must be a loopback
  name (`FORTRESS_ALLOWED_HOSTS` adds more), and importing OneNote by server-side
  path is refused (uploads still work).
- Every answer carries `X-Fortress-Instance` (the first 16 hex digits of the token's
  SHA-256, or a random id per process without one), so the app only talks to the
  backend it started.
- With `FORTRESS_PARENT_PID` the backend stops itself once that process is gone; on
  shutdown it stops running OneNote converters.

### Background work (`background.py`)

Everything done outside a request runs on a *lane* (its own thread, never the
executor that serves search and Ask):

| lane | priority | work |
| --- | --- | --- |
| `mirrors` | high | Markdown mirrors follow SQLite (`documents.MirrorWriter`) |
| `imports` | normal | OneNote import jobs, one at a time (§7b) |
| `backups` | normal | the daily snapshot (`AUTO_BACKUP_ENABLED`) |
| `ocr` | low | text in pictures and PDFs (§6; `VISION_ENABLED`) |
| `index` | low | ColBERT embeddings (`embeddings.Reindexer`; `EMBEDDINGS_ENABLED`) |

Low-priority lanes wait briefly while a search is scoring. A restore pauses every lane;
shutdown stops them (and any converter) and writes pending mirrors.

### Search corpus, unified search and related notes

- **Corpus** (`corpus.py`): one list of `(block_id, document_id, version, text)` rows
  for every active paragraph, where text is `textutil.searchable_text` (the block's
  text plus the text read from its pictures/PDFs). It is updated by deltas
  (`change_log.seq`, `documents.change_seq`, `assets.text_updated_at`), not rebuilt;
  search, related notes, Ask's grep and the indexer all read the same snapshot.
- **Embeddings** (`embeddings.py`): `lightonai/Agent-ModernColBERT` (`COLBERT_MODEL`)
  via PyLate, one multi-vector document per paragraph, scored with exact **MaxSim**
  (brute force, one matrix multiply per padded length bucket). Embeddings persist in
  `NOTES_DIR/.fortress-embeddings.sqlite3` (keyed by block id and a hash of the text),
  bfloat16 where the CPU supports it (`EMBEDDINGS_PRECISION`). The `index` lane loads
  the model and encodes changed paragraphs every `REINDEX_INTERVAL_S` (default 5 s);
  until the model is loaded every scorer falls back to keyword overlap.
- **Unified search** (`GET /api/unified-search`): FTS5 words, ColBERT meaning and note
  titles each rank blocks; reciprocal rank fusion combines them (a title containing
  every query word weighs more), with a small boost for recent edits and a cap on
  blocks per note. Each result says why it matched and carries a snippet. There is
  no mode parameter: one box, one ranked list.
- **Related notes** (`GET …/related`): notes like the whole note, or like one
  paragraph with `block_id` (the app's "Seen before"). The current note is excluded;
  `relevance.py` keeps only results that beat what an unrelated paragraph would score
  for this query (cutoff 0.18, calibrated in `scripts/calibrate_related.py`).

### Ask (`ask.py`, `llm.py`)

`POST /api/ask {question}`; with `Accept: application/x-ndjson` the answer streams as
`sentence`, `step` and finally `done` events.

1. Candidates are gathered locally first (tens of ms): a literal grep for the
   question's key words and unified search, in parallel.
2. One streamed OpenRouter call (`ASK_MODEL`, default `anthropic/claude-haiku-5.5`,
   `ASK_REASONING_EFFORT`) sees those passages and answers, or asks for one more
   `grep` across every note or `read` of a whole note.
3. Every sentence must cite a verbatim quote; a sentence whose quote isn't in the
   cited passage is dropped. Checked sentences are sent as soon as they are complete.
4. A 3-second budget: when time runs short the model must answer with what it has;
   if it is too slow, the closest passages (and any sentences already checked) are
   returned. Without `OPENROUTER_API_KEY` the result is `not_configured`.

Only passages (the part of a long paragraph around the matched words) are sent to the
model, never the whole workspace.

## 4. Client

The native macOS app in `mac/` (SwiftUI + TextKit 2, macOS 26) is the only client;
see `mac/DESIGN.md`. It starts the backend itself (`mac/Sources/FortressKit/Shell/
BackendController.swift`: the repository's `backend/.venv/bin/uvicorn`, else
`uv run --frozen uvicorn`, with `FORTRESS_TOKEN`, `FORTRESS_PARENT_PID` and
`FORTRESS_ONENOTE_BIN`) and reads pictures and PDFs with Apple Vision (§6).

## 6. Images & OCR (`ocr.py`, `images.py`)

- **Paste/drop**: the editor uploads pasted or dropped files with `POST /api/files` and
  inserts an image or file block with the returned `/media/…` URL. Markdown stays
  small — images are files.
- **Fast & non-blocking**: `/api/files` saves the file and returns **immediately**
  (~ms); OCR runs later on the `ocr` background lane, never on a request.
- **OCR pipeline** (`ocr.py`): every image (png/jpeg/gif/webp/bmp/tiff) and PDF
  asset — pasted, uploaded or imported from OneNote — is read once per content
  SHA-256 and OCR model id (`ocr.MODEL`). Images: RapidOCR with PP-OCRv6 small
  (ONNX Runtime on CPU, models bundled in the `rapidocr` wheel, no torch), EXIF
  rotation applied, transparency flattened onto white, detected boxes grouped into
  lines. PDFs: the embedded text layer (pypdfium2); a page is rendered (≤2000 px)
  and OCR'd only when its text layer is (nearly) empty, or short with a picture
  covering ≥25% of the page (≤100 OCR'd pages per PDF).
- **Queue = SQLite**: `ocr_results(content_hash, model, status, text, method, pages,
  attempts, error, …)`. An asset is pending while no `done`/`failed` row exists for its
  hash and the current model, so the queue survives restarts with nothing to replay,
  a changed model re-reads everything lazily (the old text stays searchable meanwhile;
  old-model rows are dropped once re-read) and a cache hit costs nothing. The `ocr`
  lane works through it one asset at a time: newest assets first, pictures whose every
  block already has OneNote's OCR text (`alt`) last; woken by uploads and imports, and
  it polls once a minute for assets registered any other way. `running` is written before the work,
  so an asset that crashes the process is retried at most 3 times.
- **Searchable, not shown**: the text goes to `assets.text`, which feeds each owning
  block's FTS row, its ColBERT document (the block's version includes the asset's
  `text_updated_at`, so only that block is re-encoded), Ask's grep corpus and the
  sources Ask shows the model (`[Text in attached picture/PDF]`), and unified-search
  snippets when the match is in the picture. OneNote alt text stays in the block's own
  text, so both are indexed. `GET …/info` lists `picture_text` per picture/PDF block
  (status, method, pages, model, text) and `GET /api/ocr` the queue state.
- **Apple Vision from the Mac app** (`mac/Sources/FortressKit/Finding/PictureText/`,
  Settings › General › *Text in Pictures*): the app asks `GET /api/ocr/pending?model=
  apple-vision/<macOS major>` for what it hasn't read (newest first, a few at a time),
  reads each file with Vision (more scripts, handwriting) and stores the result with
  `PUT /api/ocr/{content_hash}`. Which text wins: a reading that found text beats one
  that found none, then an external reader beats the CPU model, which beats its own
  older versions, then the newest. While an external reader is active the CPU queue
  leaves the backlog to it, and it never reads an asset a preferred reader has
  finished.
- Older `<id>.txt` sidecars are migrated into `assets.text` once by
  `backfill_assets` (until the queue re-reads the file).

## 7. Workspace safety and organization

- **Trash**: deleting a note soft-deletes it in SQLite. The app shows an Undo toast;
  the Trash restores notes or deletes them forever.
- **Backups**: a background task keeps one automatic snapshot per day in
  `NOTES_DIR/.fortress-backups` (last 7 daily, 10 manual, 5 pre-restore). Restoring
  verifies the snapshot, saves the current state as `pre-restore` first, and moves
  Markdown mirrors of documents that are not in the snapshot aside rather than
  deleting them. Set `AUTO_BACKUP_ENABLED=false` to turn off daily snapshots.
- **Import/export**: File ▸ Import brings in `.md` files (Obsidian notes included:
  wikilinks between the files resolve) or a OneNote export; each note exports as
  Markdown. The notes folder itself is an Obsidian vault (§2).

## 7a. Provenance, metadata and living documents

Every note and every block records where it came from (`provenance.py`, schema v13).
Notes store it in `documents.provenance_json` (plus `source_kind` /
`source_external_id`, the re-import key, unique when set); blocks in
`blocks.provenance_json`, where NULL means "same as the note" (the blocks a note
was created with). Provenance is never part of block text, FTS or embeddings.

```jsonc
// provenance (all keys but "source" optional)
{
  "source": "onenote_import",   // manual | capture | markdown_import | onenote_import | paste | web | ai | <any lower-case word>
  "external_id": "{GUID}",      // id in the source; (source, external_id) keys re-imports
  "source_path": "Work › Meetings › Standup",  // or a file path
  "source_url": "https://…",
  "source_created_at": "…", "source_modified_at": "…",  // the source's own timestamps
  "imported_at": "…", "synced_at": "…",                  // first import, last re-import that changed it
  "importer": {"name": "onenote", "version": "1.0"},
  "origin": {"operation": "move_into", "document_id": "…",   // "duplicate"/"split" are still accepted
             "document_title": "…", "block_id": "…", "at": "…"},
  "extra": {},                   // importer-specific
  // server-maintained on blocks: "source_hash", "conflict", "source_removed", "dismissed_hash";
  // on notes: "source_removed" + "source_removed_at" (the source no longer has the page;
  // sources.mark_documents_source_removed, cleared by the next upsert);
  // "backfilled": true marks the migration's guess
}
```

- **Document payload** (`GET /api/block-documents/{id}` and every tree response) adds
  `provenance`, `last_opened_at` (set by `POST …/opened`, at most once a minute) and
  `resolved_links: {documents: {id: {title}}, blocks: {id: {document_id, document_title, type, text}}}`.
  `[[id|Title]]` links are served with the target's current title (stored text is
  untouched). Each block adds `provenance` (its own, or the note's with
  `"inherited": true`) and `revision` (the note revision that last changed it; null
  if unchanged since creation/import).
- **Navigation / list items** add `source` (the note's provenance kind).
- **Writes**: `NoteCreate.provenance` (e.g. `{"source": "ai", "extra": {"question": …}}`
  for a saved Ask answer), `CaptureRequest.provenance` (e.g. `source_url`), and
  `BlockOperation.provenance` on `insert` (default `{"source": "manual"}`) and
  `update` (omitted keeps it). Client input is validated (422/409) and cannot set the
  server-maintained keys. Blocks folded in by the former move-into keep
  `origin.operation = "move_into"`.
- **Markdown**: export and mirror front matter carry `fortress: {provenance: …}`
  (and `source:` for a `source_url`); a block whose provenance differs from the
  note's carries it in an invisible comment, e.g.
  `<!-- fortress:block {"provenance":{"external_id":"{GUID}","source":"onenote_import"}} -->`.
  Import (directory, upload, mirrors) restores both, so export → import → export is
  identical. A copy whose source key is already taken gives it up
  (`extra.copy_of_external_id`).

`GET /api/block-documents/{id}/info`:

```jsonc
{
  "id", "title", "status", "created_at", "updated_at", "last_opened_at", "revision",
  "folder_id", "folder_path": [{"id", "name"}],          // top folder first
  "provenance": {…},
  "counts": {"words", "characters", "blocks", "outgoing_links", "outgoing_document_links",
             "outgoing_block_links", "backlinks", "edits"},  // backlinks from other notes
  "blocks_by_source": {"manual": 3, "onenote_import": 12},
  "can_undo", "can_redo",
  "sync": null | {"synced_at", "checked_at", "source_title", "blocks_edited_here",
                  "blocks_removed_in_source": [block_id]},
  "conflicts": [{"kind": "title", "local_title", "source_title", "detected_at"} |
                {"kind": "block", "block_id", "local_text", "source_markdown",
                 "source_modified_at", "detected_at"}]
}
```

`POST /api/block-documents/{id}/blocks/{block_id}/source-conflict` `{"keep": "source"|"local"}`
settles a block conflict (taking the source's version is an ordinary undoable edit).

**Living documents** (`sources.py`): importers call
`documents.upsert_from_source(source=, external_id=, title=, blocks=, folder_path=,
created_at=, modified_at=, provenance=, importer=)` (or
`sources.upsert_document_from_source(db_path, …)`), with `blocks` a list of
`SourceBlock(markdown, external_id, type, attrs, children, created_at, modified_at,
provenance, content, payload, previous_external_ids)` or a Markdown body. A SourceBlock carries either
Markdown or a canonical `type` + `attrs`/`content`/`payload` (§2a; text is derived).
`sources.source_block(dict)` / `source_blocks([dict])` build them from plain dicts:

```python
source_blocks([
  {"id": "{P1}", "type": "heading", "level": 2, "inline": [{"text": "Plan", "u": 1}]},
  {"id": "{P2}", "text": "plain paragraph"},
  {"id": "{P3}", "type": "list", "checked": True, "inline": [{"text": "done", "s": 1}]},
  {"id": "{T1}", "type": "table", "borders": True, "column_widths": [96, None],
   "header_rows": 0, "rows": [["a", {"inline": [...], "background": "#DEEAF6",
     "blocks": [{"type": "list", "text": "in cell"}, {"type": "table", "rows": [["x"]]}]}]]},
  {"id": "{O1}", "type": "columns", "columns": [
     {"id": "{O1a}", "width": 0.5, "blocks": [{"text": "left"}]},
     {"id": "{O1b}", "width": 0.5, "blocks": [{"type": "image", "url": "/media/…",
                                                "alt": "OCR", "width": 300}]}]},
  {"id": "{F1}", "type": "file", "url": "/media/….pdf", "name": "a.pdf", "size": 1, "mime": "application/pdf"},
  {"id": "{M1}", "type": "math", "latex": "a^2"},
])
```

Runs may be canonical or compact (`{"text", "b", "i", "u", "s", "sup", "sub", "code",
"color", "highlight", "link"}`; other keys such as `font`/`size` are ignored).
`table_content(rows, column_widths=, header_rows=)` and `nested_block(dict)` build
table content and in-cell blocks; invalid shapes raise `BlockShapeError`. A block
whose structured content changed in the source (a table cell, a column width) is
updated like changed text; conflicts on structured blocks keep the source's
`source_content`/`source_attrs` so `keep: "source"` restores them. `folder_path=[(external_id, name), …]` finds/creates
folders by source id (`sources.ensure_folder_path`). It returns
`UpsertResult(document_id, status=created|updated|unchanged|skipped_deleted, revision,
blocks_created, blocks_updated, blocks_deleted, blocks_skipped, conflicts)`. The note
keeps its Fortress id, links, backlinks and history; a re-import is one undo step.
Policy: untouched blocks follow the source; locally edited blocks keep the local
text and record the source's version as a conflict; local additions stay after the
block they followed; local deletions stick; blocks gone from the source are deleted
unless edited (then marked `source_removed`); structure follows the source; title and
folder follow the source unless changed locally; trashed notes are skipped.

## 7b. OneNote import (`onenote_import.py`)

Background: `docs/onenote-import.md` (F1–F5). The user exports notebooks from
OneNote (web *Export notebook* .zip, OneDrive *Download* .zip, `.onepkg`, `.one`);
the Rust converter `tools/fortress-onenote` turns the export into
`manifest.json` + `assets/<sha256>.<ext>` (contract:
`tools/fortress-onenote/SCHEMA.md`, format version 1; any other version is
refused). Binary lookup: `FORTRESS_ONENOTE_BIN`, else
`tools/fortress-onenote/target/release/fortress-onenote`, else next to the
running Python (`./fortress-onenote`, `../Resources/bin/fortress-onenote` in an
app bundle), else `PATH`. Converter timeout: `FORTRESS_ONENOTE_TIMEOUT_S`
(default 3600).

**Mapping** (all through `sources.upsert_document_from_source`, source
`onenote_import`, §7a — re-imports update in place, keep local edits, report
`unchanged`):

- Notebook › section group › section → folders via `ensure_folder_path`, keyed
  `notebook:<id>`, `group:<id>`, `section:<id>` (the manifest ids: OneNote file
  identities, stable across renames/moves), created under `folder_id` if given.
- Page → note keyed by the page id (GUID; a second copy of the same notebook in
  one export carries `~hash`). Title (derived titles flagged in
  `provenance.extra.onenote.title_derived`), `source_path` "Notebook › Section ›
  Page", `source_url` (pasted-from citation), source created/modified (also the
  note's `created_at`/`updated_at`), importer `{name: "fortress-onenote-import",
  version}`, `extra.onenote: {level, order, parent_page_id, section_path, author,
  notebook_id, section_id}` (the folder source keys the page was imported under).
- **Subpages** stay in the section folder in OneNote order (right after their
  parent); the parent gets a trailing **Subpages** paragraph with a list of
  `[[id|Title]]` links to its direct subpages. (Folders list before notes in
  the sidebar, so a folder per parent page would split parent and subpages;
  `extra.onenote.level/parent_page_id` keep the tree for a later nested view.)
- **Layout**: the manifest's reading-order rows; a `columns` row → `columns` block
  with one `column` per OneNote column (`width` = relative fraction), nested as in
  the manifest; "scattered" bands (> 4 columns) are stacked.
- Blocks (flat with `indent` → nested children): heading (`level`), paragraph,
  `list_item` → `list` (`ordered`, `list_format` = OneNote marker), `check_item` →
  `list` with `checked`/`checked_at`, quote, code, math (`latex` without `\[ \]`,
  `mathml`), table (column widths ×48 px, `borders`, cell backgrounds, the first
  plain paragraph of a cell as its inline content, the rest — lists, images,
  nested tables — as cell `blocks`), image (asset copied, `alt` = OneNote OCR text
  → searchable, width/height in px, `name`, `attrs.link`/`printout_page`/
  `original_url` for EMF/XPS originals), file (asset, name, size, mime;
  `attrs.caption` when the export lacks the data), ink → image of the SVG with
  `attrs.ink` and `alt` = recognised handwriting, embed → paragraph link. Other
  note tags → `attrs.onenote_tags` (labels). Unknown block types keep their text
  and are reported.
- Marks → runs: bold/italic/underline/strike/superscript/subscript, `color`,
  `highlight`, links; monospace fonts → `code`; inline equations become inline
  code `$<LaTeX>$` (there is no inline math node; OneNote's linear text is kept only
  when an equation has no LaTeX); font sizes are dropped.
- Block source ids: `<page>/obj:<object_id>` when the manifest block has a
  OneNote object id (ExGUID `{GUID}&N`, stable across edits — SCHEMA.md
  "Object ids"), so a paragraph edited in OneNote updates the **same** Fortress
  block (id, links, backlinks and history kept; a block edited here keeps the
  local text and records the source's version as a conflict, §7a). Blocks
  without one (older manifests, ambiguous content) use `<page>/<type>:<hash of
  the block's content>` (duplicates get `#n`; a paragraph edited in OneNote then
  becomes a new block). The hash id is passed as the block's previous id
  (`SourceBlock.previous_external_ids`, dict key `previous_ids`), so notes
  imported before object ids existed are matched and re-keyed in place. Block
  provenance keeps `extra.onenote: {object_id, element_id}` (a table also
  `nested`: the ids of the blocks in its cells).
- `onenote:` links → `[[document_id|Title]]` when the target page (by
  `link_target_id`) is in this export or an earlier import. Paragraph links
  (`object-id={GUID}&N`, manifest `onenote.object_ref`) become block references
  `(( block_id ))` — the link previews the paragraph's current text and records
  a backlink — when the target page has a block whose `element_id`/`object_id`
  (or, for a paragraph inside a table cell, the table's `nested` ids) matches
  (`links.block_references`); otherwise they link to the page
  (`links.block_links_as_page_links`).
  Targets created later in the same run are resolved in a second pass (status
  `linking`), so a re-run resolves everything in one pass and stays `unchanged`.
  Unresolved links stay as plain text (the URL is kept in the block's
  `provenance.extra.unresolved_links`) and are listed in the report.
- Assets are copied once to `NOTES_DIR/assets/<sha256>.<ext>` and registered in
  `assets` (dedup by hash). Extensions outside an allowlist (html, js, attachment
  svg, ...) are stored as `.bin` (served as a download); ink `.svg` is served
  with `Content-Security-Policy: sandbox`.
- The same page GUID in two different exports is the same note: the later import
  updates it (and moves it to its folder unless moved locally).
- **Removed in OneNote**: for each notebook in the export, notes imported from
  it earlier (by `extra.onenote.notebook_id`, or for older imports by their
  folder chain) whose page is not in the export are **not deleted**: the note's
  provenance gets `source_removed: true` and `source_removed_at` (first run that
  missed it), and the notebook's report lists it in `pages_removed_in_source`
  (every run while it stays missing; `newly_removed` on the first). Importing
  the page again clears both keys. Section and section-group folders of the
  notebook that the export no longer has are listed in
  `sections_removed_in_source` (folders are kept). Pages of sections that failed
  to read this time are never counted as removed; notebooks not in the export
  are not looked at.
- **OCR**: every imported image and PDF goes through the app's OCR queue (§6);
  images without OneNote OCR text wake it as soon as their page is written
  (`images_ocr_queued` counts them), pictures that have OneNote's text are read
  last.
- **Text coverage**: per page, the words of the manifest's text (text blocks,
  table cells, image alt/OCR and ink text; math excluded) are compared with the
  imported note's blocks (case-folded words weighted by length, as a multiset;
  importer-added subpage lists excluded). Pages below 98 % are listed in
  `low_coverage_pages` so nothing is lost silently (a page edited here can show
  up too).

**Jobs**: `POST /api/import/onenote` takes multipart `file` (`.zip`, `.onepkg`,
`.one`, `.onetoc2`; streamed to `NOTES_DIR/.fortress-imports/<job_id>-upload/`,
max 2 GB, 413 above, 415 other types) or JSON `{"path": "/local/file/or/folder"}`
(local server: any readable path; 400 if missing), plus optional `folder_id`
(404 if unknown), and returns `202 {"job_id", "status": "queued"}`. Jobs run one
at a time in a background thread; uploads and converter output are deleted
afterwards. The job record is written to
`NOTES_DIR/.fortress-imports/<job_id>.json` when queued, on every status
change, about once a second while it runs, and at the end, so `GET` still finds
it after a restart. A record a restart left `queued`/`converting`/`importing`/
`linking` reads as `failed` with `"interrupted": true` and the error "The import
was interrupted by a restart. Import the export again; pages that were already
imported are kept and updated in place." (re-running is idempotent; leftover
upload/work folders are removed).

`POST /api/import/onenote/{job_id}/cancel` → `202` with the record: a queued
job becomes `cancelled` at once; a running one gets `"cancel_requested": true`,
stops the converter or stops between pages, then reads `cancelled` with error
"Import cancelled. Pages imported before cancelling are kept." (pass 2 link
updates are skipped; import again to finish). `404` unknown job, `409` already
ended.

```jsonc
// GET /api/import/onenote/{job_id}   (GET /api/import/onenote → [job, …] newest first)
{
  "job_id": "01J…",
  "status": "queued|converting|importing|linking|done|failed|cancelled",
  "progress": {"pages_total": 120, "pages_done": 37, "current": "Work › Meetings › Standup"},
  "result": null | {
    "notebooks": [{
      "name", "folder_id", "sections",
      "pages_created", "pages_updated", "pages_unchanged", "pages_skipped",  // skipped: trashed here or failed
      "images", "files", "tables", "columns_pages",
      "ink", "math", "check_items", "conflicts",                            // extras
      "images_ocr_queued": 2,
      "low_coverage_pages": [{"page": "Work › A › Page", "document_id", "coverage": 0.93,
                              "manifest_chars": 1200, "imported_chars": 1116}],
      "pages_removed_in_source": [{"document_id", "title", "source_path",
                                   "source_removed_at", "newly_removed": true}],
      "sections_removed_in_source": [{"folder_id", "name", "path": "Work › Old",
                                      "kind": "section|section_group"}],
      "warnings": ["Page title: message", …],
      "failed_sections": [{"path": "Work › Locked", "reason", "hint"}]
    }],
    "links": {"rewritten": 3, "block_references": 2, "block_links_as_page_links": 1,
              "unresolved": [{"page": "Work › A › Page", "target": "onenote:…", "title": "…"}]},
    "errors_txt": "…",             // OneNote web export's errors.txt, if any
    "input_warnings": [], "skipped_files": [], "pages_total": 120,
    "duration_ms": 5321
  },
  "error": null | "message",
  "input_name": "Work.zip", "folder_id": null, "created_at": "…", "finished_at": null | "…",
  "cancel_requested": true,   // only after a cancel request on a running job
  "interrupted": true          // only on a job a restart cut short
}
```

## 8. Roadmap / potential features

- **PLAID/Voyager index** for embedding search at larger scale (brute-force MaxSim is
  fine at personal scale).
- Larger leaderboard-class OCR/VLM (e.g. PaddleOCR-VL) as an opt-in tier.
- Subpages as nested notes (`extra.onenote.level/parent_page_id` already keep the tree).
- Graph view stays deferred (outside the single-user product boundary).
