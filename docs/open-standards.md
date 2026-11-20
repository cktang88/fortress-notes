# Open standards first

**Principle.** Fortress Notes supports open standards rather than inventing its own
formats. Wherever people or other tools can see what we write — files on disk,
exports, imports, the API — we use the open or de-facto standard, and add our own
syntax only where no standard can say the thing, namespaced and invisible to
other readers. A user should be able to point Obsidian, VS Code, GitHub, Pandoc,
`grep` or Spotlight at their notes and get sensible results without knowing that
Fortress exists.

Rules of thumb:

1. **Use the standard as written**, not "inspired by": if Obsidian writes
   `[[Note#^id]]`, we write exactly that, and read what Obsidian writes.
2. **Prefer the most widely read form** when standards compete (CommonMark/GFM
   before Obsidian extensions, Obsidian extensions before Pandoc ones, plain HTML
   only where Markdown has nothing).
3. **Custom data goes under a namespace and stays invisible**: a `fortress:` key in
   YAML front matter, a `<!-- fortress:block {...} -->` HTML comment (hidden by every
   Markdown renderer), `data-*` attributes in HTML. Never a bespoke inline syntax.
4. **Read more than we write.** Importers accept the common variants (Obsidian,
   older Fortress forms, other tools' front-matter keys) and normalise them.
5. **Lossless round trip of our own model** is a requirement, not a nice-to-have:
   export → import → export must be identical, so the namespaced extras stay.

**Companion principle: least surprise.** Things behave like they do in Apple's apps
and Obsidian. Standard Mac behaviour — ⌘C/⌘V and the pasteboard types other apps
expect, screenshots, system shortcuts, Quick Look, the share sheet, services — is
never overridden or reimplemented. (The Mac app's side of this is specified in
`mac/DESIGN.md`.)

This document lists every format Fortress invented that a user or another tool
can see, the closest standard, and the decision. Status: **done** = implemented,
**keep** = deliberately ours, **later** = recommended, not implemented yet (see
the end for effort estimates).

## Inventory and decisions

### The notes folder (the Markdown mirror) — done

Every note is mirrored to a Markdown file in `NOTES_DIR` (default `../notes`),
written by the `mirrors` background lane (`documents.MirrorWriter`, `vault.py`).
The folder is now a valid **Obsidian vault** of plain **CommonMark + GFM + Obsidian
Flavored Markdown (OFM)**. Vault format version: `vault.FORMAT_VERSION = 2`; a
mirror state without it (format 1) makes the lane rewrite every mirror once and
delete the old `<id>.md` files.

| Aspect | Before (format 1) | Standard | Now (format 2) |
|---|---|---|---|
| File naming | `NOTES_DIR/<ULID>.md`, flat | Obsidian/Bear/iA Writer: the note's title is the file name; folders are folders | `<Folder>/<Subfolder>/<Title>.md`. Characters no OS or Obsidian accepts (`\ / : * ? " < > \| # ^ [ ]`) become spaces; clashes (case-insensitive, as on macOS) get ` 2`, ` 3` in creation order; a top-level folder may not be called `assets`. A file in the folder that isn't ours is never overwritten. Renames/moves rename the file and rewrite the links to it. |
| Front matter keys | `id`, `title`, `status`, `created_at`, `updated_at`, `images`, `provenance` | YAML front matter; `title`, `aliases` (Obsidian), `created`/`updated` (most common pair; Dublin Core has `created`/`modified`), `source` (Obsidian Web Clipper, Dublin Core) | `title`, `aliases` (only when the file name differs from the title), `created`, `updated`, `id`, `status`, `source` (when the note came from a URL), then `fortress: {provenance, picture_text}`. Hand-added keys are kept. |
| Dates | ISO 8601 with offset | ISO 8601 / RFC 3339 | unchanged (ISO 8601, UTC offset) |
| Note links | `[[<document id>]]` | Obsidian wikilink by file name | `[[Title]]`, `[[Folder/Title]]` when the name isn't unique (Obsidian's "shortest path"), `[[Name\|Title]]` when the file name differs from the title. A link to a trashed/unknown note stays `[[<id>]]`. |
| Block references | `((<block id>))` | Obsidian `[[Note#^id]]`, `[[Note#Heading]]` | `[[Note#^id\|first line]]`; a reference to a heading is `[[Note#Heading]]`; within the note `[[#^id]]`. |
| Block ids | a `<!-- fortress:block {"id":…} -->` comment on **every** block | Obsidian `^id` at the end of the block (structured blocks: on its own line after) | `^id` **only on blocks something refers to**; headings (Obsidian links them by text) keep their id in their comment. See "Why ids only on referenced blocks". |
| Other block metadata | the same comment | none exists | still the comment, only when needed: provenance that differs from the note's, extension attributes, nesting under a paragraph (`"indent"`), callout icon/colour. |
| Callouts | a blockquote with `"type":"callout"` in a comment | Obsidian callouts `> [!type] Title`, `+`/`-` folding (also GitHub alerts for 5 types) | `> [!note]` (type, fold and title kept in `callout_type`/`callout_fold`/`callout_title` attributes) |
| Highlight | `<mark style="background:…">` | Obsidian `==text==` | `==text==` for the default highlight; other colours stay `<mark style>` (no standard for colour) |
| Text colour, underline | `<span style="color">`, `<u>` | none in Markdown; inline HTML is allowed by CommonMark | unchanged |
| Super/subscript | `<sup>`, `<sub>` | Pandoc `^x^`/`~x~` (not read by Obsidian/GFM) | unchanged: HTML, which CommonMark, GFM and Obsidian all render |
| Math | `$$…$$` blocks | `$`/`$$` (Obsidian, GitHub, Pandoc, MathJax/KaTeX) | unchanged; inline `$…$` written by others is kept as typed text (the model has no inline math yet) |
| Task lists | `- [ ]` / `- [x]` | GFM task lists | unchanged |
| Tables | GFM, or HTML with `data-block` on every row/cell | GFM tables | GFM whenever the table fits (one header row, one paragraph per cell, no spans/colours/widths, nothing inside referred to); per-cell provenance in the table's comment. Otherwise an HTML `<table>` (standard HTML, `data-block` attributes only where needed). |
| Images | `<img src="/media/x.png" width=…>` | CommonMark `![alt](path "title")`, Obsidian `![alt\|300](path)` for size | `![alt\|300](assets/x.png "caption")` with a path relative to the note (`../assets/…` in folders) |
| Attachments (files) | a comment with the file's attributes + `[name](/media/x)` + size text | CommonMark link; Obsidian embeds PDFs/audio/video with `![…](…)` | `![name](assets/x.pdf)` for PDF/audio/video (Obsidian shows them in place), `[name](assets/x.zip)` otherwise; size in the comment |
| Attachment folder | `NOTES_DIR/assets/` | Obsidian: any folder ("Files and links › Default location for new attachments") | unchanged (`assets/`); embeds are relative, so Obsidian, VS Code and GitHub resolve them |
| Columns | `<!-- fortress:columns -->`, `<!-- fortress:column {…} -->`, `<!-- /fortress:columns -->` around the contents | none (see below) | unchanged, without ids |
| Comments, escapes | — | Obsidian `%%comment%%`, `#tag`, `^id`, `==` | our text escapes `==`, `%%`, a `^word` after a space, `[[`, so it never turns into Obsidian syntax by accident |

