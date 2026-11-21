// Modified by fortress-onenote (vendor/PATCHES.md: P10, P15).
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property_set::PropertySetId;
use crate::onenote::ParserContext;
use crate::onenote::embedded_file::{EmbeddedFile, parse_embedded_file};
use crate::onenote::image::{Image, parse_image};
use crate::onenote::ink::{Ink, parse_ink};
use crate::onenote::outline::{Outline, parse_outline};
use crate::onestore::ObjectSpace;

/// The contents of a page.
#[derive(Clone, Debug)]
pub enum PageContent {
    /// An outline block.
    Outline(Outline),
    /// An embedded image.
    Image(Image),
    /// An embedded file.
    EmbeddedFile(EmbeddedFile),
    /// An ink drawing.
    Ink(Ink),
    /// Content of unknown type.
    Unknown,
}

impl PageContent {
    /// Return the outline data if it's an outline content.
    pub fn outline(&self) -> Option<&Outline> {
        if let PageContent::Outline(outline) = self {
            Some(outline)
        } else {
            None
        }
    }
}

pub(crate) fn parse_page_content(
    content_id: ExGuid,
    space: &(impl ObjectSpace + ?Sized),
    ctx: &mut ParserContext,
) -> Result<PageContent> {
    let content_type = space
        .get_object(content_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("page content is missing".into()))?
        .id();
    let id = PropertySetId::from_jcid(content_type).ok_or_else(|| {
        ErrorKind::MalformedOneNoteData(
            format!("invalid property set id: {:?}", content_type).into(),
        )
    })?;

    let content = match id {
        PropertySetId::ImageNode => PageContent::Image(parse_image(content_id, space, ctx)?),
        PropertySetId::EmbeddedFileNode => {
            PageContent::EmbeddedFile(parse_embedded_file(content_id, space, ctx)?)
        }
        PropertySetId::OutlineNode => PageContent::Outline(parse_outline(content_id, space, ctx)?),
        PropertySetId::InkContainer => PageContent::Ink(parse_ink(content_id, space)?),
        _ => PageContent::Unknown,
    };

    Ok(content)
}
