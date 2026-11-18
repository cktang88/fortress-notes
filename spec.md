# Fortress Notes — Spec

## 1. Goal

A local-first block-centric notes app where **SQLite is the live source of truth** for
documents, nested blocks, references, and indexes. Markdown remains the portable
import/export format. The app provides a fast two-pane UI, two search modes
(full-text + ColBERT late-interaction embedding search), live related results,
rough→polished workflow, and an LLM "review" of the current document.

Non-goals (for v1): multi-user, auth, real-time collaboration, mobile, cloud sync,
nested folders.

## 2. Data model

SQLite (`NOTES_DIR/.fortress.sqlite3`) is the only source of truth: documents,
folders, nested blocks with stable IDs, references, history, assets, and the FTS
index. Every write goes to SQLite first. Each active note also gets a read-only
Markdown mirror, `NOTES_DIR/<id>.md`, rewritten atomically after every change and
removed when the note is trashed, so notes stay readable outside the app. The app
never reads the mirrors back; only the explicit Markdown importer reads `.md` files.
A mirror looks like this (extra front-matter keys added by hand are preserved):

```markdown
---
id: 01HZX...            # ULID, also the filename
title: My note
status: rough           # "rough" | "polished"
tags: [ideas, project]
created_at: 2026-06-20T10:00:00Z
updated_at: 2026-06-20T10:05:00Z
images:                  # cached caption+OCR per referenced image (see §6)
  01J...: "A bar chart. Text in image: Q1 revenue"
---

The note body in Markdown.
```

Pasted images are stored as files in `NOTES_DIR/assets/<id>.<ext>` and referenced from
the body as raw `<img src="/media/<id>.<ext>" width=…>` HTML (kept as HTML so the
resized width survives the round-trip).

- **id**: ULID (sortable, unique). Filename = `<id>.md`.
- **status**: `rough` (braindump/brainstorm) or `polished`. Drives UI badge + filter
  and the "promote to polished" action.
- **title**: if empty, derived from the first heading/line of the body.
- Timestamps are UTC ISO-8601. `updated_at` is set on every save.

On first start an empty database imports any existing `NOTES_DIR/*.md` files once,
copying the originals to a timestamped backup folder first.

## 2a. Block shapes (`block_types.py`)

Every block row has `type`, `attrs` (JSON, indexed in `block_attrs`), `content`
(JSON), `text`, children (`parent_id`/`position`) and provenance (§7a).

- **`text` is the searchable plain text**: FTS, ColBERT embeddings, Ask grep and
  read_note read it. Blocks with structure or rich runs get it derived by the
  server (no Markdown, no HTML); plain Markdown-only blocks (imported `.md`)
  keep their Markdown source there, minus inline mark HTML.
- **`content.markdown`** is what Markdown export/mirrors write for the block. The
  server derives it for structured blocks and for rich text whose stored Markdown
  lacks the marks; otherwise a writer's Markdown is kept.

### Inline marks (the one canonical form)

Rich text lives in `content.blocknote` as BlockNote inline content (what the web
editor already stores), extended with `superscript`/`subscript` and hex colours:

```jsonc
{"type": "text", "text": "Hello", "styles": {
   "bold": true, "italic": true, "underline": true, "strike": true, "code": true,
   "superscript": true, "subscript": true,          // new (not BlockNote defaults)
   "textColor": "#C00000",                           // CSS name ("red") or #hex
   "backgroundColor": "#FFFF00"}}                    // highlight
{"type": "link", "href": "https://…" /* or "[[doc_id|Title]]" */, "content": [<text runs>]}
```

Omitted styles are off; `"default"` colours are dropped. Text blocks (`paragraph`,
`heading` attrs `level`, `list` attrs `ordered`/`checked`/`checked_at`, `quote`,
`code` attrs `language`, and the web editor's `bulletListItem`, `numberedListItem`,
`checkListItem`, `codeBlock`, `callout`) store
`content: {"blocknote": [runs], "markdown": "…"}`; a block with only
`{"markdown": …}` is plain Markdown (editors parse it). A newline inside a run is a
line break.

