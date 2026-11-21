# Fortress Notes — Spec

What the app does and what it must guarantee. The code is the reference for
everything else: module docstrings explain each part, and FastAPI describes the
HTTP API at `/openapi.json` (fetch it with the token, §3; the interactive `/docs`
page can't be used, because the backend refuses browser requests).

## 1. Goal

A fast, local-first, block-based notes app for one person on one Mac. SQLite is the
live source of truth for notes, nested blocks, links and indexes; every note is also
written, one way, to a plain Markdown file. The native Mac app (`mac/`, see
[mac/DESIGN.md](mac/DESIGN.md)) offers one search box (words + meaning + titles, no
mode toggle), related notes as you write, nested folders, Ask (cited answers from
your notes), text read from every picture and PDF, and OneNote import.

**Non-goals:** multi-user, real-time collaboration, mobile, cloud sync, tags, pinned
notes, math, ink/handwriting, a command palette, whole-workspace (zip) export, server
history or undo.

**Open standards first.** Wherever people or other tools see what Fortress reads or
writes (the notes folder, exports, imports, the API) it uses the open or de-facto
standard (CommonMark, GFM, Obsidian Flavored Markdown, YAML front matter, ISO 8601,
OpenAPI) instead of a format of its own; see
[docs/open-standards.md](docs/open-standards.md). **Least surprise:** things behave
as in Apple's apps and Obsidian, and standard Mac behaviour (keys, menus, pasteboard,
Quick Look, services) is never overridden.

## 2. Data model

- **SQLite is the only source of truth** (`NOTES_DIR/.fortress.sqlite3`): folders,
  notes, nested blocks with stable ids, links, assets, OCR text and the full-text
  index. Every write goes to SQLite first.
- **Ids** are ULIDs (clients may send UUIDs for new blocks) and never change. Times
  are UTC ISO 8601.
- **Titles.** A note with no title of its own takes its first line, and follows it
  until the user sets one; an empty title hands it back to the first line.
- **Files.** Pasted and uploaded files are stored once in `NOTES_DIR/assets/`
  (`<ULID>.<ext>`, imported ones `<sha256>.<ext>`) and served at `/media/<name>`.
  They never change once written.
- **The Markdown mirror.** Every note in use is also a Markdown file, so the notes
  folder is an Obsidian vault: `NOTES_DIR/<Folder>/<Subfolder>/<Title>.md`, with YAML
  front matter (`title`, `aliases`, `created`, `updated`, `id`, `source`,
  `picture_text`), wikilinks between notes and `assets/` embeds relative to the note.
  The mirror is **write-only**: rewritten (about 0.5 s after an edit) whenever its
  note changes, removed when the note is trashed, renamed with the note, and never
  read back. It carries nothing Fortress-only. File names, escaping and every format
  decision: [docs/open-standards.md](docs/open-standards.md).
- **New workspace.** A notes folder without a database starts with the Welcome note
  (`backend/app/welcome.md`; `WELCOME_NOTE=false` turns it off).
- **Schema.** The database is at schema version 16. Older databases are refused at
  startup with the fix (`./upgrade-database.sh`); changes within 16 follow the
  policy in [docs/block-model.md §9](docs/block-model.md#9-schema-version-and-policy).

## 2a. Block shapes

The block model is the contract between the backend and every client:
**[docs/block-model.md](docs/block-model.md)**. In short: a block is a `type` from
one registry (`backend/app/block_schema.py`), `attrs`, inline `content` (text blocks:
text, link, note-link and block-reference nodes with marks) and a `payload` (raw
HTML only). Tables, columns, quotes and callouts are containers of ordinary child
blocks. Siblings are ordered by fractional `order` keys. The server derives plain
`text`, Markdown, links, backlinks and asset links, and rejects unknown types,
illegal nesting and cross-group type changes with 422.

## 2b. Invariants

What must stay true whatever changes:

1. **Nothing is stored that the registry doesn't describe**, and clients never send
   what the server derives (`text`, order keys, Markdown, labels of links).
2. **Edits are revision-checked transactions.** A transaction applies all its
   operations or none, only against the note's current revision (else 409, and the
   client reloads and re-applies). A retried `transaction_id` is never applied twice.
3. **Block ids are stable for life**: edits, moves, renames, re-imports of an
   unchanged paragraph and restores keep them, so links and block references never
   break silently. A link to a trashed or missing target is kept and shown broken.
4. **Links show the target's current title or text**; labels are never stored.
5. **Reads never write.** Opening a note is recorded by a separate `POST …/opened`.
6. **Undo belongs to the editor** (per note, named steps) and saves as ordinary
   transactions; the server keeps no history beyond backups.
7. **Nothing is deleted without the user.** Deleting a note moves it to the Trash;
   a re-import never deletes or overwrites a note edited here; a note whose source
   page is gone is only marked; a restore saves the current state first.
