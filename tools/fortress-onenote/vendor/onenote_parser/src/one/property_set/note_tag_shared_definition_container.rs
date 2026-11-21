// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::{ErrorKind, Result};
use crate::one::property::color_ref::ColorRef;
use crate::one::property::note_tag_shape::NoteTagShape;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// An note tag shared definition container.
///
/// See [\[MS-ONE\] 2.2.41].
///
/// [\[MS-ONE\] 2.2.41]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/eb5f52d2-c507-45c8-9bda-f8c74d34533a
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) label: String,
    pub(crate) shape: NoteTagShape,
    pub(crate) highlight_color: Option<ColorRef>,
    pub(crate) text_color: Option<ColorRef>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::NoteTagSharedDefinitionContainer)?;

    let label = simple::parse_string(PropertyType::NoteTagLabel, object)?.ok_or_else(|| {
        ErrorKind::MalformedOneNoteFileData("note tag container has no label".into())
    })?;
    // PATCH(fortress-onenote, P15): the status and action item type are not
    // kept, but a definition without them is still rejected.
    if simple::parse_u32(PropertyType::NoteTagPropertyStatus, object)?.is_none() {
        return Err(
            ErrorKind::MalformedOneNoteFileData("note tag container has no status".into()).into(),
        );
    }
    let shape = simple::parse_u16(PropertyType::NoteTagShape, object)?
        .map(NoteTagShape::parse)
        .transpose()?
        .ok_or_else(|| {
            ErrorKind::MalformedOneNoteFileData("note tag container has no shape".into())
        })?;
    let highlight_color = ColorRef::parse(PropertyType::NoteTagHighlightColor, object)?;
    let text_color = ColorRef::parse(PropertyType::NoteTagTextColor, object)?;
    if simple::parse_u16(PropertyType::ActionItemType, object)?.is_none() {
        return Err(ErrorKind::MalformedOneNoteFileData(
            "note tag container has no action item type".into(),
        )
        .into());
    }

    let data = Data {
        label,
        shape,
        highlight_color,
        text_color,
    };

    Ok(data)
}
