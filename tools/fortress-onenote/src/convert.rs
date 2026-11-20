//! Parser objects -> manifest model.

use crate::assets::{self, AssetStore};
use crate::input::{self, NotebookSource};
use crate::layout::{self, LBox};
use crate::links::normalize_guid;
use crate::manifest::*;
use crate::objectids::{Ids, PageIds, SectionIds};
use crate::richtext::{self, color_hex};
use onenote_parser::Parser;
use onenote_parser::contents::{
    Content, EmbeddedFile, EmbeddedObject, FileDataStatus, Image, List, NoteTag, Outline,
    OutlineElement, OutlineItem, RichText, Table,
};
use onenote_parser::page::{Page as OnePage, PageContent};
use onenote_parser::property::common::Color;
use onenote_parser::property::embedded_file::FileType;
use onenote_parser::property::rich_text::ParagraphAlignment;
use onenote_parser::section::Section as OneSection;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use time::format_description::well_known::Rfc3339;
use typed_path::TypedPath;

// ---- estimation constants (geometry units = half inch) --------------------
const CHAR_W: f32 = 0.15;
const LINE_H: f32 = 0.4;
const PARA_GAP: f32 = 0.05;
const DEFAULT_OUTLINE_WIDTH: f32 = 13.0;

pub fn iso(t: time::UtcDateTime) -> String {
    t.format(&Rfc3339).unwrap_or_default()
}

fn color_argb(c: Option<Color>) -> Option<String> {
    let c = c?;
    if c.alpha() == 0 {
        return None;
    }
    Some(format!("#{:02X}{:02X}{:02X}", c.r(), c.g(), c.b()))
}

fn path_id(prefix: &str, rel: &str) -> String {
    let h = hex::encode(Sha256::digest(rel.as_bytes()));
    format!("{prefix}:{}", &h[..16])
}

fn snake(s: &str) -> String {
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if ch.is_ascii_uppercase() {
            if i > 0 {
                out.push('_');
            }
            out.push(ch.to_ascii_lowercase());
        } else if ch.is_ascii_digit() && i > 0 && !out.ends_with(|c: char| c.is_ascii_digit()) {
            out.push('_');
            out.push(ch);
        } else {
            out.push(ch);
        }
    }
    out
}

fn status_str(s: FileDataStatus) -> &'static str {
    match s {
        FileDataStatus::Available => "available",
        FileDataStatus::Missing => "missing",
        FileDataStatus::Invalid => "invalid",
        _ => "unknown",
    }
}

fn typed(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

/// Per-notebook conversion state.
pub struct NotebookCtx<'a> {
    pub assets: &'a mut AssetStore,
    pub report: NotebookReport,
}

/// Per-page state.
struct PageCtx<'a, 'b> {
    nb: &'b mut NotebookCtx<'a>,
    page_id: String,
    page_title: String,
    section_path: Vec<String>,
    source_url: Option<String>,
    /// Object ids of this page, when the section file could be indexed.
    ids: Option<PageIds>,
    /// Ink drawings on this page, not converted (Fortress has no ink).
    ink_skipped: usize,
    /// Equations on this page, imported as their linear text (Fortress has no math).
    equations: usize,
}

fn set_ids(common: &mut BlockCommon, ids: Ids) {
    common.object_id = ids.object_id;
    common.element_id = ids.element_id;
}

impl PageCtx<'_, '_> {
    fn ids_with(&mut self, f: impl FnOnce(&mut PageIds) -> Ids) -> Ids {
        self.ids.as_mut().map(f).unwrap_or_default()
    }

    fn warn(&mut self, message: String) {
        self.nb.report.warnings.push(Warning {
            message,
            page_id: Some(self.page_id.clone()),
            page_title: Some(self.page_title.clone()),
        });
    }

    /// An ink drawing: counted in the report, never converted.
    fn skip_ink(&mut self) {
        self.ink_skipped += 1;
        self.nb.report.counts.ink_skipped += 1;
    }

    fn missing(&mut self, kind: &str, name: Option<String>, status: &str) {
        self.nb.report.missing_data.push(MissingData {
            page_id: self.page_id.clone(),
            page_title: self.page_title.clone(),
            section_path: self.section_path.clone(),
            kind: kind.into(),
            name,
            data_status: status.into(),
        });
    }

    fn tags(&mut self, tags: &[NoteTag]) -> Vec<Tag> {
        tags.iter()
            .map(|nt| {
                let def = nt.definition();
                let label = def.map(|d| d.label().to_string()).unwrap_or_default();
                let shape = def
                    .map(|d| snake(&format!("{:?}", d.shape())))
                    .unwrap_or_else(|| "no_icon".into());
                let checkable = def.map(|d| d.shape().is_checkable()).unwrap_or(false);
                let key = if label.is_empty() {
                    shape.clone()
                } else {
                    label.clone()
                };
                *self.nb.report.tags_by_label.entry(key).or_default() += 1;
                self.nb.report.counts.tags += 1;
                // Only a check box can be completed: OneNote (and other
                // writers) also store a "completed" status and time on
                // stars, flags and other non-checkable tags.
                let completed = checkable && nt.item_status().completed();
                Tag {
                    label,
                    shape,
                    checkable,
                    completed,
                    completed_at: nt
                        .completed_at()
                        .map(time::UtcDateTime::from)
                        .filter(|t| completed && t.year() > 1980)
                        .map(iso),
                    highlight_color: def.and_then(|d| color_hex(d.highlight_color())),
                    text_color: def.and_then(|d| color_hex(d.text_color())),
                }
            })
            .collect()
    }

    // ---- outline content ---------------------------------------------------

    fn outline_items(&mut self, items: &[OutlineItem], indent: u32, out: &mut Vec<Block>) {
        for item in items {
            match item {
                OutlineItem::Element(el) => self.element(el, indent, out),
                OutlineItem::Group(g) => {
                    let extra = (g.child_level() as u32).min(8);
                    self.outline_items(g.outlines(), indent + extra, out)
                }
            }
        }
    }

    fn element(&mut self, el: &OutlineElement, indent: u32, out: &mut Vec<Block>) {
        let list = el.list_contents().first();
        let mut list_used = false;
        for content in el.contents() {
            match content {
                Content::RichText(rt) => {
                    let l = if list_used { None } else { list };
                    list_used = true;
                    self.rich_text(rt, l, indent, out);
                }
                Content::Table(t) => {
                    let b = self.table(t, indent);
                    out.push(b);
                }
                Content::Image(img) => self.image(img, indent, out),
                Content::EmbeddedFile(f) => {
                    let b = self.file(f, indent);
                    out.push(b);
                }
                Content::Ink(_) => self.skip_ink(),
                Content::Unknown => self.warn("unsupported content element skipped".into()),
            }
        }
        if !el.children().is_empty() {
            let step = (el.child_level() as u32).clamp(1, 8);
            self.outline_items(el.children(), indent + step, out);
        }
    }

