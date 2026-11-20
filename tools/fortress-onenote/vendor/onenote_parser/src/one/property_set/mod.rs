// Modified by fortress-onenote (vendor/PATCHES.md: P1, P10).
//! The property sets describing OneNote objects we can parse.
//!
//! See [\[MS-ONE\] 2.1.13] for the list of properties the OneNote file format specifies.
//!
//! [\[MS-ONE\] 2.1.13]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/73d98105-f194-4c05-a795-09840a6d24bf

use crate::errors::{ErrorKind, Result};
use crate::one::property::PropertyType;
use crate::one::property::object_reference::ObjectReference;
use crate::one::property::object_space_reference::ObjectSpaceReference;
use crate::fsshttpb::data::cell_id::CellId;
use crate::fsshttpb::data::exguid::ExGuid;
use crate::onestore::{MappingTable, Object};
use crate::onestore::shared::compact_id::CompactId;
use crate::onestore::shared::jcid::JcId;
use crate::onestore::shared::object_prop_set::ObjectPropSet;
use crate::onestore::shared::prop_set::PropertySet;
use crate::onestore::shared::property::PropertyId;
use enum_primitive_derive::Primitive;
use num_traits::FromPrimitive;
use std::rc::Rc;

pub(crate) mod embedded_file_container;
pub(crate) mod embedded_file_node;
pub(crate) mod embedded_ink_container;
pub(crate) mod iframe_node;
pub(crate) mod image_node;
pub(crate) mod math_inline_object;
pub(crate) mod note_tag_container;
pub(crate) mod note_tag_shared_definition_container;
pub(crate) mod number_list_node;
pub(crate) mod outline_element_node;
pub(crate) mod outline_group;
pub(crate) mod outline_node;
pub(crate) mod page_manifest_node;
pub(crate) mod page_metadata;
pub(crate) mod page_node;
pub(crate) mod page_series_node;
pub(crate) mod paragraph_style_object;
pub(crate) mod picture_container;
pub(crate) mod rich_text_node;
pub(crate) mod section_metadata_node;
pub(crate) mod section_node;
pub(crate) mod table_cell_node;
pub(crate) mod table_node;
pub(crate) mod table_row_node;
pub(crate) mod text_run_data;
pub(crate) mod title_node;
pub(crate) mod toc_container;

#[derive(Debug, Copy, Clone, Primitive)]
pub(crate) enum PropertySetId {
    AuthorContainer = 0x00120001,
    EmbeddedFileContainer = 0x00080036,
    EmbeddedFileNode = 0x00060035,
    ImageNode = 0x00060011,
    NoteTagSharedDefinitionContainer = 0x00120043,
    NumberListNode = 0x00060012,
    OutlineElementNode = 0x0006000D,
    OutlineGroup = 0x00060019,
    OutlineNode = 0x0006000C,
    PageManifestNode = 0x00060037,
    PageMetadata = 0x00020030,
    PageNode = 0x0006000B,
    PageSeriesNode = 0x00060008,
    ParagraphStyleObject = 0x0012004D,
    PictureContainer = 0x00080039,
    RevisionMetadata = 0x00020044,
    RichTextNode = 0x0006000E,
    SectionMetadata = 0x00020031,
    SectionNode = 0x00060007,
    TableCellNode = 0x00060024,
    TableNode = 0x00060022,
    TableRowNode = 0x00060023,
    TitleNode = 0x0006002C,
    TocContainer = 0x00020001,
    VersionHistoryMetadata = 0x00020046,
    VersionHistoryContent = 0x0006003C,

    // Undocumented:
    XpsContainer = 0x0008003A,
    InkContainer = 0x00060014,
    InkDataNode = 0x0002003B,
    InkStrokeNode = 0x00020047,
    StrokePropertiesNode = 0x00120048,
    IFrameNode = 0x00060058,

    // Undocumented (handwriting recognition / OCR):
    RecognizedTextRoot = 0x00020054,
    RecognizedTextLine = 0x00020055,
    RecognizedTextBlock = 0x00020056,
    RecognizedTextWord = 0x00020057,
}

impl PropertySetId {
    pub(crate) fn as_jcid(&self) -> JcId {
        JcId(*self as u32)
    }

    pub(crate) fn from_jcid(id: JcId) -> Option<PropertySetId> {
        PropertySetId::from_u32(id.0)
    }
}

pub(crate) fn assert_property_set(object: &Object, expected: PropertySetId) -> Result<()> {
    if object.id() == expected.as_jcid() {
        return Ok(());
    }

    Err(ErrorKind::MalformedOneNoteFileData(
        format!("unexpected object type: 0x{:X}", object.id().0).into(),
    )
    .into())
}

/// Parse every element of a property-values list (`PropertyValues`) into
/// nested objects.
///
/// PATCH(fortress-onenote, P1): the object (and object space) references of
/// the elements are stored back to back in the parent's reference lists, so
/// element *n* starts after the references consumed by elements 0..n. The
/// upstream code started every element at the property's own offset, so
/// e.g. every note tag on a paragraph resolved to the first tag's definition.
/// Each nested object also gets a mapping that translates its local
/// reference indexes back to the parent's (FSSHTTPB mapping tables
/// disambiguate duplicate compact ids by index).
pub(crate) fn parse_object_list(
    object: &Object,
    id: PropertyId,
    prop_type: PropertyType,
    sets: &[PropertySet],
) -> Result<Vec<Object>> {
    let mut object_offset = ObjectReference::get_offset(prop_type, object)?;
    let mut space_offset = ObjectSpaceReference::get_offset(prop_type, object)?;
    let mut out = Vec::with_capacity(sets.len());
    for props in sets {
        let object_count = ObjectReference::count_references(props.values());
        let space_count = ObjectSpaceReference::count_references(props.values());
        out.push(Object {
            context_id: object.context_id,
            jc_id: JcId(id.value()),
            props: ObjectPropSet {
                object_ids: slice_ids(&object.props.object_ids, object_offset, object_count),
                object_space_ids: slice_ids(
                    &object.props.object_space_ids,
                    space_offset,
                    space_count,
                ),
                context_ids: vec![],
                properties: props.clone(),
            },
            file_data: None,
            mapping: Rc::new(OffsetMapping {
                inner: object.mapping.clone(),
                object_offset,
                space_offset,
            }),
        });
        object_offset += object_count;
        space_offset += space_count;
    }
    Ok(out)
}

fn slice_ids(ids: &[CompactId], offset: usize, count: usize) -> Vec<CompactId> {
    ids.iter().skip(offset).take(count).copied().collect()
}

/// Mapping table of a nested object: its reference indexes are relative to
/// its slice of the parent's reference lists.
struct OffsetMapping {
    inner: Rc<dyn MappingTable>,
    object_offset: usize,
    space_offset: usize,
}

impl MappingTable for OffsetMapping {
    fn resolve_id(&self, index: usize, cid: &CompactId) -> Option<ExGuid> {
        self.inner.resolve_id(index + self.object_offset, cid)
    }

    fn get_object_space(&self, index: usize, cid: &CompactId) -> Option<CellId> {
        self.inner.get_object_space(index + self.space_offset, cid)
    }
}
