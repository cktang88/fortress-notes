// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::{ErrorKind, Result};
use crate::one::property::time::Timestamp;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;
use crate::shared::guid::Guid;

/// A page's metadata.
///
/// See [\[MS-ONE\] 2.2.30].
///
/// [\[MS-ONE\] 2.2.30]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/aaabcc70-5836-4dcb-8209-012ce5d45b3c
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) entity_guid: Guid,
    pub(crate) page_level: i32,
    pub(crate) created_at: Timestamp,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::PageMetadata)?;

    let entity_guid = simple::parse_guid(PropertyType::NotebookManagementEntityGuid, object)?
        .ok_or_else(|| ErrorKind::MalformedOneNoteFileData("page metadata has no guid".into()))?;
    let page_level = simple::parse_u32(PropertyType::PageLevel, object)?.unwrap_or(0) as i32;
    let created_at = Timestamp::parse(PropertyType::TopologyCreationTimeStamp, object)?
        .ok_or_else(|| {
            ErrorKind::MalformedOneNoteFileData("page metadata has no creation timestamp".into())
        })?;

    let data = Data {
        entity_guid,
        page_level,
        created_at,
    };

    Ok(data)
}
