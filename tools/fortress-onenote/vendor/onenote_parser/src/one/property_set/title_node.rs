// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::PropertyType;
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// A page title.
///
/// See [\[MS-ONE\] 2.2.29].
///
/// [\[MS-ONE\] 2.2.29]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/08bd4fd5-59fb-4568-9c82-d2d5280eced8
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) children: Vec<ExGuid>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::TitleNode)?;

    let children = ObjectReference::parse_vec(PropertyType::ElementChildNodes, object)?
        .ok_or_else(|| {
            ErrorKind::MalformedOneNoteFileData("title node has no child nodes".into())
        })?;

    Ok(Data { children })
}