8. **Search, Ask and embeddings read plain `text`** (plus the text read from
   pictures), never Markdown, and the search corpus follows SQLite's change log, so
   no write path can leave an index stale.
9. **Only matching passages leave the Mac** (to the Ask model); nothing else does.
10. **Every pinned dependency is at least 7 days old** ([docs/dependencies.md](docs/dependencies.md)).

## 3. Backend

FastAPI on loopback (`backend/app/`, Python, uv). The Mac app starts it with a fresh
token and stops it on quit (§4).

### Local auth (`auth.py`)

Loopback is reachable by web pages too, so:

- Requests a browser made (an `Origin` header, or `Sec-Fetch-Site` other than
  `none`) are refused (403).
- With `FORTRESS_TOKEN` set (the Mac app makes a random one per launch) every
  request needs it as `X-Fortress-Token` or `Authorization: Bearer` (else 401); only
  `GET /api/health` is open. Every answer carries `X-Fortress-Instance` (derived from
  the token) so the app talks only to the backend it started.
- Without a token, the `Host` must be a loopback name (`FORTRESS_ALLOWED_HOSTS` adds
  more) and importing OneNote by server-side path is refused.
- With `FORTRESS_PARENT_PID` the backend exits once that process is gone.

### HTTP behaviour

- A note's GET carries a strong `ETag` that changes with anything in the payload,
  including the titles and text of linked notes and blocks (`If-None-Match` → 304).
  Writes that answer with the note send the same tag as `X-Document-ETag`. API
  responses are `Cache-Control: no-cache`.
- `/media` files are immutable (`max-age=31536000, immutable`), support ranges, and
  are never content-sniffed; non-allowlisted imported types are stored as `.bin`.
- Errors are JSON `{"detail": …}`; a database still locked after the busy timeout
  answers 503 with `Retry-After: 1`.

### Background work (`background.py`)

Work outside a request runs on *lanes*, one thread each, never on the executor that
serves search and Ask:

| lane | priority | work |
| --- | --- | --- |
| `mirrors` | high | the Markdown mirror follows SQLite |
| `imports` | normal | OneNote import jobs, one at a time (§7b) |
| `downloads` | normal | OneNote downloads from Microsoft, one at a time (§7b) |
| `backups` | normal | the daily snapshot (`AUTO_BACKUP_ENABLED`) |
| `ocr` | low | text in pictures and PDFs (§6) |
| `index` | low | ColBERT embeddings (`EMBEDDINGS_ENABLED`) |

Low-priority lanes wait while a search is scoring. A restore pauses every lane;
shutdown stops them and any child process (converter, download tool).

### Search, related notes and Ask

- **Corpus.** One snapshot of every paragraph's searchable text (its `text` plus the
  text read from its pictures), updated by deltas; search, related notes, Ask and the
  indexer share it.
- **Embeddings.** `lightonai/Agent-ModernColBERT` (pinned revision) via PyLate, one
  multi-vector document per paragraph, exact MaxSim. Stored in
  `NOTES_DIR/.fortress-embeddings.sqlite3`. On Apple Silicon it runs on the GPU with
  MLX, falling back to the CPU on any error. Until the model is loaded (or with
  embeddings off) every scorer falls back to keyword overlap.
- **Unified search.** Words (FTS5), meaning (ColBERT) and titles each rank blocks;
  reciprocal rank fusion combines them, with a small boost for recent edits and a cap
  per note. One box, one list; each result says why it matched.
