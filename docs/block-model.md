# Block model (the contract)

This document is the single contract between the backend and every client (the
Mac app, importers, and Markdown import/export and the mirror). The backend implements it in
`backend/app/block_schema.py` (the type registry and inline content),
`backend/app/order_keys.py` (sibling order), `backend/app/block_markdown.py`
(Markdown in and out) and `backend/app/block_store.py` (storage and
transactions).

The principles:

- **One canonical shape.** A block is a `type`, `attrs`, inline `content` (text
  blocks only) and a `payload` (structured data, HTML only). Structure
  (lists of rows, cells, columns, quoted paragraphs) is always child blocks,
  never JSON inside a block.
- **The server derives everything else**: plain `text`, Markdown, links and
  backlinks, search indexes, asset links, sibling order keys. Clients never send
  them, and the server ignores them if they do.
- **Unknown types, illegal nesting and cross-category type changes are
  rejected** (HTTP 422, with the reason in `detail`). Nothing is stored that
  the registry doesn't describe.

## 1. A block

What `GET /api/block-documents/{id}` returns for each block (children nested in
document order):

```jsonc
{
  "id": "01JD5Q2Y3K4M5N6P7Q8R9S0T1V",   // ULID (server) or UUID (client); stable for life
  "document_id": "01JD5Q...",
  "parent_id": null,                      // null: a top-level block
  "order": "a3",                          // sibling order key (server-assigned, read-only)
  "position": 3,                          // DEPRECATED: index among siblings (derived, read-only)
  "type": "heading",
  "attrs": {"level": 2},
  "content": [                            // inline content: text-category types only, else null
    {"type": "text", "text": "Hello "},
    {"type": "text", "text": "world", "marks": {"bold": true}}
  ],
  "payload": null,                        // structured data: html only, else null
  "text": "Hello world",                 // derived plain text (read-only)
  "user_attrs": {},                       // the person's own key/values (see §5)
  "revision": 41,                         // document revision that last changed it (null: never)
  "created_at": "2026-11-18T09:00:00+00:00",
  "updated_at": "2026-11-18T09:05:00+00:00",
  "children": []
}
```

The document payload around the blocks is unchanged (`id`, `title`, `revision`,
`resolved_links`, `children`, ...; where a note came from is not in it, spec §7a).
Blocks have no provenance of their own.
`resolved_links` gives the current title of every linked note and the current
text of every referenced block, so clients render `docLink`/`blockRef` labels
from it (labels are never stored).

## 2. Type registry

Categories:

- **text**: has inline `content` (a list of inline nodes, §3). Text blocks may
  have children (nested list items, indented paragraphs) unless noted.
- **container**: no inline content; its content is its child blocks.
- **atomic**: no inline content and no children.

"flow" below means every type a document body may hold: all text types plus
`quote`, `callout`, `columns`, `table`, `divider`, `image`, `file`, `html`.
Top-level blocks must be flow types.

Fortress has no math or ink (drawings, handwriting): there is no `math` block
and no ink attribute. Formulas are kept as `code` blocks: a OneNote equation as its
plain linear text (`language: "text"`, docs/onenote-import.md), a Markdown `$$ … $$`
formula verbatim (`language: "latex"`, §7). Schema 15 converted stored math blocks (§10).

