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
- [ ] Add deterministic block/document ID generation and validation.
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
- [ ] Preserve each imported Markdown file under a timestamped migration backup.
- [ ] Implement block-tree → Markdown export with stable IDs omitted from output.
- [ ] Add migration report: imported, skipped, duplicate, and failed files.
- [ ] Add tests for round trips, nested lists, empty documents, malformed input, and
  rerunning migration without duplicating documents.

### Phase 2 — block API

- [x] Add a read-only block-document endpoint for migration inspection.
- [ ] Add document list/get/create/rename/delete endpoints.
- [ ] Add block subtree endpoint with parent, children, depth, and breadcrumbs.
- [x] Add transaction endpoint with optimistic revision checks.
- [ ] Support insert, update text/type, move, duplicate, delete, and merge/split.
- [ ] Support batch transactions so paste, drag, and multi-block transforms are atomic.
- [ ] Return conflict details instead of silently overwriting a newer revision.
- [ ] Keep compatibility endpoints for old note clients until the new UI is complete.

### Phase 3 — block editor shell

- [x] Replace the single-note editor with a BlockNote-backed block tree editor.
- [x] Translate BlockNote block changes into atomic server transactions and hydrate
  legacy Markdown into rich inline content on first load.
- [x] Add stable block DOM lookup and keyboard focus by block ID.
- [ ] Add block selection, multi-select, copy/cut/paste, duplicate, and delete.
- [ ] Add block handle menu: convert, insert above/below, move, duplicate, delete,
  copy block link, copy block embed.
- [ ] Add slash command menu for paragraph, heading, bullet/number/task list,
  quote/callout, code, divider, image, and embed.
- [ ] Add keyboard behavior: Enter split, Backspace merge, Tab indent, Shift+Tab
  outdent, Mod+/ menu, Mod+Shift+Arrow block movement.
- [ ] Add drag handles and accessible keyboard equivalents for moving blocks.
- [ ] Add inline marks, links, code, highlight, math placeholder, and image blocks.
- [ ] Add undo/redo based on transaction history.

### Phase 4 — navigation and knowledge links

- [ ] Add document tree/sidebar with collapsed folders and recent documents.
- [x] Add outline from heading blocks with click-to-focus navigation.
- [x] Add `[[` document link autocomplete backed by stable document IDs.
- [ ] Add block references and embeds with live target previews.
- [x] Persist block/document references and add a backlinks panel grouped by
  source document and block, with click-to-focus navigation.
- [ ] Add block permalink/copy-link actions.
- [ ] Add tag and attribute editing at block and document level.
- [ ] Add graph view only after backlinks and reference queries are stable.

### Phase 5 — search and indexing

- [x] Build SQLite FTS5 indexing over canonical block text.
- [x] Update FTS rows in the same transaction as block writes.
- [x] Add block-level FTS search results and keep the existing note-search response
  compatible while the UI gains jump-to-block navigation.
- [x] Add search results at block granularity with context and jump-to-block.
- [ ] Add filters for document, block type, tag, status, and date.
- [ ] Adapt embedding search to index blocks, not whole notes; keep keyword fallback.
- [ ] Add related blocks/documents and explain why each result matched.
- [ ] Add rebuild-index command and startup integrity check.

### Phase 6 — assets and AI

- [ ] Attach assets to block IDs instead of parsing whole Markdown bodies.
- [ ] Keep OCR/caption sidecars or DB rows keyed by asset hash.
- [ ] Add block-scoped AI review with exact block IDs and quoted ranges.
- [ ] Render consistency, stale-fact, and link findings on the affected block.
- [ ] Add accept/reject transactions with before/after previews.
- [ ] Add context picker: current block, selected blocks, document, or linked blocks.

### Phase 7 — safety and portability

- [ ] Add autosave crash recovery and an edit journal.
- [ ] Add export-all, export-document, and export-selected-blocks to Markdown/HTML.
- [ ] Add import preview with conflict handling and no destructive default.
- [ ] Add database backup/restore verification.
- [ ] Add accessibility pass for block handles, menus, selection, and keyboard use.
- [ ] Add performance fixtures for 10k blocks and 1M-word documents.
- [ ] Add end-to-end tests covering migration, editing, search, references, export,
  restart recovery, and malformed/corrupt records.

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

## Current status

The repository still serves compatibility Markdown endpoints, but the normal note
surface now reads the SQLite block tree and uses BlockNote for editing. The next
slice is adding document hierarchy/navigation and live block link previews on top
of the same store.
