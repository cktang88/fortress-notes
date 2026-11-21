// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::PropertyType;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// A table row.
///
/// See [\[MS-ONE\] 2.2.27].
///
/// [\[MS-ONE\] 2.2.27]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/d22af1aa-5e0b-40ed-b914-f6397979d6b0
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) cells: Vec<ExGuid>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::TableRowNode)?;

    let cells = ObjectReference::parse_vec(PropertyType::ElementChildNodes, object)?
        .ok_or_else(|| ErrorKind::MalformedOneNoteFileData("table row has no cells".into()))?;

    Ok(Data {
        cells,
    })
}
