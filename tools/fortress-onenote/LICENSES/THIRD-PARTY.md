# Third-party software in fortress-onenote

The `fortress-onenote` source code (everything under `tools/fortress-onenote`
except `src/math/`) is part of Fortress Notes and is covered by the Fortress
Notes repository's terms.

## onenote_parser (MPL-2.0)

- Crate: `onenote_parser` 2.1.x and `onenote_parser-macros` 2.1.x
- Source: <https://github.com/msiemens/onenote.rs> (also <https://crates.io/crates/onenote_parser>)
- Licence: Mozilla Public License 2.0, <https://mozilla.org/MPL/2.0/>

Used unmodified as a Cargo dependency and linked statically into the
`fortress-onenote` binary. MPL-2.0 is a file-level copyleft: the source code
of the MPL-covered files is available at the links above. If you ever change
files of `onenote_parser` (e.g. by vendoring or patching it), the modified
files must be published under MPL-2.0. Do not copy the crate's test fixtures
into this repository: some of them are AGPL-3.0 or otherwise third-party.

## one2html (MIT)

`src/math/` (the OneNote equation parser and its LaTeX/MathML renderers) is
adapted from one2html 2.0.1, <https://github.com/msiemens/one2html>,
Copyright 2020 Markus Siemens, MIT licence: see `one2html-MIT.txt`.

## Other Rust crates

All other crates linked into the binary are under permissive licences
(MIT, Apache-2.0, BSD-3-Clause, Zlib, Unicode-3.0 / Unicode-DFS-2016,
Unlicense, 0BSD, CC0). Notable ones: `zip` (MIT), `cab` and `lzxd` (MIT /
MIT OR Apache-2.0, used by onenote_parser for `.onepkg`), `serde`/`serde_json`,
`regex`, `sha2`, `time`, `anyhow` (MIT OR Apache-2.0), `zlib-rs` and
`foldhash` (Zlib), `encoding_rs` ((Apache-2.0 OR MIT) AND BSD-3-Clause).
Regenerate the full list with:

```sh
cargo metadata --format-version 1 | jq -r '.packages[] | "\(.name) \(.version): \(.license)"'
```
