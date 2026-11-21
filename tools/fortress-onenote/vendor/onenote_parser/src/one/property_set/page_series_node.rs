// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::Result;
use crate::fsshttpb::data::cell_id::CellId;
use crate::one::property::object_space_reference::ObjectSpaceReference;
use crate::one::property::PropertyType;
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// A page series.
///
/// See [\[MS-ONE\] 2.2.18].
///
/// [\[MS-ONE\] 2.2.18]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/e2957d3b-a2a8-4756-8662-4e67fefa9f4e
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) page_spaces: Vec<CellId>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::PageSeriesNode)?;

    let page_spaces =
        ObjectSpaceReference::parse_vec(PropertyType::ChildGraphSpaceElementNodes, object)?
            .unwrap_or_default();

    Ok(Data { page_spaces })
}
