// Modified by fortress-onenote (vendor/PATCHES.md: P12).
//! File system access used by the OneNote parser.

use bytes::Bytes;

use std::io::Error;

pub(crate) mod file_source;
mod native_fs;

// PATCH(fortress-onenote, P12): files are only opened from the native file
// system; the `FileSystem` trait and its other operations are gone.
pub(crate) use native_fs::open_file;

/// A random-access byte source backing a parse.
///
/// The parser reads notebook data through this trait. Reads take an
/// absolute `offset` and may happen in any order; the trait holds no
/// position of its own.
///
/// Implementations are `Send + Sync` and the returned [`Bytes`] are too.
pub(crate) trait FileSource: Send + Sync {
    /// Total length of the underlying source in bytes.
    fn byte_length(&self) -> u64;

    /// Read `len` bytes starting at absolute `offset`.
    ///
    /// For in-memory backings, return a refcount-shared slice
    /// (`Bytes::slice`); for backings that fetch on demand, allocate.
    fn read_at(&self, offset: u64, len: usize) -> Result<Bytes, Error>;

    /// Return the entire source as a refcount-shared buffer when it is
    /// fully held in memory.
    ///
    /// The parser uses this as a fast path for hot per-byte indexing.
    /// Return `None` if the bytes aren't all resident; the parser will
    /// fall back to [`read_at`](FileSource::read_at).
    fn as_bytes(&self) -> Option<Bytes> {
        None
    }
}