Markdown: bold `**`, italic `*`, strike `~~`, code `` ` ``, links `[t](url)`
natively; `<u>`, `<sup>`, `<sub>`, `<span style="color:#C00000">`,
`<mark style="background:#FFFF00">` for the rest. If native emphasis would not
parse back to the same runs (e.g. `**"x"**y`), the block's marks are all written
as HTML (`<strong>`, `<em>`, `<s>`, `<code>`, `<a href>`). Import parses all of
these back into runs (only blocks that contain mark HTML get runs; others stay
plain Markdown).

### Table (`type: "table"`)

```jsonc
{"type": "table",
 "attrs": {"borders": true},                 // false: no visible borders (default true)
 "content": {"blocknote": {                  // BlockNote's tableContent, extended
    "type": "tableContent",
    "columnWidths": [200, null],             // CSS px per column; null = automatic
    "headerRows": 1,                         // optional; also "headerCols"
    "rows": [{"cells": [
      {"type": "tableCell",
       "props": {"backgroundColor": "#DEEAF6", "textColor": "red",
                 "textAlignment": "center", "colspan": 1, "rowspan": 1},  // all optional
       "content": [<runs>],                  // the cell's first line of text
       "blocks": [<nested blocks>]}          // optional, after content (OneNote cells)
    ]}]},
   "markdown": "…"},
 "text": "Name\tQty\nApples\t3"}              // rows on lines, cells tab-separated
```

Nested (in-cell) blocks are `{"type", "attrs", "content", "children"?}` with no id:
text types (content `{"blocknote": runs}`), `table` (nested tables), `image`,
`file`, `math`, `thematic_break`. Rows need ≥ 1 cell; garbage is rejected with
**422** by the transactions API (`BlockShapeError`). A cell saved without a
`blocks` key keeps its nested blocks while the table keeps its shape (the web
editor cannot show them); `"blocks": []` clears them.

Markdown: a **GFM table** when there is exactly one header row and every cell is
one line of text (column-uniform alignment allowed); otherwise an HTML
`<table>` (one HTML block, no blank lines): `data-borders="false"`,
`<colgroup><col style="width:200px">…`, header cells as `<th>`, cell
`style="background-color:…;color:…;text-align:…"`, `colspan`/`rowspan`, cell content
as inline HTML then `<p>`/`<ul>`/`<ol>`/`<li data-checked>`/`<pre><code>`/`<img>`/
nested `<table>`; other table attrs in `data-attrs='{json}'`. Both import back
(GFM tables no longer become `raw`).

### Columns (`type: "columns"` → `type: "column"`)

```jsonc
{"type": "columns", "attrs": {/* free, e.g. onenote geometry */}, "content": {}, "text": "",
 "children": [
   {"type": "column", "attrs": {"width": 0.4}, "content": {}, "text": "",   // relative width
    "children": [<ordinary blocks>]},
   {"type": "column", "attrs": {"width": 0.6}, "children": […]}]}
```

Columns are ordinary tree nodes: children are indexed, linked, moved, duplicated
and undone like any block. A `columns` block holds only `column` blocks and a
`column` lives only in a `columns` block (insert/move/type change otherwise → 409).
Markdown (columns written one after another; the comments are invisible):

```markdown
<!-- fortress:columns {"onenote_y":3} -->

<!-- fortress:column {"width":0.4} -->

Left column blocks…

<!-- fortress:column {"width":0.6} -->

Right column blocks…

<!-- /fortress:columns -->
```

### Image, file, math

```jsonc
{"type": "image", "attrs": {"url": "/media/01J….png", "alt": "OCR / alt text",
  "caption": "…", "width": 480, "height": 320, "name": "scan.png"},   // px; all but url optional
 "text": "OCR / alt text\n…caption"}          // searchable
