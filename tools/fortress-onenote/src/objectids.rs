//! Stable OneNote object ids (ExGUIDs) for page content.
//!
//! Every object in a OneNote revision store has an extended GUID (`{GUID}`
//! plus a counter `n`) that stays the same across revisions: editing a
//! paragraph writes a new revision of the *same* object. Paragraph links
//! (`…&page-id={P}&object-id={GUID}&3C`) name an object by that ExGUID, with
//! `n` in hexadecimal.
//!
//! `onenote_parser` 2.1 resolves these ids internally but does not expose them
//! on its high-level types (`RichText`, `Table`, `Image`, …). Its only public
//! view of them is [`onenote_parser::Parser::dump_onestore`], the `Debug` text
//! of the revision store: every object with its ExGUID, its type and its
//! scalar properties, but without the references between objects. This
//! module parses that text (the crate version is pinned in `Cargo.toml`) and
//! attaches ids to the converted content by matching:
//!
//! - rich text (paragraphs, headings, list items, …): the `RichTextNode` whose
//!   text equals the paragraph text (whitespace-normalised) — unique within the
//!   page's object spaces (all revisions/contexts of the page, so an older
//!   revision of the same object never makes it ambiguous);
//! - the enclosing *outline element* (what OneNote's "Copy Link to Paragraph"
//!   points at): OneNote stamps an `OutlineElementNode` and its content with
//!   the same last-modified time and allocates both from the same GUID in
//!   creation order (verified on the public samples). Within a (GUID, time)
//!   group, equal numbers of elements and content nodes pair up in counter
//!   order; otherwise only a content node allocated right after an element
//!   (`n - 1`) is paired;
//! - files / images: the node whose file name / alt text matches, else the
//!   only node of that type on the page;
//! - tables: the only `TableNode` with that row and column count;
//! - ink: the only ink container on the page.
//!
//! Anything ambiguous gets no id (the importer falls back to content hashes).

use crate::links::normalize_guid;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

/// Files above this size are not dumped (the text dump is several times the
/// size of the property data).
pub const MAX_DUMP_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ExGuid {
    /// `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`, upper case.
    pub guid: String,
    pub n: u32,
}

impl ExGuid {
    /// Canonical manifest form, the same shape OneNote uses in
    /// `object-id={GUID}&N` links: `{GUID}&<n as upper-case hex>`.
    pub fn canonical(&self) -> String {
        format!("{}&{:X}", self.guid, self.n)
    }
}

/// Canonical object id from a link's `object-id={GUID}` value and the bare
/// hexadecimal parameter that follows it (`&3C`).
pub fn object_ref(guid: &str, suffix: Option<&str>) -> Option<String> {
    let g = normalize_guid(guid)?;
    let s = suffix?.trim();
    if s.is_empty() || s.len() > 8 || !s.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let n = u32::from_str_radix(s, 16).ok()?;
    Some(ExGuid { guid: g, n }.canonical())
}

#[derive(Debug, Default, Clone)]
pub struct DumpObject {
    pub id: Option<ExGuid>,
    pub kind: String,
    /// Property id (low 26 bits, as printed) → raw value text.
    pub props: Vec<(u32, String)>,
}

impl DumpObject {
    fn prop(&self, key: u32) -> Option<&str> {
        self.props
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, v)| v.as_str())
    }
}

#[derive(Debug, Default, Clone)]
pub struct DumpSpace {
    pub objects: Vec<DumpObject>,
}

const P_TEXT_UNICODE: u32 = 0x1c22;
const P_TEXT_ASCII: u32 = 0x3498;
const P_ENTITY_GUID: u32 = 0x1c30;
const P_LAST_MODIFIED: u32 = 0x1d7a;
const P_FILE_NAME: u32 = 0x1d9c;
const P_IMAGE_ALT: u32 = 0x1e58;
const P_IMAGE_FILE_NAME: u32 = 0x1dd7;
const P_ROW_COUNT: u32 = 0x1d57;
const P_COL_COUNT: u32 = 0x1d58;

