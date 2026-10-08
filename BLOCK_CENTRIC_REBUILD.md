# Fortress Notes: block-centric rebuild

This is the living implementation plan for replacing the current Markdown-file
note model with a block-first local knowledge workspace.

## Product boundary

Fortress Notes should feel like a fast, calm block editor for one person. It should
borrow the useful ideas behind SiYuan—stable block IDs, nested block trees, block
references, backlinks, block menus, outlines, and local search—without importing
the full product surface (mobile clients, sync, plugins, encryption, flashcards,
database views, publishing, or multi-user collaboration).

SiYuan is AGPL-3.0. We use its public format and behavior as design reference and
reimplement the needed behavior in this repository; we do not copy its source code.

## Target architecture

### Source of truth

- SQLite is the authoritative local store for documents, blocks, block attributes,
  assets, references, and edit history.
- Markdown is an import/export format, not the live storage format.
- Existing `notes/*.md` files are imported once, preserved as backups, and never
  silently deleted.
- SQLite migrations are numbered and run before the API serves requests.

### Core records

- `documents`: document identity, title, status, timestamps, sort position.
- `blocks`: stable ID, document ID, parent ID, sibling position, block type, typed
  attributes, rich inline content, timestamps.
- `block_attrs`: arbitrary user attributes kept separate from editor content.
- `block_refs`: parsed `[[block-id]]` / `((block-id "text"))` references and backlinks.
- `assets`: content hash, MIME type, dimensions, OCR/caption text, file path.
- `revisions`: append-only document/block changes for undo, recovery, and history.
- `blocks_fts`: rebuildable SQLite FTS5 index over block text and searchable asset text.

Every mutation goes through one transaction boundary. Block IDs are generated once
and survive edits, moves, exports, and imports. Positions are ordered values with a
rebalance operation, so moving a block does not rewrite unrelated siblings.

### Reuse map

Use a library when it owns a real product capability; keep the small amount of
Fortress-specific policy in the backend and adapters.

- **Block editor:** `@blocknote/core`, `@blocknote/react`, and the BlockNote UI
  package. This replaces the temporary custom contenteditable tree and supplies
  block handles, menus, nesting, drag/drop, inline formatting, and undo/redo.
- **Client data flow:** the existing TanStack Query layer remains responsible for
  loading, caching, and invalidating document queries.
- **HTTP/API boundary:** the existing FastAPI and Pydantic layer remains the
  narrow typed boundary; no second client state framework is needed.
- **Storage/search:** SQLite, foreign keys, WAL, and FTS5 remain the right local
  primitives for a single-user workspace. Use SQLAlchemy Core as a query builder
  when document, block, and filter queries become composable; do not add its ORM
  or let it hide SQLite-specific FTS5, recursive-tree, PRAGMA, backup, and
  transaction code. The current first slice stays on parameterized `sqlite3`
  statements because its queries are small and its SQLite behavior is explicit.
- **Markdown:** use BlockNote's native Markdown conversion for editor import/export
  where possible, and add a small server parser only for startup migration and
  headless export. Markdown is compatibility data, never the canonical model.
- **References/backlinks, block permissions, revision conflict rules, AI context
  selection, and asset ownership:** keep these as domain code because no selected
  editor library can safely own them for this app.
- **Collaboration, sync, graph visualization, and database views:** defer them;
  they are outside the local-first single-user product boundary.

### Editor boundary

The frontend editor owns a document tree whose top-level children are blocks. Use
BlockNote for the React editing surface instead of maintaining a second custom
contenteditable implementation. Its native block JSON maps to the SQLite rows;
the backend remains authoritative for IDs, ordering, attributes, references, and
transactions. Autosave sends block transactions (`insert`, `update`, `move`,
`delete`, `set-attrs`) rather than replacing a whole Markdown document.

BlockNote is preferred over Editor.js for this product because it already provides
the Notion-like interactions this app needs: nested blocks, indentation, block
movement, menus, and block-level change events. Editor.js remains a possible
export/import format or future alternative only if the product shifts toward a
plugin-driven publishing editor.