    fn rich_text(&mut self, rt: &RichText, list: Option<&List>, indent: u32, out: &mut Vec<Block>) {
        let tags = self.tags(rt.note_tags());
        let ids = self.ids_with(|p| p.rich_text(rt.text()));

        // Handwriting embedded in the paragraph is not converted.
        let inline_ink = rt
            .embedded_objects()
            .iter()
            .filter(|o| matches!(o, EmbeddedObject::Ink(_)))
            .count();
        for _ in 0..inline_ink {
            self.skip_ink();
        }
        if rt.text().trim().is_empty() && !rt.embedded_objects().is_empty() {
            return;
        }

        let style_id = rt
            .paragraph_style()
            .style_id()
            .or_else(|| rt.text_run_formatting().iter().find_map(|s| s.style_id()))
            .map(|s| s.to_ascii_lowercase());
        let heading_level = style_id.as_deref().and_then(|s| {
            let b = s.as_bytes();
            (b.len() == 2 && b[0] == b'h' && (b'1'..=b'6').contains(&b[1])).then(|| b[1] - b'0')
        });
        let conv = richtext::convert(
            rt,
            richtext::Options {
                suppress_fonts: heading_level.is_some(),
            },
        );
        {
            let c = &mut self.nb.report.counts;
            c.links_web += conv.links_web;
            c.links_onenote += conv.links_onenote;
        }
        self.equations += conv.equations;
        for n in &conv.notes {
            self.warn(n.clone());
        }
        let align = match rt.paragraph_alignment() {
            ParagraphAlignment::Center => Some("center".to_string()),
            ParagraphAlignment::Right => Some("right".to_string()),
            _ => None,
        };
        let rich = conv.rich;
        if rich.text.trim().is_empty() && tags.is_empty() && list.is_none() {
            // Blank spacing paragraph.
            return;
        }
        let common = BlockCommon {
            indent,
            tags,
            align,
            source_url: None,
            object_id: ids.object_id,
            element_id: ids.element_id,
        };
        let list_info = list.map(list_info);
        let check = common.tags.iter().find(|t| t.checkable).cloned();

        if is_display_equation(conv.all_math, list_info.is_some(), check.is_some()) {
            // Fortress has no math blocks: the equation's linear text becomes a
            // code block (language `text`, so it is not merged with code around it).
            out.push(Block::Code {
                common,
                text: rich.text,
                language: Some("text".to_string()),
            });
            return;
        }
        if let Some(level) = heading_level {
            out.push(Block::Heading {
                common,
                level,
                rich,
            });
            return;
        }
        if let Some(t) = check {
            out.push(Block::CheckItem {
                common,
                checked: t.completed,
                checked_at: t.completed_at,
                list: list_info,
                rich,
            });
            return;
        }
        if let Some(list) = list_info {
            out.push(Block::ListItem { common, list, rich });
            return;
        }
        let sid = style_id.as_deref().unwrap_or("");
        if sid == "code" || conv.all_monospace {
            out.push(Block::Code {
                common,
                text: rich.text,
                language: None,
            });
            return;
        }
        // OneNote's "Pasted from <url>" line (often in the Citation style).
        let citation = richtext::citation_url(&rich);
        if let Some(url) = &citation
            && self.source_url.is_none()
        {
            self.source_url = Some(url.clone());
        }
        if citation.is_none() && matches!(sid, "cite" | "blockquote" | "quote") {
            out.push(Block::Quote {
                common,
                variant: if sid == "cite" { "cite" } else { "quote" }.into(),
                rich,
            });
            return;
        }
        let mut common = common;
        common.source_url = citation.clone();
        out.push(Block::Paragraph {
            common,
            rich,
            is_source_citation: citation.is_some(),
        });
    }

    fn table(&mut self, t: &Table, indent: u32) -> Block {
        let tags = self.tags(t.note_tags());
        let (row_count, col_count) = (t.rows(), t.cols());
        let ids = self.ids_with(|p| p.table(row_count, col_count));
        let rows = t
            .contents()
            .iter()
            .map(|row| TableRow {
                cells: row
                    .contents()
                    .iter()
                    .map(|cell| {
                        let mut blocks = Vec::new();
                        for el in cell.contents() {
                            self.element(el, 0, &mut blocks);
                        }
                        merge_code(&mut blocks);
                        TableCell {
                            background: color_argb(cell.background_color()),
                            blocks,
                        }
                    })
                    .collect(),
            })
            .collect();
        Block::Table {
            common: BlockCommon {
                indent,
                tags,
                object_id: ids.object_id,
                element_id: ids.element_id,
                ..Default::default()
            },
            row_count,
            col_count,
            column_widths: t.col_widths().iter().map(|w| round2(*w)).collect(),
            borders_visible: t.borders_visible(),
            rows,
        }
    }

    fn image(&mut self, img: &Image, indent: u32, out: &mut Vec<Block>) {
        let tags = self.tags(img.note_tags());
        let ids = self.ids_with(|p| p.image(img.alt_text(), img.image_filename()));
        let status = img.data_status();
        let original = img.image_filename().map(assets::display_name);
        let mut asset = None;
        let mut original_asset = None;
        // Image bytes that are not any picture format we know (damaged or
        // foreign data) are kept as `.bin` and reported as invalid.
        let mut unrecognized = false;
        let mut write_failed = false;
        if let Some(mut r) = img.read() {
            let mut hint = img.extension().and_then(assets::sanitize_ext);
            // Printout pages of XPS documents are stored as XPS (a zip).
            if img.displayed_page_number().is_some() {
                hint = Some("xps".into());
            }
            let mut head = Vec::with_capacity(64);
            let _ = std::io::Read::read_to_end(&mut std::io::Read::take(&mut r, 64), &mut head);
            if !head.is_empty() && assets::sniff(&head).is_none() {
                unrecognized = true;
                hint = Some("bin".into());
            }
            let mut data = std::io::Read::chain(&head[..], r);
            match self.nb.assets.put_reader(&mut data, hint.as_deref(), false) {
                Ok(a) => asset = a,
                Err(e) => {
                    write_failed = true;
                    self.warn(format!("image could not be written: {e:#}"))
                }
            }
        }
        // Fall back to the browser-compatible picture if the primary is
        // missing or not something browsers display (EMF/WMF/TIFF/XPS...).
        let needs_web = asset
            .as_ref()
            .map(|a| !assets::is_web_image(a))
            .unwrap_or(true);
        if needs_web
            && let Some(p) = img.web_picture()
            && p.data_status() == FileDataStatus::Available
        {
            let hint = p.extension().and_then(assets::sanitize_ext);
            if let Ok(Some(a)) = self
                .nb
                .assets
                .put_reader(&mut p.read(), hint.as_deref(), false)
            {
                original_asset = asset.take();
                asset = Some(a);
            }
        }
        // One status for the block and the report: `empty` for zero bytes,
        // `invalid` for data that is not a picture (unless OneNote's
        // fallback picture could be used instead).
        let data_status = match (status, &asset) {
            (FileDataStatus::Available, None) if write_failed => "missing",
            (FileDataStatus::Available, None) => "empty",
            (FileDataStatus::Available, Some(_)) if unrecognized && original_asset.is_none() => {
                "invalid"
            }
            (s, _) => status_str(s),
        };
        if data_status != "available" {
            self.missing("image", original.clone(), data_status);
        }
        let page_no = img.displayed_page_number();
        let is_printout = page_no.is_some()
            || original
                .as_deref()
                .and_then(assets::ext_of_name)
                .map(|e| matches!(e.as_str(), "pdf" | "xps" | "oxps"))
                .unwrap_or(false);
        let alt = img
            .alt_text()
            .or(img.text())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        let embeds: Vec<String> = img
            .embeds()
            .iter()
            .map(|e| e.source_url().trim().to_string())
            .filter(|u| !u.is_empty())
            .collect();
        let common = BlockCommon {
            indent,
            tags,
            object_id: ids.object_id,
            element_id: ids.element_id,
            ..Default::default()
        };
        if !embeds.is_empty() {
            for (i, url) in embeds.into_iter().enumerate() {
                self.nb.report.counts.embeds += 1;
                let mut common = common.clone();
                if i > 0 {
                    set_ids(&mut common, Ids::default());
                }
                out.push(Block::Embed {
                    common,
                    url,
                    thumbnail_asset: asset.clone(),
                    alt_text: alt.clone(),
                });
            }
            return;
        }
        if is_printout {
            self.nb.report.counts.printout_pages += 1;
        }
        out.push(Block::Image {
            common,
            asset,
            data_status: data_status.into(),
            alt_text: alt,
            width: img.picture_width().or(img.layout_max_width()).map(round2),
            height: img.picture_height().or(img.layout_max_height()).map(round2),
            hyperlink: img.hyperlink_url().map(str::to_string),
            is_printout_page: is_printout,
            printout_page_number: page_no,
            is_background: img.is_background(),
            original_name: original,
            original_asset,
        });
    }