fn parse_exguid(s: &str) -> Option<(ExGuid, &str)> {
    // `ExGuid {{6EBB634E-CA04-4EEF-9B3D-3E948F229FA7}, 10}`
    let rest = s.trim_start().strip_prefix("ExGuid {")?;
    let close = rest.find('}')?;
    let guid = normalize_guid(&rest[..=close])?;
    let rest = rest[close + 1..].strip_prefix(", ")?;
    let end = rest.find('}')?;
    let n = rest[..end].trim().parse().ok()?;
    Some((ExGuid { guid, n }, &rest[end + 1..]))
}

/// Parse the `{:#?}` text of `Parser::dump_onestore` into object spaces.
/// Unknown lines are ignored, so format drift degrades to "no ids".
pub fn parse_dump(text: &str) -> Vec<DumpSpace> {
    let mut spaces: Vec<DumpSpace> = Vec::new();
    let mut cur: Option<DumpObject> = None;
    let mut in_props = false;
    let flush = |cur: &mut Option<DumpObject>, spaces: &mut Vec<DumpSpace>| {
        if let Some(o) = cur.take() {
            if spaces.is_empty() {
                spaces.push(DumpSpace::default());
            }
            spaces.last_mut().expect("space").objects.push(o);
        }
    };
    for line in text.lines() {
        let t = line.trim();
        if t.ends_with("ObjectSpace {") {
            flush(&mut cur, &mut spaces);
            spaces.push(DumpSpace::default());
            in_props = false;
            continue;
        }
        if t.starts_with("ExGuid {")
            && t.ends_with(": Object {")
            && let Some((id, _)) = parse_exguid(t)
        {
            flush(&mut cur, &mut spaces);
            cur = Some(DumpObject {
                id: Some(id),
                ..Default::default()
            });
            in_props = false;
            continue;
        }
        let Some(obj) = cur.as_mut() else { continue };
        if !in_props {
            if let Some(kind) = t.strip_prefix("id: ")
                && obj.kind.is_empty()
            {
                obj.kind = kind.trim_end_matches(',').to_string();
            } else if t == "props: {" {
                in_props = true;
            }
            continue;
        }
        if t == "}," || t == "}" {
            // End of props; the object closes on the next line.
            in_props = false;
            continue;
        }
        if let Some(hex) = t.strip_prefix("0x")
            && let Some(colon) = hex.find(": ")
            && let Ok(key) = u32::from_str_radix(&hex[..colon], 16)
        {
            let value = hex[colon + 2..].trim_end_matches(',');
            obj.props.push((key, value.to_string()));
        }
    }
    flush(&mut cur, &mut spaces);
    spaces
}

/// The trailing `[1, 2, 3]` byte list of a printed property value.
fn bytes_of(value: &str) -> Option<Vec<u8>> {
    let v = value.trim_end_matches(')');
    let open = v.rfind('[')?;
    let close = v[open..].find(']')? + open;
    let inner = v[open + 1..close].trim();
    if inner.is_empty() {
        return Some(Vec::new());
    }
    inner.split(',').map(|b| b.trim().parse().ok()).collect()
}

fn utf16(bytes: &[u8]) -> String {
    let units: Vec<u16> = bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|c| u16::from_le_bytes(*c))
        .take_while(|u| *u != 0)
        .collect();
    String::from_utf16_lossy(&units)
}

fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|b| char::from(*b)).collect()
}

fn u32_of(value: &str) -> Option<u32> {
    let v = value.trim();
    let inner = v
        .strip_prefix("U32(")
        .or_else(|| v.strip_prefix("U16("))
        .or_else(|| v.strip_prefix("U8("))?;
    inner.trim_end_matches(')').parse().ok()
}

/// GUID from the 16 little-endian bytes OneNote stores (`NotebookManagementEntityGuid`).
fn guid_of(value: &str) -> Option<String> {
    let b = bytes_of(value)?;
    if b.len() != 16 {
        return None;
    }
    let d1 = u32::from_le_bytes([b[0], b[1], b[2], b[3]]);
    let d2 = u16::from_le_bytes([b[4], b[5]]);
    let d3 = u16::from_le_bytes([b[6], b[7]]);
    Some(format!(
        "{{{d1:08X}-{d2:04X}-{d3:04X}-{:02X}{:02X}-{}}}",
        b[8],
        b[9],
        hex::encode_upper(&b[10..16])
    ))
}

