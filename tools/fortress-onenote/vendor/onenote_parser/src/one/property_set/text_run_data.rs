// Modified by fortress-onenote (vendor/PATCHES.md: P1).
use crate::errors::{ErrorKind, Result};
use crate::one::property::PropertyType;
use crate::one::property_set::parse_object_list;
use crate::onestore::Object;

pub(crate) fn parse(object: &Object) -> Result<Option<Vec<Object>>> {
    let (prop_id, prop_sets) = match object.props.get(PropertyType::TextRunData) {
        Some(value) => value.to_property_values().ok_or_else(|| {
            ErrorKind::MalformedOneNoteFileData(
                "text run data is not a property values list".into(),
            )
        })?,
        None => return Ok(None),
    };

    // PATCH(fortress-onenote, P1): each run has its own references.
    let data = parse_object_list(object, prop_id, PropertyType::TextRunData, prop_sets)?;

    Ok(Some(data))
}
