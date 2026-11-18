# fortress-onenote manifest — format version 1

`fortress-onenote <input> --out <dir>` writes:

```
<dir>/manifest.json            the manifest described here (UTF-8 JSON)
<dir>/assets/<sha256>.<ext>    every binary referenced by the manifest
```

The manifest is the contract between the converter and the Fortress Python
importer. Rules for evolving it:

- `format` is always `"fortress-onenote-manifest"`.
- `format_version` is an integer. It is bumped for any incompatible change
  (renamed/removed field, changed meaning or unit). Importers must refuse a
  version they do not know.
- New optional fields and new enum values (block `type`, mark `type`, layout
  `kind`) may appear without a version bump. Importers must ignore unknown
  fields and treat unknown block types as "unsupported" (keep their text if
  any, report them).
- A field documented as nullable is always present (as `null`); fields marked
  *(omitted when empty)* may be missing.

Additions without a version bump: converter 0.2.0 added block `object_id` /
`element_id` and link `onenote.object_ref` (see *Object ids*). Older
manifests simply lack them.

## Conventions

| Topic | Rule |
|---|---|
| Text offsets | Mark `start`/`end` are **Unicode code points** (Python `str` indices), half-open `[start, end)`, into the block's `text`. Not UTF-16, not bytes. |
| Geometry | OneNote **half-inch units** (`units.geometry = "half_inch"`); `units.px_per_geometry_unit = 48` (CSS px at 96 dpi). `x`/`y` are offsets from the page origin. |
| Times | ISO 8601 UTC with `Z`, e.g. `2025-10-21T11:48:58.891Z`. |
| Colours | `#RRGGBB` upper-case hex, or `null` for automatic/none. |
| GUIDs | `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}` upper case with braces (page ids, link targets). Compare case-insensitively. |
| File identities | lower-case UUID without braces (`file_identity`). |
| Assets | Referenced by name `<sha256>.<ext>`; look the name up in the top-level `assets` map. Identical content is stored once. Names never derive from file names in the notebook. |
| Untrusted strings | `filename`, `original_name`, titles and all text come from the user's files. Use them for display only, never as paths. |

## Top level

```jsonc
{
  "format": "fortress-onenote-manifest",
  "format_version": 1,
  "generator": {"name": "fortress-onenote", "version": "0.2.0", "parser": "onenote_parser 2.1"},
  "units": {"text_offsets": "code_points", "geometry": "half_inch", "px_per_geometry_unit": 48.0},
  "input": {"kind": "zip|directory|onetoc2|one|onepkg", "name": "<input base name>"},
  "notebooks": [Notebook],
  "assets": {"<sha256>.<ext>": Asset},
  "report": GlobalReport
}
```

**Asset**: `{"path": "assets/<name>", "sha256": "<hex>", "size": <bytes>, "mime": "image/png", "ext": "png"}`.
The extension comes from content sniffing (images, PDF, …); attachments keep
their own (sanitised) extension.

## Notebook tree

**Notebook**

| field | type | notes |
|---|---|---|
| `id` | string | Unique in this manifest. `file_identity` when known; otherwise `loose:<hash of section identities>` or `notebook:<hash of path>`. Copies get a `~<hash>` suffix. |
| `file_identity` | string \| null | Identity of the `.onetoc2`; stable across rename/move. **Use as the idempotency key.** |
| `name` | string | Folder name of the notebook (OneNote web export and OneDrive name the folder after the notebook), package name for `.onepkg`. |
| `color` | string \| null | |
| `source_kind` | string | `onetoc2`, `onepkg`, or `loose_sections` (`.one` files without a table of contents). |
| `source_path` | string | Notebook folder relative to the input (`/`-separated; `""` = input root). |
| `children` | [Entry] | In OneNote's table-of-contents order. |
| `report` | NotebookReport | |

**Entry** is a section group or a section, tagged by `kind`:

- `{"kind": "section_group", "id", "file_identity" (nullable), "name", "path": [..], "children": [Entry]}`
- `{"kind": "section", "id", "file_identity" (nullable), "name", "color", "path": [..], "in_toc": bool, "pages": [Page], "warnings": [Warning]}`

`path` is the display path (`[notebook, group…, section]`). `in_toc` is false
for sections found on disk but not listed in the notebook's table of contents
(OneDrive downloads often ship an empty `.onetoc2`); they are placed by folder
and come after listed entries. Section groups synthesised from folders have
`file_identity: null`.

## Page

