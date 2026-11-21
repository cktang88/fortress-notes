// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::Result;
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::file_type::FileType;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::note_tag_container::Data as NoteTagData;
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// An embedded file.
///
/// See [\[MS-ONE\] 2.2.32].
///
/// [\[MS-ONE\] 2.2.32]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/a665b5ad-ff40-4c0c-9e42-4b707254dc3f
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) picture_container: Option<ExGuid>,
    pub(crate) layout_max_width: Option<f32>,
    pub(crate) layout_max_height: Option<f32>,
    pub(crate) embedded_file_container: Option<ExGuid>,
    pub(crate) embedded_file_name: Option<String>,
    pub(crate) file_type: FileType,
    pub(crate) note_tags: Vec<NoteTagData>,
    pub(crate) offset_from_parent_horiz: Option<f32>,
    pub(crate) offset_from_parent_vert: Option<f32>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::EmbeddedFileNode)?;

    let picture_container = ObjectReference::parse(PropertyType::PictureContainer, object)?;
    let layout_max_width = simple::parse_f32(PropertyType::LayoutMaxWidth, object)?;
    let layout_max_height = simple::parse_f32(PropertyType::LayoutMaxHeight, object)?;
    let embedded_file_container =
        ObjectReference::parse(PropertyType::EmbeddedFileContainer, object)?;
    let embedded_file_name = simple::parse_string(PropertyType::EmbeddedFileName, object)?;
    let file_type = FileType::parse(object)?;
    let offset_from_parent_horiz = simple::parse_f32(PropertyType::OffsetFromParentHoriz, object)?;
    let offset_from_parent_vert = simple::parse_f32(PropertyType::OffsetFromParentVert, object)?;

    let note_tags = NoteTagData::parse(object)?.unwrap_or_default();

    let data = Data {
        picture_container,
        layout_max_width,
        layout_max_height,
        embedded_file_container,
        embedded_file_name,
        file_type,
        note_tags,
        offset_from_parent_horiz,
        offset_from_parent_vert,
    };

    Ok(data)
}