fn string_prop(o: &DumpObject, key: u32) -> Option<String> {
    o.prop(key).and_then(bytes_of).map(|b| utf16(&b))
}

/// Matching key for text: whitespace-normalised, control characters dropped.
pub fn text_key(s: &str) -> String {
    s.chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

#[derive(Debug, Clone)]
struct Node {
    id: ExGuid,
    kind: String,
    mtime: Option<u32>,
    strings: Vec<String>,
    rows: Option<u32>,
    cols: Option<u32>,
}

/// Ids available for one page (all object spaces/contexts of that page).
#[derive(Debug, Default, Clone)]
pub struct PageIds {
    texts: HashMap<String, BTreeSet<ExGuid>>,
    nodes: Vec<Node>,
    /// Outline elements by GUID → (n, mtime).
    elements: HashMap<String, Vec<(u32, Option<u32>)>>,
    used: HashSet<ExGuid>,
    /// Content node → outline element, built on first use.
    element_map: Option<HashMap<ExGuid, ExGuid>>,
}

/// Object ids assigned to one piece of content.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Ids {
    /// The content node (rich text, table, image, file, ink).
    pub object_id: Option<String>,
    /// The outline element (paragraph) containing it; what paragraph links name.
    pub element_id: Option<String>,
}

impl PageIds {
    fn add(&mut self, o: &DumpObject) {
        let Some(id) = o.id.clone() else { return };
        let mtime = o.prop(P_LAST_MODIFIED).and_then(u32_of);
        match o.kind.as_str() {
            "RichTextNode" => {
                let text = o
                    .prop(P_TEXT_UNICODE)
                    .and_then(bytes_of)
                    .map(|b| utf16(&b))
                    .or_else(|| o.prop(P_TEXT_ASCII).and_then(bytes_of).map(|b| latin1(&b)));
                if let Some(t) = text {
                    let k = text_key(&t);
                    if !k.is_empty() {
                        self.texts.entry(k).or_default().insert(id.clone());
                    }
                }
                self.push_node(id, o, mtime);
            }
            "OutlineElementNode" => {
                let v = self.elements.entry(id.guid.clone()).or_default();
                if !v.iter().any(|(n, _)| *n == id.n) {
                    v.push((id.n, mtime));
                }
            }
            "TableNode" | "ImageNode" | "EmbeddedFileNode" | "InkContainer" => {
                self.push_node(id, o, mtime)
            }
            _ => {}
        }
    }

    fn push_node(&mut self, id: ExGuid, o: &DumpObject, mtime: Option<u32>) {
        if self.nodes.iter().any(|n| n.id == id) {
            return;
        }
        let strings = [P_FILE_NAME, P_IMAGE_ALT, P_IMAGE_FILE_NAME]
            .iter()
            .filter_map(|k| string_prop(o, *k))
            .map(|s| text_key(&s))
            .filter(|s| !s.is_empty())
            .collect();
        self.nodes.push(Node {
            id,
            kind: o.kind.clone(),
            mtime,
            strings,
            rows: o.prop(P_ROW_COUNT).and_then(u32_of),
            cols: o.prop(P_COL_COUNT).and_then(u32_of),
        });
    }

    fn claim(&mut self, id: &ExGuid) -> Option<String> {
        if !self.used.insert(id.clone()) {
            return None;
        }
        Some(id.canonical())
    }

    /// Pair content nodes with their outline elements. OneNote stamps an
    /// outline element and its content with the same last-modified time and
    /// allocates them from the same GUID, in creation order. Within each
    /// (GUID, time) group: equal counts pair up in counter order; otherwise
    /// a content node pairs with the element allocated right before it.
    fn build_elements(&mut self) {
        if self.element_map.is_some() {
            return;
        }
        type Group = (Vec<u32>, Vec<u32>);
        let mut groups: BTreeMap<(String, u32), Group> = BTreeMap::new();
        for (g, v) in &self.elements {
            for (n, t) in v {
                if let Some(t) = t {
                    groups.entry((g.clone(), *t)).or_default().0.push(*n);
                }
            }
        }
        for node in &self.nodes {
            if let Some(t) = node.mtime {
                groups
                    .entry((node.id.guid.clone(), t))
                    .or_default()
                    .1
                    .push(node.id.n);
            }
        }
        let mut map = HashMap::new();
        for ((g, _), (mut oes, mut contents)) in groups {
            oes.sort_unstable();
            contents.sort_unstable();
            let mk = |n: u32| ExGuid { guid: g.clone(), n };
            if oes.len() == contents.len() {
                for (c, e) in contents.iter().zip(&oes) {
                    map.insert(mk(*c), mk(*e));
                }
            } else {
                for c in &contents {
                    if *c > 0 && oes.contains(&(c - 1)) {
                        map.insert(mk(*c), mk(c - 1));
                    }
                }
            }
        }
        self.element_map = Some(map);
    }

