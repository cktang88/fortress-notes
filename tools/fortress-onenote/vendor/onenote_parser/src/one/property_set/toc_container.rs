// Modified by fortress-onenote (vendor/PATCHES.md: P8).
use crate::errors::Result;
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;
use crate::onestore::shared::property::PropertyValue;
use crate::utils::Utf16ToString;
use crate::property::common::Color;

/// A section's table of contents.
///
/// See [\[MS-ONE\] 2.2.15].
///
/// [\[MS-ONE\] 2.2.15]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/6c1dd264-850b-4e46-af62-50b4dba49b62
#[derive(Debug)]
#[allow(dead_code)]
pub(crate) struct Data {
    pub(crate) children: Vec<ExGuid>,
    pub(crate) filename: Option<String>,
    pub(crate) ordering_id: Option<u32>,
    pub(crate) color: Option<Color>,
}

pub(crate) fn parse(object: &Object, fallback_filename_allowed: bool) -> Result<Data> {
    assert_property_set(object, PropertySetId::TocContainer)?;

    // PATCH(fortress-onenote, P8): tables of contents written by other
    // tools may store the entry list and file names in properties of their
    // own. Fall back to the container's first object-id array for the
    // children and, for a container without children, to its first
    // string-valued property for the file name.
    let children = match ObjectReference::parse_vec(PropertyType::TocChildren, object)? {
        Some(children) => children,
        None => fallback_children(object),
    };
    let filename = match simple::parse_string(PropertyType::FolderChildFilename, object)? {
        Some(name) => Some(name),
        None if children.is_empty() && fallback_filename_allowed => fallback_filename(object),
        None => None,
    }
    .map(|s| s.replace("^M", "+"))
    .map(|s| s.replace("^J", ","));
    let ordering_id = simple::parse_u32(PropertyType::NotebookElementOrderingId, object)?;
    let color = Color::parse(PropertyType::SectionColor, object)?;

    Ok(Data {
        children,
        filename,
        ordering_id,
        color,
    })
}

/// PATCH(fortress-onenote, P8): the references of the first `ObjectIds`
/// property.
fn fallback_children(object: &Object) -> Vec<ExGuid> {
    let mut offset = 0;
    for value in object.props.properties().values() {
        if let PropertyValue::ObjectIds(count) = value {
            return object
                .props
                .object_ids()
                .iter()
                .skip(offset)
                .take(*count as usize)
                .enumerate()
                .filter_map(|(index, id)| object.mapping.resolve_id(index + offset, id))
                .collect();
        }
        offset += ObjectReference::count_references(std::iter::once(value));
    }
    Vec::new()
}

/// PATCH(fortress-onenote, P8): the first property (other than the file
/// identity) holding a non-empty, NUL-terminated UTF-16 string without
/// control characters.
fn fallback_filename(object: &Object) -> Option<String> {
    let identity = PropertyType::FileIdentityGuid as u32 & 0x3ff_ffff;
    object
        .props
        .properties()
        .entries()
        .filter(|(id, _)| *id != identity)
        .filter_map(|(_, value)| value.to_vec())
        .filter(|bytes| bytes.len() >= 4 && bytes.len() % 2 == 0 && bytes.ends_with(&[0, 0]))
        .filter_map(|bytes| bytes.utf16_to_string().ok())
        .find(|s| !s.is_empty() && !s.chars().any(|c| c.is_control() || c == '\u{FFFD}'))
}
