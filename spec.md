# Fortress Notes — Spec

## 1. Goal

A local-first notes app where the source of truth is a **folder of Markdown files**.
The app provides a fast two-pane UI, two search modes (full-text + ColBERT
late-interaction embedding search), live related-notes, rough→polished workflow, and
an LLM "review" of the current note.

Non-goals (for v1): multi-user, auth, real-time collaboration, mobile, cloud sync,
nested folders.

## 2. Data model

Each note is one Markdown file in `NOTES_DIR` (default `./notes`), named `<id>.md`.
YAML frontmatter holds metadata; the body is the note content (Markdown).

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

The filesystem is authoritative. The backend reads files on demand and keeps an
in-memory cache + search index that it rebuilds on change.

## 3. Backend (FastAPI, Python / uv)

### Modules (`backend/app/`)
- `config.py` — settings from env (`pydantic-settings`).
- `models.py` — Pydantic schemas (`Note`, `NoteSummary`, `NoteCreate`, `NoteUpdate`,
  `SearchResult`, `ReviewResponse`).
- `notes_store.py` — CRUD over Markdown files (frontmatter parse via `python-frontmatter`).
- `embeddings.py` — ColBERT late-interaction via PyLate; encode + MaxSim scoring,
  with a keyword fallback when disabled/unavailable.
- `search.py` — full-text search + orchestration of embedding search & related notes.
- `llm.py` — OpenRouter chat completion (`deepseek/deepseek-v4-flash`) for note review.
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
| `POST` | `/api/notes/{id}/consistency` | `?k=5` | `ConsistencyReport` |
| `POST` | `/api/notes/{id}/heal` | — | `HealReport` |
| `POST` | `/api/images` | multipart `file` | `{url, text}` (saves image, runs caption+OCR) |
| `GET` | `/media/{file}` | — | image bytes (StaticFiles) |
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
- **Related notes**: treat the current note's body as the query, exclude itself,
  return top-k.
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

## 4. Frontend (Vite + React + TanStack Query + Tailwind + Tiptap)

### Layout
```
┌──────────────────────────┬─────────────────────────────────────┐
│ Search bar  [text|embed] │                                     │
│ [+ New]   [All|Rough|Pol]│         Note title                  │
├──────────────────────────┤   [status badge] [Promote] [AI ▾]   │
│ ▸ Note list (scroll)     │                                     │
│   • note A    rough      │   ┌───────────────────────────────┐ │
│   • note B    polished   │   │  Tiptap rich-text editor      │ │
│   • ...                  │   │  (Markdown-backed)            │ │
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
- `RelatedNotes` — bottom-left; refetches on selected-note change.
- `NoteEditor` — Tiptap editor; debounced autosave (PUT); Markdown ⇄ HTML.
- `NoteHeader` — title, status badge, Promote button, AI review dropdown.
- `ReviewPanel` — shows LLM `factcheck`/`clarify`/`object` output.
- `api.ts` — typed fetch wrappers; `hooks.ts` — TanStack Query hooks.

State: TanStack Query for server state; a little local UI state (selected id, search
mode, query). No `useEffect` for data; rely on React Compiler (no manual memoization).

## 5. Agentic passes (implemented)

Both are `analysis.py`, exposed as POST endpoints and driven from the note header.
Findings are **not** shown in side panels — they render as **inline annotations**
directly on the offending text via a ProseMirror decoration plugin
(`annotations.ts` + `AnnotationExtension`):

- Each finding's `claim` text is located in the document and gets a **red wavy
  squiggle**. Clicking it opens a small floating popup (`AnnotationPopup`) with the
  explanation and **Accept / Reject**. For stale facts, Accept replaces the text with
  the suggestion; Reject (or Dismiss for consistency) just clears the annotation.
- **Links** are auto-detected (Tiptap `Link`, `autolink`), highlighted **blue**, and
  open in a new browser tab on click. Links the heal pass found broken are
  highlighted **red** (`link-broken` decoration keyed on the dead-link URL set).

### Cross-note inconsistency detection (`POST /consistency`)
"Code review for your notes." Retrieves the top-k related notes via ColBERT
late-interaction, sends the target note + related notes to the LLM, which returns
`ConsistencyReport { summary, issues[], checked_against[] }`. Each `ConsistencyIssue`
names the conflicting related note (clickable in the UI), the target's claim, the
conflicting claim, and a severity. Returns no issues when notes are merely different.

### Self-healing notes (`POST /heal`)
Two parallel passes returning `HealReport { summary, dead_links[], stale_facts[] }`:
- **Dead-link detection** — extracts URLs from the body and HTTP-checks them
  concurrently (`httpx`); reports status code or error reason. Runs fully locally,
  no API key needed.
- **Stale-fact detection** — uses the LLM with **OpenRouter's web search plugin**
  (`plugins:[{id:"web"}]`) to flag time-sensitive claims that look outdated and
  suggest updates (`StaleFact { claim, finding, suggestion }`).

Heal reports suggestions only; it does not auto-edit the note (safer; user applies).

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
- **Searchable images**: **RapidOCR** (~1s/image) runs once per image and writes a
  `assets/<id>.txt` sidecar (compute-once cache — never re-run). `note_search_text`
  folds title + body + sidecar text so full-text and embedding search cover image text;
  the text is also mirrored into the note's `images:` frontmatter on save. An optional
  tiny VLM caption (**SmolVLM-256M**) is gated behind `VLM_CAPTION_ENABLED` (off by
  default — ~3 min/image on CPU; GPU only). Models lazy-load + pre-warm in the
  background at startup, with graceful fallback when disabled.

## 7. Roadmap / potential features

- **Auto-apply heal suggestions** (with a diff/confirm step).
- **Backlinks & wiki-links** (`[[note]]`), graph view.
- **PLAID/Voyager index** for embedding search at larger scale.
- **Tag management**, saved searches, keyboard navigation.
- Export; larger leaderboard-class OCR/VLM (e.g. PaddleOCR-VL) as an opt-in.

## 8. Open questions
- Chunking strategy for very long notes (per-paragraph vs whole-note vectors).
- Whether to persist the embedding cache to disk between runs (v1: in-memory).
- Debounce window + conflict handling if a file changes on disk while editing.