    fn file(&mut self, f: &EmbeddedFile, indent: u32) -> Block {
        let tags = self.tags(f.note_tags());
        let ids = self.ids_with(|p| p.file(f.filename()));
        let filename = assets::display_name(f.filename());
        let ext = assets::ext_of_name(f.filename());
        let status = f.data_status();
        let mut asset = None;
        let mut write_failed = false;
        if status == FileDataStatus::Available {
            match self.nb.assets.put_reader(
                &mut f.read(),
                Some(ext.as_deref().unwrap_or("bin")),
                true,
            ) {
                Ok(a) => asset = a,
                Err(e) => {
                    write_failed = true;
                    self.warn(format!(
                        "attachment {filename:?} could not be written: {e:#}"
                    ))
                }
            }
        }
        // One status for the block and the report (`empty`: zero bytes).
        let data_status = match (status, &asset) {
            (FileDataStatus::Available, None) if write_failed => "missing",
            (FileDataStatus::Available, None) => "empty",
            (s, _) => status_str(s),
        };
        if asset.is_none() {
            self.nb.report.counts.attachments_missing += 1;
            self.missing("attachment", Some(filename.clone()), data_status);
        }
        let icon_asset = f.icon().and_then(|p| {
            if p.data_status() != FileDataStatus::Available {
                return None;
            }
            let hint = p.extension().and_then(assets::sanitize_ext);
            self.nb
                .assets
                .put_reader(&mut p.read(), hint.as_deref(), false)
                .ok()
                .flatten()
        });
        let media_kind = match f.file_type() {
            FileType::Audio => "audio",
            FileType::Video => "video",
            FileType::Unknown => "other",
        };
        let mime = asset
            .as_ref()
            .and_then(|a| self.nb.assets.assets.get(a))
            .map(|a| a.mime.clone())
            .unwrap_or_else(|| assets::mime_for_ext(ext.as_deref().unwrap_or("")).to_string());
        Block::File {
            common: BlockCommon {
                indent,
                tags,
                object_id: ids.object_id,
                element_id: ids.element_id,
                ..Default::default()
            },
            asset,
            filename,
            extension: ext,
            mime,
            size: f.size(),
            data_status: data_status.into(),
            media_kind: media_kind.into(),
            icon_asset,
        }
    }
}

fn round2(v: f32) -> f32 {
    (v * 100.0).round() / 100.0
}

fn list_info(l: &List) -> ListInfo {
    let fmt = l.list_format();
    let font = l.list_font().or(l.font()).unwrap_or("");
    if let Some((style, marker)) = number_pattern(fmt) {
        ListInfo {
            ordered: true,
            style: style.into(),
            marker,
            restart: l.list_restart(),
        }
    } else {
        let raw: String = fmt.iter().filter(|c| !c.is_control()).collect();
        let marker = match (font, raw.chars().next()) {
            ("Wingdings", Some('\u{a7}')) => "▪".to_string(),
            ("Wingdings", Some('\u{a8}')) => "◻".to_string(),
            ("Wingdings", Some('w')) => "⬥".to_string(),
            ("Wingdings 2", Some('\u{ae}')) => "◆".to_string(),
            ("Wingdings 3", Some('}')) => "▶".to_string(),
            ("Symbol", Some('\u{b7}')) | (_, None) => "•".to_string(),
            (f, Some(c)) if f.starts_with("Wingdings") || (c as u32) >= 0xF000 => "•".to_string(),
            _ => raw,
        };
        ListInfo {
            ordered: false,
            style: "bullet".into(),
            marker,
            restart: None,
        }
    }
}

/// A numbered-list format: literal text with number placeholders, each a
/// U+FFFD followed by one number-format code (Word's NFC values: 0 decimal,
/// 1/2 upper/lower roman, 3/4 upper/lower letter, others such as the
/// Chinese counting formats are rendered as decimal). Placeholders may sit
/// anywhere (`(%)`, `Step %:`). Returns the style of the first placeholder
/// and the pattern with `%` for every placeholder, or `None` for a bullet.
fn number_pattern(fmt: &[char]) -> Option<(&'static str, String)> {
    let mut style = None;
    let mut marker = String::new();
    let mut it = fmt.iter().copied();
    while let Some(c) = it.next() {
        if c == '\u{FFFD}' {
            let code = it.next().map(u32::from).unwrap_or(0);
            style.get_or_insert(match code {
                1 => "upper_roman",
                2 => "lower_roman",
                3 => "upper_alpha",
                4 => "lower_alpha",
                _ => "decimal",
            });
            marker.push('%');
        } else if !c.is_control() {
            marker.push(c);
        }
    }
    style.map(|s| (s, marker))
}

/// Merge consecutive code paragraphs at the same indent into one block
/// (equations, which carry a language, stay one block each).
fn merge_code(blocks: &mut Vec<Block>) {
    let mut out: Vec<Block> = Vec::with_capacity(blocks.len());
    for b in blocks.drain(..) {
        if let Block::Code {
            common,
            text,
            language: None,
        } = &b
            && common.tags.is_empty()
            && let Some(Block::Code {
                common: pc,
                text: pt,
                language: None,
            }) = out.last_mut()
            && pc.indent == common.indent
            && pc.tags.is_empty()
        {
            pt.push('\n');
            pt.push_str(text);
            continue;
        }
        out.push(b);
    }
    *blocks = out;
}

// ---- height / width estimation ----------------------------------------------

fn est_text(text: &str, width: f32, scale: f32) -> f32 {
    let per_line = ((width / (CHAR_W * scale)).floor() as usize).max(8);
    let lines: usize = text
        .split('\n')
        .map(|l| l.chars().count().div_ceil(per_line).max(1))
        .sum();
    lines as f32 * LINE_H * scale + PARA_GAP
}

fn est_blocks(blocks: &[Block], width: f32) -> f32 {
    blocks.iter().map(|b| est_block(b, width)).sum()
}

fn est_block(b: &Block, width: f32) -> f32 {
    match b {
        Block::Heading { level, rich, .. } => {
            est_text(&rich.text, width, if *level <= 2 { 1.5 } else { 1.2 })
        }
        Block::Table {
            rows,
            column_widths,
            ..
        } => rows
            .iter()
            .map(|r| {
                r.cells
                    .iter()
                    .enumerate()
                    .map(|(i, c)| {
                        let w = column_widths.get(i).copied().unwrap_or(2.0).max(0.5);
                        est_blocks(&c.blocks, w).max(LINE_H)
                    })
                    .fold(LINE_H, f32::max)
                    + 0.05
            })
            .sum(),
        Block::Image { height, .. } => height.unwrap_or(2.0) + PARA_GAP,
        Block::Embed { .. } => 4.0,
        Block::File { .. } => 1.2,
        other => est_text(other.plain_text(), width, 1.0),
    }
}

fn est_content_width(blocks: &[Block]) -> f32 {
    blocks
        .iter()
        .map(|b| {
            let indent = 0.5 * b_indent(b) as f32;
            indent
                + match b {
                    Block::Table { column_widths, .. } => column_widths.iter().sum(),
                    Block::Image { width, .. } => width.unwrap_or(4.0),
                    Block::File { .. } => 2.0,
                    Block::Embed { .. } => 6.0,
                    other => {
                        let longest = other
                            .plain_text()
                            .split('\n')
                            .map(|l| l.chars().count())
                            .max()
                            .unwrap_or(0);
                        longest as f32 * CHAR_W + 0.4
                    }
                }
        })
        .fold(1.0, f32::max)
}

fn b_indent(b: &Block) -> u32 {
    match b {
        Block::Paragraph { common, .. }
        | Block::Heading { common, .. }
        | Block::ListItem { common, .. }
        | Block::CheckItem { common, .. }
        | Block::Quote { common, .. }
        | Block::Code { common, .. }
        | Block::Table { common, .. }
        | Block::Image { common, .. }
        | Block::Embed { common, .. }
        | Block::File { common, .. } => common.indent,
    }
}

// ---- counting -----------------------------------------------------------------

fn count_blocks(blocks: &[Block], c: &mut Counts) {
    for b in blocks {
        c.blocks += 1;
        c.text_chars += b.plain_text().chars().count();
        match b {
            Block::Paragraph { .. } => c.paragraphs += 1,
            Block::Heading { .. } => c.headings += 1,
            Block::ListItem { .. } => c.list_items += 1,
            Block::CheckItem { .. } => c.check_items += 1,
            Block::Quote { .. } => c.quotes += 1,
            Block::Code { .. } => c.code_blocks += 1,
            Block::Table { rows, .. } => {
                c.tables += 1;
                for r in rows {
                    for cell in &r.cells {
                        count_blocks(&cell.blocks, c);
                    }
                }
            }
            Block::Image { .. } => c.images += 1,
            Block::Embed { .. } => {}
            Block::File { .. } => c.attachments += 1,
        }
    }
}

fn first_text(items: &[Item]) -> Option<String> {
    items
        .iter()
        .flat_map(|i| i.blocks.iter())
        .map(|b| b.plain_text().trim())
        .find(|t| !t.is_empty())
        .map(|t| {
            let line = t.lines().next().unwrap_or(t);
            line.chars().take(80).collect()
        })
}

