// Modified by fortress-onenote (vendor/PATCHES.md: P14).
use crate::errors::Result;
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::paragraph_alignment::ParagraphAlignment;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::note_tag_container::Data as NoteTagData;
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// A rich text paragraph.
///
/// See [\[MS-ONE\] 2.2.23].
///
/// [\[MS-ONE\] 2.2.23]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/980fae36-b5dd-4581-bf1e-5ab54177153d
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) text_run_formatting: Vec<ExGuid>,
    pub(crate) text_run_indices: Vec<u32>,
    pub(crate) text_run_data_object: Vec<ExGuid>,
    pub(crate) paragraph_style: Option<ExGuid>,
    pub(crate) paragraph_alignment: ParagraphAlignment,
    pub(crate) text: Option<String>,
    pub(crate) note_tags: Vec<NoteTagData>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::RichTextNode)?;

    let text_run_formatting =
        ObjectReference::parse_vec(PropertyType::TextRunFormatting, object)?.unwrap_or_default();
    let text_run_indices =
        simple::parse_vec_u32(PropertyType::TextRunIndex, object)?.unwrap_or_default();
    let text_run_data_object =
        ObjectReference::parse_vec(PropertyType::TextRunDataObject, object)?.unwrap_or_default();
    let paragraph_style = ObjectReference::parse(PropertyType::ParagraphStyle, object)?;
    let paragraph_alignment = ParagraphAlignment::parse(object)?.unwrap_or_default();

    let text = match simple::parse_string(PropertyType::RichEditTextUnicode, object)? {
        None => simple::parse_ascii(PropertyType::TextExtendedAscii, object)?,
        text => text,
    };

    let note_tags = NoteTagData::parse(object)?.unwrap_or_default();

    let data = Data {
        text_run_formatting,
        text_run_indices,
        text_run_data_object,
        paragraph_style,
        paragraph_alignment,
        text,
        note_tags,
    };

    Ok(data)
}
