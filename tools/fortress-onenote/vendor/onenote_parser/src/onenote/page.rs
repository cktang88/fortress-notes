// Modified by fortress-onenote (vendor/PATCHES.md: P2, P7, P10, P15).
use crate::contents::RichText;
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property_set::{page_manifest_node, page_metadata, page_node, title_node};
use crate::onenote::{ParserContext, skip_damaged};
use crate::onenote::outline::{Outline, parse_outline};
use crate::onenote::page_content::{PageContent, parse_page_content};
use crate::onestore::ObjectSpace;
use time::macros::utc_datetime;

/// A page.
///
/// See [\[MS-ONE\] 1.3.2] and [\[MS-ONE\] 2.2.19].
///
/// [\[MS-ONE\] 1.3.2]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/2dd687ac-f36b-4723-b959-4d60c8a90ca9
/// [\[MS-ONE\] 2.2.19]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/e381b7c7-b434-43a2-ba23-0d08bafd281a
#[derive(Clone, Debug)]
pub struct Page {
    link_target_id: String,
    title_text: Option<String>,
    level: i32,
    created_at: time::UtcDateTime,
    updated_at: Option<time::UtcDateTime>,
    author: Option<String>,
    height: Option<f32>,
    contents: Vec<PageContent>,
}

impl Page {
    /// The page's GUID. May be referenced by internal links.
    /// Ref: [ONESTORE 2.2.58](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-one/34ea5601-f060-4a69-b5f9-5843a1f14098)
    pub fn link_target_id(&self) -> &str {
        &self.link_target_id
    }

    /// The time at which the page was created.
    pub fn created_time(&self) -> time::UtcDateTime {
        self.created_at
    }

    /// The page's last-modified time, falling back to the creation time when
    /// the page has never been edited.
    pub fn updated_time(&self) -> time::UtcDateTime {
        self.updated_at.unwrap_or_else(|| self.created_time())
    }

    /// The page's level in the section page tree.
    ///
    /// See [\[MS-ONE\] 2.3.74].
    ///
    /// [\[MS-ONE\] 2.3.74]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/a8632c90-e74a-4ef6-8852-707d4c8817cd
    pub fn level(&self) -> i32 {
        self.level
    }

    /// The page's author.
    pub fn author(&self) -> Option<&str> {
        self.author.as_deref()
    }

    /// The page's height.
    ///
    /// See [\[MS-ONE\] 2.3.7].
    ///
    /// [\[MS-ONE\] 2.3.7]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/e5d0b5e0-0702-42af-8299-0e27c895ba7e
    pub fn height(&self) -> Option<f32> {
        self.height
    }

    /// The page contents.
    pub fn contents(&self) -> &[PageContent] {
        &self.contents
    }

    /// The page's title text.
    ///
    /// This is calculated using a heuristic similar to the one OneNote uses.
    pub fn title_text(&self) -> Option<&str> {
        self.title_text.as_deref()
    }
}

pub(crate) fn parse_page(
    page_space: &(impl ObjectSpace + ?Sized),
    ctx: &mut ParserContext,
) -> Result<Page> {
    let metadata = parse_metadata(page_space)?;
    let manifest = parse_manifest(page_space)?;

    let data = parse_data(manifest, page_space)?;

    // PATCH(fortress-onenote, P7): a damaged title or content object is
    // skipped, the page survives.
    let title = match data.title {
        Some(id) => {
            let title = parse_title(id, page_space, ctx);
            skip_damaged(ctx, "page title", title)?
        }
        None => None,
    };

    let level = metadata.page_level;

    let title_text = title
        .as_ref()
        .and_then(|contents| contents.first())
        .and_then(outline_text);

    ctx.page = Some((
        metadata.entity_guid.0,
        title_text
            .clone()
            .unwrap_or_else(|| "<Unknown title>".to_owned()),
    ));

    // PATCH(fortress-onenote, P10): handwriting recognition is not parsed.
    let mut contents: Vec<PageContent> = Vec::new();
    for content_id in data.content {
        let content = parse_page_content(content_id, page_space, ctx);
        contents.extend(skip_damaged(ctx, "page element", content)?);
    }

    let link_target_id = format!("{}", metadata.entity_guid);

    ctx.page = None;

    let created_at = metadata.created_at.try_into().unwrap_or_else(|err| {
        // FILETIME values outside the representable range fall back to the
        // UNIX epoch. `last_modified` is less precise on disk so this only
        // matters for `created_at`.
        warn!(ctx, "invalid page creation timestamp: {err}");
        utc_datetime!(1970-01-01 0:00)
    });
    let updated_at = data.last_modified.map(|time| time.into());

    Ok(Page {
        link_target_id,
        title_text: title_text.as_deref().map(remove_hyperlink).or_else(|| {
            contents
                .iter()
                .filter_map(|page_content| page_content.outline())
                .filter_map(outline_text)
                .next()
        }),
        level,
        created_at,
        updated_at,
        author: data.author.map(|author| author.into_value()),
        height: data.page_height,
        contents,
    })
}

