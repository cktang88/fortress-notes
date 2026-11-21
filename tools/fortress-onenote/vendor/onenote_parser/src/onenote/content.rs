// Modified by fortress-onenote (vendor/PATCHES.md: P10, P15).
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property_set::PropertySetId;
use crate::onenote::ParserContext;
use crate::onenote::embedded_file::{EmbeddedFile, parse_embedded_file};
use crate::onenote::image::{Image, parse_image};
use crate::onenote::ink::{Ink, parse_ink};
use crate::onenote::rich_text::{RichText, parse_rich_text};
use crate::onenote::table::{Table, parse_table};
use crate::onestore::ObjectSpace;

/// The content of an outline.
#[derive(Clone, Debug)]
pub enum Content {
    /// A rich-text block.
    RichText(RichText),

    /// A table.
    Table(Table),

    /// An embedded image.
    Image(Image),

    /// An embedded file.
    EmbeddedFile(EmbeddedFile),

    /// An ink drawing.
    Ink(Ink),

    /// Content of unknown type.
    Unknown,
}

impl Content {
    /// Return the rich-text data if it's a rich-text content block.
    pub fn rich_text(&self) -> Option<&RichText> {
        if let Content::RichText(rich_text) = self {
            Some(rich_text)
        } else {
            None
        }
    }
}

pub(crate) fn parse_content(
    content_id: ExGuid,
    space: &(impl ObjectSpace + ?Sized),
    ctx: &mut ParserContext,
) -> Result<Content> {
    let content_type = space
        .get_object(content_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("page content is missing".into()))?
        .id();

    let id = PropertySetId::from_jcid(content_type).ok_or_else(|| {
        ErrorKind::MalformedOneNoteData(
            format!("invalid property set id: 0x{:X}", content_type.0).into(),
        )
    })?;

    let content = match id {
        PropertySetId::ImageNode => Content::Image(parse_image(content_id, space, ctx)?),
        PropertySetId::EmbeddedFileNode => {
            Content::EmbeddedFile(parse_embedded_file(content_id, space, ctx)?)
        }
        PropertySetId::RichTextNode => Content::RichText(parse_rich_text(content_id, space, ctx)?),
        PropertySetId::TableNode => Content::Table(parse_table(content_id, space, ctx)?),
        PropertySetId::InkContainer => Content::Ink(parse_ink(content_id, space)?),
        _ => Content::Unknown,
    };

    Ok(content)
}
