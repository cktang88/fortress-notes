// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::{PropertyType, simple};
use crate::one::property_set::note_tag_container::Data as NoteTagData;
use crate::one::property_set::{PropertySetId, assert_property_set};
use crate::onestore::Object;

/// A table.
///
/// See [\[MS-ONE\] 2.2.26].
///
/// [\[MS-ONE\] 2.2.26]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/9046980a-2410-4b2d-8a35-ec06e55648e0
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) rows: Vec<ExGuid>,
    pub(crate) row_count: u32,
    pub(crate) col_count: u32,
    pub(crate) col_widths: Vec<f32>,
    pub(crate) borders_visible: bool,
    pub(crate) note_tags: Vec<NoteTagData>,
}

pub(crate) fn parse(object: &Object) -> Result<Data> {
    assert_property_set(object, PropertySetId::TableNode)?;

    let rows = ObjectReference::parse_vec(PropertyType::ElementChildNodes, object)?
        .ok_or_else(|| ErrorKind::MalformedOneNoteFileData("table has no rows".into()))?;
    let row_count = simple::parse_u32(PropertyType::RowCount, object)?
        .ok_or_else(|| ErrorKind::MalformedOneNoteFileData("table has no row count".into()))?;
    let col_count = simple::parse_u32(PropertyType::ColumnCount, object)?
        .ok_or_else(|| ErrorKind::MalformedOneNoteFileData("table has no col count".into()))?;
    let col_widths = simple::parse_vec(PropertyType::TableColumnWidths, object)?
        .map(|value| {
            value
                .into_iter()
                .skip(1)
                .collect::<Vec<_>>()
                .chunks_exact(4)
                .map(|v| f32::from_le_bytes([v[0], v[1], v[2], v[3]]))
                .collect()
        })
        .unwrap_or_default();
    let borders_visible =
        simple::parse_bool(PropertyType::TableBordersVisible, object)?.unwrap_or(true);

    let note_tags = NoteTagData::parse(object)?.unwrap_or_default();

    let data = Data {
        rows,
        row_count,
        col_count,
        col_widths,
        borders_visible,
        note_tags,
    };

    Ok(data)
}
