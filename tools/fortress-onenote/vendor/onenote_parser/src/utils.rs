// Modified by fortress-onenote (vendor/PATCHES.md: P6).
use crate::errors::Result;
use widestring::U16CString;

pub(crate) trait Utf16ToString {
    fn utf16_to_string(&self) -> Result<String>;
}

impl Utf16ToString for &[u8] {
    fn utf16_to_string(&self) -> Result<String> {
        let data: Vec<_> = self
            .chunks_exact(2)
            .map(|v| u16::from_le_bytes([v[0], v[1]]))
            .collect();

        // PATCH(fortress-onenote, P6): unpaired surrogates (written by
        // some third-party tools) become U+FFFD instead of failing the
        // whole section.
        let value = U16CString::from_vec_truncate(data);
        Ok(value.to_string_lossy())
    }
}
