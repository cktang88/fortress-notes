// Modified by fortress-onenote (vendor/PATCHES.md: P10, P13, P14, P15, P16).
//! The properties of OneNote objects that we can parse.
//!
//! See [\[MS-ONE\] 2.1.12] for the list of properties the OneNote file format specifies.
//!
//! [\[MS-ONE\] 2.1.12]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/e9bf7da8-7aab-4668-be5e-e0c421175e3c

pub(crate) mod author;
pub(crate) mod color;
pub(crate) mod color_ref;
pub(crate) mod file_type;
pub(crate) mod note_tag;
pub(crate) mod note_tag_shape;
pub(crate) mod object_reference;
pub(crate) mod object_space_reference;
pub(crate) mod paragraph_alignment;
mod references;
pub(crate) mod simple;
pub(crate) mod time;

// PATCH(fortress-onenote, P16): only the properties the parser reads.
#[repr(u32)]
#[derive(Debug, Copy, Clone, PartialEq)]
#[allow(clippy::enum_clike_unportable_variant)]
pub(crate) enum PropertyType {
    ActionItemStatus = 0x10003470,
    ActionItemType = 0x10003463,
    Author = 0x1C001D75,
    Bold = 0x08001C04,
    ChildGraphSpaceElementNodes = 0x2C001D63,
    ColumnCount = 0x14001D58,
    ContentChildNodes = 0x24001C1F,
    CreationTimeStamp = 0x14001D09,
    DisplayedPageNumber = 0x14003480,
    ElementChildNodes = 0x24001C20,
    EmbeddedFileContainer = 0x20001D9B,
    EmbeddedFileName = 0x1C001D9C,
    FileAncestorIdentityGuid = 0x1C001D95,
    FileIdentityGuid = 0x1C001D94,
    FileLastCodeVersionThatWroteToIt = 0x14001D99,
    FileNameCrc = 0x14001D93,
    Font = 0x1C001C0A,
    FontColor = 0x14001C0C,
    FontSize = 0x10001C0B,
    Hidden = 0x08001E16,
    Highlight = 0x14001C0D,
    Hyperlink = 0x08001E14,
    HyperlinkProtected = 0x08001E19,
    IRecordMedia = 0x14001D24,
    ImageAltText = 0x1C001E58,
    ImageFilename = 0x1C001DD7,
    IsBackground = 0x08001D13,
    IsLayoutSizeSetByUser = 0x08001CBD,
    Italic = 0x08001C05,
    LastModifiedTime = 0x14001D7A,
    LayoutMaxHeight = 0x14001C1C,
    LayoutMaxWidth = 0x14001C1B,
    ListFont = 0x1C001C52,
    ListNodes = 0x24001C26,
    ListRestart = 0x14001CB7,
    MathFormatting = 0x08003401,
    NoteTagCompleted = 0x1400346F,
    NoteTagDefinitionOid = 0x20003488,
    NoteTagHighlightColor = 0x14003465,
    NoteTagLabel = 0x1C003468,
    NoteTagPropertyStatus = 0x14003467,
    NoteTagShape = 0x10003464,
    NoteTagTextColor = 0x14003466,
    NotebookManagementEntityGuid = 0x1C001C30,
    NumberListFormat = 0x1C001C1A,
    OffsetFromParentHoriz = 0x14001C14,
    OffsetFromParentVert = 0x14001C15,
    OutlineElementChildLevel = 0x0C001C03,
    PageHeight = 0x14001C02,
    PageLevel = 0x14001DFF,
    ParagraphAlignment = 0x0C003477,
    ParagraphStyle = 0x2000342C,
    ParagraphStyleId = 0x1C00345A,
    PictureContainer = 0x20001C3F,
    PictureHeight = 0x140034CE,
    PictureWidth = 0x140034CD,
    RichEditTextUnicode = 0x1C001C22,
    RowCount = 0x14001D57,
    SectionDisplayName = 0x1C00349B,
    Strikethrough = 0x08001C07,
    StructureElementChildNodes = 0x24001D5F,
    Subscript = 0x08001C09,
    Superscript = 0x08001C08,
    TableBordersVisible = 0x08001D5E,
    TableColumnWidths = 0x1C001D66,
    TextExtendedAscii = 0x1C003498,
    TextRunData = 0x40003499,
    TextRunDataObject = 0x24003458,
    TextRunFormatting = 0x24001E13,
    TextRunIndex = 0x1C001E12,
    TextRunIsEmbeddedObject = 0x08001E22,
    TopologyCreationTimeStamp = 0x18001C65,
    Underline = 0x08001C06,
    WebPictureContainer14 = 0x200034C8,
    WzHyperlinkUrl = 0x1C001E20,

    // Undocumented:
    TocChildren = 0x24001CF6,
    FolderChildFilename = 0x1C001D6B,
    NotebookElementOrderingId = 0x14001CB9,
    PictureFileExtension = 0x24003424,
    NoteTags = 0x40003489,
    SectionColor = 0x14001CBE,
    CellBackgroundColor = 0x14001E26,
    EmbeddedObjectType = 0x14003457,
    EmbeddedInkSpaceWidth = 0x14001C27,
    EmbeddedInkSpaceHeight = 0x14001C28,
    ImageEmbeddedUrl = 0x1C0035F3,
}
