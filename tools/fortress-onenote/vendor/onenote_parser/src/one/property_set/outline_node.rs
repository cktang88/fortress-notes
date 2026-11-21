// Modified by fortress-onenote (vendor/PATCHES.md: P6, P15).
use crate::errors::Result;
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// An outline group.
///
/// See [\[MS-ONE\] 2.2.20].
///
/// [\[MS-ONE\] 2.2.20]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/b25fa331-e07e-474e-99c9-b3603b7bf937
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) children: Vec<ExGuid>,
    pub(crate) layout_max_width: Option<f32>,
    pub(crate) is_layout_size_set_by_user: bool,
    pub(crate) offset_from_parent_horiz: Option<f32>,
    pub(crate) offset_from_parent_vert: Option<f32>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::OutlineNode)?;

    let children =
        ObjectReference::parse_vec(PropertyType::ElementChildNodes, object)?.unwrap_or_default();
    // PATCH(fortress-onenote, P15): the child level, indent distances,
    // spacing, alignment and flags of an outline are not read.
    let layout_max_width = simple::parse_f32(PropertyType::LayoutMaxWidth, object)?;

    let is_layout_size_set_by_user =
        simple::parse_bool(PropertyType::IsLayoutSizeSetByUser, object)?.unwrap_or_default();
    let offset_from_parent_horiz = simple::parse_f32(PropertyType::OffsetFromParentHoriz, object)?;
    let offset_from_parent_vert = simple::parse_f32(PropertyType::OffsetFromParentVert, object)?;

    let data = Data {
        children,
        layout_max_width,
        is_layout_size_set_by_user,
        offset_from_parent_horiz,
        offset_from_parent_vert,
    };

    Ok(data)
}