// Markdown: <img src="/media/01J….png" alt="…" title="caption" width="480" height="320" data-name="…">
//   (other attrs, e.g. the web editor's previewWidth, in data-attrs='{json}')

{"type": "file", "attrs": {"url": "/media/01J….pdf", "name": "Contract.pdf",
  "size": 48213, "mime": "application/pdf"}, "text": "Contract.pdf"}
// Markdown: <!-- fortress:file {"mime":…,"name":…,"size":…,"url":…} -->
//           [Contract.pdf](/media/01J….pdf) (47.1 KB)

{"type": "math", "content": {"latex": "E = mc^2", "mathml": "<math>…"}, "text": "E = mc^2"}
// Markdown: $$\nE = mc^2\n$$  (mathml is not exported)
```

A single `<img>` line in imported Markdown becomes an image block. Splitting and
merging apply to text blocks only.

## 3. Backend (FastAPI, Python / uv)

### Modules (`backend/app/`)
- `config.py` — settings from env (`pydantic-settings`).
- `models.py` — Pydantic schemas (`Note`, `NoteSummary`, `NoteCreate`, `NoteUpdate`,
  `SearchResult`, `ReviewResponse`).
- `block_store.py` — SQLite schema, migrations, blocks, transactions, FTS, rendering.
- `block_types.py` — rich inline marks, tables, columns, image/file/math: validation,
  derived Markdown/plain text, Markdown/HTML round trip (§2a).
- `documents.py` — note lifecycle (create/update/trash/restore/purge) and Markdown mirrors.
- `embeddings.py` — ColBERT late-interaction via PyLate; encode + MaxSim scoring,
  with a keyword fallback when disabled/unavailable.
- `search.py` — full-text search + orchestration of embedding search & related notes.
- `llm.py` — OpenRouter chat completion (`z-ai/glm-5.3-flash`) for note review.
- `main.py` — FastAPI app, routes, CORS.
- `http_cache.py` — ETags, `If-None-Match` → 304, rendered-body caches, API gzip.
- `media.py` — `/media` file serving (immutable caching, ranges, content types).
- `batch.py` — batch reads and moves for the native app.
- `onenote_import.py` — OneNote importer: runs `fortress-onenote`, imports its manifest as living documents, import jobs (§7b).

### API

| Method | Path | Body / Query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/notes` | `?status=` | `NoteSummary[]` (id, title, status, tags, updated_at, snippet) |
| `POST` | `/api/notes` | `NoteCreate` | `Note` |
| `GET` | `/api/notes/{id}` | — | `Note` |
| `PUT` | `/api/notes/{id}` | `NoteUpdate` | `Note` |
| `DELETE` | `/api/notes/{id}` | — | `204` |
| `POST` | `/api/notes/{id}/promote` | — | `Note` (status→polished) |
| `GET` | `/api/search` | `?q=&mode=text\|embedding` | `SearchResult[]` (summary + score) |
| `GET` | `/api/notes/{id}/related` | `?k=5` | `SearchResult[]` |
| `POST` | `/api/notes/{id}/review` | `?kind=factcheck\|clarify\|object` | `ReviewResponse` |
| `POST` | `/api/block-documents/{id}/link-checks` | — | `{links: [{url, status, checked_at, block_ids}]}` |
| `POST` | `/api/images` | multipart `file` | `{url, text}` (saves image, runs caption+OCR) |
| `GET` | `/media/{file}` | — | stored file bytes (ranges, immutable caching; see below) |
| `POST` | `/api/batch/block-documents` | `{ids: [≤50], known: {id: revision}}` | `{documents: [tree], unchanged: [id], missing: [id]}`; does not mark notes opened |
| `POST` | `/api/batch/move` | `{documents: [{id, folder_id}], folders: [{id, parent_id}]}` | `{folders, documents}` (counts moved) |
| `POST` | `/api/block-documents/{id}/transactions` | `BlockTransaction`, `?response=document\|meta` | document tree (`meta`: without `children`); header `X-Document-ETag` |
| `GET` | `/api/trash` | — | trashed documents, newest first |
| `POST` | `/api/trash/{id}/restore` | — | `{id, folder_id, position}` (also rewrites the Markdown mirror) |
| `DELETE` | `/api/trash/{id}` · `/api/trash` | — | delete one forever · empty the trash |
| `GET` · `POST` | `/api/saved-searches` | `{name, query, mode, filters}` | saved searches |
| `DELETE` | `/api/saved-searches/{id}` | — | `204` |
| `GET` · `POST` | `/api/backups` | — | list snapshots · take a manual snapshot |
| `GET` | `/api/backups/{name}/download` | — | SQLite snapshot file |
| `POST` | `/api/backups/{name}/restore` | — | `{restored, safety_backup}` |
| `GET` | `/api/block-documents/{id}/markdown` | `?download=true` | one note as Markdown (with provenance, §7a) |
| `GET` | `/api/block-documents/{id}/info` | — | note metadata: dates, provenance, counts, conflicts (§7a) |
| `POST` | `/api/block-documents/{id}/blocks/{block_id}/source-conflict` | `{keep}` | document tree (§7a) |
| `POST` | `/api/markdown-import/files` | multipart `files[]`, `folder_id?` | `{imported, errors}` |
| `POST` | `/api/import/onenote` | multipart `file` (+ `folder_id?`) or JSON `{path, folder_id?}` | `202 {job_id, status: "queued"}` (§7b) |
| `GET` | `/api/import/onenote/{job_id}` | — | import job (§7b) |
| `GET` | `/api/import/onenote` | `?limit=20` | recent import jobs, newest first |
| `GET` | `/api/health` | — | `{status, embeddings_enabled, model_loaded}` |