// ---- pages ----------------------------------------------------------------------

fn convert_page(
    nb: &mut NotebookCtx,
    page: &OnePage,
    order: usize,
    section_path: &[String],
    section_ids: Option<&mut SectionIds>,
) -> Page {
    let id = page.link_target_id().to_string();
    let raw_title = page
        .title_text()
        .map(|t| {
            t.split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .map(richtext::clean_char)
                .collect::<String>()
        })
        .filter(|t| !t.is_empty());
    let mut ctx = PageCtx {
        nb,
        page_id: id.clone(),
        page_title: raw_title.clone().unwrap_or_default(),
        section_path: section_path.to_vec(),
        source_url: None,
        ids: section_ids.and_then(|s| s.page(&id)),
        ink_skipped: 0,
        equations: 0,
    };

    let mut items: Vec<Item> = Vec::new();
    let contents = page.contents();
    let mut i = 0usize;
    while i < contents.len() {
        let item_id = format!("i{}", items.len());
        match &contents[i] {
            PageContent::Outline(o) => {
                let item = outline_item(&mut ctx, o, item_id);
                // Empty note containers (stray clicks) carry no content.
                if !item.blocks.is_empty() {
                    items.push(item);
                }
            }
            PageContent::Image(img) => {
                let mut blocks = Vec::new();
                ctx.image(img, 0, &mut blocks);
                let w = img
                    .picture_width()
                    .or(img.layout_max_width())
                    .unwrap_or(4.0);
                let h = img
                    .picture_height()
                    .or(img.layout_max_height())
                    .unwrap_or(3.0);
                items.push(Item {
                    id: item_id,
                    kind: "image",
                    x: img.offset_horizontal(),
                    y: img.offset_vertical(),
                    width: img.picture_width().or(img.layout_max_width()).map(round2),
                    width_set_by_user: false,
                    est_width: round2(w),
                    est_height: round2(h),
                    is_background: img.is_background(),
                    source_url: None,
                    blocks,
                });
            }
            PageContent::EmbeddedFile(f) => {
                let b = ctx.file(f, 0);
                items.push(Item {
                    id: item_id,
                    kind: "file",
                    x: f.offset_horizontal(),
                    y: f.offset_vertical(),
                    width: f.layout_max_width().map(round2),
                    width_set_by_user: false,
                    est_width: f.layout_max_width().unwrap_or(2.0),
                    est_height: f.layout_max_height().unwrap_or(1.2),
                    is_background: false,
                    source_url: None,
                    blocks: vec![b],
                });
            }
            PageContent::Ink(_) => ctx.skip_ink(),
            PageContent::Unknown => ctx.warn("unsupported page element skipped".into()),
        }
        i += 1;
    }

    if ctx.ink_skipped > 0 {
        let n = ctx.ink_skipped;
        ctx.warn(format!(
            "{n} ink drawing(s) not imported (Fortress does not support ink)"
        ));
    }
    if ctx.equations > 0 {
        let n = ctx.equations;
        ctx.warn(format!(
            "{n} equation(s) imported as plain linear text (Fortress does not support math)"
        ));
    }
    let page_source_url = ctx.source_url.take();
    drop(ctx);

    // Layout.
    let mut flow_y = 0.0f32;
    let boxes: Vec<LBox> = items
        .iter()
        .map(|it| {
            let y = it.y.unwrap_or(flow_y);
            flow_y = y + it.est_height;
            LBox {
                id: it.id.clone(),
                x: it.x.unwrap_or(0.0),
                y,
                w: it.est_width,
                h: it.est_height,
                background: it.is_background,
            }
        })
        .collect();
    let (rows, scattered) = layout::compute(&boxes);

    let (title, title_derived) = match raw_title {
        Some(t) => (t, false),
        None => (
            first_text(&items).unwrap_or_else(|| "Untitled Page".to_string()),
            true,
        ),
    };
    if scattered > 0 {
        nb.report.layout_notes.push(LayoutNote {
            page_id: id.clone(),
            page_title: title.clone(),
            message: format!(
                "{scattered} band(s) with more than {} side-by-side items (scattered notes)",
                layout::MAX_COLUMNS
            ),
        });
    }
    nb.report.counts.column_layouts += rows
        .iter()
        .filter(|r| matches!(r, LayoutRow::Columns { .. }))
        .count();

    let mut stats_counts = Counts::default();
    for it in &items {
        count_blocks(&it.blocks, &mut stats_counts);
    }

    let mut source_path = section_path.to_vec();
    source_path.push(title.clone());
    Page {
        id: id.clone(),
        link_target_id: id,
        title,
        title_derived,
        level: (page.level() - 1).max(0),
        parent_id: None,
        order,
        created: iso(page.created_time()),
        updated: iso(page.updated_time()),
        author: page.author().map(str::to_string).filter(|a| !a.is_empty()),
        source_path,
        source_url: page_source_url,
        height: page.height().map(round2),
        items,
        layout: rows,
        stats: PageStats {
            blocks: stats_counts.blocks,
            text_chars: stats_counts.text_chars,
        },
    }
}

fn outline_item(ctx: &mut PageCtx, o: &Outline, id: String) -> Item {
    let before = ctx.source_url.take();
    let mut blocks = Vec::new();
    ctx.outline_items(o.items(), 0, &mut blocks);
    merge_code(&mut blocks);
    let outline_source = ctx.source_url.take();
    ctx.source_url = before.or(outline_source.clone());
    if let Some(url) = &outline_source {
        for b in blocks.iter_mut() {
            let c = b.common_mut();
            if c.source_url.is_none() {
                c.source_url = Some(url.clone());
            }
        }
    }
    let declared = o.layout_max_width();
    let max_w = declared.unwrap_or(DEFAULT_OUTLINE_WIDTH);
    let est_w = if o.is_layout_size_set_by_user() {
        max_w
    } else {
        est_content_width(&blocks).min(max_w)
    };
    let est_h = est_blocks(&blocks, est_w).max(LINE_H);
    Item {
        id,
        kind: "outline",
        x: o.offset_horizontal(),
        y: o.offset_vertical(),
        width: declared.map(round2),
        width_set_by_user: o.is_layout_size_set_by_user(),
        est_width: round2(est_w),
        est_height: round2(est_h),
        is_background: false,
        source_url: outline_source,
        blocks,
    }
}

fn convert_section(
    nb: &mut NotebookCtx,
    s: &OneSection,
    parent_path: &[String],
    file_identity: Option<String>,
    rel_file: Option<&str>,
    in_toc: bool,
    mut ids: Option<SectionIds>,
) -> Section {
    let mut path = parent_path.to_vec();
    path.push(s.display_name().to_string());
    let mut pages = Vec::new();
    for series in s.page_series() {
        for p in series.pages() {
            let order = pages.len();
            pages.push(convert_page(nb, p, order, &path, ids.as_mut()));
        }
    }
    // Subpage parents.
    let mut stack: Vec<(i32, String)> = Vec::new();
    for p in pages.iter_mut() {
        while stack.last().map(|(l, _)| *l >= p.level).unwrap_or(false) {
            stack.pop();
        }
        if p.level > 0 {
            p.parent_id = stack.last().map(|(_, id)| id.clone());
        }
        stack.push((p.level, p.id.clone()));
    }
    let warnings: Vec<Warning> = s
        .report()
        .warnings()
        .iter()
        .map(|w| Warning {
            message: w.message().to_string(),
            page_id: w.page().and_then(|(id, _)| normalize_guid(&id.to_string())),
            page_title: w.page().map(|(_, t)| t.to_string()),
        })
        .collect();
    nb.report.warnings.extend(warnings.iter().cloned());
    let c = &mut nb.report.counts;
    c.sections += 1;
    for p in &pages {
        c.pages += 1;
        if p.level > 0 {
            c.subpages += 1;
        }
        for it in &p.items {
            count_blocks(&it.blocks, c);
        }
    }
    let identity = file_identity.or_else(|| Some(s.file_identity().to_string()));
    Section {
        id: identity
            .clone()
            .unwrap_or_else(|| path_id("section", rel_file.unwrap_or(""))),
        file_identity: identity,
        name: s.display_name().to_string(),
        color: color_argb(s.color()),
        path,
        in_toc,
        pages,
        warnings,
    }
}

// ---- failures -------------------------------------------------------------------

