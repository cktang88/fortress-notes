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
content object, list format or page title (and, before P10, a
handwriting-recognition object) that fails to parse is dropped with a section warning
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

**P9 — which character-format flags a style stores.** `ParagraphStyling`
reported bold, italic, underline, strikethrough, superscript and subscript
as plain `bool`s, so a text run that does not store a flag (and inherits it
from the paragraph style) could not be told apart from one that explicitly
turns it off. The new `ParagraphStyling::stored_flags()` returns them as
`StoredFlags` (`Option<bool>` each; exported as
`onenote_parser::property::rich_text::StoredFlags`); the existing accessors
are unchanged. The converter uses it to give text runs the formatting of
their paragraph style (a whole paragraph made bold and blue by a custom
paragraph style lost both). Files: `lib.rs`, `onenote/rich_text.rs`,
`one/property_set/paragraph_style_object.rs`.

**P10 — ink is located, not parsed.** Fortress has no ink; the converter
only counts ink drawings (`report.counts.ink_skipped`). `contents::Ink` is now
a marker with no data: `Content::Ink`, `PageContent::Ink` and
`EmbeddedObject::Ink` (which held an `EmbeddedInkContainer`, now removed) are
produced when the ink container object exists, without reading its strokes.
Handwriting recognition (`Page::ink_recognition`, `InkRecognition*`) is no
longer parsed. Removed files: `onenote/ink_recognition.rs`,
`one/property_set/{ink_container,ink_data_node,ink_stroke_node,stroke_properties_node,recognized_text_node}.rs`,
`one/property/ink_dimensions.rs`, `shared/multi_byte.rs` (ISF integers);
`onenote/ink.rs` shrank to the marker and the dead `simple::parse_vec_i32`
went. A damaged ink container no longer drops its outline item or paragraph
(the converter output on the regression corpus is byte-identical). Files:
`lib.rs`, `onenote/{ink,content,page_content,rich_text,page,page_series,section,mod}.rs`,
`one/property_set/mod.rs`, `one/property/{mod,simple}.rs`, `shared/mod.rs`.

**P11 — object ids.** `RichText`, `Table`, `Image`, `EmbeddedFile` and
`OutlineElement` keep the extended GUID they were parsed from and return it
from `id()`; `ExGuid` is exported as `onenote_parser::contents::ExGuid`
(public `guid`, whose `Display` is `{GUID}` in upper case, and `value`). The
converter writes them as block `object_id` / `element_id`; before this it
recovered them by matching the text of `Parser::dump_onestore` against the
content. Files: `lib.rs`, `fsshttpb/data/exguid.rs`,
`onenote/{rich_text,table,image,embedded_file,outline}.rs`.

**P12 — sections and tables of contents from native files only.** The
converter calls only `Parser::new`, `Parser::parse_section` and
`Parser::parse_toc` (P3) on files it has already extracted, and resolves
the entries of a table of contents itself. Removed: notebook and section-group
parsing (`Parser::parse_notebook`, `Notebook`, `SectionEntry`,
`SectionGroup`, the entry-path resolution), `.onepkg` parsing (`onepkg.rs`,
`Parser::parse_package`), `Parser::new_with_fs`,
`Parser::parse_section_buffer`, `Parser::dump_onestore`, the `FileSystem`
trait and `NativeFs` (a file is opened by `fs::open_file`, which keeps the
positional, page-cached reads and the Windows verbatim-path handling), the
crate features `native-fs`, `backtrace` and `onepkg`, the unused error kinds
`TocFileMissing`, `NotFssHttpBData` and `MalformedPackage`, and the `cab` and
`sanitize-filename` dependencies. `Parser` is now a unit struct and the
`errors` and `fs` modules are private. Files: `Cargo.toml`, `lib.rs`,
`errors.rs`, `fs/{mod,native_fs,file_source}.rs`,
`onenote/{mod,notebook,section}.rs`.

**P13 — math is read as linear text only.** Fortress has no math; the
converter writes equations as their plain linear text (text runs with
`ParagraphStyling::math_formatting`, which stays). The math inline objects
of math text runs (`RichText::math_inline_objects`, `MathInlineObject`,
`MathObjectType`) are no longer parsed, so a damaged one no longer drops its
paragraph. Removed files: `onenote/math_inline_object.rs`,
`one/property_set/math_inline_object.rs`; the `MathInlineObject*` property
types went. Files: `lib.rs`, `onenote/{mod,rich_text}.rs`,
`one/property/mod.rs`, `one/property_set/mod.rs`.

