# fortress-onenote

Converts exported Microsoft OneNote notebooks into a JSON manifest plus
content-addressed asset files, for the Fortress Notes OneNote importer.
No Microsoft account, Graph API or network access is involved: it reads the
files the user exported. Background and design: `docs/onenote-import.md`
(sections F1–F5). Output format: [SCHEMA.md](SCHEMA.md).

```sh
fortress-onenote <input> --out <dir> [--keep-extracted] [--compact] [-q]
```

Input can be:

| Input | Where it comes from |
|---|---|
| `.zip` | OneNote for the web → *Export notebook*, or OneDrive → select notebook/folder → *Download*. May contain several notebooks, section groups, or only loose sections. |
| folder | an unpacked export |
| `.onetoc2` | a notebook's table of contents (the folder around it is read) |
| `.one` | a single section |
| `.onepkg` | OneNote for Windows → File → Export → Notebook → OneNote Package |

Output: `<dir>/manifest.json` and `<dir>/assets/<sha256>.<ext>`. On success
one JSON summary line is printed to stdout; see SCHEMA.md for exit codes.

## Behaviour

- Zip files are extracted into `<dir>/.extracted` with zip-slip protection
  (absolute paths, `..`, drive letters, control characters and symlinks are
  rejected; nothing is overwritten; size/entry-count/compression-ratio limits)
  and removed afterwards. `OneNote_RecycleBin`, `__MACOSX` and `.DS_Store`
  are skipped; `errors.txt` (written by OneNote web when an export fails) is
  not extracted but reported in `report.export_errors`.
- A notebook is a folder with a `.onetoc2` that has no `.onetoc2` above it.
  Every `.one` file under it is parsed individually and placed in the tree by
  `file_identity` following the table of contents; sections missing from the
  TOC (OneDrive downloads often ship an empty `.onetoc2`) are placed by
  folder with `in_toc: false`. Sub-folders with their own TOC that the
  notebook does not list become separate notebooks. `.one` files outside any
  notebook form a `loose_sections` notebook per folder.
- Sections that cannot be read (password-protected, damaged, not synced) are
  listed in `report.failed_sections` with a reason and a hint; the rest of
  the notebook still converts.
- Names from the notebook (attachment file names, image names) are untrusted
  and only appear as display strings; assets are named by SHA-256.
- Ink is rendered to standalone SVG; equations to LaTeX and MathML.

## Locating the binary (for the importer)

The Python importer looks for `FORTRESS_ONENOTE_BIN`, then
`tools/fortress-onenote/target/release/fortress-onenote`.

```sh
cd tools/fortress-onenote
cargo build --release          # -> target/release/fortress-onenote
```

The macOS universal binary (arm64 + x86_64, built with `lipo`) is produced by
the `OneNote converter` GitHub Actions workflow
(`.github/workflows/onenote-tool.yml`) as the artifact
`fortress-onenote-macos-universal`.

## Tests

```sh
cargo test                     # unit tests + CLI tests with synthetic inputs
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```

Unit tests cover the layout grouping, `onenote:` link GUID extraction, mark
offsets (code points, hidden field codes, surrogate pairs), citation
detection, asset naming/dedup and zip extraction safety. Real OneNote files
are not committed (the public samples are third-party, partly AGPL). To run
the sample-based end-to-end test, point `FORTRESS_ONENOTE_SAMPLES` at a
folder of `.zip`/`.one`/`.onetoc2`/`.onepkg` files or notebook folders, e.g.
the test data of <https://github.com/msiemens/onenote.rs>
(`crates/parser/tests/samples`) and Joplin
(`packages/app-cli/tests/support/onenote`):

```sh
FORTRESS_ONENOTE_SAMPLES=/path/to/samples cargo test --release -- --nocapture
```

The test checks every manifest invariant (mark ranges, unique ids, layout
covers every item, asset hashes).

## Licences

- This tool's code: part of Fortress Notes.
- [`onenote_parser`](https://github.com/msiemens/onenote.rs) — MPL-2.0, used
  unmodified as a crate dependency (statically linked). Its source is
  available at the link above; do not modify or vendor its files without
  publishing the changes under MPL-2.0.
- `src/math/` is adapted from [one2html](https://github.com/msiemens/one2html)
  (MIT, © 2020 Markus Siemens) — see `LICENSES/one2html-MIT.txt`.
- Other dependencies are permissive (MIT / Apache-2.0 / BSD / Zlib /
  Unicode); see `LICENSES/THIRD-PARTY.md`.
