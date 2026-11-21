# OneNote import: decisions

How notebooks get from OneNote into Fortress, and why it was built this way. What
the import does is in [spec.md §7b](../spec.md#7b-onenote-import-onenote_importpy-onenote_graphpy);
the research behind these decisions (parser and exporter surveys, the Obsidian and
Joplin importers, Graph's output HTML) is in this file's git history.

## Getting notebooks out of OneNote (as of Nov 2026)

- **OneNote for Windows**: *File › Export › Notebook › OneNote Package (.onepkg)*
  (or a section as `.one`). The most complete copy; needs Windows.
- **OneNote for the web / OneDrive**: *Export notebook* was removed in 2026, and
  OneDrive doesn't download notebooks as files (the sync client shows them as `.url`
  shortcuts). OneNote for Mac only saves PDFs.
- **Microsoft Graph**: still supported for delegated (signed-in) access to personal
  and work notebooks. This is the only route left on a Mac.

So the app offers both: **Download from Microsoft** (default) and **Import a File**.

## Two converters, one manifest (F3)

Both routes produce the same `manifest.json` + content-addressed
`assets/<sha256>.<ext>` ([SCHEMA.md](../tools/fortress-onenote/SCHEMA.md)), and
`backend/app/onenote_import.py` imports it. Everything after the manifest (mapping,
links, re-import, report) is shared, so both routes behave the same.

- **Files → `tools/fortress-onenote`**, a small Rust CLI on
  [`onenote_parser`](https://crates.io/crates/onenote_parser) (MPL-2.0, vendored with
  patches). Chosen because it is the only parser that reads both the classic and the
  cloud `.one` formats and `.onepkg`, and exposes what a faithful import needs:
  outline positions (columns), tables with nested content, OCR alt text, printouts,
  note tags with labels, page GUIDs, subpage levels and object ids (stable paragraph
  identity). One static binary, no runtime dependencies. `one2html` (HTML is a lossy
  intermediate) and pure-Python readers (unproven on the cloud format) were rejected.
- **Graph → `tools/fortress-onenote-graph`**, an optional Python tool with its own
  locked dependencies (MSAL), run by the backend (`app/onenote_graph.py`) or on its
  own ([README](../tools/fortress-onenote-graph/README.md)). It converts Graph's page
  HTML into the same manifest.

## 2a. Microsoft Graph

- **Sign-in**: the user registers their own free Entra app (Fortress ships no client
  id); delegated, read-only scopes (`Notes.Read`, plus `Notes.Read.All` for work
  accounts). Browser sign-in, with a device code as fallback; tokens stay in the
  Keychain on a Mac.
- **Requests**: notebooks and section groups listed once; pages per section with
  `pagelevel=true` (subpage levels), `$top=100` and `$skip` paging (setting `$top`
  turns off `@odata.nextLink`); page HTML with `includeIDs=true` (element ids for
  paragraph links); resources from `data-fullres-src`. `includeInkML` is not
  requested (no ink).
- **Output HTML**: every outline is an absolutely positioned `div` (left/top/width),
  so columns are recovered from geometry; `background-color` is the highlighter;
  note tags are `data-tag` (custom tags are not returned); tables nest but never
  merge cells; `img alt` is OneNote's OCR text; printouts and attachments are
  `object`s; equations are MathML.
- **Throttling**: 120 requests/minute, 400/hour, 5 concurrent per app and user, and
  429s carry no `Retry-After`. The tool paces itself under the limits (shared across
  runs), backs off on 429, and caches everything it downloaded, so a stopped run
  resumes and later runs fetch only changed pages. A large first download takes
  hours (roughly 200–400 pages an hour).
- **What Graph loses** compared with `.onepkg`: custom note tags, section order and
  colours, hand-set outline widths, EMF/XPS originals; paragraph-link targets are
  unverified (they fall back to page links).

## Mapping decisions

- **Folders, not notebooks**: notebook, section group and section become nested
  folders keyed by OneNote's file identities (stable across renames and moves).
- **Subpages stay flat**: next to their parent in OneNote's order, with a *Subpages*
  list of links on the parent. A folder per parent page would separate parent and
  subpages (folders list before notes). The page tree is kept in the import details.
- **Layout**: outlines side by side become a `columns` block (relative widths);
  bands of more than 4 columns ("scattered notes") are stacked instead.
- **Tags**: To Do becomes `check_item`; every other tag is kept as a label in
  `attrs.onenote_tags`. Fortress has no tags of its own.
- **Fonts** are dropped; monospace text becomes `code`.
- **No math, no ink** (product decision, Nov 2026): an equation is kept as its plain
  linear text (a paragraph that is only an equation becomes a `code` block,
  `language: "text"`); ink is skipped and counted in the report.
- **Pictures** keep OneNote's OCR text as `alt` and also go through Fortress's own OCR.
- **Links**: `onenote:` page links become note links; paragraph links become block
  references when the paragraph is found, else page links; unresolved ones stay
  text and are reported.

## Re-import is a refresh

Pages are keyed by GUID and blocks by OneNote object id (else a content hash), so
importing the same notebook again updates changed pages in place: same note, same
block ids for the same paragraphs, links into them intact. A page edited in
Fortress since the last import is never overwritten (reported instead); a page
deleted in OneNote is only marked. Nothing is merged. Details: spec.md §7a.

## Trust and safety

The converter reads user-downloaded files: `onenote_parser` is hardened against
malicious files (path traversal, huge allocations), the vendored `cab` crate carries
our own fixes, file names from the export are sanitised, and attachment types a browser would run are stored as `.bin`.
Licences: [tools/fortress-onenote/LICENSES/THIRD-PARTY.md](../tools/fortress-onenote/LICENSES/THIRD-PARTY.md).
