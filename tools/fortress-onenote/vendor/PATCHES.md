# Vendored crates and their patches

`fortress-onenote` builds against patched copies of two crates, wired in
through `[patch.crates-io]` in `../Cargo.toml`:

| Directory | Upstream | Licence |
|---|---|---|
| `onenote_parser/` | [`onenote_parser` 2.1.0](https://crates.io/crates/onenote_parser/2.1.0), source <https://github.com/msiemens/onenote.rs> | MPL-2.0 (`onenote_parser/LICENSE`) |
| `cab/` | [`cab` 0.6.0](https://crates.io/crates/cab/0.6.0), source <https://github.com/mdsteele/rust-cab> | MIT (`cab/LICENSE`) |

Both started as the unmodified crates.io packages (commit
"vendor pristine onenote_parser 2.1.0 … and cab 0.6.0" in this repository's
history), normalised only by removing binaries, examples, tests and
dev-dependencies. Every changed file starts with
`// Modified by fortress-onenote (vendor/PATCHES.md: <ids>)` and each change
is marked in place with `PATCH(fortress-onenote, <id>)`. The modified
`onenote_parser` files remain under the MPL-2.0 and are published here, in
this repository, as the Source Code Form of the modifications required by
MPL-2.0 section 3.

Why: an adversarial corpus of public and generated OneNote files
(third-party writers, password-protected and OneNote 2007 sections, fuzzed
files) made the upstream crates panic, lose whole sections over one bad
object, or resolve data wrongly. To see all changes against upstream:

```sh
git diff 42744c3 -- tools/fortress-onenote/vendor   # pristine import
```

## onenote_parser 2.1.0

**P1 — per-element references in property-value lists.** In
`one/property_set/mod.rs`, `parse_object` gave every element of a
`PropertyValues` list (note-tag states, text-run data) the object/space
references starting at the property's own offset, so element *n* reused
element 0's references: every tag on a paragraph with several tags resolved
to the first tag's definition (label and shape). `parse_object_list` now
advances the offsets by the references each element consumes and gives each
nested object a mapping table that translates its local indexes back to the
parent's. Files: `one/property_set/{mod,note_tag_container,text_run_data}.rs`.

**P2 — panics on damaged data.** An empty `NumberListFormat`
(`number_list_node.rs`, `data[1..]`), more text-run styles than run
boundaries (`onenote/page.rs`, `visible_text`), and a broken hyperlink marker
in a page title (`onenote/page.rs`, `remove_hyperlink`, which indexed the
original string with an offset into the cleaned one) no longer panic.

**P3 — `Parser::parse_toc`.** Reads a `.onetoc2` without parsing any
section and returns its entries as stored (`onenote::notebook::TableOfContents`,
exported as `onenote_parser::notebook::TableOfContents`).
`Parser::parse_notebook` silently skips entries whose file is missing; the
converter resolves entries itself and reports missing ones.
Files: `lib.rs`, `onenote/mod.rs`, `onenote/notebook.rs`.

**P4 — lenient revision-store structure.** A file-node-list reference that
is `fcrNil`/`fcrZero` is an empty list (third-party writers emit it for
object spaces without revisions) instead of a list at offset 0; a
transaction log that cannot be read is ignored (lists are then read up to
their terminators). Files: `onestore/desktop/file_node/mod.rs`,
`onestore/desktop/one_store_file.rs`.

**P5 — password-protected sections.** New `ErrorKind::Encrypted` and
`Error::kind()`. Reaching an `ObjectDataEncryptionKeyV2FNDX` node (0x07C)
returns `Encrypted`; a hashed chunk list that cannot be read (encrypted in
protected sections) no longer hides that error. Files: `errors.rs`,
`onestore/desktop/file_node/mod.rs`, `onestore/desktop/one_store_file.rs`.

**P6 — optional or damaged data is not fatal.** Unpaired UTF-16 surrogates
become U+FFFD (`utils.rs`); outline elements without authors
(`outline_element_node.rs`), table cells without contents
(`table_cell_node.rs`), outlines and cells without indent distances and
outlines/elements without a child level (`outline_node.rs`,
`outline_element_node.rs`, `table_cell_node.rs`,
`one/property/outline_indent_distance.rs`) get defaults; a dangling
paragraph-style reference uses default styling with a warning
(`onenote/rich_text.rs`); an attachment or image whose data reference cannot
be resolved is reported as missing/invalid data instead of failing the
section (`onestore/desktop/file_node/shared.rs`).

**P7 — skip damaged objects, keep the section.** A page, outline item,
content object, list format, page title or handwriting-recognition object
that fails to parse is dropped with a section warning
(`skipped a damaged …: <error>`) instead of failing the whole section
(`onenote::skip_damaged`). Encryption errors are never swallowed. Files:
`onenote/{mod,outline,page,page_series}.rs`.

**P8 — tables of contents of third-party writers.** When the standard
`TocChildren` / `FolderChildFilename` properties are absent, a TOC container
falls back to its first object-id array for its children and (for a
non-root container without children) to its first plain UTF-16 string
property for the file name; entries without an ordering id keep their
position after ordered ones. Files: `one/property_set/toc_container.rs`,
`onenote/notebook.rs`, `onestore/shared/prop_set.rs`.

## cab 0.6.0

**C1 — no panic on corrupt cabinets.** In `folder.rs`, a file offset beyond
the folder's data blocks returns `InvalidData` instead of indexing past the
block list, and a data block that decompressed to fewer bytes than its
header promised is an error instead of an arithmetic underflow.

**C2 — lint only.** Explicit `'_` lifetimes on returned borrowing types
(`builder.rs`, `cabinet.rs`, `folder.rs`) to keep builds warning-free on
current Rust. No behaviour change.
