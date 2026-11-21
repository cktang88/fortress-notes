// Modified by fortress-onenote (vendor/PATCHES.md: P3, P7, P10, P12).
#![deny(clippy::disallowed_macros)]

use crate::errors::{ErrorKind, Result};
use crate::fs::FileSource;
use crate::fs::open_file;
use crate::fsshttpb::packaging::{OneStorePackaging, embedded_packaging_offset};
use crate::onenote::file_identity::FileIdentity;
use crate::onenote::notebook::TableOfContents;
use crate::onenote::section::Section;
use crate::onestore::desktop::one_store_file::RevisionStore;
use crate::onestore::desktop::parse::Parse;
use crate::onestore::fsshttpb::parse_store;
use crate::onestore::{ObjectSpace, OneStore, OneStoreType};
use crate::reader::Reader;
use crate::shared::guid::Guid;
use crate::warn::Report;
use std::sync::Arc;
use typed_path::TypedPath;
use uuid::Uuid;

pub(crate) mod content;
pub(crate) mod embedded_file;
pub(crate) mod file_identity;
pub(crate) mod iframe;
pub(crate) mod image;
pub(crate) mod ink;
pub(crate) mod list;
pub(crate) mod note_tag;
pub(crate) mod notebook;
pub(crate) mod outline;
pub(crate) mod page;
pub(crate) mod page_content;
pub(crate) mod page_series;
pub(crate) mod picture;
pub(crate) mod rich_text;
pub(crate) mod section;
pub(crate) mod table;

pub(crate) struct ParserContext {
    pub(crate) page: Option<(Uuid, String)>,
    pub(crate) report: Report,
}

/// PATCH(fortress-onenote, P7): keep the result of parsing one child object
/// (page, outline item, content, list, title); a damaged object is dropped
/// with a warning on the section report so the rest of the page and section
/// survive. Encryption is never swallowed.
pub(crate) fn skip_damaged<T>(
    ctx: &mut ParserContext,
    what: &str,
    result: Result<T>,
) -> Result<Option<T>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(err) if matches!(err.kind(), ErrorKind::Encrypted) => Err(err),
        Err(err) => {
            warn!(ctx, "skipped a damaged {what}: {err}");
            Ok(None)
        }
    }
}

/// The OneNote file parser.
///
/// Use [`Parser::parse_toc`] to read a `.onetoc2` table of contents or
/// [`Parser::parse_section`] to load a single `.one` section. These methods
/// auto-detect OneNote 2016 (desktop) and OneDrive (FSSHTTP) formats and will
/// return an error if the input is not the expected file type.
///
/// PATCH(fortress-onenote, P12): only native files; no notebook, section
/// group or package parsing (the converter resolves and parses the entries
/// of a table of contents itself).
///
/// # Thread safety
///
/// The parser is stateless and can be shared across threads.
#[derive(Default)]
pub struct Parser;

impl Parser {
    /// Create a new OneNote file parser.
    ///
    /// The parser holds no state; reuse a single instance across multiple
    /// parses if desired.
    pub fn new() -> Parser {
        Parser
    }

    /// Parse only the table of contents of a notebook or section group.
    ///
    /// The `path` argument must point to a `.onetoc2` file. No section is
    /// read: the entries are returned as stored (see
    /// [`TableOfContents::entries`]).
    ///
    /// PATCH(fortress-onenote, P3).
    pub fn parse_toc(&self, path: TypedPath) -> Result<TableOfContents> {
        let source = open_file(path)?;
        let store = parse_store_auto(source)?;

        if store.get_type()? != OneStoreType::TableOfContents {
            return Err(ErrorKind::NotATocFile {
                file: path.to_string_lossy().to_string(),
            }
            .into());
        }

        let (entries, color) = notebook::parse_toc(store.data_root())?;
        Ok(TableOfContents {
            file_identity: FileIdentity::new(store.as_onestore().file_identity()),
            entries,
            color,
        })
    }

