// Modified by fortress-onenote (vendor/PATCHES.md: P9, P14).
use crate::errors::Result;
use crate::one::property::color_ref::ColorRef;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::PropertySetId;
use crate::onenote::ParserContext;
use crate::onenote::rich_text::StoredFlags;
use crate::onestore::Object;

/// A paragraph style.
///
/// See [\[MS-ONE\] 2.2.43] and [\[MS-ONE\] 2.2.44]
///
/// [\[MS-ONE\] 2.2.43]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/38eb9b74-cfaf-4df7-b061-a83968c7ff5b
/// [\[MS-ONE\] 2.2.44]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/f0baabae-f42a-42e0-8cb2-869d420e865f
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) bold: bool,
    pub(crate) italic: bool,
    pub(crate) underline: bool,
    pub(crate) strikethrough: bool,
    pub(crate) superscript: bool,
    pub(crate) subscript: bool,
    pub(crate) font: Option<String>,
    pub(crate) font_size: Option<u16>,
    pub(crate) font_color: Option<ColorRef>,
    pub(crate) highlight: Option<ColorRef>,
    pub(crate) style_id: Option<String>,
    pub(crate) math_formatting: bool,
    pub(crate) hyperlink: bool,
    pub(crate) hyperlink_protected: bool,
    pub(crate) hidden: bool,
    pub(crate) text_run_is_embedded_object: bool,
    pub(crate) text_run_object_type: Option<u32>,
    // PATCH(fortress-onenote, P9): the character-format booleans as stored
    // (`None` = absent, inherited from the paragraph style).
    pub(crate) stored_flags: StoredFlags,
}

const FALLBACK: Data = Data {
    bold: false,
    italic: false,
    underline: false,
    strikethrough: false,
    superscript: false,
    subscript: false,
    font: None,
    font_size: None,
    font_color: None,
    highlight: None,
    style_id: None,
    math_formatting: false,
    hyperlink: false,
    hyperlink_protected: false,
    hidden: false,
    text_run_is_embedded_object: false,
    text_run_object_type: None,
    stored_flags: StoredFlags {
        bold: None,
        italic: None,
        underline: None,
        strikethrough: None,
        superscript: None,
        subscript: None,
    },
};

pub(crate) fn parse(object: &Object, ctx: &mut ParserContext) -> Result<Data> {
    if object.id() != PropertySetId::ParagraphStyleObject.as_jcid() {
        warn!(
            ctx,
            "Unexpected object type: 0x{:X}. Using fallback style",
            object.id().0
        );

        return Ok(FALLBACK);
    }

    // PATCH(fortress-onenote, P9): keep whether each flag is stored.
    let stored_flags = StoredFlags {
        bold: simple::parse_bool(PropertyType::Bold, object)?,
        italic: simple::parse_bool(PropertyType::Italic, object)?,
        underline: simple::parse_bool(PropertyType::Underline, object)?,
        strikethrough: simple::parse_bool(PropertyType::Strikethrough, object)?,
        superscript: simple::parse_bool(PropertyType::Superscript, object)?,
        subscript: simple::parse_bool(PropertyType::Subscript, object)?,
    };
    let bold = stored_flags.bold.unwrap_or_default();
    let italic = stored_flags.italic.unwrap_or_default();
    let underline = stored_flags.underline.unwrap_or_default();
    let strikethrough = stored_flags.strikethrough.unwrap_or_default();
    let superscript = stored_flags.superscript.unwrap_or_default();
    let subscript = stored_flags.subscript.unwrap_or_default();
    let font = simple::parse_string(PropertyType::Font, object)?;
    let font_size = simple::parse_u16(PropertyType::FontSize, object)?;
    let font_color = ColorRef::parse(PropertyType::FontColor, object)?;
    let highlight = ColorRef::parse(PropertyType::Highlight, object)?;
    let style_id = simple::parse_string(PropertyType::ParagraphStyleId, object)?;
    let math_formatting =
        simple::parse_bool(PropertyType::MathFormatting, object)?.unwrap_or_default();
    let hyperlink = simple::parse_bool(PropertyType::Hyperlink, object)?.unwrap_or_default();
    let hyperlink_protected =
        simple::parse_bool(PropertyType::HyperlinkProtected, object)?.unwrap_or_default();
    let hidden = simple::parse_bool(PropertyType::Hidden, object)?.unwrap_or_default();
    let text_run_is_embedded_object =
        simple::parse_bool(PropertyType::TextRunIsEmbeddedObject, object)?.unwrap_or_default();
    let text_run_object_type = simple::parse_u32(PropertyType::EmbeddedObjectType, object)?;

    let data = Data {
        bold,
        italic,
        underline,
        strikethrough,
        superscript,
        subscript,
        font,
        font_size,
        font_color,
        highlight,
        style_id,
        math_formatting,
        hyperlink,
        hyperlink_protected,
        hidden,
        text_run_is_embedded_object,
        text_run_object_type,
        stored_flags,
    };

    Ok(data)
}