| field | type | notes |
|---|---|---|
| `id` | string | Unique in the manifest; normally equal to `link_target_id`. |
| `link_target_id` | string | Page GUID. Equals the `page-id={…}` of `onenote:` links (verified on samples). **Idempotency key and link-rewrite key.** |
| `title` | string | Whitespace-normalised. |
| `title_derived` | bool | True if the page had no title and `title` is the first text line (or `"Untitled Page"`). |
| `level` | int | 0 = page, 1 = subpage, 2 = sub-subpage. |
| `parent_id` | string \| null | Nearest preceding page with a lower level (subpages only). |
| `order` | int | 0-based position in the section. |
| `created`, `updated` | string | ISO 8601 UTC. `updated` falls back to `created`. |
| `author` | string \| null | |
| `source_path` | [string] | notebook, groups…, section, page title (provenance). |
| `source_url` | string \| null | First "Pasted from <url>" / "Clipped from: url" source found on the page. |
| `height` | number \| null | Page height (geometry units) if stored. |
| `ink_text` | string \| null | Handwriting recognised by OneNote for Windows (lines joined by `\n`). |
| `items` | [Item] | Positioned top-level containers, in OneNote's stored order (not reading order). |
| `layout` | [LayoutRow] | Reading order computed from geometry (see below). |
| `stats` | `{"blocks": int, "text_chars": int}` | Including table cells. |

The page title and its date/time line are not repeated as blocks.

### Item

Every outline (OneNote "note container") and every free-floating image,
attachment and ink drawing on the page is an item.

| field | type | notes |
|---|---|---|
| `id` | string | `i0`, `i1`, … unique within the page. |
| `kind` | string | `outline`, `image`, `file`, `ink`. |
| `x`, `y` | number \| null | Offset from page origin (geometry units). `null` when OneNote stored none. |
| `width` | number \| null | Declared width: outline max width, picture width. |
| `width_set_by_user` | bool | Outline width was dragged by the user (otherwise it is a max width). |
| `est_width`, `est_height` | number | Estimated rendered size used by the layout algorithm (OneNote never stores heights). |
| `is_background` | bool | Background image ("Set picture as background"). |
| `source_url` | string \| null | Source citation found inside this outline. |
| `blocks` | [Block] | The content. Floating image/file/ink items hold exactly one block (an `embed` replaces the image for video embeds). Empty outlines are dropped. |

### Layout

`layout` is a list of rows, top to bottom. Every item id appears exactly once.

- `{"kind": "item", "item": "i3"}` — the item fills the row.
- `{"kind": "columns", "scattered": bool, "columns": [Column, …]}` — side by side, left to right.
  **Column**: `{"width": <fraction, sums to 1>, "x": <left>, "span": <width in units>, "rows": [LayoutRow]}`.
  Columns nest (a column can stack several items or contain columns).
  `scattered: true` marks a band with more than 4 columns (scattered notes);
  importers may stack such columns instead of rendering them side by side.

Algorithm (`src/layout.rs`): sort items by top; sweep into horizontal bands
of transitively overlapping vertical ranges (overlap > 0.15 units, so touching
or slightly overlapping outlines stay separate rows); inside a band, cluster
by horizontal overlap (> 0.1 units) into columns, ordered by x; a band whose
items overlap in both directions is stacked by top; columns are laid out
recursively. Background images are excluded from banding and emitted as rows
at their own top. Raw `x`/`y`/`width` stay on the items so an importer can
re-run a different heuristic.

## Blocks

