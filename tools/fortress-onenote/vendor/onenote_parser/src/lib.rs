// Modified by fortress-onenote (vendor/PATCHES.md: P3, P9, P10, P11, P12, P14, P15).
//! A OneNote file parser.
//!
//! `onenote_parser` provides a high-level API to parse OneNote notebooks and
//! inspect sections, pages, and their contents. It implements the underlying
//! OneNote file format layers (FSSHTTPB, OneStore, and MS-ONE) and exposes a
//! stable surface for consumers through the [`Parser`] type.
//!
//! The parser supports OneNote files from both OneDrive downloads (FSSHTTP
//! packaging) and desktop OneNote applications (2016, 2019, LTSC, etc.).
//! It is **read-only** and does _not_ support writing or modifying OneNote files.
//!
//! # Usage
//!
//! ```no_run
//! use onenote_parser::Parser;
//! use typed_path::TypedPath;
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let parser = Parser::new();
//! let section = parser.parse_section(TypedPath::derive("Notes.one"))?;
//! println!("page series: {}", section.page_series().len());
//! # Ok(())
//! # }
//! ```
//!
//! PATCH(fortress-onenote, P12): this vendored copy only reads native files
//! (`Parser::parse_section`, `Parser::parse_toc`); the `FileSystem`
//! abstraction, notebook / section-group / `.onepkg` parsing and the
//! upstream features (`native-fs`, `backtrace`, `onepkg`) are removed.
//!
//! # Architecture
//!
//! The code organization and architecture follows the OneNote file format which is
//! built from several layers of encodings:
//!
//! - `fsshttpb/`: This implements the FSSHTTP binary packaging format as specified
//!   in [\[MS-FSSHTTPB\]: Binary Requests for File Synchronization via SOAP Protocol].
//!   This is the packaging format used for files downloaded from OneDrive.
//! - `onestore/`: This implements the OneStore format as specified in
//!   [\[MS-ONESTORE\]: OneNote Revision Store File Format]. This layer handles the
//!   revision store containing all OneNote objects. It supports both the desktop
//!   file format (where the revision store is the file itself) and the FSSHTTP
//!   format (where the store is built from objects and revisions inside the package).
//! - `one/`: This implements the OneNote file format as specified in [\[MS-ONE\]:
//!   OneNote File Format]. This specifies how objects in a OneNote file are parsed
//!   from a OneStore revision file.
//! - `onenote/`: high-level API that resolves references between objects
//!
//! # Error handling
//!
//! Most fallible APIs return [`errors::Result`], which wraps an [`errors::Error`]
//! containing an error kind that can be formatted for user-facing messages.
//!
//! # Input files
//!
//! The parser supports the following OneNote file formats:
//!
//! - **`.one`** – Section files containing the actual notes and content.
//! - **`.onetoc2`** – Table of contents files used to organize sections within a notebook.
//!
//! These files can be obtained from:
//! - **OneNote Desktop** (2016, 2019, LTSC, etc.)
//! - **OneDrive** (via the "Download Notebook" feature)
//! - **OneNote for Windows 10/11** (via `.one` export)
//! - **OneNote for Mac** (as backup files)
//!
//! # I/O behaviour
//!
//! The file is read on demand via positional reads (`pread` on Unix,
//! overlapped `ReadFile` on Windows), with a small page cache in front.
//! Attachments returned by [`contents::Image`] / [`contents::EmbeddedFile`]
//! hold a refcount-shared reference to the underlying source and pull
//! bytes through the same lazy path when their reader is consumed.
//! Modifying the file while a parse is in progress — or while any derived
//! attachment is alive — is unsupported.
//!
//! # Stability
//!
//! The public API follows semantic versioning and is intended to be stable.
//!
//! **Minimum Supported Rust Version (MSRV):** 1.85
//!
//! # References
//!
//! - [\[MS-ONESTORE\]: OneNote Revision Store File Format]
//! - [\[MS-ONE\]: OneNote File Format]
//! - [\[MS-FSSHTTPB\]: Binary Requests for File Synchronization via SOAP Protocol]
//!
//! [\[MS-ONESTORE\]: OneNote Revision Store File Format]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-onestore/ae670cd2-4b38-4b24-82d1-87cfb2cc3725
//! [\[MS-ONE\]: OneNote File Format]: https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/73d22548-a613-4350-8c23-07d15576be50
//! [\[MS-FSSHTTPB\]: Binary Requests for File Synchronization via SOAP Protocol]: https://docs.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-fsshttpb/f59fc37d-2232-4b14-baac-25f98e9e7b5a

#![warn(missing_docs)]
#![deny(unused_must_use)]

#[macro_use]
mod macros;

mod debug;
mod errors;
mod fs;
mod fsshttpb;
mod one;
pub(crate) mod onenote;
mod onestore;
mod reader;
mod shared;
mod utils;
pub mod warn;

pub(crate) type Reader<'b> = &'b mut reader::Reader;

pub use crate::onenote::Parser;
pub use crate::onenote::file_identity::FileIdentity;

/// The data that represents a OneNote notebook.
pub mod notebook {
    // PATCH(fortress-onenote, P3, P12)
    pub use crate::onenote::notebook::TableOfContents;
}

/// The data that represents a OneNote section.
pub mod section {
    // PATCH(fortress-onenote, P12)
    pub use crate::onenote::section::Section;
}

/// The data that represents a OneNote page.
pub mod page {
    pub use crate::onenote::page::Page;
    pub use crate::onenote::page_content::PageContent;
    pub use crate::onenote::page_series::PageSeries;
}

/// The data that represents the contents of a OneNote section.
pub mod contents {
    pub use crate::onenote::content::Content;
    // PATCH(fortress-onenote, P11)
    pub use crate::fsshttpb::data::exguid::ExGuid;
    pub use crate::onenote::embedded_file::EmbeddedFile;
    pub use crate::onenote::image::Image;
    // PATCH(fortress-onenote, P10)
    pub use crate::onenote::ink::Ink;
    pub use crate::onenote::list::List;
    pub use crate::onenote::note_tag::NoteTag;
    pub use crate::onenote::outline::{Outline, OutlineElement, OutlineGroup, OutlineItem};
    pub use crate::onenote::picture::Picture;
    pub use crate::onenote::rich_text::{EmbeddedObject, ParagraphStyling, RichText, TextHyperlink};
    pub use crate::onenote::table::{Table, TableCell, TableRow};
    pub use crate::onestore::shared::file_blob::FileDataStatus;
}

/// Collection of properties used by the OneNote file format.
pub mod property {
    /// Properties related to multiple types of objects.
    pub mod common {
        pub use crate::one::property::color::Color;
        pub use crate::one::property::color_ref::ColorRef;
    }

    /// Properties related to embedded files.
    pub mod embedded_file {
        pub use crate::one::property::file_type::FileType;
    }

    /// Properties related to note tags.
    pub mod note_tag {
        pub use crate::one::property::note_tag::ActionItemStatus;
        pub use crate::one::property::note_tag_shape::NoteTagShape;
        pub use crate::onenote::note_tag::NoteTagDefinition;
    }

    /// Properties related to rich-text content.
    pub mod rich_text {
        pub use crate::one::property::paragraph_alignment::ParagraphAlignment;
        pub use crate::onenote::rich_text::ParagraphStyling;
        // PATCH(fortress-onenote, P9)
        pub use crate::onenote::rich_text::StoredFlags;
    }
}
