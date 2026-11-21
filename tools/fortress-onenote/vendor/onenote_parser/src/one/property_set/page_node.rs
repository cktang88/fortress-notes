// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::Result;
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::author::Author;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::time::Time;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// A page.
///
/// See [\[MS-ONE\] 2.2.19].
///
/// [\[MS-ONE\] 2.2.19]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/e381b7c7-b434-43a2-ba23-0d08bafd281a
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) last_modified: Option<Time>,
    pub(crate) author: Option<Author>,
    pub(crate) content: Vec<ExGuid>,
    pub(crate) title: Option<ExGuid>,
    pub(crate) page_height: Option<f32>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::PageNode)?;

    let last_modified = Time::parse(PropertyType::LastModifiedTime, object)?;
    let author = Author::parse(object)?;
    let content =
        ObjectReference::parse_vec(PropertyType::ElementChildNodes, object)?.unwrap_or_default();
    let title = ObjectReference::parse_vec(PropertyType::StructureElementChildNodes, object)?
        .unwrap_or_default()
        .first()
        .copied();
    let page_height = simple::parse_f32(PropertyType::PageHeight, object)?;

    let data = Data {
        last_modified,
        author,
        content,
        title,
        page_height,
    };

    Ok(data)
}
