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

## 3. Backend (FastAPI, Python / uv)

### Modules (`backend/app/`)
- `config.py` — settings from env (`pydantic-settings`).
- `models.py` — Pydantic schemas (`Note`, `NoteSummary`, `NoteCreate`, `NoteUpdate`,
  `SearchResult`, `ReviewResponse`).
- `block_store.py` — SQLite schema, migrations, blocks, transactions, FTS, rendering.
- `documents.py` — note lifecycle (create/update/trash/restore/purge) and Markdown mirrors.
- `embeddings.py` — ColBERT late-interaction via PyLate; encode + MaxSim scoring,
  with a keyword fallback when disabled/unavailable.
- `search.py` — full-text search + orchestration of embedding search & related notes.
- `llm.py` — OpenRouter chat completion (`z-ai/glm-5.3-flash`) for note review.
- `main.py` — FastAPI app, routes, CORS.

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
| `GET` | `/media/{file}` | — | image bytes (StaticFiles) |
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
| `GET` | `/api/health` | — | `{status, embeddings_enabled, model_loaded}` |

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
  // "backfilled": true marks the migration's guess
}
```

- **Document payload** (`GET /api/block-documents/{id}` and every tree response) adds
  `provenance`, `last_opened_at` (set when that GET runs) and
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
provenance)` or a Markdown body. `folder_path=[(external_id, name), …]` finds/creates
folders by source id (`sources.ensure_folder_path`). It returns
`UpsertResult(document_id, status=created|updated|unchanged|skipped_deleted, revision,
blocks_created, blocks_updated, blocks_deleted, blocks_skipped, conflicts)`. The note
keeps its Fortress id, links, backlinks and history; a re-import is one undo step.
Policy: untouched blocks follow the source; locally edited blocks keep the local
text and record the source's version as a conflict; local additions stay after the
block they followed; local deletions stick; blocks gone from the source are deleted
unless edited (then marked `source_removed`); structure follows the source; title and
folder follow the source unless changed locally; trashed notes are skipped.

## 8. Roadmap / potential features

- **PLAID/Voyager index** for embedding search at larger scale.
- Larger leaderboard-class OCR/VLM (e.g. PaddleOCR-VL) as an opt-in.
- Graph view stays deferred (outside the single-user product boundary).

## 9. Open questions
- Chunking strategy for very long notes (per-paragraph vs whole-note vectors).
- Whether to persist the embedding cache to disk between runs (v1: in-memory).
- Debounce window + conflict handling if a file changes on disk while editing.