    /// Parse a OneNote section file.
    ///
    /// The `path` argument must point to a `.one` file that contains a
    /// OneNote section. The returned [`Section`] carries any non-fatal warnings
    /// encountered while parsing, reachable via [`Section::report`].
    ///
    /// Returns [`ErrorKind::NotASectionFile`] if the file does not contain a
    /// section.
    ///
    /// The file is read on demand; mutating it while a parse is in
    /// progress, or while a derived attachment is alive, is unsupported.
    pub fn parse_section(&self, path: TypedPath) -> Result<Section> {
        let source = open_file(path)?;
        let store = parse_store_auto(source)?;

        if store.get_type()? != OneStoreType::Section {
            return Err(ErrorKind::NotASectionFile {
                file: path.to_string_lossy().to_string(),
            }
            .into());
        }

        section::parse_section(store.as_onestore(), file_name_string(path)?)
    }
}

#[derive(Debug)]
enum ParsedStore {
    Desktop(RevisionStore),
    FssHttpB(crate::onestore::fsshttpb::PackagingStore),
}

impl ParsedStore {
    fn get_type(&self) -> Result<OneStoreType> {
        match self {
            ParsedStore::Desktop(store) => store.get_type(),
            ParsedStore::FssHttpB(store) => store.get_type(),
        }
    }

    fn data_root(&self) -> &dyn ObjectSpace {
        match self {
            ParsedStore::Desktop(store) => store.data_root(),
            ParsedStore::FssHttpB(store) => store.data_root(),
        }
    }

    fn as_onestore(&self) -> &dyn OneStore {
        match self {
            ParsedStore::Desktop(store) => store,
            ParsedStore::FssHttpB(store) => store,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StoreFormat {
    Desktop,
    FssHttpB { packaging_offset: usize },
}

fn parse_store_auto(source: Arc<dyn FileSource>) -> Result<ParsedStore> {
    let new_reader = || Reader::from_source(source.clone());
    match sniff_store_format(&source) {
        Some(StoreFormat::FssHttpB { packaging_offset }) => {
            let mut reader = new_reader();
            reader.advance(packaging_offset)?;

            let packaging = OneStorePackaging::parse(&mut reader)?;
            let store = parse_store(&packaging)?;

            Ok(ParsedStore::FssHttpB(store))
        }
        Some(StoreFormat::Desktop) => {
            let mut reader = new_reader();
            let store = RevisionStore::parse(&mut reader)?;
            Ok(ParsedStore::Desktop(store))
        }
        None => {
            let fss_err = match OneStorePackaging::parse(&mut new_reader())
                .and_then(|packaging| parse_store(&packaging))
            {
                Ok(store) => return Ok(ParsedStore::FssHttpB(store)),
                Err(err) => err,
            };

            let mut reader = new_reader();
            match RevisionStore::parse(&mut reader) {
                Ok(store) => Ok(ParsedStore::Desktop(store)),
                Err(_) => Err(fss_err),
            }
        }
    }
}

fn sniff_store_format(source: &Arc<dyn FileSource>) -> Option<StoreFormat> {
    let mut reader = Reader::from_source(source.clone());
    let _file_type = Guid::parse(&mut reader).ok()?;
    let _file = Guid::parse(&mut reader).ok()?;
    let legacy_file_version = Guid::parse(&mut reader).ok()?;
    let file_format = Guid::parse(&mut reader).ok()?;

    let revision_store_format = guid!("109ADD3F-911B-49F5-A5D0-1791EDC8AED8");
    let package_store_format = guid!("638DE92F-A6D4-4BC1-9A36-B3FC2511A5B7");

    if file_format == package_store_format {
        return Some(StoreFormat::FssHttpB {
            packaging_offset: 0,
        });
    }

    if file_format == revision_store_format {
        if legacy_file_version.is_nil() {
            if let Some(packaging_offset) = embedded_packaging_offset(source) {
                return Some(StoreFormat::FssHttpB { packaging_offset });
            }

            return Some(StoreFormat::Desktop);
        }

        return Some(StoreFormat::FssHttpB {
            packaging_offset: 0,
        });
    }

    None
}

fn file_name_string(path: TypedPath) -> Result<String> {
    let name = path.file_name().ok_or_else(|| ErrorKind::InvalidPath {
        message: "path has no file name".into(),
    })?;
    Ok(String::from_utf8_lossy(name).into_owned())
}