- **Related notes.** Notes like the open note, or like one paragraph ("Seen
  before"), excluding the note itself; only results that beat what an unrelated
  paragraph would score are kept (`relevance.py`, calibrated cutoff).
- **Ask.** Candidates are gathered locally first (a literal grep for the question's
  key words plus unified search); one streamed OpenRouter call (`ASK_MODEL`) answers
  or asks for one more grep or whole note, within a 3-second budget. Every sentence
  must cite a verbatim quote from its passage, or it is dropped. When time runs out
  the closest passages are returned. Without `OPENROUTER_API_KEY` Ask answers
  `not_configured` with the closest passages.

## 4. Client

The native macOS app (SwiftUI + TextKit 2, macOS 26) is the only client
([mac/DESIGN.md](mac/DESIGN.md)). It finds the repository it was built in and
starts the backend (`backend/.venv/bin/uvicorn`, else `uv run --frozen uvicorn`)
with `FORTRESS_TOKEN`, `FORTRESS_PARENT_PID` and, when built,
`FORTRESS_ONENOTE_BIN`; a database too old to open shows the backend's reason.

## 6. Pictures and PDFs (`ocr.py`)

- Uploads return at once; reading happens later on the `ocr` lane, never in a
  request.
- Every picture and PDF is read once per content hash and OCR model: pictures with
  RapidOCR (PP-OCRv6, ONNX on the CPU), PDFs from their text layer, OCR'ing only
  pages without one. The queue is the `ocr_results` table, so it survives restarts;
  an asset that keeps failing is given up after 3 attempts.
- The Mac app also reads them with Apple Vision (`GET /api/ocr/pending`,
  `PUT /api/ocr/{hash}`); a reading that found text beats one that found none, then
  an external reader beats the CPU model. While it is active the CPU queue leaves the
  backlog to it.
- The text is **searchable, not shown**: it feeds search, related notes and Ask, and
  the inspector lists it; it never becomes part of the note's content.

## 7. Workspace safety

- **Trash.** Deleting a note moves it to the Trash (Undo toast); the Trash restores
  notes or deletes them forever.
- **Backups.** One automatic SQLite snapshot per day in `NOTES_DIR/.fortress-backups`
  (last 7 daily, 10 manual, 5 pre-restore). Restoring checks the snapshot's version,
  saves the current state as `pre-restore` first, and the mirror follows.
- **Import and export.** Markdown files (Obsidian notes included: wikilinks between
  files imported together resolve) become new notes; each note exports as Markdown;
  OneNote imports as in §7b.

## 7a. Where notes come from, and re-imports

Every note records its origin in `source_kind` (`manual`, `capture`,
`markdown_import`, `onenote_import`, …); an imported note also its source id
(`source_external_id`, unique) and what its importer said about the page (path,
source URL, the source's timestamps; `sources.import_details`, never searched).
Clients can't set any of it. Blocks record no provenance. The inspector's Info shows
the origin; the mirror's front matter only `source:` (an imported page's URL).

Importers write through `sources.upsert_document_from_source`. A re-import is a
**refresh**:

- `unchanged`: the source has what the last import wrote; nothing is written.
- `updated`: the note's blocks are replaced by the source's in one revision. The
  note keeps its id and folder, and its title follows the source unless the user
  renamed it. A block whose source id matches one of the last import keeps its id,
  so links into it survive and show the new text.
- `skipped_edited`: the note was edited here since the last import; it is left alone
  and reported (never merged or overwritten).
- `skipped_deleted`: the note is in the Trash.

A page the source no longer has is never deleted here: its note is marked
`source_removed_at` and reported. The refresh state is the note's `sync_json`
(docs/block-model.md §6).

## 7b. OneNote import (`onenote_import.py`, `onenote_graph.py`)

Decisions and background: [docs/onenote-import.md](docs/onenote-import.md). Two
inputs, one importer:

- **A file** (`.onepkg`, `.one`, `.onetoc2`, a folder or `.zip`): the Rust converter
  `tools/fortress-onenote` turns it into `manifest.json` + `assets/<sha256>.<ext>`
  ([SCHEMA.md](tools/fortress-onenote/SCHEMA.md), format version 1; others are
  refused). A folder or zip that already holds a manifest is imported as is.
- **Download from Microsoft** (the import sheet's default): the backend runs
  `tools/fortress-onenote-graph` (the user's own Entra app id, read-only scopes,
  self-paced under Microsoft's limits, resumable) on the `downloads` lane, shows its
  progress, sign-in code and rate-limit pauses, and hands the manifest it writes to
  an ordinary import job. Without the tool or uv, the status endpoint says so.

The import:

- Notebooks, section groups and sections become folders (keyed by OneNote's file
  identities); pages become notes keyed by page GUID. Subpages stay next to their
  parent, which gets a trailing *Subpages* list of links.
- Side-by-side outlines become `columns`; tables, lists, checklists, quotes, code,
  pictures (OneNote's OCR text as `alt`), attachments and marks map onto canonical
  blocks; OneNote-only details stay as extension attributes (`onenote_tags`,
  `list_format`, …). Ink is skipped and counted; an equation is kept as its plain
  linear text in a `code` block.
- Blocks are keyed by OneNote object id where the converter has one, so a paragraph
  edited in OneNote refreshes into the same Fortress block.
- `onenote:` links become note links, paragraph links become block references when
  the paragraph is found (a second pass resolves targets created in the same run);
  unresolved links stay text and are reported.
- Assets are copied once, deduplicated by hash, and queued for OCR.
- The report lists, per notebook, counts, failed sections with a hint, pages not
  refreshed because they were edited here, pages whose text coverage is below 98 %,
  unresolved links, and pages and sections removed in OneNote.
- Jobs run one at a time, survive restarts (an interrupted job reads `failed` with
  `interrupted: true`; importing again resumes in place, since re-imports are
  idempotent) and can be cancelled (pages already imported are kept).

## 8. Roadmap

- A PLAID/Voyager index for embedding search at larger scale.
- A larger OCR/VLM tier as an opt-in.
- Subpages as nested notes (the import keeps the page tree).
- Export folder, Obsidian attachments on import, Evernote ENEX import
  ([docs/open-standards.md](docs/open-standards.md#not-done-yet-high-value-moves-with-effort)).