/// What the first bytes of a section file say about it.
#[derive(Debug, PartialEq, Eq)]
enum FileSniff {
    /// Zero bytes.
    Empty,
    /// Not a OneNote revision store (another file renamed to `.one`, or
    /// something too short to be one).
    NotOneNote,
    /// A OneNote 2007 section (revision store format version 0x1B), which
    /// the parser does not support.
    OneNote2007,
    /// Looks like a OneNote 2010+ section, or could not be read.
    Other,
}

/// `guidFileType` of a `.one` section ([MS-ONESTORE] 2.3.1), as stored.
const GUID_ONE: [u8; 16] = [
    0xe4, 0x52, 0x5c, 0x7b, 0x8c, 0xd8, 0xa7, 0x4d, 0xae, 0xb1, 0x53, 0x78, 0xd0, 0x29, 0x96, 0xd3,
];
/// `guidFileType` of a `.onetoc2` table of contents.
const GUID_ONETOC2: [u8; 16] = [
    0xa1, 0x2f, 0xff, 0x43, 0xd9, 0xef, 0x76, 0x4c, 0x9e, 0xe2, 0x10, 0xea, 0x57, 0x22, 0x76, 0x5f,
];
/// `guidFileFormat` of a desktop revision store.
const GUID_FILE_FORMAT: [u8; 16] = [
    0x3f, 0xdd, 0x9a, 0x10, 0x1b, 0x91, 0xf5, 0x49, 0xa5, 0xd0, 0x17, 0x91, 0xed, 0xc8, 0xae, 0xd8,
];

fn sniff_file(file: &Path) -> FileSniff {
    use std::io::Read;
    let mut head = [0u8; 0x48];
    let n = match std::fs::File::open(file) {
        Ok(mut f) => {
            let mut n = 0;
            while n < head.len() {
                match f.read(&mut head[n..]) {
                    Ok(0) | Err(_) => break,
                    Ok(k) => n += k,
                }
            }
            n
        }
        Err(_) => return FileSniff::Other,
    };
    sniff_bytes(&head[..n])
}

fn sniff_bytes(head: &[u8]) -> FileSniff {
    if head.is_empty() {
        return FileSniff::Empty;
    }
    if head.len() < 16 || (head[..16] != GUID_ONE && head[..16] != GUID_ONETOC2) {
        return FileSniff::NotOneNote;
    }
    // ffvLastCodeThatWroteToThisFile .. ffvNewestCodeThatHasReadThisFile:
    // 0x2A for OneNote 2010+ sections, 0x1B for OneNote 2007 sections (and
    // for every table of contents).
    if head[..16] == GUID_ONE
        && head.len() >= 0x44
        && head[0x30..0x40] == GUID_FILE_FORMAT
        && head[0x40..0x44] == [0x1b, 0, 0, 0]
    {
        return FileSniff::OneNote2007;
    }
    FileSniff::Other
}

/// Human readable reason and hint for a section that could not be read,
/// from the parser error and the file itself.
pub fn classify_failure(err: &str, file: Option<&Path>) -> (String, String) {
    let sniff = file.map(sniff_file).unwrap_or(FileSniff::Other);
    let e = err.to_ascii_lowercase();
    let r = |reason: &str, hint: &str| (reason.to_string(), hint.to_string());
    if sniff == FileSniff::Empty {
        return r(
            "The section file is empty (it was not downloaded or synced).",
            "Open the notebook in OneNote, let it finish syncing, then export again.",
        );
    }
    if sniff == FileSniff::NotOneNote {
        return r(
            "The file is not a OneNote section (its contents are in another format).",
            "Nothing to do unless this file should contain notes; if it should, export the notebook again.",
        );
    }
    if sniff == FileSniff::OneNote2007 {
        return r(
            "The section is in the OneNote 2007 format, which cannot be read.",
            "Open the notebook in OneNote 2010 or later and convert it (File > Info > Settings > Properties > Convert to 2010), then export again.",
        );
    }
    // onenote_parser (vendor/PATCHES.md, P5) reports encryption explicitly.
    if e.contains("password-protected") {
        return r(
            "The section is password-protected.",
            "In OneNote, unlock the section and remove its password (Review > Password), then export again.",
        );
    }
    if e.contains("encrypt") || e.contains("password") || e.contains("odcs") {
        return r(
            "The section looks password-protected (encrypted) or damaged.",
            "In OneNote, unlock the section and remove its password (Review > Password), then export again.",
        );
    }
    if e.contains("not a section file") || e.contains("notasection") {
        return r(
            "The file is not a OneNote section.",
            "Nothing to do unless this file should contain notes.",
        );
    }
    if e.contains("rights") || e.contains("irm") {
        return r(
            "The section is rights-protected (IRM) and cannot be read.",
            "Remove the protection in OneNote for Windows and export again.",
        );
    }
    if e.contains("unexpected end of file") {
        return r(
            "The section file is incomplete: it ends before all of its data (truncated download or damaged file).",
            "Open the notebook in OneNote, let it finish syncing, then export again; if the section opens in OneNote, move its pages into a new section first.",
        );
    }
    if e.contains("undeclared revision")
        || e.contains("invalid uuid")
        || e.contains("malformed onestore")
    {
        return r(
            "The section is damaged: its internal structure is inconsistent.",
            "Open it in OneNote; if it opens, move its pages into a new section and export again.",
        );
    }
    if e.contains("parser crashed") {
        return r(
            "The section is damaged: its data is inconsistent and could not be read.",
            "Open it in OneNote; if it opens, move its pages into a new section and export again.",
        );
    }
    r(
        "The section could not be read (unsupported or damaged file).",
        "Open it in OneNote; if it opens, move its pages into a new section and export again.",
    )
}

fn failed(
    path: Vec<String>,
    file: Option<String>,
    abs: Option<&Path>,
    err: String,
) -> FailedSection {
    let (reason, hint) = classify_failure(&err, abs);
    FailedSection {
        path,
        file,
        reason,
        hint,
        error: err,
    }
}

// ---- notebooks --------------------------------------------------------------------

/// Where relative paths in the manifest are measured from: a directory on
/// disk, plus a display prefix (the package's own path for sections that
/// were extracted from a `.onepkg`).
#[derive(Clone, Debug)]
pub struct InputRoot {
    pub dir: PathBuf,
    pub prefix: String,
}

impl InputRoot {
    pub fn new(dir: &Path) -> Self {
        Self {
            dir: dir.to_path_buf(),
            prefix: String::new(),
        }
    }

    fn rel(&self, p: &Path) -> String {
        let r = rel_path(p, &self.dir);
        match (self.prefix.is_empty(), r.is_empty()) {
            (true, _) => r,
            (false, true) => self.prefix.clone(),
            (false, false) => format!("{}/{r}", self.prefix),
        }
    }
}

/// Notebook structure from the tables of contents.
enum Skel {
    Group {
        identity: Option<String>,
        name: String,
        dir: PathBuf,
        children: Vec<Skel>,
    },
    Section {
        file: PathBuf,
    },
}

/// A table-of-contents entry that has no file or folder on disk.
struct MissingEntry {
    /// Display path of the entry (notebook, groups…, entry name).
    path: Vec<String>,
    /// Where the file was expected (for the report).
    file: PathBuf,
}

fn rel_path(p: &Path, root: &Path) -> String {
    p.strip_prefix(root)
        .unwrap_or(p)
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("/")
}

/// Run third-party parsing code; a panic (damaged input that the parser
/// does not handle) becomes an error for that one file instead of aborting
/// the whole conversion.
fn guard<T>(what: &str, f: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)).unwrap_or_else(|payload| {
        let msg = payload
            .downcast_ref::<&str>()
            .map(|s| s.to_string())
            .or_else(|| payload.downcast_ref::<String>().cloned())
            .unwrap_or_default();
        Err(format!(
            "the parser crashed while reading this {what}: {msg}"
        ))
    })
}

fn parse_section_file(
    parser: &Parser,
    f: &Path,
) -> Result<(OneSection, Option<SectionIds>), String> {
    let section = guard("section", || {
        parser
            .parse_section(TypedPath::derive(&typed(f)))
            .map_err(|e| e.to_string())
    })?;
    let ids = guard("section", || Ok(SectionIds::from_file(parser, f))).unwrap_or(None);
    Ok((section, ids))
}

/// A section parsed from a file on disk, waiting to be placed in the tree.
struct Parsed {
    dir: PathBuf,
    section: Section,
}

