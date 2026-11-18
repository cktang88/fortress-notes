# Block model (the contract)

This document is the single contract between the backend and every client (the
Mac app, importers, and Markdown import/export and the mirror). The backend implements it in
`backend/app/block_schema.py` (the type registry and inline content),
`backend/app/order_keys.py` (sibling order), `backend/app/block_markdown.py`
(Markdown in and out) and `backend/app/block_store.py` (storage and
transactions). `GET /api/block-schema` serves the registry as JSON, so clients
can check what they send against the server's own rules.

The principles:

- **One canonical shape.** A block is a `type`, `attrs`, inline `content` (text
  blocks only) and a `payload` (structured data, math and HTML only). Structure
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
  "payload": null,                        // structured data: math and html only, else null
  "text": "Hello world",                 // derived plain text (read-only)
  "user_attrs": {},                       // the person's own key/values (see §5)
  "provenance": {"source": "manual"},     // where it came from (spec §7a)
  "revision": 41,                         // document revision that last changed it (null: never)
  "created_at": "2026-11-18T09:00:00+00:00",
  "updated_at": "2026-11-18T09:05:00+00:00",
  "children": []
}
```

The document payload around the blocks is unchanged (`id`, `title`, `revision`,
`can_undo`, `can_redo`, `provenance`, `resolved_links`, `children`, ...).
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
`quote`, `callout`, `columns`, `table`, `divider`, `image`, `file`, `math`,
`html`. Top-level blocks must be flow types.

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
| `math` | atomic | — | `{"latex": string, "mathml"?: string}` | none |
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

Cell contents are ordinary blocks: they have ids, can be linked to, carry
provenance and are searched and embedded like any other block. A one-line cell
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
| `insert` | `block_id`? (ULID/UUID; server generates one if omitted), placement (below), `type` (required), `attrs`?, `content`?, `payload`?, `user_attrs`?, `provenance`? |
| `update` | `block_id`, and at least one of: `type`, `attrs` (PATCH), `content` (replaces the inline content), `payload` (replaces it), `user_attrs` (PATCH), `provenance` (merge); an update naming none of them is refused (422) |
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

**provenance** on insert says where new content came from (`paste`, `web`,
`ai`, ...; default `manual`); on update its fields are merged into the block's
provenance (nothing is removed). Server-maintained fields (re-import state) are
never accepted from clients (§6).

**Atomicity and conflicts.** A transaction applies all of its operations or
none. It is accepted only when `base_revision` equals the document's current
`revision`; otherwise the response is HTTP 409 with
`{"code": "document_revision_conflict", "base_revision", "current_revision"}`
and the client re-reads the document and re-applies its edit. A successful
transaction bumps the revision by one and is one undo step
(`POST .../undo` / `.../redo` with `base_revision`). Re-sending the same
`transaction_id` with the same body returns the result again without applying
twice; with a different body it is rejected.

Later operations in a transaction see the effects of earlier ones (insert a
table, then its rows and cells, then the cells' paragraphs).

Removed from the old API: `split`, `merge`, `duplicate`, `set_attrs` (use
`update` with an attrs patch), `set_user_attrs` (use `update.user_attrs`),
`expected_updated_at` (the revision check covers it), and client-sent `text`
and `content.markdown`.

## 5. What the server derives (never accepted from clients)

| derived | from |
|---|---|
| `text` | the block's own content: text nodes; `docLink` → the note's title, `blockRef` → the block's first line; image `alt`/`caption`; file `name`/`caption`; math `latex`; html → its text without tags; containers → `""`. No Markdown syntax, ever. Search, Ask, embeddings, titles and snippets read this. Kept current when a linked note is renamed or a referenced block changes. |
| `order`, `position` | placement in operations |
| Markdown | rendered from the canonical blocks on export and for the mirror (§7) |
| links and backlinks | `docLink`/`blockRef` nodes in `content` (block_refs, document_refs tables) |
| full-text index | `text` plus OCR/caption text of the images a block shows |
| asset links | `/media/...` URLs in attrs |
| `revision`, `created_at`, `updated_at` | the transaction that wrote the block |
| re-import state | the importer (block_sync table, §6) |

`user_attrs` are the person's own key/value pairs on a block (due dates, ...);
they are not part of the content and don't affect `text`.

## 6. Provenance and re-import state

`provenance` (spec §7a) records where a block came from: `source`,
`external_id`, `source_path`, `source_url`, timestamps, `importer`, `origin`,
`extra`. It's set at insert and only ever merged into afterwards, so ordinary
edits can't clobber it.

Re-import (sync) state lives in its own `block_sync` table, never in
provenance: `source_hash` (hash of the canonical block the source last wrote),
`dismissed_hash`, `conflict` (the source's version of a locally edited block)
and `source_removed`. Re-imports compare canonical content (type, attrs,
content, payload), never Markdown. A note's own sync state (the source's title
and folder, the external ids it last listed, `source_removed_at` when the page
disappeared from its source) lives in `documents.sync_json`.

`GET /api/block-documents/{id}/info` reports it: `sync` (with
`source_removed_at`, `blocks_edited_here`, `blocks_removed_in_source`) and
`conflicts`, where a block conflict is
`{"kind": "block", "block_id", "local_text", "source_text", "source": {type,
attrs, content, payload}, "detected_at", "source_modified_at"}` (`source_text`
is the derived text of the source's version). `POST
.../blocks/{block_id}/source-conflict` with `{"keep": "source"|"local"}`
settles one: `source` applies the source's version, `local` silences that
source version (a later, different one is reported again).

## 7. Markdown

Markdown is an interchange format, rendered from the canonical blocks; it is
never stored. Every block carries its id in a Fortress comment, so an export
(or mirror file) imported again keeps block ids (and with them links and
backlinks). The mirror is written from the database and never read back into it.

- Text blocks: a trailing inline comment, `## Plans <!-- fortress:block {"id":"01J…"} -->`.
- Other blocks: a comment on the line before, `<!-- fortress:block {"id":"01J…"} -->`.
- The comment also carries what Markdown can't say: the block's `provenance`
  when it differs from the note's, extension `attrs`, and the `type` when
  Markdown can't express it (`callout` is a quote with `"type":"callout"`).