## Feature backlog

### Phase 0 — foundations

- [x] Research the current app and the relevant SiYuan workspace/AST behavior.
- [x] Choose SQLite as the canonical local store and Markdown as import/export.
- [x] Add SQLite settings, migration runner, schema version table, and test database.
- [x] Centralize generated block-store ULIDs and validate supplied insert IDs.
- [x] Add a SQLite backup API and integrity-check primitive; wire recovery and
  user-visible export flows later.
- [x] Add a feature flag so the old Markdown reader remains available during migration.
- [x] Select BlockNote as the frontend block editor and keep the editor dependency
  separate from the canonical SQLite data model.
- [x] Map the rebuild backlog to existing libraries and explicitly avoid adding
  frameworks where SQLite, FastAPI, or TanStack Query already cover the need.
- [x] Choose a query-builder boundary: SQLAlchemy Core later for composable reads,
  raw parameterized SQLite for FTS5, recursive tree writes, PRAGMAs, and backups;
  no full ORM.

### Phase 1 — data model and migration

- [x] Implement the first document/block CRUD and transaction slice in one backend store module.
- [x] Implement nested tree queries and sibling ordering.
- [x] Implement Markdown → block-tree import for headings, paragraphs, nested lists,
  tasks, quotes, code, thematic breaks, links, images, and unsupported raw blocks.
- [x] Preserve each imported Markdown file under a timestamped migration backup.
- [x] Implement block-tree → Markdown export with stable IDs omitted from output.
- [x] Add migration report: imported, skipped, duplicate, and failed files.
- [x] Add tests for round trips, nested lists, empty documents, malformed input, and
  rerunning migration without duplicating documents.

### Phase 2 — block API

- [x] Add a read-only block-document endpoint for migration inspection.
- [x] Add document list/get/create/rename/delete endpoints.
- [x] Add block subtree endpoint with parent, children, depth, and breadcrumbs.
- [x] Add transaction endpoint with optimistic revision checks.
- [x] Support insert, update text/type, move, duplicate, delete, and merge/split.
- [x] Support batch transactions so paste, drag, and multi-block transforms are atomic.
- [x] Return conflict details instead of silently overwriting a newer revision.
- [x] Keep compatibility endpoints for old note clients until the new UI is complete.

### Phase 3 — block editor shell

- [x] Replace the single-note editor with a BlockNote-backed block tree editor.
- [x] Translate BlockNote block changes into atomic server transactions and hydrate
  legacy Markdown into rich inline content on first load.
- [x] Add stable block DOM lookup and keyboard focus by block ID.
- [x] Add block selection, multi-select, copy/cut/paste, duplicate, and delete.
- [x] Use BlockNote's side menu to insert after and delete blocks, and its drag
  handle to move them.
- [x] Add app selection actions for duplicate, delete, copy a block link, and copy a block embed.
- [x] Add block actions to convert, insert above, and copy block embed.
- [x] Keep BlockNote's default slash menu enabled for paragraphs, headings,
  bullet/number/task lists, quotes, code, dividers, and image block insertion.
- [x] Add slash commands for simple rich-text callouts and block embeds using the
  existing stable block references and live target previews.
- [x] Restore saved image blocks as image blocks when loading a document.
- [x] Upload files from BlockNote's image picker, clipboard, and drag/drop handlers;
  preserve media block props when loading a saved document.
- [x] Use BlockNote's default Enter split, Backspace merge, Tab indent, Shift+Tab
  outdent, and Mod+Shift+Arrow block movement behavior.
- [x] Add a Mod+/ shortcut to open the slash menu.
- [x] Use BlockNote drag handles for pointer-based block movement.
- [x] Add keyboard-accessible move up/down controls to the selected-block toolbar.
- [x] Review editor keyboard behavior and screen-reader names/roles. The editor
  has a name; formatting and selected-block controls use named groups; formatting
  buttons expose labels and pressed state; save status and errors use live
  announcements. Text selection keeps normal Delete/Backspace behavior, and
  keyboard-selected text syncs before bold/italic shortcuts apply.