### HTTP caching, compression, errors

- **Validators.** These GETs carry a strong `ETag` and `Cache-Control: no-cache`
  (keep, but revalidate); a request whose `If-None-Match` matches gets `304` with no
  body:
  - `GET /api/block-documents/{id}` — changes with anything in the payload: the note's
    own rows (blocks, user attrs, links, history, title/status/provenance/...),
    `last_opened_at`, and every linked note or block (titles and text in
    `resolved_links`, `[[id|Title]]` labels).
  - `GET /api/navigation` (per `recent_limit`), `GET /api/trash`,
    `GET /api/block-documents/{id}/info`, `GET /api/block-documents/{id}/backlinks`
    — change with any change in the workspace (they show other notes).
  - `GET /api/backups` — changes with the snapshot list.

  The server tracks changes with SQLite triggers (`change_log.seq`, an `epoch` renewed
  by every restore, and `documents.change_seq`), so writes from any code path — and
  restores — invalidate validators. Other API responses also get
  `Cache-Control: no-cache` unless they set their own (the Ask stream: `no-store`).
- **Opening is cheap.** `GET /api/block-documents/{id}` sets `last_opened_at` at most
  once a minute per note, and skips (does not wait for) the write while another
  writer holds the database. A transaction response carries `X-Document-ETag`, the
  ETag the next GET of that note answers 304 to, and primes the server's cache.
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
  `no-cache`. Uploads (`/api/images`, `/api/files`) stream to disk while hashing.
- **Errors** are JSON `{"detail": ...}`: unknown routes 404, a database still locked
  after the 5 s busy timeout `503` with `Retry-After: 1`, anything unexpected `500`
  `{"detail": "Internal server error"}`.
- **SQLite.** WAL, `synchronous=NORMAL`, 16 MB page cache, 256 MB mmap, 5 s busy
  timeout, one pooled connection per worker thread (prepared statements reused).
  FTS rows share their block's rowid, so an edit re-indexes only the blocks it wrote.
  Markdown mirrors of edited notes are written in the background (coalesced, 0.5 s).

