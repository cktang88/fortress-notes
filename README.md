# Fortress Notes

A local-first, block-centric notes app: a native macOS app (`mac/`, SwiftUI + TextKit)
over a local Python backend (`backend/`, FastAPI). SQLite is the live source of truth for
documents, nested blocks, links, and search indexes. Markdown files remain a safe,
portable import/export format. One search box combines **full text**, **late-interaction
embedding search** (ColBERT) and note titles, and **Ask** answers questions from your
notes with a citation for every sentence.

> The point: the local SQLite store keeps stable block IDs and atomic edits, while
> Markdown export keeps your notes portable and inspectable.

## Features

- **SQLite block store** — documents contain stable, nested blocks with atomic
  transactions, references, backlinks, and an FTS5 search index.
- **Markdown portability** — existing Markdown notes are imported without deleting the
  originals; every note is also mirrored to a readable `.md` file.
- **Native Mac app** — folders sidebar, note list and a TextKit block editor; an
  inspector shows related notes, backlinks and where a note came from.
- **One search box** — results combine exact words (SQLite FTS5), meaning (late-interaction
  ColBERT / `lightonai/Agent-ModernColBERT` via [PyLate](https://github.com/lightonai/pylate))
  and note titles using reciprocal rank fusion. Each result shows the matching passage with
  your words highlighted and says why it matched; Enter jumps to that paragraph.
- **Ask your notes** — type a question and press ✦ Ask (or ⌘/Ctrl+Enter). It greps your
  notes for the question's names, dates and key words and runs meaning-based search
  (locally, in milliseconds), then one model call answers — or greps a pattern / reads a
  whole note if it needs to, within a 3-second budget. Every sentence links to the
  paragraph it came from; claims whose quotes aren't really in your notes are dropped.
  Only passages are sent to the model (needs `OPENROUTER_API_KEY`).
- **Related notes** — the bottom-left pane auto-refreshes whenever you open a note,
  showing the most semantically related notes (late-interaction similarity).
- **Rough vs. polished notes** — a `status` flag distinguishes braindump/brainstorm
  notes from polished ones.
- **Nested folders** — drag notes and folders into folders or back to All notes.
  Right-click to create items, and double-click a sidebar name to rename it.
- **Paste & resize images** — paste an image straight into the editor; it's uploaded
  and inserted instantly, and is drag-resizable. A background OCR queue (PP-OCRv6 via
  RapidOCR/ONNX, local) reads every picture and PDF once per content hash, so **text in
  images and PDFs is searchable** by full-text, ColBERT and Ask; PDFs use their own
  text layer and only image-only pages are OCR'd.

- **Titles that write themselves** — a new note opens ready for typing and its title
  follows the first line until you choose one; clear the title to hand it back.
- **Quick capture** — type into "Jot something down…" at the top of the sidebar (or press
  ⌘/Ctrl+Shift+Space from anywhere) and Enter saves it as its own note in the
  **Uncategorized** folder, titled from its first line, without leaving what you're doing.
  Drag captures into real folders whenever you like.
- **Trash with Undo** — deleting moves a note to Trash; undo it from the toast or
  restore it later from **⋯ → Trash**.
- **Automatic backups** — a SQLite snapshot is saved daily; **Backups** takes a fresh
  one or restores any snapshot (your current notes are saved first).
- **Import & export** — import `.md` files or a OneNote export; export a note as Markdown.
- **⌘K to go anywhere** — jump to a note by title (fuzzy), type a new title to create
  it, search everywhere, or open Trash, Backups and Import.

See [`spec.md`](./spec.md) for the full design, data model, API, and roadmap.

## Architecture

```
mac      (SwiftUI + TextKit 2; starts and talks to the backend on 127.0.0.1)
        │  HTTP / JSON (ETag-validated)
        ▼
backend  (FastAPI, Python)
        ├── block_store   SQLite documents, blocks, references, FTS5, migrations
        ├── documents     note lifecycle + read-only Markdown mirrors
        ├── search        full-text + ColBERT late-interaction (PyLate)
        ├── ocr           background OCR queue for pictures and PDFs (PP-OCRv6)
        ├── ask           Ask your notes (cited answers)
        └── llm           OpenRouter chat completions
        │
        ▼
   ./notes/.fortress.sqlite3 ← the canonical local store
   ./notes/*.md            ← read-only Markdown mirrors (one per note)
   ./notes/assets/*.{png,…}  ← pasted images (OCR text cached in SQLite by content hash)
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

### 2. Mac app

Requires macOS 26 and Xcode 26 (Swift 6.2):

```bash
cd mac
swift build
swift run FortressNotes        # starts the backend in ../backend if none is running
swift test                     # unit and snapshot tests
```

CI builds and tests it on GitHub Actions (`.github/workflows/mac.yml`), including
integration tests against a real backend.

## Configuration

Backend `.env` (see `backend/.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `NOTES_DIR` | `../notes` | Workspace containing the SQLite store and Markdown mirrors |
| `OPENROUTER_API_KEY` | — | Required for Ask |
| `OPENROUTER_MODEL` | `z-ai/glm-5.3-flash` | OpenRouter model id; `deepseek/deepseek-v4.1-flash` is also supported |
| `EMBEDDINGS_ENABLED` | `true` | Turn off to skip the ColBERT model |
| `COLBERT_MODEL` | `lightonai/Agent-ModernColBERT` | Late-interaction model |
| `REINDEX_INTERVAL_S` | `5` | Background re-encode interval for changed notes |
| `VISION_ENABLED` | `true` | Background OCR of pictures and PDFs — makes their text searchable |
| `OCR_THREADS` | `0` | ONNX Runtime threads for OCR (0 = half the cores, at most 4) |
| `AUTO_BACKUP_ENABLED` | `true` | Keep a daily SQLite snapshot in `NOTES_DIR/.fortress-backups` |

## Tech choices (short version)

- **Editor: native TextKit 2** — one text view for the whole note with block
  boundaries mapped to stable server block IDs; SQLite stays authoritative and edits
  travel as block transactions.
- **Storage: SQLite + small query helpers** — SQLite gives local transactions,
  foreign keys, WAL, FTS5, backups, and integrity checks without the cost of a full
  ORM. Raw parameterized SQL remains explicit for FTS and tree mutations.
- **Search: PyLate + ColBERT** — late-interaction (token-level MaxSim) beats
  single-vector embeddings for retrieval quality on small/medium collections,
  and brute-force MaxSim is plenty fast for a personal note set.
- **LLM: OpenRouter** — one API for many models; `z-ai/glm-5.3-flash` by default, with `deepseek/deepseek-v4.1-flash` as an alternative.
```
