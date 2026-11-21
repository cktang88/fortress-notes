# fortress-onenote-graph

**Optional.** Downloads your OneNote notebooks through Microsoft Graph and writes the
same `manifest.json` + `assets/` that [`fortress-onenote`](../fortress-onenote/README.md)
writes from `.one`/`.onepkg` files ([SCHEMA.md](../fortress-onenote/SCHEMA.md), format
version 1). Fortress imports it with **File › Import › From OneNote…** like any export.

Why it exists: OneNote for the web no longer has *Export notebook* (2026), OneDrive
won't download notebooks, and OneNote for Mac only saves PDFs — so on a Mac there is
no way to get the `.one` files the file route needs. On Windows, *File › Export ›
Notebook › OneNote Package (.onepkg)* still gives the most complete copy (see
[What Graph loses](#what-graph-loses-compared-with-one-files)); use this tool when that
is not an option.

**In the app (the usual way):** **File › Import › From OneNote…** › *Download from
Microsoft*. Paste your app's Application (client) ID (step 1 below; the sheet lists the
same steps), pick Personal or Work or school, and click **Download and Import**:
Fortress's backend runs this tool for you (`app/onenote_graph.py`: the wrapper below
with `--progress json`, your id in `FORTRESS_ONENOTE_CLIENT_ID`), shows sign-in and
download progress in the sheet, and imports the result. All it needs is
[uv](https://docs.astral.sh/uv/) (`brew install uv`) and this folder next to the
backend (or `FORTRESS_ONENOTE_GRAPH_DIR` pointing at it). The command line below still
works on its own, e.g. on a machine without Fortress.

The tool is a separate program with its own locked dependencies (MSAL for the
sign-in; everything else is the Python standard library). Fortress itself never
holds your Microsoft sign-in: the tool keeps it (Keychain on a Mac).

## 1. Register a (free) app in Microsoft Entra — once

Microsoft only lets programs read OneNote through an app registration, and this tool
ships none: you create your own, so nobody else's app ever gets access to your notes.
It costs nothing and takes about five minutes.

1. Sign in to the **Microsoft Entra admin center**, <https://entra.microsoft.com>
   (or the Azure portal, <https://portal.azure.com>), with any Microsoft account.
   - *Personal Microsoft account and the site says you have no directory/tenant?*
     Since 2024 every app registration must live in a directory. Create one by signing
     up for a free Azure account (<https://azure.microsoft.com/free>; Microsoft asks
     for identity verification, app registrations are free) and come back, or use a
     work/school tenant you can register apps in.
2. **Identity › Applications › App registrations › New registration**.
   - *Name*: anything, e.g. `Fortress OneNote download`.
   - *Supported account types*: **Accounts in any organizational directory (Any
     Microsoft Entra ID tenant – Multitenant) and personal Microsoft accounts**.
     (Choose *Personal Microsoft accounts only* if you only use a personal account.)
   - *Redirect URI*: platform **Public client/native (mobile & desktop)**, URI
     **`http://localhost`** (MSAL picks a free port on localhost; Entra accepts any
     port for `http://localhost`).
   - **Register**, then copy the **Application (client) ID** from the overview page.
3. **Authentication** › *Advanced settings* › **Allow public client flows: Yes** ›
   Save (needed for the device-code sign-in; the browser sign-in works without it).
4. **API permissions** › *Add a permission* › **Microsoft Graph** › **Delegated
   permissions**, add:
   - **Notes.Read** (personal accounts; also enough for your own notebooks in a work account),
   - **Notes.Read.All** (only for work/school accounts, `--account work`; reads
     notebooks shared with you; your organisation may have to approve it),
   - **offline_access** and **User.Read** (User.Read is usually there already).
   No admin consent is needed for a personal account. Do **not** create a client
   secret or certificate: this is a public client.

Every permission is read-only; the tool never asks for `Notes.ReadWrite*` or
`Notes.Create`.

## 2. Command line: install

Needs [uv](https://docs.astral.sh/uv/) (Fortress's backend uses it too). No install
step: `./fortress-onenote-graph` runs the tool with its locked dependencies
(`uv run --frozen`, installed on first use into `.venv/` here).

```sh
cd tools/fortress-onenote-graph
export FORTRESS_ONENOTE_CLIENT_ID=<your Application (client) ID>   # or --client-id
./fortress-onenote-graph login        # opens your browser; --device-code for a code instead
./fortress-onenote-graph notebooks    # lists your notebooks
```

## 3. Command line: download and import

```sh
./fortress-onenote-graph download --out ~/Desktop/OneNote.zip            # every notebook
./fortress-onenote-graph download --notebook "Work" --out ~/Desktop/Work.zip
```

Then in Fortress: **File › Import › From OneNote…**, drop the `.zip`. Fortress sees
that it is already a manifest and imports it without the converter. (`--out` may also
be a folder; the backend's `POST /api/import/onenote {"path": "<folder>"}` imports a
manifest folder in place.)

Download everything in **one** run when you can: links between pages of different
notebooks resolve when both are in the import (or were imported before).

### Re-running

Run the same `download` again whenever you like: it lists sections and pages (one
request per section and per 100 pages), then fetches only pages whose
`lastModifiedDateTime` changed and pictures/files not cached yet. Importing the new zip
refreshes the notes whose page changed (pages you edited in Fortress since the last
import are left alone and listed in the report), exactly like re-importing a `.one`
export.

### Time, throttling, interruptions

Microsoft allows OneNote **120 requests per minute and 400 per hour** per app and user,
and its throttling answers (429) say nothing about when to retry. The tool therefore
paces itself: at most 110 requests a minute and 380 an hour (the count is kept in the
cache, so a second run within the hour respects it), one request at a time; after a
429 it pauses 60 s, doubling up to 10 min, and spaces requests more until things
recover. Each page costs one request plus one per picture/attachment/printout page,
so **expect roughly an hour per 200–400 pages** on the first run; later runs are quick.

You can stop it at any time (Ctrl-C, sleep, network loss). Nothing already downloaded
is fetched again: run the same command and it continues. Exit code **3** means
"interrupted (throttling or network) — run again to resume".

### Progress output

Readable progress goes to stderr. `--progress json` prints one JSON object per line
instead, for a host app (Fortress's backend reads these to drive the import sheet):

```json
{"event": "phase", "phase": "pages"}
{"event": "progress", "phase": "pages", "done": 12, "total": 340, "current": "Work › Meetings › Standup"}
{"event": "wait", "seconds": 60.0, "reason": "throttled by Microsoft (429), backing off"}
{"event": "device_code", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "message": "…"}
{"event": "warning", "message": "…"}
{"event": "info", "message": "…"}
```

`device_code` appears only when the browser sign-in did not work and a code must be
entered instead. `notebooks --json` prints `{"notebooks": [{"name": "Work", "sections": 3}]}`.

Phases: `signin`, `hierarchy`, `sections`, `pages`, `convert`, `done`. When it
finishes, stdout gets one summary line like the converter's: `{"manifest": "…",
"format_version": 1, "notebooks": n, "sections": n, "sections_failed": n, "pages": n,
"assets": n, "download": {…}}`.

### Other commands

- `convert --out X.zip` — write the manifest again from the cache, no network.
- `download --no-convert` — only refresh the cache.
- `logout` — forget the cached sign-in.
- `--account work` — work/school account (`Notes.Read.All`, authority `organizations`);
  `--tenant <id|common>` or `FORTRESS_ONENOTE_TENANT` overrides the authority.

## Where things are kept

| What | macOS | elsewhere |
|---|---|---|
| Sign-in (refresh token) | **Keychain**, service `fortress-onenote-graph` | `~/.config/fortress-onenote-graph/msal_token_cache.bin`, mode 0600 |
| Download cache | `~/Library/Caches/fortress-onenote-graph/` | `~/.cache/fortress-onenote-graph/` |

The cache holds your pages' HTML and files (one folder per account, files named by
hashes of Graph ids); delete it to start over (`--cache-dir` to put it elsewhere). The
access token is only ever sent to `https://graph.microsoft.com`: picture and file URLs
taken from page HTML are checked before they are fetched, and anything pointing
elsewhere is reported as missing instead.

## What the manifest contains (Graph specifics)

Everything in [SCHEMA.md](../fortress-onenote/SCHEMA.md) applies; on top of that:

- **Ids.** Graph ids are not OneNote GUIDs, so notebooks, section groups, sections and
  pages get `id`/`file_identity` = `graph:<Graph id>`. Fortress keys notes by page `id`,
  so pages downloaded through Graph never collide with pages imported from `.one`
  files (importing the same notebook both ways gives two copies), and re-downloads
  update the same notes. `link_target_id` is the page GUID OneNote uses in
  `onenote:` links (`page-id={…}`, read from the page's `links.oneNoteClientUrl`), so
  links between pages are rewritten to Fortress links. Notebook `source_kind` is `graph`;
  `input.kind` is `graph`.
- **Block ids.** `object_id` is taken from Graph's `includeIDs` element ids
  (`id="p:{GUID}{N}"` → `{GUID}&N` in hex), used by Fortress as stable block keys so a
  paragraph edited in OneNote updates the same block. That these equal OneNote's own
  ExGUIDs (which paragraph links name) is not verified; when they differ, paragraph
  links fall back to page links.
- **Layout.** Each outline is an absolutely positioned `div`; the reading-order layout
  is the converter's algorithm ported to Python (`layout.py`), with heights estimated
  conservatively, so side-by-side outlines become columns only when their positions
  clearly say so.
- **Blocks.** Paragraphs, headings 1–6, bulleted/numbered lists with levels and styles,
  check items from to-do tags (and other checkbox tags), other note tags as `tags`,
  tables (cell shading, column widths, borders, nested tables), images (full
  resolution, `alt_text` = OneNote's OCR text, kept), attachments, printouts (the file +
  one image per page), video embeds, links (web and `onenote:`, incl. OneNote-for-web
  `wd=target(…)` links), bold/italic/underline/strikethrough/superscript/subscript,
  text colour, highlight, fonts/sizes, alignment, "Pasted from" source lines.
  Consolas/monospace paragraphs become `code` blocks (adjacent ones merged).
- **Equations** come back as MathML; Fortress has no math blocks, so an equation is
  kept as its text in a `code` block (inline ones as plain text) and every page with
  one gets a warning in the import report.

## What Graph loses compared with .one files

| | `.one` / `.onepkg` (fortress-onenote) | Graph (this tool) |
|---|---|---|
| Ink / handwriting | skipped and counted (Fortress has no ink) | **not downloaded** (the report says so once) |
| Equations | plain linear text (a `code` block when alone in a paragraph) | text in a code block |
| Custom note tags | kept with their labels | **dropped by Graph** (the text stays) |
| Section order | table-of-contents order | Graph's listing order, sections before section groups |
| Section colours, author, page height | kept | not available |
| Outline widths set by hand vs automatic | known | unknown (treated as automatic) |
| EMF pictures, XPS printouts | original kept next to the preview | only what Graph serves |
| Paragraph link targets | ExGUIDs, verified | `includeIDs` ids, unverified (falls back to page links) |
| Password-protected sections | must be unlocked first | listed as failed sections; unlock and re-run |

Everything else (text, formatting, lists, to-dos, tables, pictures with OCR text,
attachments, printouts, links, subpages, dates) comes through both routes.

## Tests

```sh
uv run --frozen python -m unittest discover -s tests -t .
```

No network and no Microsoft account: Graph is an in-memory fake (`tests/fakes.py`) and
the clock is fake. The fixtures in `tests/fixtures/` are written from Microsoft's
documented output HTML. The end-to-end test (tool output → the backend's import job →
notes and blocks) is `backend/tests/test_onenote_graph_import.py`. CI:
`.github/workflows/onenote-graph.yml` (Linux and macOS).

**Not verified without a real account**: the exact HTML Graph returns today for every
construct (fixtures follow the documentation), whether `includeIDs` ids equal OneNote's
ExGUIDs, the nested hierarchy `$expand` on large accounts (the tool falls back to
listing level by level), the Keychain write on a real Mac (msal-extensions'
`KeychainPersistence`), and the throttling numbers under load.
