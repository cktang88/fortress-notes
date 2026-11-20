// Modified by fortress-onenote (vendor/PATCHES.md: P10).
//! PATCH(fortress-onenote, P10): ink is located but not parsed. Strokes,
//! bounding boxes and handwriting recognition were removed; the converter
//! only counts ink drawings (Fortress has no ink).

use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::onestore::ObjectSpace;

/// An ink drawing or handwriting. Its strokes are not parsed.
#[derive(Clone, Debug)]
pub struct Ink;

pub(crate) fn parse_ink(id: ExGuid, space: &(impl ObjectSpace + ?Sized)) -> Result<Ink> {
    space
        .get_object(id)
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("ink container is missing".into()))?;
    Ok(Ink)
}
