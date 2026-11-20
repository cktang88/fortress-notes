// Modified by fortress-onenote (vendor/PATCHES.md: C3).
//! A library for reading/writing [Windows
//! cabinet](https://en.wikipedia.org/wiki/Cabinet_(file_format)) (CAB) files.
//!
//! # Overview
//!
//! CAB is an archive file format used by Windows.  A cabinet file can contain
//! multiple compressed files, which are divided into "folders" (no relation to
//! filesystem folders/directories); files in the same folder are compressed
//! together, and each folder in the cabinet can potentially use a different
//! compression scheme.  The CAB file format supports multiple different
//! compression schemes; this library can recognize all of them when reading
//! metadata for an existing cabinet file, but currently only supports
//! encoding/decoding some of them, as shown:
//!
//! | Compression                | Supported         |
//! |----------------------------|-------------------|
//! | Uncompressed               | Yes               |
//! | MSZIP ([Deflate][deflate]) | Yes               |
//! | [Quantum][quantum]         | No                |
//! | [LZX][lzx]                 | Yes (decode only) |
//!
//! [deflate]: https://en.wikipedia.org/wiki/DEFLATE
//! [quantum]: https://en.wikipedia.org/wiki/Quantum_compression
//! [lzx]: https://en.wikipedia.org/wiki/LZX_(algorithm)
//!
//! # Example usage
//!
//! Use the `Cabinet` type to read an existing cabinet file:
//!
//! ```no_run
//! use cab;
//! use std::fs;
//! use std::io;
//!
//! let cab_file = fs::File::open("path/to/cabinet.cab").unwrap();
//! let mut cabinet = cab::Cabinet::new(cab_file).unwrap();
//! // List all files in the cabinet, with file sizes and compression types:
//! for folder in cabinet.folder_entries() {
//!     for file in folder.file_entries() {
//!         println!("File {} ({} B) is compressed with {:?}",
//!                  file.name(),
//!                  file.uncompressed_size(),
//!                  folder.compression_type());
//!     }
//! }
//! // Decompress a particular file in the cabinet and save it to disk:
//! let mut reader = cabinet.read_file("images/example.png").unwrap();
//! let mut writer = fs::File::create("out/example.png").unwrap();
//! io::copy(&mut reader, &mut writer).unwrap();
//! ```
//!
//! PATCH(fortress-onenote, C3): the writer (`CabinetBuilder`, `builder.rs`)
//! was removed; this copy only reads cabinets.

#![warn(missing_docs)]

pub use lzxd::WindowSize;

pub use cabinet::Cabinet;
pub use ctype::CompressionType;
pub use file::{FileEntries, FileEntry, FileReader};
pub use folder::{FolderEntries, FolderEntry};

#[macro_use]
mod macros;

mod cabinet;
mod checksum;
mod consts;
mod ctype;
mod datetime;
mod file;
mod folder;
mod mszip;
mod string;
