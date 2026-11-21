// Modified by fortress-onenote (vendor/PATCHES.md: P11, P15).
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property::color::Color;
use crate::one::property_set::{table_cell_node, table_node, table_row_node};
use crate::onenote::ParserContext;
use crate::onenote::note_tag::{NoteTag, parse_note_tags};
use crate::onenote::outline::{OutlineElement, parse_outline_element};
use crate::onestore::ObjectSpace;

/// A table.
///
/// See [\[MS-ONE\] 2.2.26].
///
/// [\[MS-ONE\] 2.2.26]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/9046980a-2410-4b2d-8a35-ec06e55648e0
#[derive(Clone, Debug)]
pub struct Table {
    /// PATCH(fortress-onenote, P11)
    pub(crate) id: ExGuid,
    pub(crate) rows: u32,
    pub(crate) cols: u32,
    pub(crate) contents: Vec<TableRow>,
    pub(crate) col_widths: Vec<f32>,

    pub(crate) borders_visible: bool,
    pub(crate) note_tags: Vec<NoteTag>,
}

impl Table {
    /// PATCH(fortress-onenote, P11): the object's id (extended GUID), stable
    /// across revisions of the object.
    pub fn id(&self) -> ExGuid {
        self.id
    }

    /// The number of rows in this table.
    pub fn rows(&self) -> u32 {
        self.rows
    }

    /// The number of columns in this table.
    pub fn cols(&self) -> u32 {
        self.cols
    }

    /// The table rows.
    pub fn contents(&self) -> &[TableRow] {
        &self.contents
    }

    /// The column widths in half-inch increments.
    ///
    /// See [\[MS-ONE\] 2.3.66].
    ///
    /// [\[MS-ONE\] 2.3.66]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/aea7e232-e7f3-444b-8d2c-e8f46fa8cc59
    pub fn col_widths(&self) -> &[f32] {
        &self.col_widths
    }

    /// Whether the table borders are visible.
    ///
    /// See [\[MS-ONE\] 2.3.65].
    ///
    /// [\[MS-ONE\] 2.3.65]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/37be8f3c-e015-4c45-be99-615a669439b8
    pub fn borders_visible(&self) -> bool {
        self.borders_visible
    }

    /// Note tags for this table.
    pub fn note_tags(&self) -> &[NoteTag] {
        &self.note_tags
    }
}

/// A table row.
///
/// See [\[MS-ONE\] 2.2.27].
///
/// [\[MS-ONE\] 2.2.27]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/d22af1aa-5e0b-40ed-b914-f6397979d6b0
#[derive(Clone, Debug)]
pub struct TableRow {
    pub(crate) contents: Vec<TableCell>,
}

impl TableRow {
    /// The cells in the table row.
    pub fn contents(&self) -> &[TableCell] {
        &self.contents
    }
}

/// A table cell.
///
/// See [\[MS-ONE\] 2.2.28].
///
/// [\[MS-ONE\] 2.2.28]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/e5660d6b-72c3-4d9f-bad0-435c00f42183
#[derive(Clone, Debug)]
pub struct TableCell {
    pub(crate) contents: Vec<OutlineElement>,

    pub(crate) background_color: Option<Color>,
}

impl TableCell {
    /// The contents of the table cell.
    pub fn contents(&self) -> &[OutlineElement] {
        &self.contents
    }

    /// The cell's background color.
    pub fn background_color(&self) -> Option<Color> {
        self.background_color
    }
}

pub(crate) fn parse_table(
    table_id: ExGuid,
    space: &(impl ObjectSpace + ?Sized),
    ctx: &mut ParserContext,
) -> Result<Table> {
    let table_object = space
        .get_object(table_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("table object is missing".into()))?;
    let data = table_node::parse(table_object)?;

    let contents = data
        .rows
        .into_iter()
        .map(|row_id| parse_row(row_id, space, ctx))
        .collect::<Result<_>>()?;

    let table = Table {
        id: table_id,
        rows: data.row_count,
        cols: data.col_count,
        contents,
        col_widths: data.col_widths,
        borders_visible: data.borders_visible,
        note_tags: parse_note_tags(&data.note_tags, space)?,
    };

    Ok(table)
}

fn parse_row(
    row_id: ExGuid,
    space: &(impl ObjectSpace + ?Sized),
    ctx: &mut ParserContext,
) -> Result<TableRow> {
    let row_object = space
        .get_object(row_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("row object is missing".into()))?;
    let data = table_row_node::parse(row_object)?;

    let contents = data
        .cells
        .into_iter()
        .map(|cell_id| parse_cell(cell_id, space, ctx))
        .collect::<Result<_>>()?;

    let row = TableRow { contents };

    Ok(row)
}

fn parse_cell(
    cell_id: ExGuid,
    space: &(impl ObjectSpace + ?Sized),
    ctx: &mut ParserContext,
) -> Result<TableCell> {
    let cell_object = space
        .get_object(cell_id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("cell object is missing".into()))?;
    let data = table_cell_node::parse(cell_object)?;

    let contents = data
        .contents
        .into_iter()
        .map(|element_id| parse_outline_element(element_id, space, ctx))
        .collect::<Result<_>>()?;

    let cell = TableCell {
        contents,
        background_color: data.background_color,
    };

    Ok(cell)
}