- [x] Use BlockNote's inline formatting, links, code, highlights, and rich HTML
  clipboard for formatted text copy/paste. Cross-app image embedding is tracked
  separately below.
- [x] Add a math placeholder block. It remains a placeholder, without math editing
  or rendering support.
- [x] Use BlockNote's local undo/redo history during the current editor session.
- [x] Add undo/redo backed by persistent transaction history.

### Phase 4 — navigation and knowledge links

- [x] Add document tree/sidebar with collapsed folders and recent documents.
- [x] Add outline from heading blocks with click-to-focus navigation.
- [x] Add `[[` document and `((` block-reference autocomplete backed by stable IDs.
- [x] Add block references and embeds with live target previews.
- [x] Persist block/document references and add a backlinks panel grouped by
  source document and block, with click-to-focus navigation.
- [x] Add block permalinks that reopen the document and focus the linked block.
- [x] Add separate block-level tags and key/value attributes, stored independently
  from BlockNote rendering props and updated through block transactions.
- [x] Add document tag editing in the note header.
- [x] Defer graph view; graph visualization is outside the product boundary.

### Phase 5 — search and indexing

- [x] Build SQLite FTS5 indexing over canonical block text.
- [x] Update FTS rows in the same transaction as block writes.
- [x] Add block-level FTS search results and keep the existing note-search response
  compatible while the UI gains jump-to-block navigation.
- [x] Add search results at block granularity with context and jump-to-block.
- [x] Add filters for document, block type, tag, status, and date.
- [x] Adapt embedding search to index blocks, not whole notes; keep keyword fallback.
- [x] Add related blocks/documents and explain why each result matched.
- [x] Add rebuild-index endpoint and startup integrity check.

### Phase 6 — assets and AI

- [x] Attach uploaded assets to exact block IDs in the block transaction store.
- [x] Keep OCR/caption text in an asset-hash keyed DB row and sidecar, and include it
  in block FTS and embedding inputs.
- [x] Add block-scoped AI review with exact block IDs and source-validated quotes.
- [x] Show single-block AI review findings with their exact block ID and quote.
- [x] Render existing document consistency, stale-fact, and broken-link findings
  beside blocks when the reported claim or URL matches saved block text.
- [x] Show a before/after preview and accept stale-fact suggestions when the exact
  finding covers plain text runs with identical formatting; acceptance uses the
  normal revision-checked block edit and is undoable. Links, mixed formatting,
  and non-text block content stay preview-only.
- [x] Save accept/reject decisions across reloads in browser storage.
- [x] Support safe suggestion edits across richer block content when the exact
  finding spans plain text runs with identical formatting. Mixed formatting,
  links, stale text, and non-text content stay preview-only.
- [x] Offer review actions for the current block and the whole document.
- [x] Add a review context picker for the current block, selected blocks, whole
  document, and directly linked blocks.

### Phase 7 — safety and portability

- [x] Persist queued block edits in browser storage and replay them after reload;
  store a server receipt atomically with each edit so a lost response can be
  retried without applying the edit twice. Edits still depend on browser storage
  being available, and real revision conflicts remain visible for resolution.
- [x] Add export-all and export-document to Markdown for compatibility.
- [x] Add a read-only Markdown import-preview API that reports ready, skipped,
  conflicting, and invalid files before import.
- [x] Verify database backups can be restored and pass integrity checks.
- [x] Add large-input fixtures for 10k blocks and 1M-word documents; they verify
  loaded shape and size without asserting timing thresholds.
- [x] Add a backend API flow test covering migration, editing, search, references,
  export, restart recovery, and malformed requests.
- [x] Add app integration coverage for deep-link focus and sidebar navigation;
  add an API test that rejects corrupted block JSON without changing the record.
