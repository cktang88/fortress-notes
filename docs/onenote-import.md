# OneNote -> Fortress Notes migration research (Nov 2026)

STATUS: complete — file-based route (F1–F5) is the recommendation; sections 2a + Obsidian notes are Graph background only.

## 0. Fortress block model as it stands (from repo)
- `backend/app/block_store.py`: table `blocks(id, document_id, parent_id, position, type, attrs_json, content_json, text, ...)`; content is stored per block as `{"markdown": "<source>"}` (or a `blocknote` native payload); `ImportedBlock(id, type, source, attrs, children)`.
- Block types produced by Markdown import: `paragraph`, `heading`, `list` (attrs `ordered`, `checked`; children nested), `quote` (container), `code` (attrs `language`), `thematic_break`, and `raw` — **GFM tables and HTML blocks currently land as `raw`** (no structured table block). Inline formatting lives inside each block's Markdown source (so bold/italic/strike/links exist; highlight/colour/underline/font size do not).
- No external-id / import-source column exists -> idempotent re-import needs a mapping table (see section 3).

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
- **Images**: `<img src=".../resources/{id}/$value" data-src-type data-fullres-src=".../resources/{id}/$value" data-fullres-src-type width height alt style>`. `alt` carries user alt text. OCR text ("Copy text from picture") is **not documented** as returned; Obsidian/others observe OCR text isn't exported reliably — we must OCR ourselves (we have `vision.py`).
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
1. **OneNote for the web -> "Export notebook"** (Microsoft's official route; personal OneDrive only). onenote.com/notebooks -> right-click notebook -> *Export notebook* -> pop-up shows size -> *Export* -> downloads a **.zip containing a folder named after the notebook** with the notebook's OneNote files (`.one` per section, `.onetoc2`, section-group subfolders). If the zip contains only `errors.txt`, the export failed (open it for details). Large notebooks can fail with "too large to export" -> split the notebook. "Import" of such zips back into OneNote is deprecated. ([Microsoft Support: Export OneNote notebooks](https://support.microsoft.com/en-us/onenote/export-and-import-onenote-notebooks)). Some users report the menu item missing for some account/subscription types ([MS Q&A](https://learn.microsoft.com/en-us/answers/questions/5424986/how-do-i-export-my-entire-onenote); [Outline help](https://help.outline.app/article/150-how-to-export-notebook-from-onedrive)).
2. **OneDrive web -> select the notebook (or its parent folder, e.g. Documents) -> Download** -> .zip of the same folder structure (`.one` + `.onetoc2`). This is what Joplin documents (Joplin ≥3.5.1: "OneNote Web -> Show all notebooks (opens OneDrive) -> My files -> download the folder containing your notebooks as a ZIP"; "may not work for notebooks larger than 2–4 GB") ([Joplin import/export help](https://joplinapp.org/help/apps/import_export)). These .one files are in the **cloud/FSSHTTP "package store" format**, not the classic desktop revision-store format — the parser must support it (onenote.rs and Obsidian's TS parser both do).
3. **OneNote for Windows (Microsoft 365 / 2016 / 2021) -> File -> Export -> Notebook -> OneNote Package (*.onepkg)** or Section -> `*.one`. Needs a Windows machine (or Parallels/UTM VM). `.onepkg` is a **Microsoft Cabinet (CAB, LZX/MSZIP-compressed) archive** of the `.one`/`.onetoc2` files in *classic desktop revision-store format*. The Store "OneNote for Windows 10" app and OneNote for Mac cannot export these formats ([Obsidian help](https://obsidian.md/help/import/onenote); Joplin help). Joplin only imports .onepkg on Windows (it relies on the OS to unpack the CAB).
4. **OneNote for Mac**: only per-page/section **PDF** export (File -> Save as PDF). Useless for structured import (lossy, no structure) — not recommended. The Mac app keeps notebooks in a private cache, not as .one files.
5. **OneDrive desktop sync on Mac**: OneNote notebooks are *not* synced as .one files; the client shows them as internet-shortcut placeholders (`.url`) — not a usable route. (see verification note below)

**Recommendation**: Route 1 (OneNote web "Export notebook") per notebook, falling back to Route 2 (OneDrive web Download) if the menu is missing. Both produce a .zip we accept directly (drag the .zip onto Fortress). Route 3 (.onepkg) supported as well for users with Windows access.
- Verification of route 5: OneDrive sync shows notebooks as Internet-shortcut `.url` placeholders that only open OneNote web; the real `.one` files are not materialised locally ([MS answers thread](https://answers.microsoft.com/en-us/msoffice/forum/all/onenote-files-are-saved-as-a-web-link-instead-of-a/5fdf2311-c113-440b-bf50-21460832d4d8); [OneNoteGem](https://en.onenotegem.com/a/documents/gem-for-OneNote/Gem_Fix_Tools/2020/0331/open-notebook-url.html)). Not usable.

### In-app instructions to show the user (Mac, personal OneDrive)
> **Export from OneNote (about 1 minute per notebook)**
> 1. In a browser, open **onenote.com/notebooks** and sign in with your Microsoft account.
> 2. Right-click a notebook → **Export notebook** → **Export**. A `.zip` downloads (it contains a folder named after the notebook).
> 3. Repeat for each notebook. Don't unzip — drag all the `.zip` files onto Fortress (or File → Import → OneNote…).
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

### C. Obsidian Importer's TypeScript parser — **MIT** (see section above). Reads cloud package-store and desktop revision-store `.one`, `.onepkg` (own CAB+LZX), detects `.onex`/IRM. Model is simpler: no outline positions, no fonts/colours except highlight, tags with labels, ink with recognized text. Useful only if we wanted a JS/WASM-free TS path in a web frontend; not for us.

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

### CLI contract
`fortress-onenote extract <input.zip|.onepkg|.onetoc2|.one|dir> --out <workdir>` →
- unzips (zip) itself or accepts a directory; finds every `*.onetoc2` (skip `OneNote_RecycleBin`), else treats loose `.one` files as sections; `.onepkg` via `parse_package`.
- writes `workdir/manifest.json` (schema-versioned) + `workdir/assets/<sha256>.<ext>` (content-addressed, so duplicates collapse and re-runs are free).
```json
{ "schema": 1, "tool": {"name":"fortress-onenote","parser":"onenote_parser 2.1.0"},
  "notebooks": [{ "fileIdentity":"…","name":"Recipes","color":"#…",
    "children": [ {"kind":"sectionGroup","fileIdentity":"…","name":"…","children":[…]},
                  {"kind":"section","fileIdentity":"…","name":"Soups","color":"#…",
                   "warnings":[…],
                   "pages":[{ "guid":"{…}","title":"Pho","level":0,"order":3,
                              "created":"2019-…Z","modified":"2024-…Z","author":"…",
                              "outlines":[{ "x":0.5,"y":1.2,"width":6.5,"userWidth":true,
                                            "elements":[ /* paragraph | list item | table | image | file | ink, nested */ ] }],
                              "floating":[ {"kind":"image","x":…,"y":…,"asset":"sha.png","alt":"OCR…"} ],
                              "inkText":"recognized handwriting…" }]} ] }],
  "report": { "sectionsFailed":[{"path":"…","reason":"password-protected"}], "warnings":[…] } }
```
Paragraph element: `{"kind":"p","style":"h2|p|cite|code|…","align":"left|center|right","indent":1,"list":{"ordered":true,"format":"1.","restart":null}|null,"tags":[{"label":"To Do","shape":"…","checkable":true,"completed":false}],"runs":[{"text":"…","b":1,"i":1,"u":1,"s":1,"sup":1,"sub":1,"font":"Consolas","size":11,"color":"#C00000","highlight":"#FFFF00","link":"https://…","math":0}],"children":[…]}`.
Table: `{"kind":"table","borders":true,"colWidths":[…],"rows":[[{"bg":"#DEEAF6","children":[…]}]]}`. Image: `{"kind":"image","asset":"…","mime":"image/png","w":…,"h":…,"alt":"…","link":"…","printoutPage":3|null}`. File: `{"kind":"file","asset":"…","name":"Contract.pdf","mime":"application/pdf","status":"ok|missing|invalid"}`. Ink: `{"kind":"ink","svgAsset":"…","bbox":[…],"recognized":"…"}`. Math: run with `"math":1` plus `"mathml":"<math…>"` (port one2html's math renderer, MIT).

### Python side: `backend/app/onenote_import.py` + `POST /api/import/onenote` (upload zip/onepkg; job id; progress via polling)
**Built** (Nov 2026): see spec.md §7b for the mapping as implemented (subpages as a "Subpages" link list on the parent, two-pass link rewrite) and the job API. Update (Nov 2026): blocks are keyed by OneNote object ids (ExGUIDs, stable across edits; `onenote_parser` 2.1 keeps them internal, so the converter reads them from `Parser::dump_onestore` and matches them to content — tools/fortress-onenote/SCHEMA.md "Object ids"), paragraph links become block references, pages deleted in OneNote are marked `source_removed`, jobs persist/cancel, and each page gets a text-coverage check.
Pipeline: upload → temp dir → run CLI → read manifest → **plan** (diff against mapping table) → apply in one transaction per page (existing block transaction machinery) → second pass link rewrite → report.

**Mapping**
| OneNote | Fortress |
|---|---|
| Notebook | top-level folder (under an optional "OneNote" parent the user picks) |
| Section group | nested folder |
| Section | folder (section colour → folder `attrs.color` if/when folders get colours) |
| Page (level 0) | document in the section folder; title = page title (empty title → first line, as OneNote does) |
| Subpage (level 1/2) | **document nested under its parent page** if we add `documents.parent_document_id` (recommended, see parity list); interim: child document in the same folder with `attrs.onenote_parent` + a "Subpages" list block of `[[id|Title]]` links appended to the parent. Parent = nearest preceding page with level-1 (same rule as Obsidian). |
| created / modified | `documents.created_at` / `updated_at` set from the page (and block timestamps = page modified), not import time |
| Page title + date line | title only (OneNote's date/time under the title = created) |
| Single outline / outlines stacked vertically | blocks in y-order (ties broken by x) |
| **Outlines side by side** | `columns` layout block (see algorithm) with one `column` child per outline (attrs `width` as fraction) |
| Paragraph `style_id` h1..h6 | `heading` attrs `level` (OneNote has Heading 1–6, Page Title, Citation, Quote, Code) |
| Citation/Quote style | `quote`; "Code" style or runs all in Consolas/Courier → `code` block (merge adjacent, Obsidian's trick) |
| Bullets / numbering | `list` blocks (existing), `ordered`, nested children; keep `list_format` in attrs (`a.`, `i.`) |
| To Do tag (checkable shapes) | `list` with `checked` true/false (existing check items); `completed_at` → `attrs.checked_at` |
| Other tags (Important, Question, custom labelled) | block attr `tags: ["important","question","Custom label"]` rendered as marker chips + filterable in search (`block_attrs` table already indexes attrs); fallback text `#important` |
| Inline bold/italic/strike/link | Markdown marks (already supported) |
| underline, highlight (`highlight` colour), font colour, super/sub | **new inline marks** (BlockNote already has `underline`, `textColor`, `backgroundColor` styles; store via the existing `{"blocknote": [...]}` content payload, or in Markdown as `<u>`, `<mark data-color>`, `<span data-color>`, `<sup>/<sub>`) — map arbitrary OneNote colours to the nearest of BlockNote's named colours and keep the exact hex in a data attribute |
| fonts / font sizes | dropped by default (keep only "monospace" → code). Optionally large size runs → no-op. Report count of dropped font changes. |
| Tables | **new structured `table` block** (see shape) — cells may contain multiple blocks; nested table → rendered as a nested table block inside the cell (or flattened to text with a report entry if v1 cells are inline-only) |
| Images | asset via existing image store (`/api/images`, `NOTES_DIR/assets`, dedupe by `content_hash`); image block attrs `{asset_id, width, alt: OCR text, source_url}`; OCR alt indexed for search/Ask |
| Attachments | file block via `/api/files` → asset; show file card (name, size, type icon, open/quick-look) |
| Printouts | file block for the PDF + its page images as an image gallery (or just the PDF with inline PDF viewer later); keep OCR text from the page images |
| Video iframes | link block / embed with the URL |
| Audio/video recordings | file block (audio/*) with player |
| Ink | SVG asset in an image block positioned in reading order (by y) + `alt` = recognized handwriting |
| Math | `math` block/inline (frontend already has `MathPlaceholderBlock.tsx`) storing LaTeX (via MathML→LaTeX) and original MathML |
| Web links | Markdown links |
| `onenote:` links | **rewrite in pass 2**: parse `page-id={GUID}` (and `section-id`) from the URL → mapping table → `[[doc_id|Title]]`; `object-id={…}` (paragraph links) → block reference if we kept the paragraph id, else page link; unresolved (other notebooks / not imported) → keep text + original URL in `attrs.unresolved_link`, list in report |
| Pasted web content | content as-is; OneNote appends a "Pasted from <https://…>" line — detect that paragraph (link-only paragraph starting "Pasted from"/localized) → set document/blocks `attrs.source_url` and keep the link |

**Columns detection** (outline geometry; units = half-inches):
1. Collect outlines + floating images/files/ink as boxes `(x, y, w, h_est)`; `h_est` = Σ estimated line heights (≈0.18 in per text line at 11pt, wrap by width; image height from picture height) — no height is stored.
2. Sort by y. Sweep into horizontal **bands**: a box joins the current band if its y < band.top + 0.6·band.min_h_est and it does not horizontally overlap any box in the band (x-ranges disjoint with ≥0.1 in gap). Otherwise start a new band.
3. Band with 1 box → its blocks inline. Band with ≥2 boxes → `columns` block, children ordered by x, `width` = box.w / Σw. Small floating image beside text → also a column (common "image next to notes" layout).
4. Page "scattered notes" (many tiny outlines) → if >4 columns in a band, fall back to sequential blocks and flag in report.
Keep raw geometry in each column's attrs (`onenote_x/y/w`) so we can re-run with better heuristics without re-parsing.

**Idempotent re-import**
- New table `import_sources(source TEXT, external_id TEXT, kind TEXT /*notebook|group|section|page|block|asset*/, local_id TEXT, content_hash TEXT, source_modified TEXT, imported_at TEXT, PRIMARY KEY(source, external_id))` with `source = 'onenote'`.
- Keys: notebook/section/group → `file_identity` (stable across rename/move; fall back to path); page → `link_target_id` GUID; paragraphs → object id (ExGUID) where the converter can match one, else a content hash.
- Re-run: page unchanged (same `source_modified` and `content_hash` of the generated block tree) → skip; changed in OneNote and **untouched in Fortress** (document revision == revision recorded at import) → replace blocks; changed on both sides → import as a new revision and keep Fortress edits in history (or create "Title (OneNote update)" and report a conflict). Renamed/moved sections → move folders. Deleted in OneNote → never auto-delete; list in report.
- Assets content-addressed by sha256 → no duplicate files.
- Job state persisted (`import_jobs` table: phase, current section/page, counts) so a worker restart resumes from the last committed page. Each page is its own transaction.

**Verification report** (shown after import; also saved as a Fortress note "OneNote import report – <date>")
- Counts per notebook: sections, section groups, pages (by level), images, attachments, tables, ink drawings, equations, tags (by label), links (web / onenote resolved / unresolved) — source counts from manifest vs created counts from DB, highlighted when different.
- Text coverage check per page: normalised plain text from the manifest vs `text` column of imported blocks (character count + similarity); pages under 99% listed.
- Lists: sections failed (password-protected, malformed, IRM) with fix instructions; attachments with `data_status != ok`; unresolved `onenote:` links; layouts degraded (scattered >4 columns); dropped formatting (font family/size changes) counts; parser warnings verbatim.
- Spot-check helper: link per page to the original in OneNote web (if a `oneNoteWebUrl` can be derived — otherwise just title + section path).

## F4. Fortress features needed for parity — prioritised for THIS user
1. **Table block** (P0 — "tables" + pasted web content are heavy users of tables). Shape: `type:"table"`, `attrs:{columnWidths:[0.3,0.7], borders:true, headerRow:false}`, children `table_row` → `table_cell` blocks (`attrs:{background:"#DEEAF6", align}`), each cell holding child blocks (paragraphs/lists/images; nested tables allowed). Markdown export: GFM table when every cell is a single inline paragraph, else HTML `<table>`. BlockNote 0.57 (MPL-2.0) already ships a `table` block with cell `backgroundColor`/`textColor` and header rows — but its cells are inline-only; nested blocks in cells would need a custom block or flattening (report it). Currently GFM tables import as `raw` blocks — upgrade the Markdown importer at the same time.
2. **Inline formatting marks: underline, highlight (with colour), text colour, super/subscript** (P0 — user said "rich text formatting"; highlight is ubiquitous in OneNote notes). Search/Ask must index plain text only.
3. **Attachments display** (P0): file block card (name/size/type, open, Quick Look on Mac, inline PDF preview later); printout = PDF + page images; ensure `/api/files` handles large PDFs.
4. **Columns layout block** (P1 — explicit user requirement): `type:"columns"` with `column` children (`attrs:{width: fraction}`), each holding blocks; responsive collapse to stacked on narrow widths; Markdown export as sequential sections with an HTML comment marker (`<!-- columns -->`) for round-trip. **Note: BlockNote's `@blocknote/xl-multi-column` is GPL-3.0 OR proprietary** — don't use it; write our own column block in the web editor and in SwiftUI.
5. **Page hierarchy / subpages** (P1): `documents.parent_document_id` (document nesting inside a folder) with collapsible sidebar tree; otherwise subpages become siblings.
6. **Image block with alt/OCR text + width** (P1): keep OCR alt (OneNote's own OCR) → searchable; we already have `vision.py` for images lacking OCR.
7. **Tags/markers on blocks** (P2): Important/Question/custom tag chips + a "tagged items" view (OneNote's "Find tags" summary), checklist `checked_at`.
8. **Math block/inline** (P2): LaTeX rendering (KaTeX) — frontend has a MathPlaceholderBlock to build on.
9. **Ink as SVG images** (P2) with recognized handwriting as alt text.
10. **Source URL on documents** (P3) for pasted web clippings; **folder colours** (P3) for section colours; **embedded video** (P3).

## F5. Licensing
- `onenote_parser` — **MPL-2.0** (file-level weak copyleft): fine to link into our CLI/app (static linking OK) as long as any *modified MPL files* are published; unmodified use from crates.io needs only the licence notice + pointer to source. Its test fixtures include AGPL-3.0 samples — don't copy those into our repo.
- `one2html` — **MIT** (reuse rendering/math code freely with notice). `cab` crate (used by `onepkg` feature) — MIT.
- Obsidian Importer (Graph + file importers, InkML→SVG, code-detection, tag mapping) — **MIT**.
- `mathml-to-latex` (npm) — MIT; BlockNote core — MPL-2.0; **`@blocknote/xl-multi-column` — GPL-3.0 OR proprietary (avoid)**.
- aspose-note-foss — README says MIT, PyPI metadata `LicenseRef-Aspose-Split`: verify before any reuse. pyOneNote — Apache-2.0.
- Our `fortress-onenote` CLI can be MIT/our licence; include NOTICE entries for MPL/MIT deps.

## Appendix: why Graph was dropped (kept for reference)
User refused any Microsoft/Azure sign-in. Also: a personal-account user can't register an Entra app without first creating a directory (since June 2024 all new app registrations must live in a tenant — [Entra breaking changes](https://learn.microsoft.com/en-us/entra/identity-platform/reference-breaking-changes)), rclone users report the friction in Feb 2026 ([rclone forum](https://forum.rclone.org/t/onedrive-own-client-id-guide-is-outdated/53431)); and OneNote Graph throttling is 400 req/hour per user → 3–5 h for this user's ~500 pages. The file route avoids all of that and gives *more* data (positions as numbers, custom tag labels, handwriting recognition) — the one thing Graph gives that files don't is nothing material for this user.

STATUS: complete (Nov 18 2026).

## Open items to verify with the user's real export (first thing to do)
1. Run `one2html` on one exported zip (`cargo install --locked --no-default-features --features bin,onepkg one2html`) and compare to OneNote web visually — confirms format support + layout fidelity.
2. Confirm `page-id={GUID}` in this user's `onenote:` links equals `Page::link_target_id()` (doc comment says internal links reference it; not tested here).
3. Confirm the web "Export notebook" zip contains `.onetoc2` + all section `.one` files (Microsoft's page only says "two OneNote files") and whether embedded files/printouts are included in full (Joplin/Obsidian users have reported incomplete zips: [Joplin forum](https://discourse.joplinapp.org/t/onenote-zip-import-incomplete/43826)).
4. "Pasted from <url>" source lines are a OneNote-for-Windows convention; check whether the user's clippings have them.
