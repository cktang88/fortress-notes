// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::Result;
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onenote::ParserContext;
use crate::onestore::Object;

/// A section.
///
/// See [\[MS-ONE\] 2.2.23].
///
/// [\[MS-ONE\] 2.2.23]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/980fae36-b5dd-4581-bf1e-5ab54177153d
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) page_series: Vec<ExGuid>,
}

pub(crate) fn parse(object: &Object, ctx: &mut ParserContext) -> Result<Data> {
    assert_property_set(object, PropertySetId::SectionNode)?;

    // PATCH(fortress-onenote, P15): the GUID and creation time are not
    // kept; a missing GUID is still reported.
    if simple::parse_guid(PropertyType::NotebookManagementEntityGuid, object)?.is_none() {
        warn!(ctx, "Section has no GUID");
    }
    let page_series =
        ObjectReference::parse_vec(PropertyType::ElementChildNodes, object)?.unwrap_or_default();

    Ok(Data { page_series })
}