- [x] Add a repeatable real-browser end-to-end test suite for blank-note editing,
  bold and italic shortcuts, rich HTML clipboard copy/paste with an inline image,
  same-origin image embedding, reload recovery, and undo/redo.
- [x] Verify copied page HTML includes formatting and image bytes. Real-browser
  clipboard checks confirm loaded same-origin images are copied as data URLs.
  Word/Docs paste was not checked, at the user's request; unloaded and
  cross-origin images retain their URLs.

### Phase 8 — everyday workspace flows

- [x] Turn delete into Move to Trash: an Undo toast restores the note immediately,
  and a Trash dialog restores, deletes forever, or empties the trash. Restoring puts
  the note back in its folder (or the top level if the folder is gone).
- [x] Wire the backup primitive into a recovery flow: a daily automatic SQLite
  snapshot (last 7 kept), "Back up now & download", and restore from any snapshot.
  Every restore first saves a `pre-restore` snapshot, verifies the chosen snapshot's
  integrity and schema version, rebuilds FTS, and moves stray Markdown mirrors aside
  instead of deleting them.
- [x] Add user-visible Markdown export of one note from the header or right-click
  menu.
- [x] Add user-visible Markdown import: pick `.md` files to create new notes, with
  front-matter titles, tags, and status preserved and unreadable files reported.
- [x] Add saved searches stored in SQLite (migration 11) and shown as one-click
  chips under the search box.
- [x] Add keyboard navigation: ⌘/Ctrl+K or `/` to search, Alt+N for a new note,
  arrow keys and Enter through results, Escape to clear, `?` for tips.

### Phase 9 — one source of truth and a sturdier editor

- [x] Serve every note read from SQLite and write SQLite first; `.md` files become
  atomic, write-only mirrors managed by `documents.py`. The legacy Markdown editor
  fallback (which could replace a whole block document) and `notes_store` are gone.
- [x] Batch editor keystrokes into one save per pause; keep editing offline with
  automatic retry; show a "load latest version" choice on a real conflict; adopt
  outside changes such as a backup restore.
- [x] Fix backup restore pruning its own source, FTS gaps after trash/restore,
  folder position drift, and title edits that raced per keystroke.

## Acceptance gates

1. A fresh workspace can create, edit, move, nest, split, merge, and delete blocks;
   a restart preserves the exact tree and stable IDs.
2. Existing Markdown notes can be previewed, imported without loss of supported
   structure, and exported back without deleting the originals.
3. Search, related results, OCR text, references, and backlinks operate at block
   granularity and jump to the correct block.
4. Every multi-step edit is atomic, conflict-aware, undoable, and recoverable.
5. The current AI review features still work, but their findings attach to blocks
   instead of whole-note text ranges.
6. The app remains local-first, single-user, and simple to operate.
7. People can edit and copy/paste rich text with familiar controls without needing to
   know the storage or interchange format; copied pages include formatting and image bytes.

## Current status

The repository still serves compatibility Markdown endpoints, but the normal note
surface reads the SQLite block tree and uses BlockNote for editing. SQLite is opened
with foreign keys, WAL, a busy timeout, full synchronous durability, numbered
migrations, FTS5, backups, and integrity checks. The editor supports familiar
formatting shortcuts, rich HTML text clipboard handling, local media uploads, block
attributes, links that reopen at a block, and persistent undo/redo. Block-level AI
review includes current, selected, document, and directly linked context; users can
keep accept/reject decisions across reloads. Real-browser tests verify blank-note
editing, bold and italic shortcuts, rich HTML copy/paste with images, reload recovery,
and undo/redo. Clipboard output embeds loaded local image bytes; paste into Word or
Docs was not checked at the user's request. Markdown remains a compatibility and
storage format; people can use the editor as a rich-text editor without seeing or
editing it. Deleted notes go to a recoverable Trash, the workspace is snapshotted
daily with one-click restore, notes import from and export to Markdown, saved
searches re-run in one click, and the main flows are reachable from the keyboard.
