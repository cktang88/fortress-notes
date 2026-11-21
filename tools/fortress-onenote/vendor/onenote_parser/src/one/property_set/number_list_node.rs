// Modified by fortress-onenote (vendor/PATCHES.md: P2, P15).
use crate::errors::{ErrorKind, Result};
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;
use std::char::{REPLACEMENT_CHARACTER, decode_utf16};

/// A number list definition.
///
/// See [\[MS-ONE\] 2.2.25].
///
/// [\[MS-ONE\] 2.2.25]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/1a141e7a-4455-4971-bf0b-1621e221984e
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) list_font: Option<String>,
    pub(crate) list_restart: Option<i32>,
    pub(crate) list_format: Vec<char>,
    pub(crate) font: Option<String>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::NumberListNode)?;

    let list_font = simple::parse_string(PropertyType::ListFont, object)?;
    let list_restart =
        simple::parse_u32(PropertyType::ListRestart, object)?.map(|value| value as i32);
    let list_format = simple::parse_vec_u16(PropertyType::NumberListFormat, object)?
        .map(parse_list_format)
        .ok_or_else(|| {
            ErrorKind::MalformedOneNoteFileData("number list has no list format".into())
        })?;
    let font = simple::parse_string(PropertyType::Font, object)?;

    let data = Data {
        list_font,
        list_restart,
        list_format,
        font,
    };

    Ok(data)
}

fn parse_list_format(data: Vec<u16>) -> Vec<char> {
    // PATCH(fortress-onenote, P2): an empty format must not panic.
    decode_utf16(data.get(1..).unwrap_or_default().iter().copied())
        .map(|r| r.unwrap_or(REPLACEMENT_CHARACTER))
        .collect()
}