/// The child `name` of `dir`: exact match first, else the only entry whose
/// name matches ignoring case (tables of contents are written on Windows,
/// whose file names are case-insensitive).
fn find_child(dir: &Path, name: &str) -> Option<PathBuf> {
    let exact = dir.join(name);
    if std::fs::symlink_metadata(&exact)
        .map(|m| !m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Some(exact);
    }
    let lower = name.to_lowercase();
    let matches: Vec<PathBuf> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().map(|t| !t.is_symlink()).unwrap_or(false))
        .filter(|e| e.file_name().to_string_lossy().to_lowercase() == lower)
        .map(|e| e.path())
        .collect();
    if matches.len() == 1 {
        matches.into_iter().next()
    } else {
        None
    }
}

/// A TOC entry name must be a single path component: entries are untrusted
/// and must not reach outside the notebook folder.
fn valid_entry_name(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.contains(['/', '\\', ':'])
        && !name.chars().any(char::is_control)
}

fn tocs_in(dir: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| p.is_file() && input::ext_lower(p) == "onetoc2")
                .collect()
        })
        .unwrap_or_default();
    v.sort();
    v
}

/// Read the table of contents of `dir`. OneDrive downloads sometimes
/// contain several `.onetoc2` files in a folder (e.g. an empty `.onetoc2`
/// next to `Open Notebook.onetoc2`); use the one that lists the most
/// entries. `preferred` is tried first.
fn read_best_toc(
    parser: &Parser,
    dir: &Path,
    preferred: Option<&Path>,
) -> Result<onenote_parser::notebook::TableOfContents, String> {
    let mut candidates = tocs_in(dir);
    if let Some(p) = preferred
        && !candidates.iter().any(|c| c == p)
    {
        candidates.insert(0, p.to_path_buf());
    }
    let mut best: Option<onenote_parser::notebook::TableOfContents> = None;
    let mut first_err = None;
    for c in candidates {
        let r = guard("table of contents", || {
            parser
                .parse_toc(TypedPath::derive(&typed(&c)))
                .map_err(|e| e.to_string())
        });
        match r {
            Ok(t) => {
                if best
                    .as_ref()
                    .map(|b| t.entries().len() > b.entries().len())
                    .unwrap_or(true)
                {
                    best = Some(t);
                }
            }
            Err(e) => {
                first_err.get_or_insert(e);
            }
        }
    }
    best.ok_or_else(|| first_err.unwrap_or_else(|| "no table of contents".into()))
}

/// Build the notebook structure from the table of contents of `dir`, and
/// recursively from those of its section groups. Entries without a file on
/// disk are collected in `missing`.
fn toc_skeleton(
    parser: &Parser,
    toc: &onenote_parser::notebook::TableOfContents,
    dir: &Path,
    path: &[String],
    depth: usize,
    missing: &mut Vec<MissingEntry>,
    warnings: &mut Vec<String>,
) -> Vec<Skel> {
    let mut out = Vec::new();
    for name in toc.entries() {
        // OneNote web exports list the table of contents itself.
        if name.eq_ignore_ascii_case(input::RECYCLE_BIN)
            || name.to_ascii_lowercase().ends_with(".onetoc2")
        {
            continue;
        }
        if !valid_entry_name(name) {
            warnings.push(format!(
                "the table of contents lists an invalid file name {name:?}; ignored"
            ));
            continue;
        }
        let mut epath = path.to_vec();
        let Some(p) = find_child(dir, name) else {
            epath.push(name.trim_end_matches(".one").to_string());
            missing.push(MissingEntry {
                path: epath,
                file: dir.join(name),
            });
            continue;
        };
        if p.is_dir() {
            epath.push(name.clone());
            let (identity, children) = match read_best_toc(parser, &p, None) {
                Ok(t) if depth < 64 => (
                    Some(t.file_identity().to_string()),
                    toc_skeleton(parser, &t, &p, &epath, depth + 1, missing, warnings),
                ),
                Ok(t) => (Some(t.file_identity().to_string()), Vec::new()),
                Err(_) => (None, Vec::new()),
            };
            out.push(Skel::Group {
                identity,
                name: name.clone(),
                dir: p,
                children,
            });
        } else if input::ext_lower(&p) == "one" {
            out.push(Skel::Section { file: p });
        }
    }
    out
}

fn missing_section(m: &MissingEntry, root: &InputRoot) -> FailedSection {
    FailedSection {
        path: m.path.clone(),
        file: Some(root.rel(&m.file)),
        reason: "The section is listed in the notebook but its file is missing from the export."
            .into(),
        hint: "Open the notebook in OneNote, let it finish syncing, then export it again.".into(),
        error: format!(
            "table of contents entry {:?} has no file",
            m.file
                .file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default()
        ),
    }
}

