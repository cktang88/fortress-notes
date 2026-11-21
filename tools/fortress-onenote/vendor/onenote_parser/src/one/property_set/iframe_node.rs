// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::{ErrorKind, Result};
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// An ink data container.
pub(crate) struct Data {
    pub(crate) source_url: String,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::IFrameNode)?;

    let source_url = simple::parse_string(PropertyType::ImageEmbeddedUrl, object)?
        .ok_or_else(|| ErrorKind::MalformedOneNoteFileData("iframe has no source URL".into()))?;

    Ok(Data { source_url })
}
