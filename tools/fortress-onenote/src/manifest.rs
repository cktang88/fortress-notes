//! Serialisable manifest model. See `SCHEMA.md` for the documented contract.
//!
//! Every struct here is part of the public, versioned JSON schema. Changing a
//! field name or meaning requires bumping [`FORMAT_VERSION`]; adding an
//! optional field does not.

use serde::Serialize;
use std::collections::BTreeMap;

/// Version of the manifest format. Bump on any incompatible change.
pub const FORMAT_VERSION: u32 = 1;
/// Value of the top-level `format` field.
pub const FORMAT_NAME: &str = "fortress-onenote-manifest";

#[derive(Serialize, Debug)]
pub struct Manifest {
    pub format: &'static str,
    pub format_version: u32,
    pub generator: Generator,
    pub units: Units,
    pub input: InputInfo,
    pub notebooks: Vec<Notebook>,
    /// Asset file name (`<sha256>.<ext>`) -> metadata. Files live in `assets/`.
    pub assets: BTreeMap<String, AssetInfo>,
    pub report: GlobalReport,
}

#[derive(Serialize, Debug)]
pub struct Generator {
    pub name: &'static str,
    pub version: &'static str,
    pub parser: &'static str,
}

#[derive(Serialize, Debug)]
pub struct Units {
    /// Always `"code_points"`: mark offsets count Unicode scalar values.
    pub text_offsets: &'static str,
    /// Always `"half_inch"`: geometry is in OneNote's half-inch units.
    pub geometry: &'static str,
    /// CSS pixels (96 dpi) per geometry unit (48).
    pub px_per_geometry_unit: f32,
}

#[derive(Serialize, Debug)]
pub struct InputInfo {
    /// `zip`, `directory`, `onetoc2`, `one` or `onepkg`.
    pub kind: String,
    /// Base name of the input (never a full path).
    pub name: String,
}

#[derive(Serialize, Debug, Clone)]
pub struct AssetInfo {
    pub path: String,
    pub sha256: String,
    pub size: u64,
    pub mime: String,
    pub ext: String,
}

#[derive(Serialize, Debug)]
pub struct Notebook {
    /// Stable id: `file_identity` when known, else a hash of the source path.
    pub id: String,
    pub file_identity: Option<String>,
    pub name: String,
    pub color: Option<String>,
    /// `onetoc2` (normal notebook), `onepkg` or `loose_sections` (no TOC found).
    pub source_kind: String,
    /// Path of the notebook root relative to the input (forward slashes).
    pub source_path: String,
    pub children: Vec<Entry>,
    pub report: NotebookReport,
}

#[derive(Serialize, Debug)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Entry {
    SectionGroup(SectionGroup),
    Section(Section),
}

#[derive(Serialize, Debug)]
pub struct SectionGroup {
    pub id: String,
    pub file_identity: Option<String>,
    pub name: String,
    /// Display path: notebook, groups..., this group.
    pub path: Vec<String>,
    pub children: Vec<Entry>,
}

#[derive(Serialize, Debug)]
pub struct Section {
    pub id: String,
    pub file_identity: Option<String>,
    pub name: String,
    pub color: Option<String>,
    pub path: Vec<String>,
    /// False when the section file was found on disk but not listed in the
    /// notebook's table of contents (appended after the listed entries).
    pub in_toc: bool,
    pub pages: Vec<Page>,
    pub warnings: Vec<Warning>,
}