Blocks are a **flat list** per outline / table cell. Nesting is expressed by
`indent` (0 = top level; a list item's children have a larger indent), which
maps directly to nested list items or indented paragraphs.

Common fields on every block:

| field | type | notes |
|---|---|---|
| `type` | string | see below |
| `indent` | int | Nesting depth within the outline or cell. |
| `tags` | [Tag] | *(omitted when empty)* Note tags on this paragraph/object. |
| `align` | string | *(omitted when left)* `center` or `right`. |
| `source_url` | string | *(omitted when none)* Source citation of the outline this block belongs to. |
| `object_id` | string | *(omitted when unknown)* OneNote ExGUID of the content node (rich text, table, image, attachment, ink), `{GUID}&N` (see *Object ids*). Stable across edits of the paragraph: **use it as the block's idempotency key.** Unique within a page. |
| `element_id` | string | *(omitted when unknown)* ExGUID of the enclosing outline element (the paragraph); this is what paragraph links (`object-id=`) name. |

Text-bearing blocks (`paragraph`, `heading`, `list_item`, `check_item`,
`quote`) also carry **rich text**: `text` (string, hidden field codes removed,
vertical tab/CR normalised to `\n`) and `marks` (*omitted when empty*).

| `type` | extra fields |
|---|---|
| `paragraph` | rich text; `is_source_citation` *(omitted when false)*: the "Pasted from <url>" line — the importer may hide it and keep `source_url` instead. |
| `heading` | `level` 1–6 (OneNote Heading 1–6 styles); rich text (font family/size marks are not emitted in headings). |
| `list_item` | `list`: ListInfo; rich text. |
| `check_item` | To Do style tag (any checkable tag shape): `checked` bool, `checked_at` (string \| null), `list` (ListInfo \| null); rich text. The tag is also in `tags`. |
| `quote` | OneNote "Quote"/"Citation" style: `variant` `quote` \| `cite`; rich text. |
| `code` | `text` (plain). OneNote "Code" style or all-monospace (Consolas, Courier…) paragraphs; consecutive code paragraphs at the same indent are merged with `\n`. |
| `math` | Paragraph that is only an equation: `text` (OneNote's linear form, markers removed), `latex` (string \| null, `\[…\]`), `mathml` (string \| null). Inline equations are `math` marks instead. |
| `table` | `row_count`, `col_count`, `column_widths` ([number], geometry units), `borders_visible` bool, `rows`: [{`cells`: [{`background`: colour \| null, `blocks`: [Block]}]}]. Cells hold any blocks, including nested tables. OneNote has no merged cells. |
| `image` | `asset` (string \| null), `data_status` (`available` \| `missing` \| `invalid`), `alt_text` (string \| null — usually OneNote's OCR text), `width`/`height` (geometry units \| null), `hyperlink` (string \| null), `is_printout_page` bool, `printout_page_number` (int \| null, as stored; may be 0-based), `is_background` bool, `original_name` (sanitised display name \| null), `original_asset` (string \| null: the original when `asset` is OneNote's browser-friendly fallback picture, e.g. for EMF images or XPS printout pages). |
| `embed` | Online video/iframe: `url`, `thumbnail_asset` (string \| null). |
| `file` | Attachment: `asset` (string \| null), `filename` (sanitised display name), `extension` (string \| null), `mime`, `size` (bytes as recorded), `data_status` (`available` \| `missing` \| `invalid`), `media_kind` (`audio` \| `video` \| `other` — OneNote recordings are audio/video), `icon_asset` (string \| null). |
| `ink` | Drawing/handwriting rendered to SVG: `asset` (string \| null, `image/svg+xml`), `recognized_text` (string \| null), `width`, `height` (geometry units), `stroke_count`. |

A PDF/XPS printout appears as a `file` block (the document, when OneNote
kept it) plus `image` blocks with `is_printout_page: true` (one per page, OCR
text in `alt_text`); the page images may sit in the same outline or be
separate floating items stacked below it.
Horizontal rules do not exist in OneNote and are never emitted.

**ListInfo**: `{"ordered": bool, "style": "decimal|upper_roman|lower_roman|upper_alpha|lower_alpha|bullet", "marker": string, "restart": int|null}`.
For ordered lists `marker` is the pattern with `%` for the number (`"%."`,
`"(%)"`); for bullets it is the bullet character (Wingdings bullets are
mapped to Unicode). `restart` is the explicit start number if numbering
restarts.

**Tag** (note tag): `{"label": string, "shape": snake_case icon name (e.g. "blue_check_box", "yellow_star", "no_icon"), "checkable": bool, "completed": bool, "completed_at": string|null, "highlight_color": colour|null, "text_color": colour|null}`.
Labels are what the user sees (localised, custom tags included).

### Marks

`{"start": int, "end": int, "type": …}` plus type-specific fields. Spans of
the same mark are merged; marks may overlap; sorted by `start`.

| `type` | fields |
|---|---|
| `bold`, `italic`, `underline`, `strikethrough`, `superscript`, `subscript` | — |
| `highlight` | `color` (text background/highlighter) |
| `color` | `color` (font colour) |
| `font` | `family` — only when not the default `Calibri` |
| `font_size` | `pt` — only when not the default 11 pt |
| `link` | `href` (as stored); `onenote` *(only for OneNote links)*: `{"page_id", "section_id", "object_id", "object_ref", "section_file", "page_title"}` (GUIDs normalised, all nullable; `object_id` is the GUID of `object-id=` alone, `object_ref` the full target `{GUID}&N` when the link carries the counter); `auto: true` *(omitted when false)* when the URL was found in plain text rather than stored as a link. |
| `math` | inline equation: `latex`, `mathml` *(each omitted when rendering failed)* |

**Rewriting OneNote links**: for a link mark with `onenote.page_id`, look up
the page whose `link_target_id` equals it (case-insensitive) — in this
manifest or in earlier imports — and replace the link with an internal
document link. A paragraph link (`object_ref` set) targets the block of that
page whose `element_id` (else `object_id`) equals `object_ref`; a block inside
a table cell stands for its table. Fall back to the page link when no block
matches. Unresolved links (other notebooks) should keep `href` and be listed
in the import report.

### Object ids

OneNote gives every object an extended GUID: a GUID plus a counter `N`. It is
the object's identity in the revision store and survives edits (an edit writes
a new revision of the same object). Paragraph links use it:
`onenote:…&page-id={P}&object-id={GUID}&3C` names the outline element
`{GUID}` / `N = 0x3C`. The manifest writes ExGUIDs in that form:
`{GUID}&N` — upper-case GUID with braces, `&`, `N` in upper-case hexadecimal
without leading zeros. Compare as strings.

`onenote_parser` 2.1 resolves these ids internally but does not expose them
on its content types, so the converter reads them from the parser's public
`Parser::dump_onestore` view (`src/objectids.rs`; the crate version is pinned
with `=` in `Cargo.toml` because that text format is not a stable API) and
matches them to the converted content per page:

| field | how it is found |
|---|---|
| `object_id` of rich text (paragraph, heading, list/check item, quote, code, math) | the page's `RichTextNode` whose text equals the paragraph text (whitespace-normalised), when exactly one does |
| `object_id` of a file / image | the `EmbeddedFileNode` / `ImageNode` with that file name / alt text (or file name), else the page's only one |
| `object_id` of a table | the page's only `TableNode` with that row and column count |
| `object_id` of ink | the page's only ink container (page-level drawings merged from several ink objects get none) |
| `element_id` | the `OutlineElementNode` OneNote created with the content: same GUID and last-modified time; within such a group, elements and content nodes pair up in counter order (or by counter `n − 1` when the numbers differ) |

Anything ambiguous (two paragraphs with identical text on a page, a merged
code block's later lines, the second embed of one picture) gets no id, and an
id is never given to two blocks of a page. Table cells and items have no ids
(their blocks do). Sections inside a `.onepkg` are not on disk and get no ids.
On the public samples about 90 % of text blocks get an `object_id`, and
the ids are identical across two downloads of the same section. Importers
must fall back to their own keys (e.g. a content hash) when a block has no
`object_id`.

## Reports

**NotebookReport**

```jsonc
{
  "counts": {"section_groups", "sections", "sections_failed", "pages", "subpages", "blocks",
             "paragraphs", "headings", "list_items", "check_items", "code_blocks", "quotes",
             "tables", "images", "printout_pages", "attachments", "attachments_missing",
             "ink", "math", "embeds", "links_web", "links_onenote", "tags", "column_layouts",
             "text_chars"},
  "tags_by_label": {"To Do": 4, "Important": 1},
  "warnings": [Warning],          // parser warnings and lossy-conversion notes
  "failed_sections": [{"path": [..], "file": "rel/path.one"|null, "reason": "human readable",
                        "hint": "what the user can do", "error": "raw parser error"}],
  "missing_data": [{"page_id", "page_title", "section_path": [..], "kind": "attachment|image",
                     "name": string|null, "data_status": "missing|invalid|empty"}],
  "layout_notes": [{"page_id", "page_title", "message"}]
}
```

`counts.blocks` and the per-type counts include blocks inside table cells.
Failed sections typically are password-protected, damaged, or empty
(not synced) files; their pages are absent from the tree.

**Warning**: `{"message": string, "page_id": string|null, "page_title": string|null}`.

**GlobalReport**

```jsonc
{
  "export_errors": [{"file": "errors.txt", "text": "…"}],  // OneNote web export failures
  "input_warnings": ["unsafe zip entry rejected (parent directory component): \"../x\""],
  "skipped": ["Notebook/OneNote_RecycleBin/OneNote_DeletedPages.one", "readme.docx"],
  "notebooks": 1, "sections": 4, "sections_failed": 0, "pages": 37,
  "assets": 12, "assets_bytes": 1234567
}
```

## CLI contract

- Exit 0: manifest written (it may still contain failed sections/warnings).
- Exit 1: nothing convertible (e.g. a zip holding only `errors.txt`) or a fatal error; message on stderr.
- Exit 2: usage error.
- On success stdout is one JSON line: `{"manifest": "<path>", "format_version": 1, "notebooks": n, "sections": n, "sections_failed": n, "pages": n, "assets": n}`. Progress goes to stderr (`-q` silences it).
- Zip inputs are extracted to `<dir>/.extracted` and removed afterwards (`--keep-extracted` keeps them).
