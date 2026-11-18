//! Parser objects -> manifest model.

use crate::assets::{self, AssetStore};
use crate::ink;
use crate::input::{self, NotebookSource};
use crate::layout::{self, LBox};
use crate::links::normalize_guid;
use crate::manifest::*;
use crate::richtext::{self, color_hex};
use onenote_parser::Parser;
use onenote_parser::contents::{
    Content, EmbeddedFile, EmbeddedObject, FileDataStatus, Image, Ink, List, NoteTag, Outline,
    OutlineElement, OutlineItem, RichText, Table,
};
use onenote_parser::page::{Page as OnePage, PageContent};
use onenote_parser::property::common::Color;
use onenote_parser::property::embedded_file::FileType;
use onenote_parser::property::rich_text::ParagraphAlignment;
use onenote_parser::section::{Section as OneSection, SectionEntry};
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
}

impl PageCtx<'_, '_> {
    fn warn(&mut self, message: String) {
        self.nb.report.warnings.push(Warning {
            message,
            page_id: Some(self.page_id.clone()),
            page_title: Some(self.page_title.clone()),
        });
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
                Tag {
                    label,
                    shape,
                    checkable,
                    completed: nt.item_status().completed(),
                    completed_at: nt
                        .completed_at()
                        .map(time::UtcDateTime::from)
                        .filter(|t| nt.item_status().completed() && t.year() > 1980)
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
                Content::Ink(i) => {
                    if let Some(b) = self.ink_block(&[(i, (0.0, 0.0))], indent) {
                        out.push(b);
                    }
                }
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

        // Handwriting embedded in the paragraph.
        if !rt.embedded_objects().is_empty() {
            let parts = inline_ink_parts(rt.embedded_objects());
            let refs: Vec<(&Ink, (f32, f32))> = parts.iter().map(|(i, o)| (*i, *o)).collect();
            if let Some(mut b) = self.ink_block(&refs, indent) {
                b.common_mut().tags = tags.clone();
                out.push(b);
            }
            if rt.text().trim().is_empty() {
                return;
            }
        }

        let style_id = rt
            .paragraph_style()
            .style_id()
            .or_else(|| rt.text_run_formatting().iter().find_map(|s| s.style_id()))
            .map(|s| s.to_ascii_lowercase());
        let heading_level = style_id.as_deref().and_then(|s| {
            let b = s.as_bytes();
            (b.len() == 2 && b[0] == b'h' && (b'1'..=b'6').contains(&b[1]))
                .then(|| b[1] - b'0')
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
            c.math += conv.math_count;
        }
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
        };
        let list_info = list.map(list_info);
        let check = common.tags.iter().find(|t| t.checkable).cloned();

        if conv.all_math
            && let Some((text, latex, mathml)) = conv.math
        {
            out.push(Block::Math {
                common,
                text,
                latex,
                mathml,
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
                ..Default::default()
            },
            row_count: t.rows(),
            col_count: t.cols(),
            column_widths: t.col_widths().iter().map(|w| round2(*w)).collect(),
            borders_visible: t.borders_visible(),
            rows,
        }
    }

    fn image(&mut self, img: &Image, indent: u32, out: &mut Vec<Block>) {
        let tags = self.tags(img.note_tags());
        let status = img.data_status();
        let original = img.image_filename().map(assets::display_name);
        let mut asset = None;
        let mut original_asset = None;
        if let Some(mut r) = img.read() {
            let mut hint = img.extension().and_then(assets::sanitize_ext);
            // Printout pages of XPS documents are stored as XPS (a zip).
            if img.displayed_page_number().is_some() {
                hint = Some("xps".into());
            }
            match self.nb.assets.put_reader(&mut r, hint.as_deref(), false) {
                Ok(a) => asset = a,
                Err(e) => self.warn(format!("image could not be written: {e:#}")),
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
            if let Ok(Some(a)) = self.nb.assets.put_reader(&mut p.read(), hint.as_deref(), false) {
                original_asset = asset.take();
                asset = Some(a);
            }
        }
        if asset.is_none() {
            self.missing("image", original.clone(), status_str(status));
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
            ..Default::default()
        };
        if !embeds.is_empty() {
            for url in embeds {
                self.nb.report.counts.embeds += 1;
                out.push(Block::Embed {
                    common: common.clone(),
                    url,
                    thumbnail_asset: asset.clone(),
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
            data_status: status_str(status).into(),
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
        let filename = assets::display_name(f.filename());
        let ext = assets::ext_of_name(f.filename());
        let status = f.data_status();
        let mut asset = None;
        if status == FileDataStatus::Available {
            match self
                .nb
                .assets
                .put_reader(&mut f.read(), Some(ext.as_deref().unwrap_or("bin")), true)
            {
                Ok(a) => asset = a,
                Err(e) => self.warn(format!("attachment {filename:?} could not be written: {e:#}")),
            }
        }
        if asset.is_none() {
            self.nb.report.counts.attachments_missing += 1;
            let st = if status == FileDataStatus::Available {
                "empty"
            } else {
                status_str(status)
            };
            self.missing("attachment", Some(filename.clone()), st);
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
            .unwrap_or_else(|| {
                assets::mime_for_ext(ext.as_deref().unwrap_or("")).to_string()
            });
        Block::File {
            common: BlockCommon {
                indent,
                tags,
                ..Default::default()
            },
            asset,
            filename,
            extension: ext,
            mime,
            size: f.size(),
            data_status: status_str(status).into(),
            media_kind: media_kind.into(),
            icon_asset,
        }
    }

    fn ink_block(&mut self, parts: &[(&Ink, (f32, f32))], indent: u32) -> Option<Block> {
        let r = ink::render(parts)?;
        let asset = match self.nb.assets.put_bytes(r.svg.as_bytes(), Some("svg")) {
            Ok(a) => a,
            Err(e) => {
                self.warn(format!("ink could not be written: {e:#}"));
                None
            }
        };
        Some(Block::Ink {
            common: BlockCommon {
                indent,
                ..Default::default()
            },
            asset,
            recognized_text: r.recognized_text,
            width: round2(r.width),
            height: round2(r.height),
            stroke_count: r.stroke_count,
        })
    }
}

/// Lay out handwriting containers of one paragraph left to right.
fn inline_ink_parts(objects: &[EmbeddedObject]) -> Vec<(&Ink, (f32, f32))> {
    let gap = ink::INK_UNITS_PER_PX * 6.0;
    let mut cursor = (0.0f32, 0.0f32);
    let mut line_h: f32 = 0.0;
    let mut out = Vec::new();
    for o in objects {
        match o {
            EmbeddedObject::Ink(c) => {
                let i = c.ink();
                if let Some((minx, miny, maxx, maxy)) = ink::bounds(i) {
                    out.push((i, (cursor.0 - minx, cursor.1 - miny)));
                    cursor.0 += (maxx - minx) + gap;
                    line_h = line_h.max(maxy - miny);
                }
            }
            EmbeddedObject::InkSpace(_) => cursor.0 += gap,
            EmbeddedObject::InkLineBreak => {
                cursor = (0.0, cursor.1 + line_h + gap);
                line_h = 0.0;
            }
        }
    }
    out
}

fn round2(v: f32) -> f32 {
    (v * 100.0).round() / 100.0
}

fn list_info(l: &List) -> ListInfo {
    let fmt = l.list_format();
    let font = l.list_font().or(l.font()).unwrap_or("");
    if fmt.first() == Some(&'\u{FFFD}') {
        let style = match fmt.get(1).copied().unwrap_or('\0') {
            '\u{1}' => "upper_roman",
            '\u{2}' => "lower_roman",
            '\u{3}' => "upper_alpha",
            '\u{4}' => "lower_alpha",
            _ => "decimal",
        };
        let suffix: String = fmt.iter().skip(2).filter(|c| !c.is_control()).collect();
        ListInfo {
            ordered: true,
            style: style.into(),
            marker: format!("%{suffix}"),
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
            (f, Some(c)) if f.starts_with("Wingdings") || (c as u32) >= 0xF000 => {
                "•".to_string()
            }
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

/// Merge consecutive code paragraphs at the same indent into one block.
fn merge_code(blocks: &mut Vec<Block>) {
    let mut out: Vec<Block> = Vec::with_capacity(blocks.len());
    for b in blocks.drain(..) {
        if let Block::Code { common, text } = &b
            && common.tags.is_empty()
            && let Some(Block::Code {
                common: pc,
                text: pt,
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
        Block::Ink { height, .. } => *height + PARA_GAP,
        Block::Math { .. } => LINE_H * 2.0,
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
                    Block::Ink { width, .. } => *width,
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
        | Block::Math { common, .. }
        | Block::Table { common, .. }
        | Block::Image { common, .. }
        | Block::Embed { common, .. }
        | Block::File { common, .. }
        | Block::Ink { common, .. } => common.indent,
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
            Block::Math { .. } => {}
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
            Block::Ink { .. } => c.ink += 1,
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
) -> Page {
    let id = page.link_target_id().to_string();
    let raw_title = page
        .title_text()
        .map(|t| t.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|t| !t.is_empty());
    let mut ctx = PageCtx {
        nb,
        page_id: id.clone(),
        page_title: raw_title.clone().unwrap_or_default(),
        section_path: section_path.to_vec(),
        source_url: None,
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
            PageContent::Ink(_) => {
                // Merge consecutive page-level ink objects into one drawing.
                let start = i;
                while i + 1 < contents.len() && matches!(contents[i + 1], PageContent::Ink(_)) {
                    i += 1;
                }
                let inks: Vec<&Ink> = contents[start..=i]
                    .iter()
                    .filter_map(|c| c.ink())
                    .collect();
                let unit = 48.0 * ink::INK_UNITS_PER_PX;
                let parts: Vec<(&Ink, (f32, f32))> = inks
                    .iter()
                    .map(|k| {
                        (
                            *k,
                            (
                                k.offset_horizontal().unwrap_or(0.0) * unit,
                                k.offset_vertical().unwrap_or(0.0) * unit,
                            ),
                        )
                    })
                    .collect();
                let origin = parts
                    .iter()
                    .filter_map(|(k, (dx, dy))| {
                        ink::bounds(k).map(|(x, y, _, _)| ((x + dx) / unit, (y + dy) / unit))
                    })
                    .fold((f32::MAX, f32::MAX), |a, b| (a.0.min(b.0), a.1.min(b.1)));
                if let Some(b) = ctx.ink_block(&parts, 0) {
                    let (w, h) = match &b {
                        Block::Ink { width, height, .. } => (*width, *height),
                        _ => (1.0, 1.0),
                    };
                    let has_pos = origin.0 < f32::MAX;
                    items.push(Item {
                        id: item_id,
                        kind: "ink",
                        x: has_pos.then(|| round2(origin.0.max(0.0))),
                        y: has_pos.then(|| round2(origin.1.max(0.0))),
                        width: Some(w),
                        width_set_by_user: false,
                        est_width: w,
                        est_height: h,
                        is_background: false,
                        source_url: None,
                        blocks: vec![b],
                    });
                }
            }
            PageContent::Unknown => ctx.warn("unsupported page element skipped".into()),
        }
        i += 1;
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
        ink_text: page
            .ink_recognition()
            .map(|r| r.text())
            .filter(|t| !t.trim().is_empty()),
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
) -> Section {
    let mut path = parent_path.to_vec();
    path.push(s.display_name().to_string());
    let mut pages = Vec::new();
    for series in s.page_series() {
        for p in series.pages() {
            let order = pages.len();
            pages.push(convert_page(nb, p, order, &path));
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

pub fn classify_failure(err: &str, file: Option<&Path>) -> (String, String) {
    let size = file.and_then(|f| std::fs::metadata(f).ok()).map(|m| m.len());
    let e = err.to_ascii_lowercase();
    if size == Some(0) {
        return (
            "The section file is empty (it was not downloaded or synced).".into(),
            "Open the notebook in OneNote, let it finish syncing, then export again.".into(),
        );
    }
    if e.contains("encrypt") || e.contains("password") || e.contains("odcs") {
        return (
            "The section looks password-protected (encrypted) or damaged.".into(),
            "In OneNote, unlock the section and remove its password (Review > Password), then export again.".into(),
        );
    }
    if e.contains("not a section file") || e.contains("notasection") {
        return (
            "The file is not a OneNote section.".into(),
            "Nothing to do unless this file should contain notes.".into(),
        );
    }
    if e.contains("rights") || e.contains("irm") {
        return (
            "The section is rights-protected (IRM) and cannot be read.".into(),
            "Remove the protection in OneNote for Windows and export again.".into(),
        );
    }
    (
        "The section could not be read (unsupported or damaged file).".into(),
        "Open it in OneNote; if it opens, move its pages into a new section and export again."
            .into(),
    )
}

fn failed(path: Vec<String>, file: Option<String>, abs: Option<&Path>, err: String) -> FailedSection {
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

enum Skel {
    Group {
        identity: String,
        name: String,
        children: Vec<Skel>,
    },
    Section {
        identity: String,
    },
}

fn skeleton(entries: &[SectionEntry]) -> Vec<Skel> {
    entries
        .iter()
        .map(|e| match e {
            SectionEntry::Section(s) => Skel::Section {
                identity: s.file_identity().to_string(),
            },
            SectionEntry::SectionGroup(g) => Skel::Group {
                identity: g.file_identity().to_string(),
                name: g.display_name().to_string(),
                children: skeleton(g.entries()),
            },
        })
        .collect()
}

fn rel_path(p: &Path, root: &Path) -> String {
    p.strip_prefix(root)
        .unwrap_or(p)
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("/")
}

/// A section parsed from a file on disk, waiting to be placed in the tree.
struct Parsed {
    dir: PathBuf,
    section: Section,
}

pub fn convert_notebook(
    src: &NotebookSource,
    input_root: &Path,
    assets: &mut AssetStore,
    log: &mut dyn FnMut(&str),
    extra: &mut Vec<NotebookSource>,
) -> Notebook {
    let mut nb = NotebookCtx {
        assets,
        report: NotebookReport::default(),
    };
    let parser = Parser::new();
    match src {
        NotebookSource::Package { path, name, rel } => {
            log(&format!("package {rel}"));
            let mut out = Notebook {
                id: path_id("notebook", rel),
                file_identity: None,
                name: name.clone(),
                color: None,
                source_kind: "onepkg".into(),
                source_path: rel.clone(),
                children: Vec::new(),
                report: NotebookReport::default(),
            };
            match parser.parse_package(TypedPath::derive(&typed(path))) {
                Ok(n) => {
                    let ident = n.file_identity().to_string();
                    out.id = ident.clone();
                    out.file_identity = Some(ident);
                    out.color = color_argb(n.color());
                    let base = vec![name.clone()];
                    out.children = package_entries(&mut nb, n.entries(), &base);
                    absorb_notebook_warnings(&mut nb, n.report().warnings(), &base, None, rel);
                }
                Err(e) => nb.report.failed_sections.push(failed(
                    vec![name.clone()],
                    Some(rel.clone()),
                    Some(path),
                    format!("{e}"),
                )),
            }
            out.report = finish_report(nb.report);
            out
        }
        NotebookSource::Loose {
            dir,
            sections,
            name,
            rel,
        } => {
            log(&format!("sections in {}", if rel.is_empty() { "." } else { rel }));
            let base = vec![name.clone()];
            let mut children = Vec::new();
            for f in sections {
                let frel = rel_path(f, input_root);
                match parser.parse_section(TypedPath::derive(&typed(f))) {
                    Ok(s) => children.push(Entry::Section(convert_section(
                        &mut nb,
                        &s,
                        &base,
                        None,
                        Some(&frel),
                        false,
                    ))),
                    Err(e) => nb.report.failed_sections.push(failed(
                        {
                            let mut p = base.clone();
                            p.push(file_stem(f));
                            p
                        },
                        Some(frel),
                        Some(f),
                        format!("{e}"),
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
            log(&format!("notebook {}", if rel.is_empty() { name } else { rel }));
            let base = vec![name.clone()];
            // 1. Structure from the table of contents.
            let (skel, identity, color) =
                match parse_best_toc(&parser, toc, root) {
                    Ok(n) => {
                        let s = skeleton(n.entries());
                        absorb_notebook_warnings(
                            &mut nb,
                            n.report().warnings(),
                            &base,
                            Some(root),
                            rel,
                        );
                        (Some(s), Some(n.file_identity().to_string()), n.color())
                    }
                    Err(e) => {
                        nb.report.warnings.push(Warning {
                            message: format!(
                                "table of contents could not be read ({e}); sections are listed by folder"
                            ),
                            page_id: None,
                            page_title: None,
                        });
                        (None, None, None)
                    }
                };
            // Sub-folders with their own TOC that the notebook does not list
            // as section groups are separate notebooks (e.g. a notebook
            // folder that also holds other notebooks).
            let mut foreign: Vec<PathBuf> = Vec::new();
            if let Some(s) = skel.as_ref().filter(|s| !s.is_empty()) {
                let mut group_dirs = Vec::new();
                skel_group_dirs(s, root, &mut group_dirs);
                input::walk(root, &mut |p, is_dir| {
                    if !is_dir
                        || foreign.iter().any(|f| p.starts_with(f))
                        || group_dirs.iter().any(|g| g == p)
                    {
                        return;
                    }
                    let has_toc = std::fs::read_dir(p)
                        .map(|rd| {
                            rd.filter_map(|e| e.ok())
                                .any(|e| input::ext_lower(&e.path()) == "onetoc2")
                        })
                        .unwrap_or(false);
                    if has_toc {
                        foreign.push(p.to_path_buf());
                    }
                });
            }
            for f in &foreign {
                let toc = std::fs::read_dir(f)
                    .ok()
                    .and_then(|rd| {
                        let mut v: Vec<PathBuf> = rd
                            .filter_map(|e| e.ok())
                            .map(|e| e.path())
                            .filter(|p| input::ext_lower(p) == "onetoc2")
                            .collect();
                        v.sort();
                        v.into_iter().next()
                    })
                    .unwrap_or_else(|| f.join(".onetoc2"));
                extra.push(NotebookSource::Toc {
                    toc,
                    root: f.clone(),
                    name: f
                        .file_name()
                        .map(|s| s.to_string_lossy().into_owned())
                        .unwrap_or_default(),
                    rel: rel_path(f, input_root),
                });
            }
            // 2. Parse every section file on disk.
            let mut parsed: HashMap<String, Vec<Parsed>> = HashMap::new();
            let mut no_identity: Vec<Parsed> = Vec::new();
            for f in input::list_sections(root)
                .into_iter()
                .filter(|f| !foreign.iter().any(|d| f.starts_with(d)))
            {
                let frel = rel_path(&f, input_root);
                let dir = f.parent().unwrap_or(root).to_path_buf();
                let mut parent = base.clone();
                parent.extend(
                    dir.strip_prefix(root)
                        .unwrap_or(Path::new(""))
                        .components()
                        .map(|c| c.as_os_str().to_string_lossy().into_owned()),
                );
                match parser.parse_section(TypedPath::derive(&typed(&f))) {
                    Ok(s) => {
                        let ident = s.file_identity().to_string();
                        let sec =
                            convert_section(&mut nb, &s, &parent, Some(ident.clone()), Some(&frel), true);
                        parsed.entry(ident).or_default().push(Parsed { dir, section: sec });
                    }
                    Err(e) => {
                        let mut p = parent.clone();
                        p.push(file_stem(&f));
                        nb.report
                            .failed_sections
                            .push(failed(p, Some(frel), Some(&f), format!("{e}")));
                    }
                }
            }
            // 3. Assemble.
            let mut children = match &skel {
                Some(s) => place(s, &mut parsed, root, &base),
                None => Vec::new(),
            };
            let mut leftovers: Vec<Parsed> = parsed.into_values().flatten().collect();
            leftovers.append(&mut no_identity);
            leftovers.sort_by(|a, b| a.section.path.cmp(&b.section.path));
            for mut p in leftovers {
                p.section.in_toc = skel.is_none();
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

fn skel_group_dirs(skel: &[Skel], dir: &Path, out: &mut Vec<PathBuf>) {
    for s in skel {
        if let Skel::Group { name, children, .. } = s {
            let d = dir.join(name);
            skel_group_dirs(children, &d, out);
            out.push(d);
        }
    }
}

fn count_skel_entries(entries: &[SectionEntry]) -> usize {
    entries
        .iter()
        .map(|e| match e {
            SectionEntry::Section(_) => 1,
            SectionEntry::SectionGroup(g) => 1 + count_skel_entries(g.entries()),
        })
        .sum()
}

/// Parse the notebook TOC. OneDrive downloads sometimes contain several
/// `.onetoc2` files in the notebook folder (e.g. an empty `.onetoc2` next to
/// `Open Notebook.onetoc2`); use the one that lists the most entries.
fn parse_best_toc(
    parser: &Parser,
    toc: &Path,
    root: &Path,
) -> onenote_parser::errors::Result<onenote_parser::notebook::Notebook> {
    let mut candidates: Vec<PathBuf> = std::fs::read_dir(root)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| p.is_file() && input::ext_lower(p) == "onetoc2")
                .collect()
        })
        .unwrap_or_default();
    candidates.sort();
    if !candidates.iter().any(|c| c == toc) {
        candidates.insert(0, toc.to_path_buf());
    }
    let mut best: Option<onenote_parser::notebook::Notebook> = None;
    let mut first_err = None;
    for c in candidates {
        match parser.parse_notebook(TypedPath::derive(&typed(&c))) {
            Ok(n) => {
                let better = best
                    .as_ref()
                    .map(|b| count_skel_entries(n.entries()) > count_skel_entries(b.entries()))
                    .unwrap_or(true);
                if better {
                    best = Some(n);
                }
            }
            Err(e) => {
                if first_err.is_none() {
                    first_err = Some(e);
                }
            }
        }
    }
    match (best, first_err) {
        (Some(b), _) => Ok(b),
        (None, Some(e)) => Err(e),
        (None, None) => parser.parse_notebook(TypedPath::derive(&typed(toc))),
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

/// Turn the parser's top-level "failed to import section" warnings into
/// failed-section entries (for packages; for folders the per-file pass
/// reports failures itself), keep other warnings.
fn absorb_notebook_warnings(
    nb: &mut NotebookCtx,
    warnings: &[onenote_parser::warn::Warning],
    base: &[String],
    disk_root: Option<&Path>,
    rel: &str,
) {
    for w in warnings {
        let msg = w.message();
        if let Some(rest) = msg.strip_prefix("failed to import section ") {
            if disk_root.is_some() {
                // Reported by the per-file pass.
                continue;
            }
            let (file, err) = rest.split_once(": ").unwrap_or((rest, ""));
            let name = file.rsplit(['/', '\\']).next().unwrap_or(file);
            let mut path = base.to_vec();
            path.push(name.trim_end_matches(".one").to_string());
            let _ = rel;
            nb.report
                .failed_sections
                .push(failed(path, Some(file.to_string()), None, err.to_string()));
        } else {
            nb.report.warnings.push(Warning {
                message: msg.to_string(),
                page_id: None,
                page_title: None,
            });
        }
    }
}

fn package_entries(nb: &mut NotebookCtx, entries: &[SectionEntry], parent: &[String]) -> Vec<Entry> {
    entries
        .iter()
        .map(|e| match e {
            SectionEntry::Section(s) => {
                Entry::Section(convert_section(nb, s, parent, None, None, true))
            }
            SectionEntry::SectionGroup(g) => {
                nb.report.counts.section_groups += 1;
                let mut path = parent.to_vec();
                path.push(g.display_name().to_string());
                let ident = g.file_identity().to_string();
                Entry::SectionGroup(SectionGroup {
                    id: ident.clone(),
                    file_identity: Some(ident),
                    name: g.display_name().to_string(),
                    children: package_entries(nb, g.entries(), &path),
                    path,
                })
            }
        })
        .collect()
}

fn place(
    skel: &[Skel],
    parsed: &mut HashMap<String, Vec<Parsed>>,
    dir: &Path,
    parent: &[String],
) -> Vec<Entry> {
    let mut out = Vec::new();
    for s in skel {
        match s {
            Skel::Section { identity } => {
                let list = parsed.get_mut(identity);
                if let Some(list) = list
                    && !list.is_empty()
                {
                    // Prefer the file in the expected directory.
                    let idx = list.iter().position(|p| p.dir == dir).unwrap_or(0);
                    let p = list.remove(idx);
                    out.push(Entry::Section(p.section));
                }
            }
            Skel::Group {
                identity,
                name,
                children,
            } => {
                let mut path = parent.to_vec();
                path.push(name.clone());
                let gdir = dir.join(name);
                out.push(Entry::SectionGroup(SectionGroup {
                    id: identity.clone(),
                    file_identity: Some(identity.clone()),
                    name: name.clone(),
                    children: place(children, parsed, &gdir, &path),
                    path,
                }));
            }
        }
    }
    parsed.retain(|_, v| !v.is_empty());
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
    fn entries(seen: &mut HashSet<String>, pages: &mut HashSet<String>, es: &mut [Entry], salt: &str) {
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
    fn failure_classification() {
        let (r, _) = classify_failure("object may be encrypted or corrupt ('odcs' is 0x2)", None);
        assert!(r.contains("password"));
        let (r, _) = classify_failure("weird", None);
        assert!(r.contains("could not be read"));
    }

    #[test]
    fn code_paragraphs_merge() {
        let code = |t: &str, indent| Block::Code {
            common: BlockCommon {
                indent,
                ..Default::default()
            },
            text: t.into(),
        };
        let mut blocks = vec![code("a", 0), code("b", 0), code("c", 1)];
        merge_code(&mut blocks);
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].plain_text(), "a\nb");
    }
}