#[derive(Serialize, Debug)]
pub struct Page {
    /// Same as `link_target_id` (the page GUID, `{XXXXXXXX-...}` upper case).
    pub id: String,
    pub link_target_id: String,
    pub title: String,
    /// True when the page had no title text and `title` was derived.
    pub title_derived: bool,
    /// 0 = page, 1 = subpage, 2 = sub-subpage.
    pub level: i32,
    /// Id of the nearest preceding page with a lower level (subpages only).
    pub parent_id: Option<String>,
    /// 0-based position in the section.
    pub order: usize,
    pub created: String,
    pub updated: String,
    pub author: Option<String>,
    /// notebook, groups..., section, page title.
    pub source_path: Vec<String>,
    /// First "Pasted from <url>" style source found on the page.
    pub source_url: Option<String>,
    /// Page height (geometry units) if OneNote stored one.
    pub height: Option<f32>,
    /// Top-level positioned items in OneNote's z/document order.
    pub items: Vec<Item>,
    /// Reading-order layout computed from `items` geometry.
    pub layout: Vec<LayoutRow>,
    pub stats: PageStats,
}

#[derive(Serialize, Debug, Default, Clone)]
pub struct PageStats {
    pub blocks: usize,
    pub text_chars: usize,
}

#[derive(Serialize, Debug)]
pub struct Item {
    /// `i0`, `i1`, ... unique within the page.
    pub id: String,
    /// `outline`, `image`, `file`.
    pub kind: &'static str,
    pub x: Option<f32>,
    pub y: Option<f32>,
    /// Declared width (outline max width / picture width), geometry units.
    pub width: Option<f32>,
    pub width_set_by_user: bool,
    /// Estimated rendered width/height used by the layout algorithm.
    pub est_width: f32,
    pub est_height: f32,
    pub is_background: bool,
    pub source_url: Option<String>,
    pub blocks: Vec<Block>,
}

#[derive(Serialize, Debug, Clone)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LayoutRow {
    /// One item occupying the full row.
    Item { item: String },
    /// Side-by-side columns, ordered left to right.
    Columns {
        columns: Vec<LayoutColumn>,
        /// True when the band had too many columns to be a real layout
        /// (scattered notes); importers may stack the columns instead.
        scattered: bool,
    },
}

#[derive(Serialize, Debug, Clone)]
pub struct LayoutColumn {
    /// Fraction of the band's total column span (sums to 1).
    pub width: f32,
    pub x: f32,
    pub span: f32,
    /// Nested rows (columns may themselves contain columns).
    pub rows: Vec<LayoutRow>,
}

