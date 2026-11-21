// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::Result;
use crate::one::property::color::Color;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// A section's metadata.
///
/// See [\[MS-ONE\] 2.2.31].
///
/// [\[MS-ONE\] 2.2.31]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/c8cd2fe2-593b-45f8-8da0-03ca6f6f704d
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) display_name: Option<String>,
    pub(crate) color: Option<Color>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::SectionMetadata)?;

    let display_name = simple::parse_string(PropertyType::SectionDisplayName, object)?;
    let color = Color::parse(PropertyType::SectionColor, object)?;

    let data = Data {
        display_name,
        color,
    };

    Ok(data)
}
