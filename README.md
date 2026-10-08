# Fortress Notes

A local-first, block-centric notes app. SQLite is the live source of truth for
documents, nested blocks, links, and search indexes. Markdown files remain a safe,
portable import/export format. Two-pane layout: a document tree on the left and a
BlockNote editor on the right. Search by **full text** or by **late-interaction
embedding search** (ColBERT). An LLM can fact-check, question, or push back on the note
you're reading.

> The point: the local SQLite store keeps stable block IDs and atomic edits, while
> Markdown export keeps your notes portable and inspectable.

## Features

- **SQLite block store** — documents contain stable, nested blocks with atomic
  transactions, references, backlinks, and an FTS5 search index.
- **Markdown portability** — existing Markdown notes are imported without deleting the
  originals; every note is also mirrored to a readable `.md` file.
- **Two-pane UI** — left: search + document tree + related/backlink panes; right: a
  BlockNote rich-text editor for the selected document.
- **Two search modes** (toggle in the search bar):
  - **Full-text** — fast substring/keyword search across all notes.
  - **Embedding search** — late-interaction (ColBERT / `lightonai/Agent-ModernColBERT`)
    multi-vector retrieval via [PyLate](https://github.com/lightonai/pylate).
- **Related notes** — the bottom-left pane auto-refreshes whenever you open a note,
  showing the most semantically related notes (late-interaction similarity).
- **Rough vs. polished notes** — a `status` flag distinguishes braindump/brainstorm
  notes from polished ones. Promote a rough note to polished in one click.
- **Selection review** — highlight text or select blocks to fact-check them with
  `z-ai/glm-5.3-flash` via [OpenRouter](https://openrouter.ai). The help button explains
  this and other editor shortcuts.
- **Nested folders** — drag notes and folders into folders or back to All notes.
  Right-click to create items, and double-click a sidebar name to rename it.
- **Monthly link checks** — opening a note checks links in blocks that have not been
  edited for 30 days. Valid checks are cached in SQLite, and failed links can be retried
  the next time the note opens.
- **Smart links** — URLs are auto-detected, highlighted **blue**, and open in the
  browser on click; links found broken are highlighted **red**.
- **Paste & resize images** — paste an image straight into the editor; it's uploaded
  and inserted instantly, and is drag-resizable. OCR runs once per image in the
  background so **image text is searchable** by both full-text and embedding search.
  The small VLM adds local captions, and content-hash caching reuses OCR and captions
  for duplicate image bytes.

- **Seen before** — a quiet chip on the editor's status line. After you pause writing it
  shows `↗ 2 related` (paragraphs from other notes like the one you're writing); if the
  whole note repeats another it turns amber: `Looks like “Kitchen reno”`. It never pops up
  by itself — click it to **Link** a related paragraph, or **Move** this note into the one
  it repeats (links keep working; the emptied note goes to Trash), or mark it
  **Not a duplicate**.
- **Trash with Undo** — deleting moves a note to Trash; undo it from the toast or
  restore it later from **⋯ → Trash**.
- **Automatic backups** — a SQLite snapshot is saved daily; **⋯ → Backups** downloads a
  fresh one or restores any snapshot (your current notes are saved first).
- **Import & export** — import `.md` files from **⋯**; export a note from its header.
- **Saved searches** — save a search and re-run it from a one-click chip.
- **Keyboard first** — ⌘/Ctrl+K or `/` to search, Alt+N for a new note, arrow keys and
  Enter through results, Esc to clear, `?` for tips.

See [`spec.md`](./spec.md) for the full design, data model, API, and roadmap.

## Architecture

```
frontend (Vite + React + TanStack Query + Tailwind + BlockNote)
        │  HTTP / JSON
        ▼
backend  (FastAPI, Python)
        ├── block_store   SQLite documents, blocks, references, FTS5, migrations
        ├── documents     note lifecycle + read-only Markdown mirrors
        ├── search        full-text + ColBERT late-interaction (PyLate)
        ├── vision        SmolVLM-256M caption + RapidOCR for pasted images
        └── llm           OpenRouter review
        │
        ▼
   ./notes/.fortress.sqlite3 ← the canonical local store
   ./notes/*.md            ← read-only Markdown mirrors (one per note)
   ./notes/assets/*.{png,…}  ← pasted images (+ <id>.txt caption/OCR cache)
   ./notes/.fortress-backups ← daily / manual / pre-restore SQLite snapshots
```

The embedding model needs Python, so the backend is Python-only (FastAPI). If the
ColBERT model isn't downloaded, the app still runs — embedding search and "related
notes" degrade gracefully to keyword matching.

## Quick start

### 1. Backend

```bash
cd backend
uv sync                      # create env + install deps
cp .env.example .env         # add your OPENROUTER_API_KEY
uv run uvicorn app.main:app --reload --port 8000
```

The first request that uses embeddings downloads `lightonai/Agent-ModernColBERT`
from Hugging Face (~150M params). Set `EMBEDDINGS_ENABLED=false` in `.env` to skip it.

### 2. Frontend

The frontend uses the [VoidZero](https://voidzero.dev) **Vite+** toolchain (the `vp`
CLI: Vite + Rolldown + Vitest + oxlint + oxfmt in one config).

```bash
cd frontend
npm install
npm run dev                  # vp dev → http://localhost:5173
```

The frontend proxies `/api` to the backend on port 8000 (see `vite.config.ts`).

Toolchain commands (all driven by `vp`, configured in `vite.config.ts`):

| Command | Tool | Purpose |
| --- | --- | --- |
| `npm run dev` | `vp dev` | Dev server |
| `npm run build` | `tsc -b && vp build` | Type-check + Rolldown production build |
| `npm test` | `vp test` | Vitest |
| `npm run lint` | `vp lint` | oxlint |
| `npm run fmt` | `vp fmt` | oxfmt (add `--check` to verify only) |
| `npm run check` | `vp check` | lint + format + typecheck (use `--fix` to autofix) |

`staged` in `vite.config.ts` runs `vp check --fix` on staged files for a pre-commit hook.

## Configuration

Backend `.env` (see `backend/.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `NOTES_DIR` | `../notes` | Workspace containing the SQLite store and Markdown mirrors |
| `OPENROUTER_API_KEY` | — | Required for AI review |
| `OPENROUTER_MODEL` | `z-ai/glm-5.3-flash` | OpenRouter model id; `deepseek/deepseek-v4.1-flash` is also supported |
| `EMBEDDINGS_ENABLED` | `true` | Turn off to skip the ColBERT model |
| `COLBERT_MODEL` | `lightonai/Agent-ModernColBERT` | Late-interaction model |
| `REINDEX_INTERVAL_S` | `5` | Background re-encode interval for changed notes |
| `VISION_ENABLED` | `true` | Image OCR (fast, ~1s/image) — makes image text searchable |
| `VLM_CAPTION_ENABLED` | `true` | Run local image captioning with OCR |
| `VLM_MODEL` | `HuggingFaceTB/SmolVLM-256M-Instruct` | Small local VLM used for captions |
| `AUTO_BACKUP_ENABLED` | `true` | Keep a daily SQLite snapshot in `NOTES_DIR/.fortress-backups` |

## Frontend toolchain

Built on the [VoidZero](https://voidzero.dev) **Vite+** stack via the `vp` CLI —
Vite + Rolldown (build), Vitest (test), oxlint (lint), oxfmt (format) — all unified in
a single `vite.config.ts`. Falls back to standard Vite plugins (React Compiler,
Tailwind) inside the same config.

## Tech choices (short version)

- **Editor: BlockNote** over Editor.js/Tiptap — BlockNote already owns the
  Notion-like block interactions this product needs: nested blocks, handles, menus,
  indentation, drag/drop, and block change events. SQLite remains authoritative behind
  a narrow adapter, so editor JSON is not the storage format.
- **Storage: SQLite + small query helpers** — SQLite gives local transactions,
  foreign keys, WAL, FTS5, backups, and integrity checks without the cost of a full
  ORM. Raw parameterized SQL remains explicit for FTS and tree mutations.
- **Search: PyLate + ColBERT** — late-interaction (token-level MaxSim) beats
  single-vector embeddings for retrieval quality on small/medium collections,
  and brute-force MaxSim is plenty fast for a personal note set.
- **LLM: OpenRouter** — one API for many models; `z-ai/glm-5.3-flash` by default, with `deepseek/deepseek-v4.1-flash` as an alternative.
```