**P14 — rich text: only what the converter reads.** Paragraphs and
paragraph styles no longer parse properties the converter never reads:
`RichText::{paragraph_space_before, paragraph_space_after,
paragraph_line_spacing_exact, layout_alignment_in_parent,
layout_alignment_self}`, `ParagraphStyling::{charset, next_style,
paragraph_alignment, paragraph_space_before, paragraph_space_after,
paragraph_line_spacing_exact, language_code, hyperlink}` (the hyperlink flag
itself stays for `RichText::hyperlinks`), and the rich-text node's
last-modified time, title/boiler-text flags, language and reading order.
`EmbeddedObject::InkSpace` no longer carries a size (`EmbeddedInkSpace`
removed; a space without a size is still an error, as before), and of an
embedded ink container only the space size is read. A rich-text node without
a last-modified time is no longer rejected. Removed file:
`one/property/charset.rs`. Files: `lib.rs`, `onenote/rich_text.rs`,
`one/property/mod.rs`,
`one/property_set/{rich_text_node,paragraph_style_object,embedded_ink_container}.rs`.

**P15 — pages, outlines, tables, images, files, lists and tags: only what
the converter reads.** The same trim as P14 for the other objects. No longer
parsed or exposed: page titles' layout (`Page::title`, `page::Title`; the
title's outlines are still read for `Page::title_text`), page size, margins,
orientation, cached titles and other page / page-series / section metadata;
outlines' child level, indents, list spacing, alignment, height and
reserved/minimum widths (`OutlineIndentDistance` removed); outline elements'
list spacing, authors and flags; tables' locked columns and alignment, cells'
max width and indents; images' size, alignment and text language; embedded
files' text, source path, picture size and layout flags; lists' bold, italic,
font size and colour; iframes' embed type; pictures' size; note-tag
definitions' status and action item type (`NoteTagPropertyStatus`,
`ActionItemType` removed; a definition without them is still rejected, as
before) and `ActionItemStatus::{disabled, task_tag}`; the `Content` /
`PageContent` accessors `table`, `image`, `embedded_file`, `ink` (match on
the enums instead). Last-modified and creation times, GUIDs and schema
revisions that nothing reads are not parsed, so their absence no longer
rejects an object (a section without a GUID still gets its warning). Removed
files: `one/property/{layout_alignment,note_tag_property_status,outline_indent_distance,page_size}.rs`.
Files: `lib.rs`, `onenote/{content,embedded_file,iframe,image,list,note_tag,outline,page,page_content,picture,table}.rs`,
`one/property/{mod,note_tag}.rs`, `one/property_set/*_node.rs`,
`one/property_set/{page_metadata,note_tag_container,note_tag_shared_definition_container}.rs`.

**P16 — unused declarations.** After P10 and P12–P15, 95 `PropertyType`
ids (ink, handwriting recognition, layout, metadata) are no longer read and
went, as did the never-used `ObjectSpaceObjectStreamHeader`
(`onestore/desktop/common/object_space_object_stream_header.rs`) and
`ObjectRefAndId`. The vendored crate now builds without warnings. Files:
`one/property/mod.rs`, `onestore/desktop/common/mod.rs`,
`onestore/desktop/file_node/shared.rs`.

## cab 0.6.0

**C1 — no panic on corrupt cabinets.** In `folder.rs`, a file offset beyond
the folder's data blocks returns `InvalidData` instead of indexing past the
block list, and a data block that decompressed to fewer bytes than its
header promised is an error instead of an arithmetic underflow.

**C2 — lint only.** Explicit `'_` lifetimes on returned borrowing types
(`builder.rs`, since removed by C3, `cabinet.rs`, `folder.rs`) to keep
builds warning-free on current Rust. No behaviour change.

**C3 — read-only.** The cabinet writer was only used by the converter's
tests to build `.onepkg` inputs; they now use small checked-in fixtures
(`tests/fixtures/*.onepkg`, how they were made is in `tests/cli.rs`).
Removed `builder.rs` (`CabinetBuilder`, `CabinetWriter`, `FolderBuilder`,
`FileBuilder`, `FileWriter`) and the code only it used: `MsZipCompressor`,
`CompressionType::to_bitfield`, `datetime_to_bits`, the writer limits in
`consts.rs`, and the inline unit tests of those (the inline tests needed
removed dev-dependencies already). Files: `lib.rs`, `mszip.rs`, `ctype.rs`,
`datetime.rs`, `consts.rs`.