| type | category | attrs | payload | children |
|---|---|---|---|---|
| `paragraph` | text | — | — | flow |
| `heading` | text | `level`: int 1–6 (default 1) | — | flow |
| `bullet_item` | text | — | — | flow |
| `numbered_item` | text | — | — | flow |
| `check_item` | text | `checked`: bool (default false) | — | flow |
| `code` | text (plain: no marks, no links) | `language`: string | — | none |
| `quote` | container | — | — | flow |
| `callout` | container | `icon`: string, `color`: colour | — | flow |
| `columns` | container | — | — | `column` |
| `column` | container | `width`: number > 0 (relative weight) | — | flow except `columns` |
| `table` | container | `column_widths`: list of (px number or null), `header_rows`: int ≥ 0, `header_cols`: int ≥ 0, `borders`: bool | — | `table_row` |
| `table_row` | container | — | — | `table_cell` |
| `table_cell` | container | `colspan`, `rowspan`: int ≥ 1, `background`, `color`: colour, `align`: `left`/`center`/`right`/`justify` | — | flow except `columns` |
| `divider` | atomic | — | — | none |
| `image` | atomic | `url`, `alt`, `caption`, `name`: string; `width`, `height`: number ≥ 0 (px) | — | none |
| `file` | atomic | `url`, `name`, `mime`, `caption`: string; `size`: number ≥ 0 (bytes) | — | none |
| `html` | atomic | — | `{"html": string}` (raw HTML kept from an import) | none |

A colour is `#rgb`/`#rrggbb`/`#rrggbbaa`, a CSS colour name, or `rgb()/rgba()/hsl()/hsla()`.