    fn element_of(&mut self, id: &ExGuid) -> Option<String> {
        self.build_elements();
        self.element_map.as_ref()?.get(id).map(|e| e.canonical())
    }

    /// Ids for a paragraph with this (raw) text.
    pub fn rich_text(&mut self, text: &str) -> Ids {
        let k = text_key(text);
        let Some(set) = self.texts.get(&k).filter(|s| s.len() == 1) else {
            return Ids::default();
        };
        let id = set.iter().next().expect("one").clone();
        let element_id = self.element_of(&id);
        match self.claim(&id) {
            Some(object_id) => Ids {
                object_id: Some(object_id),
                element_id,
            },
            None => Ids::default(),
        }
    }

    fn unique(&mut self, pred: impl Fn(&Node) -> bool) -> Option<ExGuid> {
        let c: Vec<&Node> = self.nodes.iter().filter(|n| pred(n)).collect();
        match c.as_slice() {
            [n] => Some(n.id.clone()),
            _ => None,
        }
    }

    fn named(&mut self, kind: &str, names: &[Option<&str>]) -> Ids {
        let keys: Vec<String> = names
            .iter()
            .flatten()
            .map(|s| text_key(s))
            .filter(|s| !s.is_empty())
            .collect();
        let id = keys
            .iter()
            .find_map(|k| self.unique(|n| n.kind == kind && n.strings.contains(k)))
            .or_else(|| self.unique(|n| n.kind == kind));
        self.finish(id)
    }

    fn finish(&mut self, id: Option<ExGuid>) -> Ids {
        let Some(id) = id else {
            return Ids::default();
        };
        let element_id = self.element_of(&id);
        match self.claim(&id) {
            Some(o) => Ids {
                object_id: Some(o),
                element_id,
            },
            None => Ids::default(),
        }
    }

    pub fn file(&mut self, filename: &str) -> Ids {
        self.named("EmbeddedFileNode", &[Some(filename)])
    }

    pub fn image(&mut self, alt: Option<&str>, filename: Option<&str>) -> Ids {
        self.named("ImageNode", &[alt, filename])
    }

    pub fn table(&mut self, rows: u32, cols: u32) -> Ids {
        let id = self
            .unique(|n| n.kind == "TableNode" && n.rows == Some(rows) && n.cols == Some(cols))
            .or_else(|| self.unique(|n| n.kind == "TableNode"));
        self.finish(id)
    }

    pub fn ink(&mut self) -> Ids {
        let id = self.unique(|n| n.kind == "InkContainer");
        self.finish(id)
    }
}

/// Ids of every page of one section file, keyed by page GUID.
#[derive(Debug, Default)]
pub struct SectionIds {
    pages: HashMap<String, PageIds>,
}

impl SectionIds {
    pub fn from_dump(text: &str) -> SectionIds {
        let mut pages: HashMap<String, PageIds> = HashMap::new();
        for space in parse_dump(text) {
            let is_page = space
                .objects
                .iter()
                .any(|o| o.kind == "PageNode" || o.kind == "PageManifestNode");
            if !is_page {
                continue;
            }
            let guids: BTreeSet<String> = space
                .objects
                .iter()
                .filter(|o| o.kind == "PageMetadata")
                .filter_map(|o| o.prop(P_ENTITY_GUID).and_then(guid_of))
                .collect();
            // A page space carries exactly one page's metadata.
            if guids.len() != 1 {
                continue;
            }
            let page = pages
                .entry(guids.into_iter().next().expect("one"))
                .or_default();
            for o in &space.objects {
                page.add(o);
            }
        }
        SectionIds { pages }
    }

