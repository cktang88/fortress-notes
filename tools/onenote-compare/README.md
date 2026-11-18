# onenote-compare — does an imported note contain everything?

A developer tool (not shipped in the app) that checks the OneNote importer
against an independent rendering of the same files, page by page:

```
tools/onenote-compare/run.sh <export.zip | notebook folder | Section.one | Notebook.onepkg>... [-o REPORT_DIR]
tools/onenote-compare/run.sh --each <folder> -o REPORT_DIR   # every .one/.onepkg/.zip in it, separately
```

Large corpora: `--shots failing --sample 25` takes screenshots/PDFs only of pages
with a failing check (and every 25th page), `--prune` deletes each input's
converted files and temporary notes once its pages are checked, and `--skip TEXT`
leaves out inputs whose path contains TEXT.

`REPORT_DIR` defaults to `./onenote-compare-report`. Open `REPORT_DIR/index.html`
(pass rate per check, one row per page, links to a
detail page per page with the diffs and a side-by-side image). `results.json`
holds the same data.

Nothing leaves the machine: the browser runs offline (`file://` only), no AI or
OCR calls are made, and every import goes into a throwaway `NOTES_DIR` inside the
report folder (`units/<input>/fortress/notes`). The report contains your notes'
content and pictures — keep it private and delete it when done.

## What it does

For each input:

1. **Reference.** [`one2html`](https://github.com/msiemens/one2html) (MIT; it
   shares no code with our converter, only the `onenote_parser` crate) renders
   every section to HTML, laying outlines out at their absolute positions like
   OneNote does. Headless Chromium (Playwright) renders each page to
   `ref.png` + `ref.pdf` and measures where every outline / floating image ended up.
2. **Fortress.** `fortress-onenote` converts the input, the real importer
   (`backend/app/onenote_import.py`, `import_manifest`) imports the manifest into
   a fresh notes folder, and every imported note is rendered twice:
   `tree.html` (the block tree from `block_store.document_tree`, i.e. what
   `/api/block-documents/{id}` returns, drawn with columns, tables, cell colours,
   check boxes, images, files and math) and `md.html` (the note's Markdown
   export, rendered with markdown-it with HTML enabled; `<!-- fortress:columns -->`
   markers become flex columns). Both go to PNG + PDF.
3. **Diff.** `ref.py` (one2html HTML) and `fortress.py` (block tree) reduce a
   page to the same model (`model.py`); `checks.py` compares them:

   | check | compares |
   |---|---|
   | title | page title (untitled pages: Fortress names the note after its first line) |
   | text | multiset of normalised words (NFC, case-folded), missing/extra with context |
   | marks | per word: bold, italic, underline, strike, sup/sub, code (monospace), text colour, highlight |
   | headings | (level, text) |
   | lists | (text, ordered, depth) of real list items |
   | checks | to-do items (text, checked) |
   | links | text + href; `onenote:` links must become internal links when their page was imported |
   | tables | count, rows × cols, every cell's words, cell backgrounds |
   | images | SHA-256 of every picture (or of its original, for EMF/XPS printouts) |
   | image_alt | alt / OCR text of matched pictures |
   | files | attachments by SHA-256, name, size |
   | math | number of equations (block and inline) |
   | ink | drawings present, recognised handwriting kept |
   | tags | note tags other than to-do boxes |
   | align | centred / right-aligned paragraphs |
   | layout | outlines side by side in OneNote must be columns (left to right) in Fortress; an outline above another in the same area must come first |
   | export | the Markdown export rendered by a CommonMark viewer: no word missing, no code block that is not code, list nesting kept |
4. **Side by side.** `side.png` per page: one2html | block tree | Markdown export.

## Requirements

* Rust (stable) — builds the converter; installs `one2html` 2.0.1 into
  `~/.cache/fortress-onenote-compare` (or `$FORTRESS_COMPARE_CACHE`) on first use (its default `backtrace`
  feature needs nightly, so it is built without it). Set `ONE2HTML` to use another
  binary and `FORTRESS_ONENOTE_BIN` to skip building the converter.
* `uv` and the backend's locked environment (`cd backend && uv sync --frozen`).
* Chromium for Playwright: set `PLAYWRIGHT_BROWSERS_PATH` to an installed browser
  folder, or run `uv run --with playwright==1.63.0 playwright install chromium` once.

## Reading the results — known, intended differences

* **Equations.** one2html shows MathML. Fortress has no inline-math run: an
  inline equation becomes inline code `$<LaTeX>$`; a paragraph that is only an
  equation is a `math` block (LaTeX + MathML). The text check ignores equation
  words; the math check counts equations on both sides.
* **Ink.** one2html draws each ink object separately; the converter merges a
  page's drawings into one SVG, so only presence and recognised words are checked.
* **Printouts / EMF.** one2html writes the raw XPS/EMF (browsers cannot show it);
  Fortress stores the browser-friendly picture and keeps the original
  (`attrs.original_url`). Images match on either hash.
* **Alt text.** Fortress also keeps OneNote's OCR text where one2html shows no
  alt; only a reference alt that differs counts as a failure.
* **Non-web links.** Only http(s)/mailto/tel/ftp/… links stay clickable;
  `file:`, UNC paths, `javascript:`, `ms-word:` and over-long URLs are kept as
  text (target in the block's provenance, `dropped_links`) and reported as info.
* **Broken stored links.** Where OneNote's stored hyperlink is cut short but the
  visible text is a full URL, the converter links the full URL (one2html links
  the stored one).
* **one2html failures.** one2html panics on a few damaged files (listed under
  *Errors*); pages it cannot render have no reference (`page: n/a`), while a page
  one2html rendered that is missing in Fortress is `page: fail`.

## Files

`run.sh` (entry point) · `compare.py` (driver, rendering, composites, report) ·
`ref.py` (one2html → model) · `fortress.py` (import, block tree → model / HTML,
Markdown → HTML) · `model.py` · `checks.py`.
