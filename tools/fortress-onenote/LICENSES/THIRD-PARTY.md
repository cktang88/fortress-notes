# Third-party software in fortress-onenote

The `fortress-onenote` source code (everything under `tools/fortress-onenote`
except `vendor/`) is part of Fortress Notes and is covered by the Fortress
Notes repository's terms.

## onenote_parser (MPL-2.0)

- Crate: `onenote_parser` 2.1.x and `onenote_parser-macros` 2.1.x
- Source: <https://github.com/msiemens/onenote.rs> (also <https://crates.io/crates/onenote_parser>)
- Licence: Mozilla Public License 2.0, <https://mozilla.org/MPL/2.0/>

Vendored with modifications in `vendor/onenote_parser/` (wired in through
`[patch.crates-io]`) and linked statically into the `fortress-onenote`
binary; `onenote_parser-macros` is used unmodified from crates.io.
MPL-2.0 is a file-level copyleft: the modified files remain under MPL-2.0
(licence text in `vendor/onenote_parser/LICENSE`), their source is published
in this repository, and `vendor/PATCHES.md` lists every change. Do not copy
the crate's test fixtures into this repository: some of them are AGPL-3.0 or
otherwise third-party.

## cab (MIT)

`cab` 0.6.0, <https://github.com/mdsteele/rust-cab>, Copyright Matthew D.
Steele, MIT licence (`vendor/cab/LICENSE`); vendored with the fixes listed
in `vendor/PATCHES.md`. Reads `.onepkg` packages (CAB archives).

## Other Rust crates

All other crates linked into the binary are under permissive licences
(MIT, Apache-2.0, BSD-3-Clause, Zlib, Unicode-3.0 / Unicode-DFS-2016,
Unlicense, 0BSD, CC0). Notable ones: `zip` (MIT), `lzxd` (MIT OR Apache-2.0, LZX
decompression for `.onepkg`), `serde`/`serde_json`,
`regex`, `sha2`, `time`, `anyhow` (MIT OR Apache-2.0), `zlib-rs` and
`foldhash` (Zlib), `encoding_rs` ((Apache-2.0 OR MIT) AND BSD-3-Clause).
Regenerate the full list with:

```sh
cargo metadata --format-version 1 | jq -r '.packages[] | "\(.name) \(.version): \(.license)"'
```
