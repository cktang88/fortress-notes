// Modified by fortress-onenote (vendor/PATCHES.md: P14).
use crate::errors::Result;
use crate::one::property::{PropertyType, simple};
use crate::onestore::Object;

/// An embedded ink handwriting container.
///
/// PATCH(fortress-onenote, P14): only the size of an ink space is read.
#[derive(Debug)]
pub(crate) struct Data {
    pub(crate) space_width: Option<f32>,
    pub(crate) space_height: Option<f32>,
}

impl Data {
    pub(crate) fn parse(object: Object) -> Result<Data> {
        let space_width = simple::parse_f32(PropertyType::EmbeddedInkSpaceWidth, &object)?;
        let space_height = simple::parse_f32(PropertyType::EmbeddedInkSpaceHeight, &object)?;

        Ok(Data {
            space_width,
            space_height,
        })
    }
}