**Columns.** No Markdown dialect has columns. Candidates: Obsidian plugins —
*Multi-Column Markdown* (`--- start-multi-column: ID` … `--- column-end ---` …
`--- end-multi-column`), *Obsidian Columns* (a `` ```col `` code block), and the
*Modular CSS Layout* snippet (`> [!multi-column]` with nested callouts). Each needs
the plugin or snippet; without it the first renders as rules and stray text, the
second as a code block (the content disappears into code), the third as nested
quotes with a visible `[!multi-column]` on GitHub and in every non-Obsidian tool.
**Decision:** keep the contents in reading order with invisible comment markers. Every
tool shows the text, top to bottom, as a phone would; Fortress restores the layout.
Revisit if one plugin syntax becomes a de-facto standard.

**Why ids only on referenced blocks.** Obsidian's model is the same: a block gets an
id when something links to it. Ids everywhere would put a 26-character ULID at the
end of every line in GitHub, VS Code, Bear, Typora and every other non-Obsidian
viewer (Obsidian hides them). What a round trip of our own model must keep is what
the ids *carry*: links and backlinks (kept, since every referenced block has its
id), provenance and extension attributes (kept in the comment), and the note id
(front matter). Ids of blocks nothing refers to are not observable in the model, so
they are not written; a re-import gives those blocks new ids. Re-import of an
Obsidian vault maps Obsidian's short ids (`^abc123`, unique only per note) to new
workspace-unique ids, consistently for every link to them. If we ever need every
id (e.g. a sync that edits mirrors and reads them back), the switch is one argument
(`RenderLinks.anchors=None`) — at the cost of the noise above.

### Markdown import (files, folders, Obsidian vaults) — done

Reads everything the mirror writes, every older Fortress form (`[[id]]`,
`((id))`, `<!-- fortress:block -->` with ids, `<img>`, `<!-- fortress:file -->`), and
what Obsidian writes: wikilinks by file name, path, title or alias (`aliases`),
`[[Note#Heading]]`, `[[Note#^id]]`, `![[image.png|300]]`, `![[file.pdf]]`, callouts
with titles and folding, `==highlight==`, task lists, `$$` math, block ids
(`^id`, also on a line of its own after a list/quote/table). A folder import
recreates the folder tree, skips hidden folders (`.obsidian`, `.trash`) and
`assets/`, and resolves links across all the files and to notes already in the
workspace; files uploaded together (File › Import) link to each other the same
way. Front matter: `created`/`created_at`/`date`, `updated`/`updated_at`/`modified`,
`aliases`/`alias`, `source` (a URL becomes the note's `source_url`). Not yet: Obsidian
attachments are not copied in (an upload has only the `.md` files; the folder import
leaves `![[x.png]]` pointing at `x.png`), `#tags` stay text (tags are not a feature),
`%%comments%%` stay visible text, inline `$math$` stays text, transclusion
`![[Note]]` becomes a note link.

### Single-note export — done

`GET /api/block-documents/{id}/markdown` (⇧⌘E) returns the same Markdown as the
mirror (front matter + OFM body, wikilinks named as in the vault). Stored files keep
their `/media/…` URL, since a single file has no `assets/` beside it — a folder export
would fix that (below).

### Other formats

| Format | Today | Closest standard | Decision |
|---|---|---|---|
| Whole-workspace export | none (a stated non-goal), but the notes folder *is* the export | a Markdown folder / Obsidian vault; zip of it; HTML site | **later**: "Export folder…" = copy of the vault with `assets/` (and `/media` URLs rewritten); no zip export (product decision) |
| Backups | SQLite snapshots (`.fortress-backups/*.sqlite3`) | SQLite file (the most portable database format there is) | **keep**; the vault is the human-readable copy |
| OneNote import | `.onepkg`, `.one`, `.onetoc2`, zip | (proprietary; MS-ONESTORE is documented) | **keep**: reading OneNote's own files is the open move |
| OneNote manifest (`tools/fortress-onenote/SCHEMA.md`) | versioned JSON between converter and backend | none needed (internal contract, not user-facing) | **keep**; it already follows standards inside (ISO 8601 `Z` times, `#RRGGBB`, code-point offsets, sha256 asset names) |
| Evernote import | none | ENEX (Evernote's XML export, the de-facto interchange for Evernote, Apple Notes importers, Bear, Joplin) | **later** |
| Apple Notes / Bear / Joplin / Notion import | via Markdown export of those apps | Markdown + front matter, HTML, ENEX | covered by Markdown import; Notion's export adds ids to file names (`Title 1a2b…md`) — **later**: strip them |
| HTML in Markdown | `<table>`, `<img>` read structurally; any other HTML block kept as an `html` block | HTML (CommonMark allows raw HTML) | **keep** |
| Pasteboard (copy/paste with other apps) | Mac app | the system pasteboard types (`public.utf8-plain-text`, `public.html`, `public.rtf`, file URLs, images) | per the least-surprise principle; owned by the Mac app (`mac/DESIGN.md`) |
| API description | FastAPI generates OpenAPI 3.1 at `/openapi.json` (needs the per-launch token; the `/docs` page can't be used, since browser requests are refused by design) | OpenAPI 3.1, JSON Schema 2020-12 | **later**: check `openapi.json` into the repo (and test it is current), publish the block registry as JSON Schema |
| API errors | `{"detail": …}` (FastAPI), 409 `{"code": …}` | RFC 9457 `application/problem+json` | **later** (low value for a single local client) |
| Ask stream | `application/x-ndjson` | NDJSON (de-facto) / SSE | **keep** |
| HTTP caching | ETags, `If-None-Match`, `Cache-Control` | RFC 9110/9111 | **keep** (already standard) |
| Auth | `X-Fortress-Token` or `Authorization: Bearer` | RFC 6750 Bearer | **keep** (Bearer is accepted) |
| Dates/times | ISO 8601 UTC everywhere | ISO 8601 / RFC 3339 | **keep** |
| Ids | ULID (notes, blocks, assets); clients may send UUIDs | ULID spec, RFC 9562 UUID | **keep** |
| Stored files | `assets/<ULID or sha256>.<ext>` served at `/media/` | content-addressed names are common (Git, IPFS-style) | **keep** |
| Colours | `#rgb`/`#rrggbb[aa]`, CSS names, `rgb()/hsl()` | CSS Color | **keep** |
| Search | plain words, all must match (FTS5, operators not exposed) + embeddings | no query language needed; if one is added, the de-facto web/GitHub one: `"phrase"`, `-word`, `OR` | **keep**; adopt that syntax if a query language is ever added |
| Quick capture inbox | an `Uncategorized` folder, newest first | no standard (Things/OmniFocus "Inbox") | **keep**; it is a plain folder in the vault |
| Themes | (another change adopts Base16/Base24) | Base16/Base24 | out of scope here |
| Provenance | our JSON (`provenance.py`), `fortress.provenance` in front matter | W3C PROV (too heavy), Dublin Core `source`/`created`/`modified` | **keep** our object under the namespace; map `source_url` → `source` (done); a Dublin Core / PROV-O export is not worth it |
| Text read from pictures | `fortress.picture_text` in front matter (was `images`) | none (Spotlight indexes file contents) | **keep**, namespaced |

## Not done yet: high-value moves, with effort

| Move | Value | Effort |
|---|---|---|
| **Export folder** (vault copy with `assets/`, `/media` → relative paths; Pandoc-ready; no zip — product decision) | real backup + migration path out | S: ½–1 day (the vault writer already renders everything; copy + rewrite + a menu item) |
| **Copy Obsidian attachments on import** (folder import: copy `![[x.png]]` targets into `assets/`, register them, OCR them) | Obsidian vaults import with their pictures | S–M: 1 day |
| **Evernote ENEX import** (XML; ENML → HTML → blocks; base64 resources → assets; `created`/`updated`/`source-url`/`tag`) | the most common legacy notes export | M: 2–3 days (ENML is XHTML; the HTML-to-blocks reader exists) |
| **OpenAPI document**: check in `openapi.json`, document the transaction model in it, publish the block registry as JSON Schema | other tools and clients can use the API | S: ½ day for the dump + CI check; M: 2 days for a block-registry JSON Schema |
| **Inline math** (`$…$`) as an inline node or mark | Obsidian/LaTeX users' notes keep their math | M: 2 days (schema, editor rendering, Markdown both ways) |
| **Notion export import** (strip the hex ids from file names and links, CSV databases → tables) | Notion migration | S: 1 day |
| **HTML export** (one note or the folder as standalone HTML) | sharing, printing, archiving | S: 1 day (markdown-it renders our output; add CSS) |
| **RFC 9457 problem details** for API errors | consistency for third-party clients | S: ½ day |
| **Read mirrors back (two-way vault)**: edits made in Obsidian flow into SQLite | true interoperability | L: 1–2 weeks (conflicts, ids of new blocks, file watching); would also argue for ids on every block |