### Late-interaction search (PyLate)
- Model: `lightonai/Agent-ModernColBERT` (configurable).
- On index build, encode each note body (chunked if long) as multi-vector doc
  embeddings (`model.encode(..., is_query=False)`); cache them keyed by id+updated_at.
- Query: encode `q` as query embeddings (`is_query=True`), score every doc with
  **MaxSim** (sum over query tokens of max cosine to doc tokens), return top-k.
- Brute-force is fine at personal scale; PLAID/Voyager index is a future optimization.
- **Indexing**: embeddings are computed lazily and cached per `(id, updated_at)`. A
  background task (`_reindex_loop`, every `REINDEX_INTERVAL_S`, default 5s) pre-encodes
  any changed notes via `embeddings.warm_index()` so searches stay instant; it also
  evicts embeddings for deleted notes. Steady-state (nothing changed) it's a no-op.
- **Related notes**: start with the current note's body as the query, then use the
  focused block. Exclude the current note and return top-k.
- **Fallback**: if embeddings disabled or model missing, score by keyword overlap so
  the UI still works.

### LLM review (`llm.py`)
- POST to `https://openrouter.ai/api/v1/chat/completions`, model `OPENROUTER_MODEL`.
- Three `kind`s, each a different system prompt:
  - `factcheck` — flag claims that are wrong/unsupported; cite uncertainty.
  - `clarify` — ask clarifying questions about gaps/ambiguity.
  - `object` — steelman objections, surface inconsistencies/faulty reasoning
    ("code review for your notes").
- Returns structured `ReviewResponse { kind, summary, items[] }`.

## 4. Frontend (Vite + React + TanStack Query + Tailwind + BlockNote)

### Layout
```
┌──────────────────────────┬─────────────────────────────────────┐
│ Search bar  [text|embed] │                                     │
│ [+ New]   [All|Rough|Pol]│         Note title                  │
├──────────────────────────┤   [status badge] [Promote] [AI ▾]   │
│ ▸ Document tree (scroll) │                                     │
│   • note A    rough      │   ┌───────────────────────────────┐ │
│   • note B    polished   │   │  BlockNote rich-text editor  │ │
│   • ...                  │   │  (SQLite-backed)              │ │
│                          │   │                               │ │
├──────────────────────────┤   └───────────────────────────────┘ │
│ Related notes (auto)     │   [AI review panel, when open]      │
│   • note C   0.82        │                                     │
│   • note D   0.79        │                                     │
└──────────────────────────┴─────────────────────────────────────┘
```

### Features → components (`src/features/notes/`)
- `NoteList` — list + status filter; clicking selects a note.
- `SearchBar` — query input + mode toggle (text/embedding); drives the list.
- `RelatedNotes` — bottom-left; refetches on selected-note or focused-block change.
- `BlockNoteEditor` — nested block editor; debounced transactional autosave.
- `NoteHeader` — title, status badge, Promote button.
- `BlockReviewPanel` — fact-checks highlighted text or selected blocks.
- `api.ts` — typed fetch wrappers; `hooks.ts` — TanStack Query hooks.

State: TanStack Query for server state; a little local UI state (selected id, search
mode, query). No `useEffect` for data; rely on React Compiler (no manual memoization).

## 5. Automatic link checks

When a document opens, the client requests `POST /api/block-documents/{id}/link-checks`.
The server checks links in blocks last edited at least 30 days ago. Successful checks
are cached in SQLite for 30 days. Failed status codes and network error names are
returned and retried on a later open; they never edit note content. Results include
the current block IDs for each link, so removed or changed links do not keep stale
associations.

## 6. Images & vision (`images.py`, `vision.py`)

- **Paste/drop**: the editor's `handlePaste`/`handleDrop` (logic in `paste.ts`,
  unit-tested) pull image files from the clipboard/drop, `POST /api/images`, and insert
  an `<img>` with the returned `/media/…` URL. Markdown stays small — images are files.
