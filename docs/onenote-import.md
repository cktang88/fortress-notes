# OneNote -> Fortress Notes migration research (Nov 2026)

STATUS (Nov 20 2026): two routes are built. **Update:** OneNote for the web's
*Export notebook* is gone (2026) and OneDrive won't download notebooks, so on a Mac
the file route has no input any more. The **Graph route** is now built as an
optional, standalone tool: [`tools/fortress-onenote-graph`](../tools/fortress-onenote-graph/README.md)
downloads notebooks with Microsoft Graph (§2a below: delegated MSAL sign-in with
the user's own Entra app registration, read-only scopes, self-paced under the
throttling limits, resumable cache, re-runs fetch only changed pages) and writes
the same manifest the converter writes, which the importer takes as is (spec.md
§7b "Ready-made manifests"). Not requested: `includeInkML` (Fortress dropped ink and
math); MathML is kept as text in a code block. The earlier "no Graph" decision
(below) predates the export's removal. On Windows, `.onepkg` stays the most complete
route.

The file-based route (F1–F5) shipped first:
`tools/fortress-onenote` (Rust converter, manifest format in
[SCHEMA.md](../tools/fortress-onenote/SCHEMA.md)) → `backend/app/onenote_import.py` →
File › Import › From OneNote… in the Mac app. **The behaviour as implemented is
specified in [spec.md §7b](../spec.md#7b-onenote-import-onenote_importpy)**; this
document keeps the research and the reasoning behind it. Sections 2a and the Obsidian
notes are Graph background only.

## 0. Fortress block model

At the time of this research (early Nov 2026) blocks stored Markdown or BlockNote JSON
and GFM tables landed as raw blocks. That model is gone: blocks are now canonical
(`type`, `attrs`, inline `content` with marks, `payload`), with structured tables
(`table` → `table_row` → `table_cell`, cells holding any blocks), `columns`/`column`,
`check_item`, `file` and `image` blocks, and colour/highlight/underline/sup/sub
marks — see [docs/block-model.md](block-model.md). Re-import keys live in
`documents.source_kind`/`source_external_id`, and what a refresh needs in the
note's `sync_json` (`backend/app/sources.py`, spec.md §7a).

## 2a. Microsoft Graph OneNote API (verified Nov 2026)

### Status
- **Still supported for delegated (signed-in user) access, including consumer notebooks on OneDrive.** Docs pages are headed "Applies to: Consumer notebooks on OneDrive | Enterprise notebooks on Microsoft 365" ([Get content](https://learn.microsoft.com/en-us/graph/onenote-get-content), updated 2025-08).
- **App-only auth retired 31 March 2025** (Message Center MC1011142; [mc.merill.net](https://mc.merill.net/message/MC1011142); [Integrate with OneNote](https://learn.microsoft.com/graph/integrate-with-onenote)). Irrelevant to us: a personal tool uses delegated tokens. No 2026 retirement of the OneNote Graph API was found; the 2026 Graph retirements (Graph Toolkit, `/drive/recent`, `/insights/*`) don't touch OneNote. OneNote 2021/LTSC 2021 *desktop* apps hit end of support 13 Oct 2026 ([lifecycle](https://learn.microsoft.com/en-us/lifecycle/end-of-support/end-of-support-2026)) — doesn't affect the API or OneNote for Windows/Mac (M365).
- Microsoft itself notes OneNote API "limitations" for full backup/restore scenarios and points to OneDrive file scanning instead ([Get content](https://learn.microsoft.com/en-us/graph/onenote-get-content)). For ~500 pages this is fine, just slow.

### Endpoints (root `https://graph.microsoft.com/v1.0/me/onenote`)
| Need | Request |
|---|---|
| Whole hierarchy in one call | `GET /notebooks?$expand=sections,sectionGroups($expand=sections,sectionGroups($levels=max;$expand=sections))` |
| Notebooks / groups / sections | `GET /notebooks`, `/notebooks/{id}/sectionGroups`, `/sectionGroups/{id}/sectionGroups`, `/sectionGroups/{id}/sections`, `/notebooks/{id}/sections` |
| Pages of a section, in section order with indent level | `GET /sections/{id}/pages?pagelevel=true&$top=100&$select=id,title,createdDateTime,lastModifiedDateTime,level,order,contentUrl,links` — page through with `@odata.nextLink` or `$skip` (default 20, **max `$top`=100**) |
| Page HTML | `GET /pages/{id}/content?includeIDs=true` (Accept `text/html`) |
| Binary resource (image full-res, attachment, printout) | `GET /resources/{resource-id}/$value` (URLs come from the HTML; collections of resources can't be listed) |
| Plain text preview (cheap verification) | `GET /pages/{id}/preview` (≤300 chars) |
Source: [Get OneNote content and structure](https://learn.microsoft.com/en-us/graph/onenote-get-content). Prefer `/sections/{id}/pages` over the global `/pages`: `pagelevel` only works per section, and the global list is known to fail (error 10008/20266 "too many items") for large accounts.
Page properties of interest: `id`, `title`, `createdDateTime`, `lastModifiedDateTime`, `level` (0,1,2 = subpage depth), `order`, `links.oneNoteClientUrl/oneNoteWebUrl` (to resolve `onenote:` links), `contentUrl`.

### Returned HTML (output HTML) — what we parse
From [Input and output HTML](https://learn.microsoft.com/en-us/graph/onenote-input-output-html) and [note tags](https://learn.microsoft.com/en-us/graph/onenote-note-tags):
- `<title>` = page title; `<meta name="created" content="...">` also present.
- `<body data-absolute-enabled="true" style="font-family:Calibri;font-size:11pt">` — default font for the page.
- **Every outline (OneNote "note container") is a top-level `<div style="position:absolute;left:48px;top:120px;width:624px" data-id=... id=...>`**. Free-floating images/objects can also be direct `body` children with `position:absolute;left;top`. Height is never given (auto). => side-by-side columns are recoverable only geometrically: same-ish `top` band, different `left`, widths from `width`.
- Text: `p`, `h1`–`h6` (headings come back with inline `font-size/color/margin` styles), `br`, `ul/ol/li` (with `list-style-type` on each `li`: decimal, lower/upper-alpha, lower/upper-roman, disc/circle/square/none), `span style="..."`, `a href`, `cite`, `sup`, `sub`, `pre`/code style fonts (no semantic code tag — code shows up as Consolas spans).
- Inline styles returned: `font-family`, `font-size` (pt), `color`, `background-color` (**this is OneNote's text highlight**), `font-weight:bold`, `font-style:italic`, `text-decoration:underline|line-through`, `text-align` on blocks. Style lives on `span`s mostly.
- **Tables**: `<table style="border:1px solid;border-collapse:collapse">` (or `border:0px` for borderless), `tr`, `td` with `style="background-color:...;width:...;"` (cell shading). **Nested tables supported; `rowspan`/`colspan` not supported** (OneNote has no merged cells). No `th` (OneNote's "header row" is just a shaded/bold row).
- **Images**: `<img src=".../resources/{id}/$value" data-src-type data-fullres-src=".../resources/{id}/$value" data-fullres-src-type width height alt style>`. `alt` carries user alt text. OCR text ("Copy text from picture") is **not documented** as returned; Obsidian/others observe OCR text isn't exported reliably — we must OCR ourselves (the app's OCR queue, `backend/app/ocr.py`).
- **Printouts** (File > Insert as printout): an `<object data-attachment="x.pdf" data=".../resources/{id}/$value" type="application/pdf">` plus a run of `<img ... data-index="n" data-options="printout">` images (one per PDF page, all with same `data-id`). Order by y then x.
- **Attachments**: `<object data=".../resources/{id}/$value" data-attachment="fileName.pdf" type="application/pdf" style=...>`.
- **Video embeds**: `<iframe data-original-src="https://www.youtube.com/watch?v=..." src="...embed..." width height>`.
- **Note tags**: `data-tag="to-do"`, `to-do:completed`, `important`, `question`, `definition` (returned as remember-for-later!), `highlight`, `contact`, `address`, `phone-number`, `web-site-to-visit`, `idea`, `password`, `critical`, `project-a/b`, `remember-for-later`, `movie-to-see`, `book-to-read`, `music-to-listen-to`, `source-for-article`, `remember-for-blog`, `discuss-with-person-a/b`, `discuss-with-manager`, `send-in-email`, `schedule-meeting`, `call-back`, `to-do-priority-1/2`, `client-request` (+ `:completed` for checkbox shapes). Multiple tags are comma-separated. On `p`/`h*`/`img` directly; **on lists the tag is on a `span` inside the `li`**. **Custom tags are not supported/returned** by the API (the content survives, the tag doesn't).
- Not represented in output HTML (lost via Graph): **ink/drawings** (handwriting, shapes; at best nothing or a placeholder), **math equations** (come back as plain/garbled text or image depending on client; treat as lossy), **audio/video recordings** made inside OneNote (may appear as `<object>` attachments of type audio/* — download as files), **Loop components**, page background/rule lines, section colours, page templates, author/version history.
- `includeIDs=true` adds `id="{guid}{n}"` per element — useful as stable paragraph IDs for block-reference mapping and idempotency.

### Throttling (official — Graph throttling page, OneNote section)
| | delegated (per app per user) |
|---|---|
| Request rate | **120 requests/minute and 400 requests/hour** |
| Concurrency | **5 concurrent requests** |
"The preceding resources don't return a `Retry-After` header on 429" — so we must self-pace ([Graph throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits#onenote-service-limits)). Practitioner notes: `$batch` inner requests each count against the limit, and a 200 batch can hide inner 429/503s ([DEV: OneNote rate limiting](https://dev.to/ensky/microsoft-graph-api-what-the-docs-dont-tell-you-about-onenote-rate-limiting-2lgl)); use `x-ms-throttle-limit-percentage` response header to slow down early; the 2016 OneNote blog says limits are per app+user ([OneNote API throttling](https://devblogs.microsoft.com/microsoft365dev/onenote-api-throttling-and-how-to-avoid-it/)).

**Budget for this user**: ~500 pages => ~500 content GETs + ~10–20 listing calls + N resource GETs (images + attachments + printout pages; easily 1–3 per page) => roughly 1,000–2,000 requests. At **400/hour that is 3–5 hours wall-clock.** Design consequence: the importer must be a resumable background job with a persistent local cache (raw HTML + resource bytes on disk), not a single blocking request. Pace at ~6 req/min under the hourly cap (or burst 100/min until ~380/h then sleep), max 2–3 concurrent.

### Extra Graph capabilities discovered in Obsidian Importer source (MIT, v3.2.0, commit fa9444a, 12 Nov 2026)
Source: `github.com/obsidianmd/obsidian-importer` `src/formats/onenote.ts`, `src/formats/onenote/convert.ts`, `auth.ts`.
- **`GET /pages/{id}/content?includeInkML=true`** returns a *multipart* body: part 1 `text/html`, part 2 `application/inkml+xml` with all ink strokes on the page. Obsidian converts InkML -> one SVG per page (`onenote/inkml.ts`, `ink-svg.ts`) and embeds it at the end. => ink is NOT lost via Graph; we can render it to SVG (position info is in the InkML).
- **Math comes back as MathML (`<math display="block">`)** inside the HTML; Obsidian converts it to LaTeX with the `mathml-to-latex` npm package (MIT). We could use a Python MathML->LaTeX converter or keep MathML.
- **"OneNote stores its OCR output in alt text"** (comment in `getAllAttachments`): `img alt` = OCR text of the picture. Obsidian strips it; *we should keep it* (alt + search index + Ask) — a parity win.
- Code: OneNote has no code element; Obsidian detects code as spans/paragraphs whose `font-family` contains `Consolas` and merges adjacent ones into a fenced block (`onenote/code.ts`).
- Tags: `to-do`/`to-do:completed` -> `- [ ]`/`- [x]`; every other tag appended as `#tagname` (`:` -> `-`). Highlight (`background-color`) -> `<mark>`; underline -> `<u>`; `cite` -> blockquote; `onenote:` links reduced to the page-name fragment after `#` (crude: no rewrite to the actual imported note).
- Video iframes: YouTube -> embed, others -> plain link. Attachments: `<object>` -> download -> `![[file]]` embed. Images: download `data-fullres-src` (not the downscaled `src`).
- Auth: authority `https://login.microsoftonline.com/common/oauth2/v2.0`, **personal scopes `openid user.read notes.read` (+ `offline_access`)**; organization scopes use `notes.read.all` ("Personal accounts cannot consent to Notes.Read.All"). Account type detected from token `tid` = `9188040d-6c67-4c5b-b112-36a304b66dad` (the consumer tenant). Obsidian ships its own registered client id (auth-code + redirect flow).
- Pagination: `/sections/{id}/pages?pagelevel=true&$top=100&$skip=n&$select=id,title,createdDateTime,lastModifiedDateTime,level,order,contentUrl` — comment: "Setting $top disables @odata.nextLink, so paginate with $skip"; also guards against Graph ignoring `$skip` and repeating a batch.
- Throttling: on 429 waits 60 s (no Retry-After is ever sent), retries indefinitely on 429, max 5 retries on other errors, adaptive spacing between attachment downloads (+500 ms per 429, up to 3 s, decays on success). Refreshes token on 401 once.
- Subpages: page with `level>0` is placed in a folder named after the nearest preceding page with `level = level-1`.
- Re-runs: notes carry a `sourceId` (page id) so a re-import updates/skips instead of duplicating; attachments deduped by resource id + mtime.
- **Positioned outlines are ignored**: the importer converts the HTML in DOM order, so side-by-side outlines become sequential blocks (no columns). Tables: Markdown tables, and "Markdown tables cannot nest, and a table that holds one is layout" (file importer flattens nested tables).

### Obsidian also ships a native .one/.onepkg/.onex parser (TypeScript, MIT) — `src/formats/onenote-file/`
~4.3k lines: `cabinet/` (CAB + LZX decompressor for `.onepkg`), `onestore/` (MS-ONESTORE revision store), `semantic/` (MS-ONE object mapping). Its content model (`semantic/content.ts`): `TextRun{bold,italic,underline,strikethrough,superscript,subscript,hyperlinkUrl,math,highlight}`, `Paragraph{runs,children,list{level,ordered,format},styleId,tags[]}`, `Outline`, `Table{rows[cells[children]]}`, `Image{data}`, `EmbeddedFile{data}`, `Ink{strokes, recognizedText}`, `Page{id,title,level,createdUtc,lastModifiedUtc,isConflictPage,isDeleted,outlines,directContent}`, `Section{name,colorArgb,pages}`. Tags carry **custom labels** (mapped by shape since labels are localized) — something Graph cannot return. **No outline x/y positions** in its model. Reasons it reports: unsupported / rights-protected / malformed / too large. Note `.onepkg`/`.one` export requires OneNote for Windows (Mac and the old Win10 app can't export).

---------------------------------------------------------------------
# DIRECTION CHANGE (user): NO Microsoft Graph / Azure / Entra sign-in. File export -> import only.
The Graph material above is kept only as background (it documents what the HTML route *would* give); the design below is file-based.
---------------------------------------------------------------------

## F1. Getting notebooks out as files (Mac user, personal OneDrive) — verified Nov 2026 (in progress)
1. **OneNote for the web -> "Export notebook"** (Microsoft's official route; personal OneDrive only; **removed in 2026** — use the Graph tool instead). onenote.com/notebooks -> right-click notebook -> *Export notebook* -> pop-up shows size -> *Export* -> downloads a **.zip containing a folder named after the notebook** with the notebook's OneNote files (`.one` per section, `.onetoc2`, section-group subfolders). If the zip contains only `errors.txt`, the export failed (open it for details). Large notebooks can fail with "too large to export" -> split the notebook. "Import" of such zips back into OneNote is deprecated. ([Microsoft Support: Export OneNote notebooks](https://support.microsoft.com/en-us/onenote/export-and-import-onenote-notebooks)). Some users report the menu item missing for some account/subscription types ([MS Q&A](https://learn.microsoft.com/en-us/answers/questions/5424986/how-do-i-export-my-entire-onenote); [Outline help](https://help.outline.app/article/150-how-to-export-notebook-from-onedrive)).
2. **OneDrive web -> select the notebook (or its parent folder, e.g. Documents) -> Download** -> .zip of the same folder structure (`.one` + `.onetoc2`). This is what Joplin documents (Joplin ≥3.5.1: "OneNote Web -> Show all notebooks (opens OneDrive) -> My files -> download the folder containing your notebooks as a ZIP"; "may not work for notebooks larger than 2–4 GB") ([Joplin import/export help](https://joplinapp.org/help/apps/import_export)). These .one files are in the **cloud/FSSHTTP "package store" format**, not the classic desktop revision-store format — the parser must support it (onenote.rs and Obsidian's TS parser both do).
3. **OneNote for Windows (Microsoft 365 / 2016 / 2021) -> File -> Export -> Notebook -> OneNote Package (*.onepkg)** or Section -> `*.one`. Needs a Windows machine (or Parallels/UTM VM). `.onepkg` is a **Microsoft Cabinet (CAB, LZX/MSZIP-compressed) archive** of the `.one`/`.onetoc2` files in *classic desktop revision-store format*. The Store "OneNote for Windows 10" app and OneNote for Mac cannot export these formats ([Obsidian help](https://obsidian.md/help/import/onenote); Joplin help). Joplin only imports .onepkg on Windows (it relies on the OS to unpack the CAB).
4. **OneNote for Mac**: only per-page/section **PDF** export (File -> Save as PDF). Useless for structured import (lossy, no structure) — not recommended. The Mac app keeps notebooks in a private cache, not as .one files.
5. **OneDrive desktop sync on Mac**: OneNote notebooks are *not* synced as .one files; the client shows them as internet-shortcut placeholders (`.url`) — not a usable route. (see verification note below)

**Recommendation**: Route 1 (OneNote web "Export notebook") per notebook, falling back to Route 2 (OneDrive web Download) if the menu is missing. Both produce a .zip we accept directly (File › Import › From OneNote…, then choose or drop the .zip). Route 3 (.onepkg) supported as well for users with Windows access.
- Verification of route 5: OneDrive sync shows notebooks as Internet-shortcut `.url` placeholders that only open OneNote web; the real `.one` files are not materialised locally ([MS answers thread](https://answers.microsoft.com/en-us/msoffice/forum/all/onenote-files-are-saved-as-a-web-link-instead-of-a/5fdf2311-c113-440b-bf50-21460832d4d8); [OneNoteGem](https://en.onenotegem.com/a/documents/gem-for-OneNote/Gem_Fix_Tools/2020/0331/open-notebook-url.html)). Not usable.

### In-app instructions to show the user (Mac, personal OneDrive)
> **Export from OneNote (about 1 minute per notebook)**
> 1. In a browser, open **onenote.com/notebooks** and sign in with your Microsoft account.
> 2. Right-click a notebook → **Export notebook** → **Export**. A `.zip` downloads (it contains a folder named after the notebook).
> 3. Repeat for each notebook. Don't unzip — in Fortress choose File → Import → From OneNote… and drop each `.zip` on the sheet.
> *No "Export notebook" item?* Open **onedrive.live.com → My files**, tick the notebook (or the folder holding your notebooks, usually *Documents*) → **Download**. Drag that `.zip` in instead.
> *Got a zip with only `errors.txt`?* The notebook was too big for Microsoft's exporter — split it into smaller notebooks in OneNote and export again (or use the OneDrive "Download" route).
> *Have a Windows PC?* OneNote for Windows → File → Export → Notebook → **OneNote Package (.onepkg)** also works and is the most complete copy (includes the classic desktop format).
Tip to show: open each notebook in OneNote once first and let it finish syncing, so pages and images are fully uploaded (Joplin users lost pages from not-yet-synced notebooks — [Joplin forum](https://discourse.joplinapp.org/t/importing-onenote-not-working-looking-for-alternative-route-using-intermediate-tool/47287)).
Fortress must ignore `OneNote_RecycleBin/` folders and `errors.txt`, and tolerate several notebooks inside one zip (parent-folder download).

## F2. Parsing .one / .onepkg — options (verified Nov 2026)

### A. `onenote_parser` (msiemens/onenote.rs) — Rust, **MPL-2.0** — RECOMMENDED CORE
- Very actively maintained again: 0.4.0 (2025-12-27) → 1.0 (2025-12-28, non-legacy MS-ONESTORE) → 1.1 (inline maths) → 1.1.1 (2026-05-15, security) → **2.0.0 (2026-08-06)** → **2.1.0 (2026-10-19)**; last commits 19 Oct 2026; CI on Linux/macOS/Windows + WASM; MSRV Rust 1.85 ([crates.io](https://crates.io/crates/onenote_parser), CHANGELOG in repo). Driven largely by Joplin, whose `@joplin/onenote-converter` (Rust→WASM) is "based on the one2html and onenote.rs projects" ([npm README](https://cdn.jsdelivr.net/npm/@joplin/onenote-converter@3.6.3/README.md)).
- Formats: **FSSHTTP/OneDrive-download `.one`/`.onetoc2` (what the web export/OneDrive Download gives)** AND (since 2.0) **classic desktop `.one`/`.onetoc2`**; header GUIDs are sniffed automatically. **`.onepkg` via `Parser::parse_package` (feature `onepkg`) — CAB decompressed in memory, no extraction step needed.** Entry points: `parse_notebook(.onetoc2)`, `parse_section(.one)`, `parse_package(.onepkg)`. Non-fatal problems are collected per section in a `Report` (warnings) instead of aborting; attachments stream lazily (`read()`, `size()`, `stream_to_file`). Hardened against malicious files (path traversal in .onetoc2, huge allocations) — important since we parse user-downloaded files.
- What it exposes (read from source `crates/parser/src/onenote/*.rs`, v2.1.0):
  | Feature | API | Notes |
  |---|---|---|
  | Notebook / section groups / sections | `Notebook::entries()` → `SectionEntry::{Section, SectionGroup}` (recursive), `display_name()`, `color()`, `file_identity()` (stable across rename/move — **idempotency key**) | TOC order preserved (fixed in 2.0) |
  | Pages, order, **subpage level** | `Section::page_series()` → `PageSeries::pages()`; `Page::level()`, `title_text()`, `created_time()`, `updated_time()`, `author()` | timestamps added in 2.0 |
  | Page GUID | `Page::link_target_id()` — "May be referenced by internal links" → matches `page-id={GUID}` in `onenote:` links → **idempotency key + link rewriting** |
  | **Outline positions (columns)** | `Outline::offset_horizontal()/offset_vertical()` (half-inch units from page origin), `layout_max_width()`, `is_layout_size_set_by_user()`, `layout_max_height()`, `child_level()`, `indents()`; same offsets on free-floating `Image`, `EmbeddedFile`, `Ink`, and page `Title` | one2html renders 1 unit = 48 px |
  | Rich text runs | `RichText::text()`, `text_run_formatting()` + `text_run_indices()` → `ParagraphStyling{bold, italic, underline, strikethrough, superscript, subscript, font, font_size, font_color, highlight, style_id (e.g. h1..h6/"p"/"cite"/"code"), hyperlink, hidden, math_formatting, language_code}`; paragraph alignment, space before/after; `hyperlinks()` → `TextHyperlink{target, start, end}` | headings come from `style_id` |
  | Lists | `OutlineElement` list info: `list_format()` (chars incl. numbering pattern), `list_restart()`, list font/color | nesting via outline element children |
  | **Tables** | `Table::rows()/cols()/contents()/col_widths()/borders_visible()/cols_locked()`; `TableCell::contents()` (any outline elements → **nested tables, images, lists in cells**), `background_color()` (shading), `layout_max_width()` | **no merged cells — OneNote tables have no rowspan/colspan** (Graph docs confirm "do not support rowspan or colspan") |
  | **Images** | `Image::read()`, `extension()`, `image_filename()`, `alt_text()` ("usually the result of OneNote's OCR processing"), `text()`, `picture_width/height()`, `hyperlink_url()`, `displayed_page_number()` (**printout page of a PDF/XPS**), `is_background()`, `web_picture()` | OCR text available |
  | **Attachments / printouts** | `EmbeddedFile::filename()`, `file_type()`, `read()`, `size()`, `data_status()` (invalid/missing payload flagged), `icon()`, offsets | printout = EmbeddedFile + a run of Images with `displayed_page_number` |
  | Video embeds | `Image::embeds()` → `IFrame::source_url()` | |
  | **Ink** | `Ink` strokes, `InkStroke::absolute_path()` (2.1), bounding box, colour/width; handwriting recognition: `Page::ink_recognition()`, `InkStroke::recognized_word()` (2.0) | render to SVG + keep recognized text for search |
  | **Math** | `RichText::math_inline_objects()` + `math_formatting` runs (1.1) | one2html has a math lexer/parser/renderer → MathML |
  | **Note tags** | `NoteTag::{definition().label(), shape(), status(), item_status(), completed_at(), highlight_color(), text_color(), action_item_type()}` on rich text, images, tables, files | **custom tags *with labels*** (better than Graph, which drops custom tags) |
  | Not exposed | audio/video *recordings* show up only as embedded files (download them); Loop components, page background colour/rule lines, section passwords (encrypted sections fail), version history beyond latest revision |
- Known limitations/bugs: password-protected sections can't be read (must remove password first); rights-protected (IRM) notebooks unreadable; occasional malformed-file sections → warning + skipped (we must surface these in the report); `onenote:` links are just URLs in `hyperlinks()` — **rewriting is our job**; image `image_filename()`/`EmbeddedFile::filename()` are attacker-controlled → sanitise (we already have `safe_filename` in `backend/app/assets.py`). Open issues: 4, PRs: 4 ([GitHub](https://github.com/msiemens/onenote.rs)).

### B. `one2html` (msiemens) — Rust CLI, **MIT**, v2.0.1 (Oct 2026, last commit 28 Oct 2026)
Converts notebook/section/.onepkg to HTML; renders each outline as `position:absolute; left/top` divs (unit × 48 px), `width` when user-sized else `max-width`; tables with `border-collapse`, borders when visible, col widths; math → MathML; ink → absolutely positioned SVG; note-tag icons. Install `cargo binstall one2html` or `cargo install --locked --no-default-features --features bin,onepkg one2html`. Good **reference implementation** and a zero-code prototype path (run it, parse its HTML in Python), but HTML is a lossy intermediate (style flattening, no GUIDs/levels/OCR text as data).

### C. Obsidian Importer's TypeScript parser — **MIT** (see section above). Reads cloud package-store and desktop revision-store `.one`, `.onepkg` (own CAB+LZX), detects `.onex`/IRM. Model is simpler: no outline positions, no fonts/colours except highlight, tags with labels, ink with recognized text. Useful only for a TypeScript/JS client; not for us.

### D. Python options
- **aspose-note-foss** (PyPI, "Aspose.Note FOSS for Python", 26.9.0, Sept 2026; README says MIT but PyPI license metadata says `LicenseRef-Aspose-Split` — check before vendoring): pure Python, reads `.one` → DOM of Page/Outline/OutlineElement/RichText(TextRun styles: bold/italic/underline/strike/super/sub/font/colour/hyperlinks)/Image/Table/AttachedFile/NoteTag (incl. custom), `Page.Level`, CreationTime. **Not documented: FSSHTTP (OneDrive-download) format, `.onepkg`, outline positions, ink, math.** Brand-new (first release Jan 2026 as `aspose-note`, renamed), vendor "free edition" of a commercial product. Attractive (no binary to ship) but unproven on exactly the files this user will have — evaluate with a real export before considering. ([PyPI](https://pypi.org/project/aspose-note-foss/))
- **pyOneNote** (DissectMalware, Apache-2.0, 0.0.2 2023; `pyonenote-parser` 0.0.3 2026): malware-analysis tool to dump embedded files/metadata; no page/text model. Not suitable. `one-extract` (Volexity): same category.

### E. Windows-only exporters (not usable on Mac): onenote-md-exporter (alxnbl, v1.6.0 Dec 2025; needs OneNote 2013+ desktop **and Word** via COM; known gaps: font colour unsupported, images nested in tables; translates `onenote://` links to md links), ConvertOneNote2MarkDown (PowerShell + OneNote 2016 desktop + pandoc via Word export). ([ecosyste.ms](https://awesome.ecosyste.ms/projects/github.com%2Falxnbl%2Fonenote-md-exporter))

## F3. Recommended importer design (file route)

### Pick: a small Rust CLI `fortress-onenote` on `onenote_parser` 2.1 that emits JSON + asset files; Python backend calls it.
Why this over the alternatives:
- **Fidelity**: the only parser that exposes everything this user needs — outline **x/y/width** (side-by-side layouts), fonts/colours/highlight, tables with shading + nested content, OCR alt text, printout page numbers, attachments, ink + handwriting recognition, math, labelled custom tags, page GUIDs, levels, timestamps — and reads **both** the web-export/OneDrive-download format and `.onepkg`.
- **Maintenance**: actively maintained (2 major releases in 2026), used in production by Joplin, security-hardened, semver-stable API.
- **Shipping in a Mac app**: one static universal binary (`cargo build --release --target aarch64-apple-darwin` + `x86_64-apple-darwin`, `lipo`), no runtime deps, ~2–4 MB; bundled in the .app (`Contents/Resources/bin/`) and signed/notarised with the app; the Python backend finds it via an env/config path (`FORTRESS_ONENOTE_BIN`). For the dev/server backend, `cargo install` it or ship the binary. (Alternative later: PyO3 wheel via maturin — same code, `import fortress_onenote` — avoids subprocess; not needed for v1.)
- **Why not one2html as the pipeline**: we'd parse HTML back into structure and lose GUIDs, levels, OCR text, tag labels, data statuses. Use it as the *reference* for unit conversions/rendering decisions and for side-by-side visual diffing in tests.
- **Why not pure Python (aspose-note-foss)**: unverified on the FSSHTTP format the web export produces, no positions/ink/math, brand-new, unclear licence metadata. Re-evaluate later if we want zero native code.
- Prototype shortcut (day 1): run `one2html` on the user's zip to eyeball fidelity before writing the CLI.

### CLI contract (as built)
`fortress-onenote <input.zip|folder|.onetoc2|.one|.onepkg> --out <dir> [--keep-extracted] [--compact] [-q]`
→ `<dir>/manifest.json` (schema-versioned; any other version is refused by the
importer) + `<dir>/assets/<sha256>.<ext>` (content-addressed, so duplicates collapse and
re-runs are free), and one JSON summary line on stdout. It unzips safely, finds every
notebook (`.onetoc2`, skipping `OneNote_RecycleBin`), treats loose `.one` files as
sections, reads `.onepkg` via `parse_package`, and reports failed sections with a reason
and a hint. The manifest format (pages, layout rows, blocks, marks, object ids, report)
is specified in [tools/fortress-onenote/SCHEMA.md](../tools/fortress-onenote/SCHEMA.md);
usage and tests in [its README](../tools/fortress-onenote/README.md).

### Python side: `backend/app/onenote_import.py` + `POST /api/import/onenote` (upload zip/onepkg; job id; progress via polling)
**Built** (Nov 2026): see spec.md §7b for the mapping as implemented (subpages as a "Subpages" link list on the parent, two-pass link rewrite) and the job API. Update (Nov 2026): blocks are keyed by OneNote object ids (ExGUIDs, stable across edits; the vendored `onenote_parser` exposes them — tools/fortress-onenote/SCHEMA.md "Object ids"), paragraph links become block references, pages deleted in OneNote are marked `source_removed_at`, jobs persist/cancel, and each page gets a text-coverage check.
Pipeline: upload → temp dir → run CLI → read manifest → **plan** (diff against mapping table) → apply in one transaction per page (existing block transaction machinery) → second pass link rewrite → report.

**Mapping** (as built; the authoritative list is spec.md §7b)
| OneNote | Fortress |
|---|---|
| Notebook | top-level folder (or under a folder the user picks), keyed by `file_identity` |
| Section group | nested folder |
| Section | folder (section colours are not kept) |
| Page (level 0) | note in the section folder; title = page title (untitled pages: first line); keyed by the page GUID |
| Subpage (level 1/2) | note in the same folder, right after its parent; the parent gets a trailing **Subpages** list of links. `extra.onenote.level`/`parent_page_id` keep the tree |
| created / modified | the note's `created_at` / `updated_at` (and provenance `source_created_at`/`source_modified_at`) |
| Outlines stacked vertically | blocks in reading order |
| **Outlines side by side** | `columns` block with one `column` per outline (`width` = relative fraction); bands of more than 4 are stacked |
| Paragraph `style_id` h1..h6 | `heading` with `level` |
| Citation/Quote style | `quote`; "Code" style or all-monospace runs → `code` (adjacent ones merged) |
| Bullets / numbering | `bullet_item` / `numbered_item`, nested; OneNote's marker kept in `attrs.list_format` |
| To Do tag (checkable shapes) | `check_item` with `checked`; `completed_at` → `attrs.checked_at` |
| Other note tags (Important, Question, custom) | kept as labels in `attrs.onenote_tags` (extension attribute; Fortress has no tags of its own) |
| bold/italic/underline/strike/sup/sub, font colour, highlight, links | canonical marks (`bold`, `italic`, `underline`, `strike`, `sup`, `sub`, `color`, `highlight`) and `link` nodes |
| fonts / font sizes | dropped (monospace → `code`) |
| Tables | `table` → `table_row` → `table_cell`; column widths, borders, cell backgrounds; cells hold any blocks, nested tables included |
| Images | asset copied to `NOTES_DIR/assets/<sha256>.<ext>`; `image` block with `alt` = OneNote's OCR text, width/height; the app's OCR queue reads it too |
| Attachments | `file` block (name, size, mime) |
| Printouts | the page pictures as `image` blocks with `attrs.printout_page`; EMF/XPS originals kept as `attrs.original_url` |
| Video iframes | paragraph with a link to the video |
| Audio/video recordings | `file` block |
| Ink | not imported: Fortress does not support ink or handwriting (decision, Nov 2026). The converter skips ink and counts it (`ink_skipped`, a warning per page) so nothing disappears silently |
| Math | Fortress does not support math (same decision): a paragraph that is only an equation becomes a `code` block with OneNote's plain linear text (`language: "text"`); inline equations stay plain text; each page with equations gets a report warning |
| `onenote:` links | rewritten in a second pass: page links → `docLink`; paragraph links (`object-id`) → `blockRef` when the paragraph is found, else a page link; unresolved → plain text, listed in the report |
| "Pasted from <url>" | `source_url` in the note's provenance |

**Columns detection** (outline geometry; units = half-inches):
1. Collect outlines + floating images/files as boxes `(x, y, w, h_est)`; `h_est` = Σ estimated line heights (≈0.18 in per text line at 11pt, wrap by width; image height from picture height) — no height is stored.
2. Sort by y. Sweep into horizontal **bands**: a box joins the current band if its y < band.top + 0.6·band.min_h_est and it does not horizontally overlap any box in the band (x-ranges disjoint with ≥0.1 in gap). Otherwise start a new band.
3. Band with 1 box → its blocks inline. Band with ≥2 boxes → `columns` block, children ordered by x, `width` = box.w / Σw. Small floating image beside text → also a column (common "image next to notes" layout).
4. Page "scattered notes" (many tiny outlines) → if >4 columns in a band, fall back to sequential blocks and flag in report.
Keep raw geometry in each column's attrs (`onenote_x/y/w`) so we can re-run with better heuristics without re-parsing.

**Re-import is a refresh** (as built: `backend/app/sources.py`, spec.md §7a)
- Every note is keyed by `(source, external_id)` = `("onenote_import", page GUID)`; folders by `notebook:`/`group:`/`section:` + `file_identity`; paragraphs by OneNote object id (ExGUID) where the converter can match one, else a content hash.
- Re-run: a page whose content and title are what the last import wrote reports `unchanged`. A page that changed in OneNote is refreshed: the note's blocks are replaced by the page's (title follows OneNote; the note's id and folder stay), and a paragraph with the same OneNote id keeps its Fortress block id, so links and block references into it keep working. A note edited in Fortress since the last import is **not** refreshed (nothing is merged or overwritten): the report lists it in `pages_not_refreshed` and as a warning. The .one converter and the Graph download go through the same importer, so both behave the same. Deleted in OneNote → never deleted here; marked `source_removed_at` and listed in the report.
- Assets content-addressed by SHA-256 → no duplicate files.
- Jobs run one at a time on the `imports` background lane; the job record is persisted in `NOTES_DIR/.fortress-imports/`, survives restarts (an interrupted job reads `failed` with `interrupted: true`; importing again resumes in place) and can be cancelled.

**Verification report** (as built: the job's `result`, shown in the import sheet)
- Per notebook: pages created/updated/unchanged/skipped, images, files, tables, pages with columns, ink drawings skipped, check items, pages edited here and not refreshed, images queued for OCR, failed sections with reason and hint, warnings.
- Text coverage per page: the manifest's words vs the imported note's (multiset, weighted by length); pages under 98 % are listed.
- Links: rewritten, block references, paragraph links that fell back to page links, unresolved links; pages and sections removed in OneNote; OneNote web's `errors.txt`.

## F4. Fortress features needed for parity — status

1. **Table block** — done (structured `table`/`table_row`/`table_cell`, nested blocks in cells, GFM or HTML in Markdown).
2. **Inline marks: underline, highlight, text colour, super/subscript** — done (search and Ask index plain text only).
3. **Attachments** — done (`file` blocks; the Mac app shows file chips).
4. **Columns layout block** — done (`columns`/`column`; Markdown round-trips via `<!-- fortress:columns -->` comments).
5. **Page hierarchy / subpages** — partial: subpages sit next to their parent with a *Subpages* link list; nested notes are not built.
6. **Image alt/OCR text + width** — done (OneNote's OCR text in `alt`, plus the app's own OCR: RapidOCR in the backend and Apple Vision in the Mac app).
7. **Note tags** — kept as `attrs.onenote_tags` labels only; Fortress deliberately has no tags or tag views.
8. **Math** — not supported, by decision: equations are kept as their plain linear text (stored math blocks were converted by schema 15).
9. **Ink** — not supported, by decision: skipped and counted in the report (images imported from ink earlier stay plain images).
10. **Source URL on notes** — done (provenance `source_url`); folder colours and embedded video players are not built.

## F5. Licensing
- `onenote_parser` — **MPL-2.0** (file-level weak copyleft): fine to link into our CLI (static linking OK) as long as any *modified MPL files* are published. We vendor it with fixes in `tools/fortress-onenote/vendor/onenote_parser/` (changes listed in `vendor/PATCHES.md`). Its test fixtures include AGPL-3.0 samples — not copied into our repo.
- `one2html` — **MIT**; the converter's `src/math/` is adapted from it (notice in `tools/fortress-onenote/LICENSES/`). `cab` crate (`.onepkg`) — MIT, vendored with hardening fixes.
- Obsidian Importer (Graph + file importers, InkML→SVG, code-detection, tag mapping) — **MIT**; used as reference only.
- aspose-note-foss — README says MIT, PyPI metadata `LicenseRef-Aspose-Split`: not used. pyOneNote — Apache-2.0, not used.
- Third-party notices: `tools/fortress-onenote/LICENSES/THIRD-PARTY.md`.

## Appendix: why Graph was dropped (kept for reference)
User refused any Microsoft/Azure sign-in. Also: a personal-account user can't register an Entra app without first creating a directory (since June 2024 all new app registrations must live in a tenant — [Entra breaking changes](https://learn.microsoft.com/en-us/entra/identity-platform/reference-breaking-changes)), rclone users report the friction in Feb 2026 ([rclone forum](https://forum.rclone.org/t/onedrive-own-client-id-guide-is-outdated/53431)); and OneNote Graph throttling is 400 req/hour per user → 3–5 h for this user's ~500 pages. The file route avoids all of that and gives *more* data (positions as numbers, custom tag labels, handwriting recognition) — the one thing Graph gives that files don't is nothing material for this user.

STATUS: complete (Nov 18 2026).

## Open items from the research
1. ~~Run `one2html` on an exported zip and compare to OneNote visually.~~ Was done as a tool (`tools/onenote-compare`, since removed to cut code).
2. Confirm `page-id={GUID}` in the user's `onenote:` links equals `Page::link_target_id()` — the importer relies on it (links it cannot resolve are listed in the report).
3. Confirm the web "Export notebook" zip contains `.onetoc2` + every section `.one` and embedded files/printouts in full (Joplin/Obsidian users have reported incomplete zips: [Joplin forum](https://discourse.joplinapp.org/t/onenote-zip-import-incomplete/43826)); the converter places sections missing from the TOC by folder (`in_toc: false`).
4. "Pasted from <url>" source lines are a OneNote-for-Windows convention; check whether the user's clippings have them.