#[derive(Serialize, Debug, Clone, Default)]
pub struct Rich {
    pub text: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub marks: Vec<Mark>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Mark {
    /// Start offset (inclusive), Unicode code points.
    pub start: usize,
    /// End offset (exclusive), Unicode code points.
    pub end: usize,
    #[serde(flatten)]
    pub kind: MarkKind,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum MarkKind {
    Bold,
    Italic,
    Underline,
    Strikethrough,
    Superscript,
    Subscript,
    /// Inline code (an equation's LaTeX, `$…$`).
    Code,
    Highlight {
        color: String,
    },
    Color {
        color: String,
    },
    Font {
        family: String,
    },
    FontSize {
        pt: f32,
    },
    Link {
        href: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        onenote: Option<OneNoteLink>,
        /// True when the URL was found in plain text rather than stored as a link.
        #[serde(skip_serializing_if = "std::ops::Not::not")]
        auto: bool,
    },
}

#[derive(Serialize, Debug, Clone, PartialEq, Default)]
pub struct OneNoteLink {
    /// Target page GUID, `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}` upper case.
    pub page_id: Option<String>,
    pub section_id: Option<String>,
    /// Paragraph link target (object-id GUID only), if any.
    pub object_id: Option<String>,
    /// Full paragraph link target `{GUID}&N` (N hex, upper case): equals the
    /// `element_id` (or `object_id`) of the linked block.
    pub object_ref: Option<String>,
    /// Section file part of the URL (decoded), e.g. `.../Notebook/Section.one`.
    pub section_file: Option<String>,
    /// Page title fragment (decoded).
    pub page_title: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
pub struct Tag {
    pub label: String,
    /// Icon shape in snake_case (e.g. `green_check_box`, `no_icon`).
    pub shape: String,
    pub checkable: bool,
    pub completed: bool,
    pub completed_at: Option<String>,
    pub highlight_color: Option<String>,
    pub text_color: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
pub struct ListInfo {
    pub ordered: bool,
    /// decimal, upper_roman, lower_roman, upper_alpha, lower_alpha, bullet
    pub style: String,
    /// Bullet character (unordered) or the format pattern with `%` standing
    /// for the number (ordered), e.g. `%.` or `(%)`.
    pub marker: String,
    pub restart: Option<i32>,
}

/// Fields shared by every block.
#[derive(Serialize, Debug, Clone, Default)]
pub struct BlockCommon {
    /// Nesting depth inside the outline / cell (0 = top level).
    pub indent: u32,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub tags: Vec<Tag>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub align: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_url: Option<String>,
    /// ExGUID of the OneNote content node (`{GUID}&N`), stable across edits.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_id: Option<String>,
    /// ExGUID of the enclosing outline element (paragraph): what paragraph
    /// links (`object-id=`) name.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub element_id: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
pub struct TableCell {
    pub background: Option<String>,
    pub blocks: Vec<Block>,
}

#[derive(Serialize, Debug, Clone)]
pub struct TableRow {
    pub cells: Vec<TableCell>,
}

#[derive(Serialize, Debug, Clone)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Block {
    Paragraph {
        #[serde(flatten)]
        common: BlockCommon,
        #[serde(flatten)]
        rich: Rich,
        /// True for OneNote's "Pasted from <url>" / "From <url>" citation line.
        #[serde(skip_serializing_if = "std::ops::Not::not")]
        is_source_citation: bool,
    },
    Heading {
        #[serde(flatten)]
        common: BlockCommon,
        level: u8,
        #[serde(flatten)]
        rich: Rich,
    },
    ListItem {
        #[serde(flatten)]
        common: BlockCommon,
        list: ListInfo,
        #[serde(flatten)]
        rich: Rich,
    },
    CheckItem {
        #[serde(flatten)]
        common: BlockCommon,
        checked: bool,
        checked_at: Option<String>,
        list: Option<ListInfo>,
        #[serde(flatten)]
        rich: Rich,
    },
    Quote {
        #[serde(flatten)]
        common: BlockCommon,
        /// `quote` or `cite`.
        variant: String,
        #[serde(flatten)]
        rich: Rich,
    },
    Code {
        #[serde(flatten)]
        common: BlockCommon,
        text: String,
        /// `latex` for an equation (its LaTeX); none for code.
        #[serde(skip_serializing_if = "Option::is_none")]
        language: Option<String>,
    },
    Table {
        #[serde(flatten)]
        common: BlockCommon,
        row_count: u32,
        col_count: u32,
        column_widths: Vec<f32>,
        borders_visible: bool,
        rows: Vec<TableRow>,
    },
    Image {
        #[serde(flatten)]
        common: BlockCommon,
        asset: Option<String>,
        data_status: String,
        alt_text: Option<String>,
        width: Option<f32>,
        height: Option<f32>,
        hyperlink: Option<String>,
        is_printout_page: bool,
        printout_page_number: Option<u32>,
        is_background: bool,
        /// Sanitised original file name, for display only.
        original_name: Option<String>,
        /// The stored original when `asset` is OneNote's browser-compatible
        /// fallback picture (e.g. EMF, XPS printout page).
        original_asset: Option<String>,
    },
    Embed {
        #[serde(flatten)]
        common: BlockCommon,
        url: String,
        /// Poster image for the embed, if any.
        thumbnail_asset: Option<String>,
        /// Alt / OCR text of the picture that carried the embed.
        alt_text: Option<String>,
    },
    File {
        #[serde(flatten)]
        common: BlockCommon,
        asset: Option<String>,
        /// Sanitised file name for display (never use as a path).
        filename: String,
        extension: Option<String>,
        mime: String,
        size: u64,
        /// `available`, `missing`, `invalid`.
        data_status: String,
        /// `audio`, `video` or `other`.
        media_kind: String,
        icon_asset: Option<String>,
    },
}

impl Block {
    pub fn common_mut(&mut self) -> &mut BlockCommon {
        match self {
            Block::Paragraph { common, .. }
            | Block::Heading { common, .. }
            | Block::ListItem { common, .. }
            | Block::CheckItem { common, .. }
            | Block::Quote { common, .. }
            | Block::Code { common, .. }
            | Block::Table { common, .. }
            | Block::Image { common, .. }
            | Block::Embed { common, .. }
            | Block::File { common, .. } => common,
        }
    }

    /// Plain text carried by the block itself (not nested cells).
    pub fn plain_text(&self) -> &str {
        match self {
            Block::Paragraph { rich, .. }
            | Block::Heading { rich, .. }
            | Block::ListItem { rich, .. }
            | Block::CheckItem { rich, .. }
            | Block::Quote { rich, .. } => &rich.text,
            Block::Code { text, .. } => text,
            _ => "",
        }
    }
}

#[derive(Serialize, Debug, Clone)]
pub struct Warning {
    pub message: String,
    pub page_id: Option<String>,
    pub page_title: Option<String>,
}

#[derive(Serialize, Debug, Default)]
pub struct Counts {
    pub section_groups: usize,
    pub sections: usize,
    pub sections_failed: usize,
    pub pages: usize,
    pub subpages: usize,
    pub blocks: usize,
    pub paragraphs: usize,
    pub headings: usize,
    pub list_items: usize,
    pub check_items: usize,
    pub code_blocks: usize,
    pub quotes: usize,
    pub tables: usize,
    pub images: usize,
    pub printout_pages: usize,
    pub attachments: usize,
    pub attachments_missing: usize,
    /// Ink drawings not converted (Fortress does not support ink).
    pub ink_skipped: usize,
    pub embeds: usize,
    pub links_web: usize,
    pub links_onenote: usize,
    pub tags: usize,
    pub column_layouts: usize,
    pub text_chars: usize,
}

#[derive(Serialize, Debug)]
pub struct FailedSection {
    /// Display path of the containing notebook/groups plus the file name.
    pub path: Vec<String>,
    /// Section file path relative to the input (forward slashes).
    pub file: Option<String>,
    /// Human-readable reason.
    pub reason: String,
    /// What the user can do about it.
    pub hint: String,
    /// Raw parser error.
    pub error: String,
}

#[derive(Serialize, Debug)]
pub struct MissingData {
    pub page_id: String,
    pub page_title: String,
    pub section_path: Vec<String>,
    /// `attachment` or `image`.
    pub kind: String,
    pub name: Option<String>,
    pub data_status: String,
}

#[derive(Serialize, Debug)]
pub struct LayoutNote {
    pub page_id: String,
    pub page_title: String,
    pub message: String,
}

#[derive(Serialize, Debug, Default)]
pub struct NotebookReport {
    pub counts: Counts,
    pub tags_by_label: BTreeMap<String, usize>,
    pub warnings: Vec<Warning>,
    pub failed_sections: Vec<FailedSection>,
    pub missing_data: Vec<MissingData>,
    pub layout_notes: Vec<LayoutNote>,
}

#[derive(Serialize, Debug, Default)]
pub struct GlobalReport {
    /// Contents of any `errors.txt` found in the export (trimmed).
    pub export_errors: Vec<ExportError>,
    /// Problems with the input itself (unsafe zip entries, unreadable files).
    pub input_warnings: Vec<String>,
    /// Files/entries deliberately skipped (recycle bin, unrelated files).
    pub skipped: Vec<String>,
    pub notebooks: usize,
    pub sections: usize,
    pub sections_failed: usize,
    pub pages: usize,
    pub assets: usize,
    pub assets_bytes: u64,
}

#[derive(Serialize, Debug)]
pub struct ExportError {
    pub file: String,
    pub text: String,
}