- **Fast & non-blocking**: `/api/images` saves the file and returns **immediately**
  (~ms); caption/OCR runs in a tracked background task (`asyncio.to_thread`, references
  held so it isn't GC'd) so the image appears instantly and the event loop never blocks.
- **Resize**: `ImageResize` (extends Tiptap `Image`) adds a drag handle that writes a
  `width` attribute; turndown `keep(['img'])` preserves `<img width=…>` as raw HTML so
  the size round-trips through Markdown.
- **Searchable images**: **RapidOCR** and local SmolVLM captions run once per image and
  write per-image and content-hash sidecars (cache reused across duplicate bytes). `note_search_text`
  folds title + body + sidecar text so full-text and embedding search cover image text;
  the text is also mirrored into the note's `images:` frontmatter on save. Captions are
  enabled by default and use the small **SmolVLM-256M** model; OCR and captioning run
  locally. Models lazy-load + pre-warm in the background at startup.

## 7. Workspace safety and organization

- **Trash**: deleting a note soft-deletes it in SQLite. The app shows an Undo toast;
  the Trash dialog (⋯ menu) restores notes or deletes them forever.
- **Backups**: a background task keeps one automatic snapshot per day in
  `NOTES_DIR/.fortress-backups` (last 7 daily, 10 manual, 5 pre-restore). Restoring
  verifies the snapshot, saves the current state as `pre-restore` first, and moves
  Markdown mirrors of documents that are not in the snapshot aside rather than
  deleting them. Set `AUTO_BACKUP_ENABLED=false` to turn off daily snapshots.
- **Import/export**: the ⋯ menu imports `.md` files as new notes; each note exports
  from its header or right-click menu.
- **Saved searches**: saved from the search box and re-run from chips beneath it.
- **Keyboard**: ⌘/Ctrl+K or `/` search, Alt+N new note, ↑/↓/Enter through results,
  Esc clears search, `?` shows tips.

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
  "origin": {"operation": "move_into|duplicate|split", "document_id": "…",
             "document_title": "…", "block_id": "…", "at": "…"},
  "extra": {},                   // importer-specific
  // server-maintained on blocks: "source_hash", "conflict", "source_removed", "dismissed_hash";
  // on notes: "source_removed" + "source_removed_at" (the source no longer has the page;
  // sources.mark_documents_source_removed, cleared by the next upsert);
  // "backfilled": true marks the migration's guess
}
```

- **Document payload** (`GET /api/block-documents/{id}` and every tree response) adds
  `provenance`, `last_opened_at` (set when that GET runs, at most once a minute) and
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
  server-maintained keys. Duplicate/split copies get `origin` and lose `external_id`;
  move-into stamps `origin.operation = "move_into"` on every moved block.
- **Markdown**: export front matter carries `provenance:`; a block whose provenance
  differs from the note's is preceded by one line
  `<!-- fortress:block {"external_id":"{GUID}","source":"onenote_import",…} -->`.
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
provenance, content, text)` or a Markdown body. A SourceBlock carries either
Markdown or `type` + structured `content` (§2a; Markdown and text are derived).
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
  `highlight`, links; monospace fonts → `code`; inline equations keep OneNote's
  linear text; font sizes are dropped.
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
- **Images without OneNote OCR text** (png/jpeg/gif/webp) are queued for the
  app's local caption + OCR (`images.extract_and_store`, one at a time on a
  background thread, after the import; skipped if the asset already has text)
  so their text becomes searchable; `images_ocr_queued` counts them.
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

- **PLAID/Voyager index** for embedding search at larger scale.
- Larger leaderboard-class OCR/VLM (e.g. PaddleOCR-VL) as an opt-in.
- Graph view stays deferred (outside the single-user product boundary).

## 9. Open questions
- Chunking strategy for very long notes (per-paragraph vs whole-note vectors).
- Whether to persist the embedding cache to disk between runs (v1: in-memory).
- Debounce window + conflict handling if a file changes on disk while editing.