    /// Read and index a section file. `None` if the file is too large or the
    /// store cannot be dumped.
    pub fn from_file(parser: &onenote_parser::Parser, path: &std::path::Path) -> Option<Self> {
        let size = std::fs::metadata(path).ok()?.len();
        if size > MAX_DUMP_BYTES {
            return None;
        }
        let data = std::fs::read(path).ok()?;
        let text = parser.dump_onestore(&data).ok()?;
        Some(Self::from_dump(&text))
    }

    pub fn page(&mut self, page_guid: &str) -> Option<PageIds> {
        let g = normalize_guid(page_guid)?;
        self.pages.remove(&g)
    }

    #[cfg(test)]
    pub fn page_count(&self) -> usize {
        self.pages.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn u16s(s: &str) -> String {
        let mut b: Vec<String> = s
            .encode_utf16()
            .flat_map(|u| u.to_le_bytes())
            .map(|x| x.to_string())
            .collect();
        b.push("0".into());
        b.push("0".into());
        format!("{s:?} ([{}])", b.join(", "))
    }

    // Page GUID {2B1B6B5D-1234-4A5B-9C8D-0123456789AB} as stored (LE).
    const PAGE_BYTES: &str =
        "[93, 107, 27, 43, 52, 18, 91, 74, 156, 141, 1, 35, 69, 103, 137, 171]";
    const PAGE: &str = "{2B1B6B5D-1234-4A5B-9C8D-0123456789AB}";
    const G: &str = "072D2464-6721-4399-AD2D-12367E397AFD";

    fn obj(n: u32, kind: &str, props: &[(u32, String)]) -> String {
        let mut s = format!(
            "                ExGuid {{{{{G}}}, {n}}}: Object {{\n                    id: {kind},\n                    props: {{\n"
        );
        for (k, v) in props {
            s.push_str(&format!("                        {k:#x}: {v},\n"));
        }
        s.push_str("                    },\n                },\n");
        s
    }

    fn sample(texts: &[(u32, &str)], extra: &str) -> String {
        let mut s = String::from(
            "Desktop(\n    OneStoreFile(RootFileNodeList {\n        object_spaces: [\n            ObjectSpace {\n                id: ExGuid {{E51C77BD-6332-4F0D-8B0B-A4DDC0800617}, 1},\n                objects: {\n",
        );
        s.push_str(&obj(
            11,
            "PageMetadata",
            &[(P_ENTITY_GUID, PAGE_BYTES.to_string())],
        ));
        s.push_str(&obj(14, "PageNode", &[]));
        for (n, t) in texts {
            s.push_str(&obj(
                *n - 1,
                "OutlineElementNode",
                &[(P_LAST_MODIFIED, "U32(1461430680)".into())],
            ));
            s.push_str(&obj(
                *n,
                "RichTextNode",
                &[
                    (P_LAST_MODIFIED, "U32(1461430680)".into()),
                    (P_TEXT_UNICODE, u16s(t)),
                ],
            ));
        }
        s.push_str(extra);
        s.push_str("                },\n            },\n        ],\n    }),\n)\n");
        s
    }

    #[test]
    fn parses_exguid_and_guid_bytes() {
        let (id, _) = parse_exguid(&format!("ExGuid {{{{{G}}}, 31}}: Object {{")).unwrap();
        assert_eq!(id.n, 31);
        assert_eq!(id.canonical(), format!("{{{G}}}&1F"));
        assert_eq!(guid_of(PAGE_BYTES).as_deref(), Some(PAGE));
    }

    #[test]
    fn link_object_ref_matches_canonical_id() {
        assert_eq!(
            object_ref(&format!("{{{}}}", G.to_lowercase()), Some("1f")).as_deref(),
            Some(format!("{{{G}}}&1F").as_str())
        );
        assert_eq!(object_ref("{nope}", Some("1F")), None);
        assert_eq!(object_ref(&format!("{{{G}}}"), Some("end")), None);
        assert_eq!(object_ref(&format!("{{{G}}}"), None), None);
    }

    #[test]
    fn assigns_rich_text_and_element_ids() {
        let d = sample(
            &[
                (19, "Hello  world"),
                (32, "Second"),
                (35, "dup"),
                (38, "dup"),
            ],
            "",
        );
        let mut s = SectionIds::from_dump(&d);
        assert_eq!(s.page_count(), 1);
        let mut p = s.page(&PAGE.to_lowercase()).unwrap();
        let ids = p.rich_text("Hello world");
        assert_eq!(ids.object_id, Some(format!("{{{G}}}&13")));
        assert_eq!(ids.element_id, Some(format!("{{{G}}}&12")));
        assert_eq!(p.rich_text("Second").object_id, Some(format!("{{{G}}}&20")));
        // Duplicate text is ambiguous → no id; unknown text → no id.
        assert_eq!(p.rich_text("dup"), Ids::default());
        assert_eq!(p.rich_text("missing"), Ids::default());
        // An id is never handed out twice.
        assert_eq!(p.rich_text("Second"), Ids::default());
    }

    #[test]
    fn elements_pair_by_modification_time() {
        // Element 22 ↔ text 26 (t=1), element 31 ↔ text 41 (t=2): not adjacent.
        let rt = |n: u32, t: u32, s: &str| {
            obj(
                n,
                "RichTextNode",
                &[
                    (P_LAST_MODIFIED, format!("U32({t})")),
                    (P_TEXT_UNICODE, u16s(s)),
                ],
            )
        };
        let oe = |n: u32, t: u32| {
            obj(
                n,
                "OutlineElementNode",
                &[(P_LAST_MODIFIED, format!("U32({t})"))],
            )
        };
        let extra = oe(22, 1) + &oe(31, 2) + &rt(26, 1, "first") + &rt(41, 2, "second");
        let mut p = SectionIds::from_dump(&sample(&[], &extra))
            .page(PAGE)
            .unwrap();
        assert_eq!(
            p.rich_text("second").element_id,
            Some(format!("{{{G}}}&1F"))
        );
        assert_eq!(p.rich_text("first").element_id, Some(format!("{{{G}}}&16")));
    }

    #[test]
    fn edited_paragraph_keeps_its_id() {
        // Same object (n = 19) in a later revision with new text.
        let before = sample(&[(19, "Draft text"), (32, "Other")], "");
        let after = sample(&[(19, "Final text, edited"), (32, "Other")], "");
        let a = SectionIds::from_dump(&before)
            .page(PAGE)
            .unwrap()
            .rich_text("Draft text");
        let b = SectionIds::from_dump(&after)
            .page(PAGE)
            .unwrap()
            .rich_text("Final text, edited");
        assert!(a.object_id.is_some());
        assert_eq!(a, b);
    }

    #[test]
    fn files_images_tables() {
        let extra = obj(50, "EmbeddedFileNode", &[(P_FILE_NAME, u16s("report.pdf"))])
            + &obj(52, "EmbeddedFileNode", &[(P_FILE_NAME, u16s("other.docx"))])
            + &obj(60, "ImageNode", &[(P_IMAGE_ALT, u16s("A cat"))])
            + &obj(
                70,
                "TableNode",
                &[
                    (P_ROW_COUNT, "U32(2)".into()),
                    (P_COL_COUNT, "U32(3)".into()),
                ],
            )
            + &obj(80, "InkContainer", &[]);
        let mut p = SectionIds::from_dump(&sample(&[], &extra))
            .page(PAGE)
            .unwrap();
        assert_eq!(p.file("report.pdf").object_id, Some(format!("{{{G}}}&32")));
        assert_eq!(
            p.image(Some("A  cat"), None).object_id,
            Some(format!("{{{G}}}&3C"))
        );
        assert_eq!(p.table(2, 3).object_id, Some(format!("{{{G}}}&46")));
        assert_eq!(p.ink().object_id, Some(format!("{{{G}}}&50")));
    }

    #[test]
    fn spaces_without_one_page_are_ignored() {
        let d = "Desktop(\n    ObjectSpace {\n        objects: {\n";
        assert_eq!(SectionIds::from_dump(d).page_count(), 0);
        assert!(parse_dump("garbage\n{\n").is_empty() || parse_dump("garbage").is_empty());
    }
}