- Lists: `- `, `1. `, `- [ ] `/`- [x] `; children indented under their item.
- `quote`: `> ` before each line of its children. `code`: a fence longer than any
  backtick run inside. `divider`: `---`. `math`: `$$ … $$`.
- Marks: `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``; underline, sup, sub,
  colour and highlight as `<u>`, `<sup>`, `<sub>`, `<span style="color:…">`,
  `<mark style="background:…">`. Links `[text](href)`; note links
  `[[<document id>]]`; block references `((<block id>))`.
- Tables: a GFM table when the table has one header row, every cell holds one
  paragraph (or nothing) and there are no spans, colours or widths; otherwise an
  HTML `<table>` whose rows, cells and cell blocks carry their comment's JSON
  in a `data-block` attribute. The GFM form lists row and cell ids in its
  comment (`"rows"`).
- Columns: `<!-- fortress:columns {…} -->`, `<!-- fortress:column {…} -->` …
  `<!-- /fortress:columns -->` around the column contents.
- Images: `<img src="…" alt="…" width="…">`; files: a comment with the file's
  attrs followed by a `[name](url)` link line.
- An empty paragraph (no content, no children) is written as nothing: Markdown
  has no empty paragraph, and a blank line is just spacing.
- A list item's children are indented by its bullet's width (`- `, `1. `); a
  task's `[ ] ` is part of the item's text and doesn't count.

Reading Markdown (import, uploads, capture, new notes created with a body,
importer sources) produces canonical blocks directly. Only `<table>`, `<img>`
and Fortress `div[data-type]` HTML is read structurally; any other HTML block
becomes an `html` block. An id from a comment is reused when
it isn't already taken by another block in the workspace; otherwise a new id is
made. Comments written by older versions (`<!-- fortress:block {provenance} -->`,
`<!-- fortress:file {...} -->`) are still read.

## 8. Order keys

Siblings are ordered by `order`, a fractional-index string key
(`order_keys.py`: the base-62 "integer part + fraction" scheme used by Figma and
Logseq). Keys compare by plain byte order. The server generates a key between
the neighbours named by the placement, so inserts and moves write one row and
never renumber siblings; undo restores a block's `(parent_id, order)` exactly.
A unique index on `(document_id, parent_id or '', order)` covers top-level
blocks too. Clients only ever sort by it (or rely on the order of `children`).

## 9. Client migration (Mac app)

The server still accepts the Mac app's current requests while it migrates, at
the boundary only (nothing below is stored):

- BlockNote/legacy type names map to canonical ones: `bulletListItem`,
  `toggleListItem` → `bullet_item`; `numberedListItem` → `numbered_item`;
  `checkListItem` → `check_item`; `list` → by `attrs.checked`/`attrs.ordered`;
  `codeBlock` → `code`; `thematic_break`, `hr` → `divider`; `blockquote` →
  `quote`; `audio`, `video`, `pdf` → `file`.
- `content: {"blocknote": [runs]}` is read as inline content: `styles` names
  (`superscript`, `subscript`, `textColor`, `backgroundColor`, `strikethrough`)
  map to marks; link nodes with href `[[id|Title]]` become `docLink`, href
  `(( id ))` becomes `blockRef`; `[[id]]`/`((id))` typed as text become nodes.
  `content.markdown`, `text` and `expected_updated_at` are ignored (so an
  update must still carry `content`, `type` or `attrs`).
  `content: {"latex": …}` on math is read as the payload. Image `previewWidth`
  maps to `width`.
- An inline `quote` or `callout` insert/update with runs becomes the container
  with one paragraph child holding the runs (an update replaces the first
  paragraph child's content).
- `position` (an index among the new parent's children, not counting the moved
  block) is accepted on insert and move in place of `after_id`.

What the Mac must change:

1. **Read the new shape.** `content` is an array (or null) of inline nodes, not
   `{"blocknote", "markdown"}`; `payload` holds math LaTeX. Marks are under
   `marks` with the names in §3. Build note links from `docLink`/`blockRef`
   nodes and label them from `resolved_links` (drop `currentLinkTitles` and the
   `[[id|Title]]` href parsing). Use `type` names from §2; headings keep levels
   4–6 (`heading(level)`), never clamped.
2. **Tables and columns are child blocks.** Decode `table → table_row →
   table_cell → blocks` (cell blocks have ids); editing a cell's text is an
   `update` of the cell's paragraph; adding a row is inserts of a `table_row`,
   its cells and their paragraphs. Delete `TableContent(blockNote:)` and the
   loaded-shape compatibility code.
3. **Quotes/callouts are containers**: a quote's lines are its paragraph
   children.
4. **Send canonical operations**: `insert`/`move` with `after_id` (or
   `before_id`/`parent_id`) instead of `position`; `update` with only what
   changed — `attrs` as a patch (`null` deletes; stop resending all attrs),
   `content` as canonical inline nodes. Stop sending `text`,
   `content.markdown`, `expected_updated_at`. Drop the `split`, `merge`,
   `duplicate`, `set_attrs`, `set_user_attrs` cases from `BlockOperationKind`.
5. **Delete the client Markdown serializer** (`BlockCodec.markdown`,
   `InlineMarkdown.render` for storage) and the Markdown-only decoding paths
   (`content.markdown` parsing, `soleImage`, fence stripping): every stored
   block is canonical after the server's migration.
6. Fetch `GET /api/block-schema` (or bundle it) to know which types may nest
   where; unknown types from a newer server render as read-only placeholders and
   are never rewritten.

Once the Mac sends canonical operations, the boundary mapping above and the
deprecated `position` field are removed.

## 10. Migration of stored data

Schema version 14 converts every stored block once, at startup, in one SQLite
transaction:

- types to canonical names (as in §9); Markdown-only blocks are parsed into
  canonical content (a heading's `#`, a list item's marker, a code fence, a
  sole image, `$$` math); `raw` HTML becomes `html` blocks;
- blockquotes become quote containers (and the blocks a quote swallowed by the
  old importer bug are dropped, since they were duplicates of the blocks after
  it); inline quotes/callouts become containers with a paragraph;
- table JSON becomes `table_row`/`table_cell` blocks with paragraph (and
  nested) children; table and cell properties become attrs;
- `[[id|Title]]` / `((id "label"))` links become `docLink`/`blockRef` nodes;
- integer positions become order keys; the `UNIQUE(document_id, parent_id,
  position)` constraint becomes the order-key index (which also covers
  top-level blocks);
- re-import state moves from provenance to `block_sync` (and from document
  provenance to `documents.sync_json`); source hashes of untouched imported
  blocks are recomputed over the canonical content;
- `text` is re-derived for every block, then links, asset links and the
  full-text index are rebuilt;
- the dead `revisions` and `block_attrs` tables are dropped, and undo history
  (stored in the old snapshot format) is cleared.