pub fn convert_notebook(
    src: &NotebookSource,
    input_root: &InputRoot,
    work: &Path,
    assets: &mut AssetStore,
    log: &mut dyn FnMut(&str),
    extra: &mut Vec<(NotebookSource, InputRoot)>,
) -> Notebook {
    let mut nb = NotebookCtx {
        assets,
        report: NotebookReport::default(),
    };
    let parser = Parser::new();
    match src {
        NotebookSource::Package { path, name, rel } => {
            log(&format!("package {rel}"));
            let rel = input_root.rel(path);
            let empty = |report: NotebookReport| Notebook {
                id: path_id("notebook", &rel),
                file_identity: None,
                name: name.clone(),
                color: None,
                source_kind: "onepkg".into(),
                source_path: rel.clone(),
                children: Vec::new(),
                report: finish_report(report),
            };
            // Extract the package and convert its contents like a folder,
            // so that every section that cannot be read (or is missing) is
            // reported, exactly as for a folder or zip.
            let dest = work.join(".onepkg").join(&path_id("p", &rel)[2..]);
            let _ = std::fs::remove_dir_all(&dest);
            let unpacked = match input::extract_cab(path, &dest) {
                Ok(u) => u,
                Err(e) => {
                    nb.report.failed_sections.push(FailedSection {
                        path: vec![name.clone()],
                        file: Some(rel.clone()),
                        reason: "The notebook package could not be opened (damaged or not a .onepkg file).".into(),
                        hint: "Export the notebook again, or export it from OneNote for the web as a .zip.".into(),
                        error: format!("{e:#}"),
                    });
                    return empty(nb.report);
                }
            };
            let inner_root = InputRoot {
                dir: dest.clone(),
                prefix: rel.clone(),
            };
            let mut rep = input::InputReport::default();
            let found = input::discover(&dest, &mut rep);
            // The package's own notebook: its top-level table of contents,
            // else (packages written without one) all of its sections.
            let main = found
                .iter()
                .position(|s| matches!(s, NotebookSource::Toc { rel, .. } if rel.is_empty()))
                .or_else(|| {
                    found
                        .iter()
                        .position(|s| matches!(s, NotebookSource::Toc { .. }))
                });
            let inner = match main {
                Some(i) => match found[i].clone() {
                    // The package's notebook is named after the package,
                    // not after the extraction folder.
                    NotebookSource::Toc { toc, root, rel, .. } if rel.is_empty() => {
                        NotebookSource::Toc {
                            toc,
                            root,
                            name: name.clone(),
                            rel,
                        }
                    }
                    other => other,
                },
                None => NotebookSource::Loose {
                    dir: dest.clone(),
                    sections: input::list_sections(&dest),
                    name: name.clone(),
                    rel: String::new(),
                },
            };
            let mut out = convert_notebook(&inner, &inner_root, work, nb.assets, log, extra);
            if let Some(i) = main {
                for (j, s) in found.into_iter().enumerate() {
                    if j != i && !matches!(s, NotebookSource::Package { .. }) {
                        extra.push((s, inner_root.clone()));
                    }
                }
            }
            if main.is_none() {
                out.report.warnings.push(Warning {
                    message: "the package has no table of contents (.onetoc2); its sections are listed by file name".into(),
                    page_id: None,
                    page_title: None,
                });
            }
            for (file, error) in unpacked.failed_sections {
                let mut p = vec![name.clone()];
                p.extend(file.trim_end_matches(".one").split('/').map(str::to_string));
                // Replaces "listed but missing" for the same file.
                let fpath = format!("{rel}/{file}");
                out.report
                    .failed_sections
                    .retain(|f| f.file.as_deref() != Some(fpath.as_str()));
                out.report.failed_sections.push(FailedSection {
                    path: p,
                    file: Some(fpath),
                    reason: "The section could not be unpacked from the package (damaged package)."
                        .into(),
                    hint: "Export the notebook again, or export it from OneNote for the web as a .zip."
                        .into(),
                    error,
                });
            }
            if out.children.is_empty() && out.report.failed_sections.is_empty() {
                out.report.failed_sections.push(FailedSection {
                    path: vec![name.clone()],
                    file: Some(rel.clone()),
                    reason: "The package contains no readable sections.".into(),
                    hint: "Export the notebook again, or export it from OneNote for the web as a .zip."
                        .into(),
                    error: "no section files in the package".into(),
                });
            }
            for w in unpacked.warnings {
                out.report.warnings.push(Warning {
                    message: w,
                    page_id: None,
                    page_title: None,
                });
            }
            out.report.failed_sections.extend(nb.report.failed_sections);
            out.report = finish_report(out.report);
            if out.file_identity.is_none() {
                out.id = path_id("notebook", &rel);
            }
            out.name = name.clone();
            out.source_kind = "onepkg".into();
            out.source_path = rel;
            out
        }
        NotebookSource::Loose {
            dir,
            sections,
            name,
            rel,
        } => {
            log(&format!(
                "sections in {}",
                if rel.is_empty() { "." } else { rel }
            ));
            let base = vec![name.clone()];
            let mut children = Vec::new();
            for f in sections {
                let frel = input_root.rel(f);
                match parse_section_file(&parser, f) {
                    Ok((s, ids)) => children.push(Entry::Section(convert_section(
                        &mut nb,
                        &s,
                        &base,
                        None,
                        Some(&frel),
                        false,
                        ids,
                    ))),
                    Err(e) => nb.report.failed_sections.push(failed(
                        {
                            let mut p = base.clone();
                            p.push(file_stem(f));
                            p
                        },
                        Some(frel),
                        Some(f),
                        e,
                    )),
                }
            }
            let _ = dir;
            Notebook {
                id: loose_id(&children, rel),
                file_identity: None,
                name: name.clone(),
                color: None,
                source_kind: "loose_sections".into(),
                source_path: rel.clone(),
                children,
                report: finish_report(nb.report),
            }
        }
        NotebookSource::Toc {
            toc,
            root,
            name,
            rel,
        } => {
            log(&format!(
                "notebook {}",
                if rel.is_empty() { name } else { rel }
            ));
            let base = vec![name.clone()];
            // 1. Structure from the tables of contents.
            let mut missing = Vec::new();
            let mut toc_warnings = Vec::new();
            let (skel, identity, color) = match read_best_toc(&parser, root, Some(toc)) {
                Ok(t) => {
                    let s =
                        toc_skeleton(&parser, &t, root, &base, 0, &mut missing, &mut toc_warnings);
                    (Some(s), Some(t.file_identity().to_string()), t.color())
                }
                Err(e) => {
                    toc_warnings.push(format!(
                        "table of contents could not be read ({e}); sections are listed by folder"
                    ));
                    (None, None, None)
                }
            };
            for message in toc_warnings {
                nb.report.warnings.push(Warning {
                    message,
                    page_id: None,
                    page_title: None,
                });
            }
            for m in &missing {
                nb.report
                    .failed_sections
                    .push(missing_section(m, input_root));
            }
            // Sub-folders with their own TOC that the notebook does not list
            // as section groups are separate notebooks (e.g. a notebook
            // folder that also holds other notebooks).
            let mut foreign: Vec<PathBuf> = Vec::new();
            if let Some(s) = skel.as_ref().filter(|s| !s.is_empty()) {
                let mut group_dirs = Vec::new();
                skel_group_dirs(s, &mut group_dirs);
                input::walk(root, &mut |p, is_dir| {
                    if !is_dir
                        || foreign.iter().any(|f| p.starts_with(f))
                        || group_dirs.iter().any(|g| g == p)
                    {
                        return;
                    }
                    if !tocs_in(p).is_empty() {
                        foreign.push(p.to_path_buf());
                    }
                });
            }
            for f in &foreign {
                let toc = tocs_in(f)
                    .into_iter()
                    .next()
                    .unwrap_or_else(|| f.join(".onetoc2"));
                extra.push((
                    NotebookSource::Toc {
                        toc,
                        root: f.clone(),
                        name: f
                            .file_name()
                            .map(|s| s.to_string_lossy().into_owned())
                            .unwrap_or_default(),
                        rel: input_root.rel(f),
                    },
                    input_root.clone(),
                ));
            }
            // 2. Parse every section file on disk.
            let mut parsed: HashMap<PathBuf, Parsed> = HashMap::new();
            for f in input::list_sections(root)
                .into_iter()
                .filter(|f| !foreign.iter().any(|d| f.starts_with(d)))
            {
                let frel = input_root.rel(&f);
                let dir = f.parent().unwrap_or(root).to_path_buf();
                let mut parent = base.clone();
                parent.extend(
                    dir.strip_prefix(root)
                        .unwrap_or(Path::new(""))
                        .components()
                        .map(|c| c.as_os_str().to_string_lossy().into_owned()),
                );
                match parse_section_file(&parser, &f) {
                    Ok((s, ids)) => {
                        let ident = s.file_identity().to_string();
                        let sec = convert_section(
                            &mut nb,
                            &s,
                            &parent,
                            Some(ident),
                            Some(&frel),
                            true,
                            ids,
                        );
                        parsed.insert(f, Parsed { dir, section: sec });
                    }
                    Err(e) => {
                        let mut p = parent.clone();
                        p.push(file_stem(&f));
                        nb.report
                            .failed_sections
                            .push(failed(p, Some(frel), Some(&f), e));
                    }
                }
            }
            // 3. Assemble: listed entries in TOC order, then the rest by folder.
            let mut children = match &skel {
                Some(s) => place(s, &mut parsed, &base),
                None => Vec::new(),
            };
            let mut leftovers: Vec<Parsed> = parsed.into_values().collect();
            leftovers.sort_by(|a, b| a.section.path.cmp(&b.section.path));
            for mut p in leftovers {
                p.section.in_toc = false;
                insert_by_dir(&mut children, p, root, &base);
            }
            let nb_ident = identity;
            Notebook {
                id: nb_ident.clone().unwrap_or_else(|| path_id("notebook", rel)),
                file_identity: nb_ident,
                name: name.clone(),
                color: color_argb(color),
                source_kind: "onetoc2".into(),
                source_path: rel.clone(),
                children,
                report: finish_report(nb.report),
            }
        }
    }
}

fn skel_group_dirs(skel: &[Skel], out: &mut Vec<PathBuf>) {
    for s in skel {
        if let Skel::Group { dir, children, .. } = s {
            skel_group_dirs(children, out);
            out.push(dir.clone());
        }
    }
}

/// Id for a set of loose sections: derived from the sections' file
/// identities (stable across re-exports and moves), else from the path.
fn loose_id(children: &[Entry], rel: &str) -> String {
    let mut idents: Vec<&str> = children
        .iter()
        .filter_map(|e| match e {
            Entry::Section(s) => s.file_identity.as_deref(),
            Entry::SectionGroup(_) => None,
        })
        .collect();
    idents.sort();
    if idents.is_empty() {
        path_id("notebook", rel)
    } else {
        path_id("loose", &idents.join(","))
    }
}

