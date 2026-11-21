// Modified by fortress-onenote (vendor/PATCHES.md: P3, P8, P12).
use crate::errors::{ErrorKind, Result};
use crate::fsshttpb::data::exguid::ExGuid;
use crate::one::property_set::toc_container;
use crate::onenote::file_identity::FileIdentity;
use crate::onestore::ObjectSpace;
use crate::property::common::Color;
use std::collections::HashSet;

/// The table of contents of a notebook or section group (`.onetoc2`),
/// without its sections.
///
/// PATCH(fortress-onenote, P3): lets callers resolve, parse and report the
/// listed entries themselves (e.g. report entries whose file is missing).
#[derive(Clone, Debug)]
pub struct TableOfContents {
    pub(crate) file_identity: FileIdentity,
    pub(crate) entries: Vec<String>,
    pub(crate) color: Option<Color>,
}

impl TableOfContents {
    /// The identity stored in the table-of-contents file.
    pub fn file_identity(&self) -> FileIdentity {
        self.file_identity
    }

    /// The entry names in notebook order, exactly as stored: file names of
    /// sections (`*.one`) and folder names of section groups.
    ///
    /// These strings come from the file and are untrusted: they may contain
    /// path separators, `..` or other characters that are not valid in
    /// file names.
    pub fn entries(&self) -> &[String] {
        &self.entries
    }

    /// The color of the notebook or section group.
    pub fn color(&self) -> Option<Color> {
        self.color
    }
}

struct TocEntry {
    entries: Vec<(u32, String)>,
    color: Option<Color>,
}

pub(crate) fn parse_toc(
    space: &(impl ObjectSpace + ?Sized),
) -> Result<(Vec<String>, Option<Color>)> {
    let content_id = space
        .content_root()
        .ok_or_else(|| ErrorKind::MalformedOneNoteData("notebook has no content root".into()))?;

    let entry = parse_toc_entry(content_id, space, true)?;
    let toc = ordered_toc_entries(entry.entries);

    Ok((toc, entry.color))
}

fn ordered_toc_entries(entries: Vec<(u32, String)>) -> Vec<String> {
    // OneNote can retain older ordering snapshots in the same TOC. Later
    // references to a filename are authoritative.
    let mut seen = HashSet::new();
    let mut latest = entries
        .into_iter()
        .enumerate()
        .rev()
        .filter(|(_, (_, name))| seen.insert(name.clone()))
        .collect::<Vec<_>>();
    latest.sort_by_key(|(source_index, (ordering_id, _))| (*ordering_id, *source_index));
    latest.into_iter().map(|(_, (_, name))| name).collect()
}

fn parse_toc_entry(
    content_id: ExGuid,
    space: &(impl ObjectSpace + ?Sized),
    is_root: bool,
) -> Result<TocEntry> {
    let content = space.get_object(content_id).ok_or_else(|| {
        ErrorKind::MalformedOneNoteData("notebook content root is missing".into())
    })?;

    // PATCH(fortress-onenote, P8): the root container is never itself an
    // entry by the fallback file name (an empty section group would list
    // its own name).
    let toc = toc_container::parse(content, !is_root)?;

    if let Some(name) = toc.filename {
        // PATCH(fortress-onenote, P8): entries without an ordering id keep
        // their position in the parent's list (after ordered entries).
        let ordering_id = toc.ordering_id.unwrap_or(u32::MAX);

        Ok(TocEntry {
            entries: vec![(ordering_id, name)],
            color: toc.color,
        })
    } else {
        let children = toc
            .children
            .into_iter()
            .map(|content_id| parse_toc_entry(content_id, space, false))
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .flat_map(|entry| entry.entries)
            .collect();

        Ok(TocEntry {
            entries: children,
            color: toc.color,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::ordered_toc_entries;

    #[test]
    fn later_toc_snapshot_entries_override_earlier_ordering() {
        let entries = vec![
            (1, "Notes.one".to_owned()),
            (5, "Training".to_owned()),
            (10, "Datasets.one".to_owned()),
            (1, "Notes.one".to_owned()),
            (2, "Datasets.one".to_owned()),
            (3, "Training".to_owned()),
        ];

        assert_eq!(
            ordered_toc_entries(entries),
            ["Notes.one", "Datasets.one", "Training"]
        );
    }
}