**Attributes.** Declared attributes are validated per type. Any other
lower-case key (`^[a-z][a-z0-9_]{0,63}$`) is an *extension attribute* (importer
metadata such as OneNote's `onenote_tags`, `list_format`, `original_url`): any
JSON value, kept verbatim, never interpreted by the server. Clients must keep
attributes they don't understand; the attrs PATCH semantics (§4) do that for
free.

**Type changes** (`update` with a new `type`) are allowed only within a change
group: any text type ↔ any text type, and `quote` ↔ `callout`. Everything else
(image → file, paragraph → table, ...) is rejected; delete and insert instead.
On a type change, declared attributes of the old type that the new type doesn't
declare are dropped (a heading's `level` when it becomes a paragraph); extension
attributes stay. Marks are removed from content that becomes `code`.

**Nesting** is checked on every insert, move and type change: the parent's
children list must allow the child's type, and a block can't move into its own
subtree. Containers may be empty (a client builds a table with several inserts
in one transaction).

### Tables and columns

```
table {column_widths, header_rows, header_cols, borders}
└─ table_row
   └─ table_cell {colspan, rowspan, background, color, align}
      └─ paragraph / bullet_item / image / table / ...   (ordinary blocks with ids)

columns
└─ column {width}
   └─ ordinary blocks
```

Cell contents are ordinary blocks: they have ids, can be linked to, and are
searched and embedded like any other block. A one-line cell
is a cell with one paragraph.

### Quotes and callouts

`quote` and `callout` are containers: `> one\n>\n> two` is a quote holding two
paragraphs. A quote with a single line is a quote with one paragraph.

## 3. Inline content

`content` is a flat list of inline nodes. There are no offsets anywhere in the
API, so there is no UTF-16 vs code point question: text is plain Unicode
strings, and a line break inside a block is `"\n"` in a text node.

| node | shape |
|---|---|
| text | `{"type": "text", "text": "…", "marks"?: {…}}` |
| link | `{"type": "link", "href": "https://…", "content": [text nodes]}` |
| note link | `{"type": "docLink", "id": "<document id>"}` |
| block reference | `{"type": "blockRef", "id": "<block id>"}` |

Marks (all optional; omitted when false/empty):

| mark | value |
|---|---|
| `bold`, `italic`, `underline`, `strike`, `code` | `true` |
| `sup`, `sub` | `true` (not both) |
| `color` | colour (text colour) |
| `highlight` | colour (background) |

Rules (the server normalizes to this form; it's the only form it stores and
serves):

- Empty text nodes are dropped; neighbouring text nodes with equal marks are
  merged; neighbouring links with the same `href` are merged.
- A link's `content` holds only text nodes and is never empty. `href` must be
  non-empty and must not use the `javascript:`, `vbscript:` or `data:` scheme
  (refused on write; such links read from Markdown, HTML or old data stay text).
- `"\n"` inside a text node is a line break within the block. Markdown soft
  breaks (a single newline inside a paragraph) are read as line breaks too, as
  Obsidian and Bear show them; they are written back as hard breaks.
- `docLink` and `blockRef` carry no label: their visible label is the target's
  current title / text, from `resolved_links`. A link to a trashed or missing
  target renders as a broken link; it is kept.
- `code` blocks hold only unmarked text nodes.

## 4. Operations

`POST /api/block-documents/{id}/transactions`

```json
{"transaction_id": "uuid", "base_revision": 41, "operations": [ ... ]}
```

| operation | fields |
|---|---|
| `insert` | `block_id`? (ULID/UUID; server generates one if omitted), placement (below), `type` (required), `attrs`?, `content`?, `payload`?, `user_attrs`? |
| `update` | `block_id`, and at least one of: `type`, `attrs` (PATCH), `content` (replaces the inline content), `payload` (replaces it), `user_attrs` (PATCH); an update naming none of them is refused (422) |
| `move` | `block_id`, placement |
| `delete` | `block_id` (deletes the whole subtree) |

**Placement** (insert and move): `after_id` (place right after that sibling),
or `before_id` (right before it), or neither (append as the last child of
`parent_id`; `parent_id` omitted or null means the top level). With `after_id`/
`before_id` the parent is that block's parent; a `parent_id` sent alongside must
agree. Give one of `after_id`/`before_id`, not both. The server assigns the
order key; clients never send one.

**PATCH semantics** (`attrs`, `user_attrs` on update): keys present are set, a
key set to `null` is deleted, keys not mentioned are kept. On insert, `attrs` is
the full set (null values are ignored). Defaults (`heading.level = 1`,
`check_item.checked = false`) are filled in by the server.

**Atomicity and conflicts.** A transaction applies all of its operations or
none. It is accepted only when `base_revision` equals the document's current
`revision`; otherwise the response is HTTP 409 with
`{"code": "document_revision_conflict", "base_revision", "current_revision"}`
and the client re-reads the document and re-applies its edit. A successful
transaction bumps the revision by one (undo is the client's: it saves as an
ordinary transaction). Re-sending the same
`transaction_id` with the same body returns the result again without applying
twice; with a different body it is rejected.

Later operations in a transaction see the effects of earlier ones (insert a
table, then its rows and cells, then the cells' paragraphs).

Removed from the old API: `split`, `merge`, `duplicate`, `set_attrs` (use
`update` with an attrs patch), `set_user_attrs` (use `update.user_attrs`),
`expected_updated_at` (the revision check covers it), `position` (use
`after_id`/`before_id`), and client-sent `text` and `content.markdown`. An
operation with a field not listed above is refused (422).

## 5. What the server derives (never accepted from clients)

| derived | from |
|---|---|
| `text` | the block's own content: text nodes; `docLink` → the note's title, `blockRef` → the block's first line; image `alt`/`caption`; file `name`/`caption`; html → its text without tags; containers → `""`. No Markdown syntax, ever. Search, Ask, embeddings, titles and snippets read this. Kept current when a linked note is renamed or a referenced block changes. |
| `order`, `position` | placement in operations |
| Markdown | rendered from the canonical blocks on export and for the mirror (§7) |
| links and backlinks | `docLink`/`blockRef` nodes in `content` (block_refs, document_refs tables) |
| full-text index, ColBERT input, Ask corpus | `text` plus the text read from the images and PDFs a block shows (`assets.text`, filled by the OCR queue, `backend/app/ocr.py`; never part of `text` or the visible content; listed per block as `picture_text` in `…/info`) |
| asset links | `/media/...` URLs in attrs |
| `revision`, `created_at`, `updated_at` | the transaction that wrote the block |
| re-import state | the importer (the note's `sync_json`, §6) |

`user_attrs` are the person's own key/value pairs on a block (due dates, ...);
they are not part of the content and don't affect `text`.

## 6. Re-import state

A note imported from a source (OneNote) is found again by its source key
(`documents.source_kind`, `source_external_id`). Re-importing it is a refresh
(spec §7a): unchanged source, nothing happens; changed source, the note's blocks
are replaced by the source's; edited here since the last import, the note is
left alone and reported. What a refresh needs lives in the note's
`documents.sync_json`: the note's revision after the last import (a later
revision means "edited here"), a hash of what the source had, and which block
each source id became (`blocks`, plus `anchors` for other ids, such as those
links name). Matching blocks keep their ids, so links and references into them
survive. Nothing is kept per block.

## 7. Markdown

Markdown is an interchange format, rendered from the canonical blocks; it is
never stored. It is CommonMark + GFM + Obsidian Flavored Markdown, chosen per
[open-standards.md](open-standards.md): the vault mirror and exports read well in
Obsidian, GitHub, VS Code or Pandoc. Writing is one way: the mirror and exports
are never read back as Fortress notes, so they carry nothing Fortress-only (no
metadata comments, provenance or round-trip attributes); what Markdown can't say
stays in the database.

Writing (`render_markdown`, with `RenderLinks` naming notes and blocks):

- Text: `#` headings; `- `, `1. `, `- [ ] `/`- [x] ` lists, children indented
  under their item by its bullet's width (a task's `[ ] ` doesn't count); `code`
  as a fence longer than any backtick run inside; `divider` as `---`; `html` as itself.
- `quote`: `> ` before each line of its children. `callout`: an Obsidian callout,
  `> [!type]` (attrs `callout_type`, default `note`; `callout_fold` `+`/`-`; with
  `callout_title` its first paragraph is the title on the marker line).
- Marks: `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, `==highlight==` (the
  default `yellow`); underline, sup, sub, colour and other highlight colours as
  `<u>`, `<sup>`, `<sub>`, `<span style="color:…">`, `<mark style="background:…">`.
  If the Markdown form would not read back as the same nodes, every mark is
  written as an HTML tag. Text is escaped so it never becomes Markdown or Obsidian
  syntax (`==`, `%%`, `^id`, `[[`, `((`, ...).
- Links `[text](href)`. Note links are wikilinks by the target's vault file
  name: `[[Title]]`, `[[Folder/Title]]` when the name isn't unique,
  `[[Name|Title]]` when it differs from the title; an unknown target `[[<id>]]`.
  Block references: `[[Note#^<block id>|first line]]` (`[[#^id]]` within the
  note), a heading `[[Note#Heading text]]`.
- Block ids: written only for blocks something refers to (`RenderLinks.anchors`;
  none when no `RenderLinks` is given). Obsidian's `^id` at the end of a
  text block's line, or on its own line after a quote, callout, table, code,
  divider or HTML block; headings (Obsidian links them by text) and ids Obsidian
  can't hold (`_`) get none.
- Children of a paragraph or heading (Markdown can't nest under them) follow it.
  Extension attributes and provenance are not written.
- Tables: a GFM table when the table has one header row, every cell holds one
  paragraph (or nothing) and there are no spans, colours or widths; otherwise a
  plain HTML `<table>`.
- Columns (no Markdown dialect has them): the contents in order.
- Images: `![alt|width(xheight)](url "caption")` (Obsidian's size syntax);
  files: `[name](url)`, or `![name](url)` for PDF, audio and video. In the vault,
  `/media/<name>` is written as a path relative to the note (`assets/<name>`,
  `../assets/<name>`).
- An empty paragraph (no content, no children) is written as nothing: Markdown
  has no empty paragraph, and a blank line is just spacing.

Reading Markdown (uploads, capture, new notes created with a body, importer
sources) produces canonical blocks directly (`parse_markdown`, with
`ParseLinks` resolving names). It is a plain CommonMark/GFM/Obsidian reader:
wikilinks by file name, path, title or alias, `[[Note#Heading]]`,
`[[Note#^id]]`, embeds `![[file.png|300]]` / `![[file.pdf]]`, callouts,
`==highlight==`, a `$$ … $$` formula (a `code` block, `language: "latex"`, so
its source is kept verbatim), block ids (Obsidian's short ids become new workspace-unique ids,
consistently for every reference in the import), and Markdown links/images to
`assets/<name>` (read as `/media/<name>`). An unresolved wikilink stays text.
Only `<table>` and `<img>` HTML is read structurally; an HTML block that is only
a comment is dropped and any other becomes an `html` block. An id is reused when it isn't
already taken by another block in the workspace; otherwise a new id is made.

## 8. Order keys

Siblings are ordered by `order`, a fractional-index string key
(`order_keys.py`: the base-62 "integer part + fraction" scheme used by Figma and
Logseq). Keys compare by plain byte order. The server generates a key between
the neighbours named by the placement, so inserts and moves write one row and
never renumber siblings.
A unique index on `(document_id, parent_id or '', order)` covers top-level
blocks too. Clients only ever sort by it (or rely on the order of `children`).

## 9. Client migration (Mac app) — done

The Mac app reads and writes the canonical model (`mac/Sources/FortressKit/Editor/Model/`:
`BlockCodec` reads and writes inline nodes, `BlockDiff` sends `insert`/`update`/
`move`/`delete` with `after_id`/`before_id`/`parent_id` and attrs patches,
`docLink`/`blockRef` nodes for note links, tables and quotes as child blocks).
The boundary compatibility layer the server kept during the migration is
removed: the transaction API accepts only the shapes of §2-§4, and anything else
is refused with HTTP 422 (nothing is written):

- older or BlockNote type names (`bulletListItem`, `numberedListItem`,
  `checkListItem`, `list`, `codeBlock`, `blockquote`, `hr`, ...) are unknown
  types;
- `content` must be a list of inline nodes (or null): `{"blocknote": runs}`,
  `{"markdown": …}` and `{"latex": …}` objects are refused; `[[id|Title]]`/`((id))` typed as text stay text (send `docLink`/
  `blockRef` nodes);
- inline content on a `quote`/`callout` (a container: send paragraph children),
  BlockNote attrs such as image `previewWidth` (use `width`);
- fields outside §4 — `position` (use `after_id`/`before_id`/`parent_id`),
  `text`, `expected_updated_at`, ... — are refused, not ignored.

The `position` in responses (§1) is still served, read-only.

## 10. Stored data and older databases

A new database is created at schema version 16 with every table, index and
trigger (`block_store._SCHEMA`); a database at 16 opens as it is. Version 16 is
the schema the `last-migrating-release` branch leaves a database in after its
startup cleanup, which keeps the database recorded as 15: a database at 15
with nothing of that cleanup left (`block_sync`, `blocks.provenance_json`,
`documents.status`/`tags_json`, the undo history and other retired tables) is
recorded as 16 and opens. This version no longer migrates: anything older (an
older version, a 15 not yet cleaned up, or notes with no version) is refused at
startup with a message saying to run `./upgrade-database.sh`, which backs the
file up and upgrades it with the code of `last-migrating-release`, the last release with the migrations (BlockNote shapes
to canonical blocks in 14, math and ink retired in 15, then the cleanup); a
database at a newer version is refused as before (`block_store.stored_version`).
Restoring a backup checks the same. The Mac app shows the backend's reason (the
line after `Fortress can't start: ` in its log) instead of the exit code.

**Schema changes within version 16** need no migration code. Startup (cheap, every
time) creates missing tables and indexes (`CREATE ... IF NOT EXISTS`), runs the
idempotent `DROP TABLE/INDEX IF EXISTS` statements in `block_store._RETIRED` for
objects a release retired, and drops every trigger and creates the current ones
again, so a changed or retired trigger reaches existing databases. Anything else
(a new column, a changed table) needs the owner's decision on a version bump.