fn parse_title(
    title_id: ExGuid,
    space: &(impl ObjectSpace + ?Sized),
    ctx: &mut ParserContext,
) -> Result<Vec<Outline>> {
    let title_object = space
        .get_object(title_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("title object is missing".into()))?;
    let title = title_node::parse(title_object)?;
    // PATCH(fortress-onenote, P15): only the title's outlines are kept
    // (for `Page::title_text`).
    title
        .children
        .into_iter()
        .map(|outline_id| parse_outline(outline_id, space, ctx))
        .collect()
}

fn parse_data(
    manifest: page_manifest_node::Data,
    space: &(impl ObjectSpace + ?Sized),
) -> Result<page_node::Data> {
    let page_id = manifest.page;
    let page_object = space
        .get_object(page_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("page object is missing".into()))?;

    page_node::parse(page_object)
}

fn parse_manifest(space: &(impl ObjectSpace + ?Sized)) -> Result<page_manifest_node::Data> {
    let page_manifest_id = space
        .content_root()
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("page content id is missing".into()))?;
    let page_manifest_object = space
        .get_object(page_manifest_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("page object is missing".into()))?;

    page_manifest_node::parse(page_manifest_object)
}

fn parse_metadata(space: &(impl ObjectSpace + ?Sized)) -> Result<page_metadata::Data> {
    let metadata_id = space
        .metadata_root()
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("page metadata id is missing".into()))?;
    let metadata_object = space
        .get_object(metadata_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("page metadata object is missing".into()))?;

    page_metadata::parse(metadata_object)
}

fn outline_text(outline: &Outline) -> Option<String> {
    let text = outline
        .items
        .first()?
        .element()?
        .contents
        .first()?
        .rich_text()?;

    let visible = visible_text(text);
    Some(visible).filter(|s| !s.is_empty())
}

/// Concatenate text runs that are not Hidden (per [MS-ONE] §2.3.76).
/// `text_run_indices` are UTF-16 code unit positions where each run ends;
/// the last run extends to the end of the paragraph text.
fn visible_text(rt: &RichText) -> String {
    let formatting = rt.text_run_formatting();
    if formatting.is_empty() {
        return rt.text().to_owned();
    }

    let utf16: Vec<u16> = rt.text().encode_utf16().collect();
    let indices = rt.text_run_indices();

    // Build run boundaries: [0, idx_0, idx_1, ..., utf16.len()]
    let mut bounds: Vec<usize> = std::iter::once(0)
        .chain(indices.iter().map(|&i| i as usize))
        .collect();
    if bounds.len() == formatting.len() {
        bounds.push(utf16.len());
    }

    let mut out: Vec<u16> = Vec::with_capacity(utf16.len());
    for (i, style) in formatting.iter().enumerate() {
        if style.hidden() {
            continue;
        }
        // PATCH(fortress-onenote, P2): more run styles than run boundaries
        // (corrupt file) must not panic.
        let start = bounds.get(i).copied().unwrap_or(utf16.len()).min(utf16.len());
        let end = bounds
            .get(i + 1)
            .copied()
            .unwrap_or(utf16.len())
            .min(utf16.len());
        if start < end {
            out.extend_from_slice(&utf16[start..end]);
        }
    }

    String::from_utf16_lossy(&out)
}

fn remove_hyperlink(title: &str) -> String {
    const HYPERLINK_MARKER: &str = "\u{fddf}HYPERLINK \"";

    let mut clean_title = title.to_string();

    // Find the first hyperlink mark
    while let Some(marker_start) = clean_title.find(HYPERLINK_MARKER) {
        let hyperlink_part = &clean_title[marker_start + HYPERLINK_MARKER.len()..];

        // Find the closing double quote of the hyperlink
        if let Some(quote_end) = hyperlink_part.find('"') {
            let before_hyperlink = &clean_title[..marker_start];
            let after_hyperlink = &hyperlink_part[quote_end + 1..];
            clean_title = format!("{}{}", before_hyperlink, after_hyperlink);
        } else {
            // Sometimes links are broken, in these cases we only consider what is before the mark
            // PATCH(fortress-onenote, P2): `marker_start` indexes
            // `clean_title`, not `title` (could panic or loop forever).
            clean_title.truncate(marker_start);
        }
    }

    clean_title
}
