# Fortress Notes

A local-first notes app. Notes are plain **Markdown files** in a folder you own — the
app is just a fast, smart UI on top of them. Two-pane layout: a list of notes on the
left, a rich-text editor on the right. Search by **full text** or by **late-interaction
embedding search** (ColBERT). An LLM can fact-check, question, or push back on the note
you're reading.

> The point: your notes stay as portable `.md` files on disk forever. Everything else
> (search index, embeddings, AI) is a disposable layer on top.

## Features

- **Folder of Markdown notes** — every note is a `.md` file with YAML frontmatter
  (`id`, `title`, `status`, `tags`, timestamps). Edit them here or in any other editor.
- **Two-pane UI** — left: search + note list + a live "related notes" sub-pane;
  right: a Tiptap rich-text editor for the selected note.
- **Two search modes** (toggle in the search bar):
  - **Full-text** — fast substring/keyword search across all notes.
  - **Embedding search** — late-interaction (ColBERT / `lightonai/Agent-ModernColBERT`)
    multi-vector retrieval via [PyLate](https://github.com/lightonai/pylate).
- **Related notes** — the bottom-left pane auto-refreshes whenever you open a note,
  showing the most semantically related notes (late-interaction similarity).
- **Rough vs. polished notes** — a `status` flag distinguishes braindump/brainstorm
  notes from polished ones. Promote a rough note to polished in one click.
- **AI review** — a button feeds the current note to an LLM (`xiaomi/mimo-v2.5-pro`
  via [OpenRouter](https://openrouter.ai)) which fact-checks, asks clarifying
  questions, and raises objections / inconsistencies.
- **Cross-note consistency check** — agentic "code review for your notes": ColBERT
  retrieves related notes, the LLM flags contradictions, shown as **red squiggles**
  directly on the offending text. Click a squiggle → popup with the explanation and
  Accept/Reject.
- **Self-healing** — detects broken links (concurrent HTTP checks) and uses LLM +
  web search to flag stale/out-of-date facts. Stale claims get a squiggle whose popup
  offers a one-click **Accept** to apply the suggested fix.
- **Smart links** — URLs are auto-detected, highlighted **blue**, and open in the
  browser on click; links found broken are highlighted **red**.

See [`spec.md`](./spec.md) for the full design, data model, API, and roadmap.

## Architecture

```
frontend (Vite + React + TanStack Query + Tailwind + Tiptap)
        │  HTTP / JSON
        ▼
backend  (FastAPI, Python)
        ├── notes_store   reads/writes Markdown files in ./notes
        ├── search        full-text + ColBERT late-interaction (PyLate)
        └── llm           OpenRouter (xiaomi/mimo-v2.5-pro) review
        │
        ▼
   ./notes/*.md   ← the source of truth
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
| `NOTES_DIR` | `../notes` | Folder of Markdown notes (the source of truth) |
| `OPENROUTER_API_KEY` | — | Required for AI review |
| `OPENROUTER_MODEL` | `xiaomi/mimo-v2.5-pro` | OpenRouter model id |
| `EMBEDDINGS_ENABLED` | `true` | Turn off to skip the ColBERT model |
| `COLBERT_MODEL` | `lightonai/Agent-ModernColBERT` | Late-interaction model |
| `REINDEX_INTERVAL_S` | `5` | Background re-encode interval for changed notes |

## Frontend toolchain

Built on the [VoidZero](https://voidzero.dev) **Vite+** stack via the `vp` CLI —
Vite + Rolldown (build), Vitest (test), oxlint (lint), oxfmt (format) — all unified in
a single `vite.config.ts`. Falls back to standard Vite plugins (React Compiler,
Tailwind) inside the same config.

## Tech choices (short version)

- **Editor: Tiptap** over Lexical/MarkText — MarkText is a desktop app (not
  embeddable); Lexical is great but lower-level. Tiptap is headless + ships
  Markdown I/O and a StarterKit, so less glue code. Isolated in one component
  so it's swappable.
- **Search: PyLate + ColBERT** — late-interaction (token-level MaxSim) beats
  single-vector embeddings for retrieval quality on small/medium collections,
  and brute-force MaxSim is plenty fast for a personal note set.
- **LLM: OpenRouter** — one API for many models; `xiaomi/mimo-v2.5-pro` per request.
```