fn file_stem(p: &Path) -> String {
    p.file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn place(skel: &[Skel], parsed: &mut HashMap<PathBuf, Parsed>, parent: &[String]) -> Vec<Entry> {
    let mut out = Vec::new();
    for s in skel {
        match s {
            Skel::Section { file } => {
                if let Some(p) = parsed.remove(file) {
                    out.push(Entry::Section(p.section));
                }
            }
            Skel::Group {
                identity,
                name,
                children,
                ..
            } => {
                let mut path = parent.to_vec();
                path.push(name.clone());
                out.push(Entry::SectionGroup(SectionGroup {
                    id: identity
                        .clone()
                        .unwrap_or_else(|| path_id("group", &path.join("/"))),
                    file_identity: identity.clone(),
                    name: name.clone(),
                    children: place(children, parsed, &path),
                    path,
                }));
            }
        }
    }
    out
}

/// Insert a section that the TOC did not list into the group matching its
/// directory, creating (synthetic) groups as needed.
fn insert_by_dir(children: &mut Vec<Entry>, p: Parsed, root: &Path, base: &[String]) {
    let rel: Vec<String> = p
        .dir
        .strip_prefix(root)
        .unwrap_or(Path::new(""))
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    let mut level = children;
    let mut path = base.to_vec();
    for name in rel {
        path.push(name.clone());
        let pos = level
            .iter()
            .position(|e| matches!(e, Entry::SectionGroup(g) if g.name == name));
        let idx = match pos {
            Some(i) => i,
            None => {
                level.push(Entry::SectionGroup(SectionGroup {
                    id: path_id("group", &path.join("/")),
                    file_identity: None,
                    name: name.clone(),
                    path: path.clone(),
                    children: Vec::new(),
                }));
                level.len() - 1
            }
        };
        level = match &mut level[idx] {
            Entry::SectionGroup(g) => &mut g.children,
            Entry::Section(_) => unreachable!(),
        };
    }
    level.push(Entry::Section(p.section));
}

fn count_groups(entries: &[Entry]) -> usize {
    entries
        .iter()
        .map(|e| match e {
            Entry::SectionGroup(g) => 1 + count_groups(&g.children),
            Entry::Section(_) => 0,
        })
        .sum()
}

fn finish_report(mut r: NotebookReport) -> NotebookReport {
    r.counts.sections_failed = r.failed_sections.len();
    r
}

/// Make every `id` unique within the manifest. Copies of the same notebook,
/// section or page (same `file_identity` / `link_target_id`) get a
/// path-derived suffix; the original identity fields are left untouched.
pub fn dedupe_ids(nbs: &mut [Notebook]) {
    use std::collections::HashSet;
    fn uniq(seen: &mut HashSet<String>, id: &mut String, salt: &str) {
        if seen.insert(id.clone()) {
            return;
        }
        let h = hex::encode(Sha256::digest(salt.as_bytes()));
        let mut n = 8;
        loop {
            let cand = format!("{id}~{}", &h[..n.min(h.len())]);
            if seen.insert(cand.clone()) {
                *id = cand;
                return;
            }
            n += 4;
            if n > h.len() {
                let cand = format!("{id}~{}", seen.len());
                seen.insert(cand.clone());
                *id = cand;
                return;
            }
        }
    }
    fn entries(
        seen: &mut HashSet<String>,
        pages: &mut HashSet<String>,
        es: &mut [Entry],
        salt: &str,
    ) {
        for (i, e) in es.iter_mut().enumerate() {
            match e {
                Entry::SectionGroup(g) => {
                    let s = format!("{salt}/{i}:{}", g.path.join("/"));
                    uniq(seen, &mut g.id, &s);
                    entries(seen, pages, &mut g.children, &s);
                }
                Entry::Section(sec) => {
                    let s = format!("{salt}/{i}:{}", sec.path.join("/"));
                    uniq(seen, &mut sec.id, &s);
                    let mut remap: Vec<(String, String)> = Vec::new();
                    for p in sec.pages.iter_mut() {
                        let old = p.id.clone();
                        uniq(pages, &mut p.id, &format!("{s}#{}", p.order));
                        if old != p.id {
                            remap.push((old, p.id.clone()));
                        }
                    }
                    for p in sec.pages.iter_mut() {
                        if let Some(pid) = &p.parent_id
                            && let Some((_, new)) = remap.iter().find(|(o, _)| o == pid)
                        {
                            p.parent_id = Some(new.clone());
                        }
                    }
                }
            }
        }
    }
    let mut seen = HashSet::new();
    let mut pages = HashSet::new();
    for (i, nb) in nbs.iter_mut().enumerate() {
        let salt = format!("{i}:{}", nb.source_path);
        uniq(&mut seen, &mut nb.id, &salt);
        entries(&mut seen, &mut pages, &mut nb.children, &salt);
    }
}

/// Whether a paragraph becomes an equation code block. An equation alone in a list item
/// or to-do stays a list item (with its LaTeX as inline code), so the bullet / box survives.
fn is_display_equation(all_math: bool, in_list: bool, checkable: bool) -> bool {
    all_math && !in_list && !checkable
}

/// Fill in counts that depend on the assembled tree.
pub fn finalize(nb: &mut Notebook) {
    nb.report.counts.section_groups = count_groups(&nb.children);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snake_case_shapes() {
        assert_eq!(snake("GreenCheckBox"), "green_check_box");
        assert_eq!(snake("BlueCheckBox1"), "blue_check_box_1");
        assert_eq!(snake("NoIcon"), "no_icon");
    }

    #[test]
    fn sniffs_onenote_2007_and_foreign_files() {
        let mut h = GUID_ONE.to_vec();
        h.extend([0u8; 0x20]);
        h.extend(GUID_FILE_FORMAT);
        h.extend([0x1b, 0, 0, 0]);
        assert_eq!(sniff_bytes(&h), FileSniff::OneNote2007);
        h[0x40] = 0x2a;
        assert_eq!(sniff_bytes(&h), FileSniff::Other);
        // Tables of contents always carry 0x1B.
        h[..16].copy_from_slice(&GUID_ONETOC2);
        h[0x40] = 0x1b;
        assert_eq!(sniff_bytes(&h), FileSniff::Other);
        assert_eq!(sniff_bytes(b"ABABABPADDING!"), FileSniff::NotOneNote);
        assert_eq!(
            sniff_bytes(b"<html><body>not a section</body>"),
            FileSniff::NotOneNote
        );
        assert_eq!(sniff_bytes(b""), FileSniff::Empty);
    }

    #[test]
    fn number_patterns_with_text_around_the_number() {
        let p = |s: &str| number_pattern(&s.chars().collect::<Vec<_>>());
        assert_eq!(p("\u{FFFD}\u{0}."), Some(("decimal", "%.".into())));
        assert_eq!(p("(\u{FFFD}\u{2})"), Some(("lower_roman", "(%)".into())));
        assert_eq!(
            p("Step \u{FFFD}\u{3}:"),
            Some(("upper_alpha", "Step %:".into()))
        );
        assert_eq!(p("[\u{FFFD}\u{0}]"), Some(("decimal", "[%]".into())));
        // Chinese counting (NFC 39) falls back to decimal; the code char is
        // never part of the marker.
        assert_eq!(p("§\u{FFFD}'"), Some(("decimal", "§%".into())));
        assert_eq!(p("-\u{FFFD}#-"), Some(("decimal", "-%-".into())));
        assert_eq!(
            p("\u{FFFD}\u{0}.\u{FFFD}!"),
            Some(("decimal", "%.%".into()))
        );
        assert_eq!(p("\u{FFFD}"), Some(("decimal", "%".into())));
        assert_eq!(p("•"), None);
        assert_eq!(p(""), None);
    }

    #[test]
    fn failure_classification() {
        let (r, _) = classify_failure("object may be encrypted or corrupt ('odcs' is 0x2)", None);
        assert!(r.contains("password"));
        let (r, _) = classify_failure("weird", None);
        assert!(r.contains("could not be read"));
        let (r, h) = classify_failure("The section is password-protected (encrypted)", None);
        assert_eq!(r, "The section is password-protected.");
        assert!(h.contains("remove its password"));
        let (r, _) = classify_failure("Unexpected end of file", None);
        assert!(r.contains("incomplete"));
    }

    #[test]
    fn equations_in_lists_stay_list_items() {
        assert!(is_display_equation(true, false, false));
        assert!(!is_display_equation(true, true, false));
        assert!(!is_display_equation(true, false, true));
        assert!(!is_display_equation(false, false, false));
    }

    #[test]
    fn code_paragraphs_merge() {
        let code = |t: &str, indent| Block::Code {
            common: BlockCommon {
                indent,
                ..Default::default()
            },
            text: t.into(),
            language: None,
        };
        let mut blocks = vec![code("a", 0), code("b", 0), code("c", 1)];
        merge_code(&mut blocks);
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].plain_text(), "a\nb");
        // An equation (language `text`) is never merged into the code around it.
        let equation = Block::Code {
            common: BlockCommon::default(),
            text: "x^2".into(),
            language: Some("text".into()),
        };
        let mut blocks = vec![code("a", 0), equation, code("b", 0)];
        merge_code(&mut blocks);
        assert_eq!(blocks.len(), 3);
    }
}
