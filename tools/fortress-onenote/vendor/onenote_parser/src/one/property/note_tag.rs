// Modified by fortress-onenote (vendor/PATCHES.md: P15).
use crate::errors::{ErrorKind, Result};
use crate::one::property::PropertyType;
use crate::onestore::Object;

/// The action status of a note tag.
///
/// See [\[MS-ONE\] 2.3.91].
///
/// [\[MS-ONE\] 2.3.91]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/6b516f12-8f47-40b3-9dd4-44c00aac206b
#[derive(Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash, Debug)]
pub struct ActionItemStatus {
    completed: bool,
}

impl ActionItemStatus {
    /// Whether the checkable note tag is completed.
    pub fn completed(&self) -> bool {
        self.completed
    }
}

impl ActionItemStatus {
    pub(crate) fn parse(object: &Object) -> Result<Option<ActionItemStatus>> {
        let value = match object.props.get(PropertyType::ActionItemStatus) {
            Some(value) => value.to_u16().ok_or_else(|| {
                ErrorKind::MalformedOneNoteFileData("action item status is not a u16".into())
            })?,
            None => return Ok(None),
        };

        let completed = value & 0x1 != 0;

        Ok(Some(ActionItemStatus { completed }))
    }
}
