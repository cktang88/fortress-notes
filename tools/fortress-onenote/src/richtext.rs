//! Rich text runs -> plain text + mark spans (code-point offsets).

use crate::links::{is_onenote_link, parse_onenote_link};
use crate::manifest::{Mark, MarkKind, Rich};
use onenote_parser::contents::RichText;
use onenote_parser::property::common::ColorRef;
use onenote_parser::property::rich_text::ParagraphStyling;
use regex::Regex;
use std::sync::LazyLock;

/// OneNote's default body font.
pub const DEFAULT_FONT: &str = "Calibri";
/// OneNote's default body size, points.
pub const DEFAULT_FONT_SIZE_PT: f32 = 11.0;

const MONOSPACE_FONTS: &[&str] = &[
    "consolas",
    "courier",
    "courier new",
    "lucida console",
    "lucida sans typewriter",
    "menlo",
    "monaco",
    "source code pro",
    "cascadia code",
    "cascadia mono",
    "fira code",
    "fira mono",
    "jetbrains mono",
    "sf mono",
    "dejavu sans mono",
    "liberation mono",
    "andale mono",
    "monospace",
];

pub fn is_monospace(font: &str) -> bool {
    let f = font.trim().trim_matches('"').to_ascii_lowercase();
    MONOSPACE_FONTS.iter().any(|m| f == *m || f.starts_with(m))
}

pub fn color_hex(c: Option<ColorRef>) -> Option<String> {
    match c? {
        ColorRef::Auto => None,
        ColorRef::Manual { r, g, b } => Some(format!("#{r:02X}{g:02X}{b:02X}")),
    }
}

/// Formatting of one text run, extracted from `ParagraphStyling` so the
/// offset logic can be unit-tested without parser objects.
#[derive(Debug, Clone, Default)]
pub struct RunFmt {
    pub bold: bool,
    pub italic: bool,
    pub underline: bool,
    pub strikethrough: bool,
    pub superscript: bool,
    pub subscript: bool,
    pub hidden: bool,
    pub math: bool,
    /// Visible display text of a hyperlink field.
    pub hyperlink: bool,
    pub font: Option<String>,
    /// Points.
    pub size_pt: Option<f32>,
    pub color: Option<String>,
    pub highlight: Option<String>,
}

impl RunFmt {
    pub fn from_styling(st: &ParagraphStyling) -> Self {
        RunFmt {
            bold: st.bold(),
            italic: st.italic(),
            underline: st.underline(),
            strikethrough: st.strikethrough(),
            superscript: st.superscript(),
            subscript: st.subscript(),
            hidden: st.hidden(),
            math: st.math_formatting(),
            hyperlink: st.hyperlink_protected(),
            font: st.font().map(str::to_string),
            size_pt: st.font_size().map(|s| s as f32 / 2.0),
            color: color_hex(st.font_color()),
            highlight: color_hex(st.highlight()),
        }
    }

    /// This run's formatting with every character property the run does not
    /// store (`stored`) taken from `base`, the paragraph style's formatting.
    /// Run-only flags (hidden, math, hyperlink) are never inherited.
    pub fn inherit(self, stored: Stored, base: &RunFmt) -> RunFmt {
        let pick = |own: bool, set: bool, inherited: bool| if set { own } else { inherited };
        RunFmt {
            bold: pick(self.bold, stored.bold, base.bold),
            italic: pick(self.italic, stored.italic, base.italic),
            underline: pick(self.underline, stored.underline, base.underline),
            strikethrough: pick(self.strikethrough, stored.strikethrough, base.strikethrough),
            superscript: pick(self.superscript, stored.superscript, base.superscript),
            subscript: pick(self.subscript, stored.subscript, base.subscript),
            font: if stored.font {
                self.font
            } else {
                base.font.clone()
            },
            size_pt: if stored.size {
                self.size_pt
            } else {
                base.size_pt
            },
            color: if stored.color {
                self.color
            } else {
                base.color.clone()
            },
            highlight: if stored.highlight {
                self.highlight
            } else {
                base.highlight.clone()
            },
            ..self
        }
    }
}

/// Which character properties a text run stores itself; the others inherit
/// from the paragraph style. An explicit "off" (`bold = false`, automatic
/// colour) counts as stored and overrides the paragraph style.
#[derive(Debug, Clone, Copy, Default)]
pub struct Stored {
    pub bold: bool,
    pub italic: bool,
    pub underline: bool,
    pub strikethrough: bool,
    pub superscript: bool,
    pub subscript: bool,
    pub font: bool,
    pub size: bool,
    pub color: bool,
    pub highlight: bool,
}

impl Stored {
    pub fn of(st: &ParagraphStyling) -> Self {
        let f = st.stored_flags();
        Stored {
            bold: f.bold.is_some(),
            italic: f.italic.is_some(),
            underline: f.underline.is_some(),
            strikethrough: f.strikethrough.is_some(),
            superscript: f.superscript.is_some(),
            subscript: f.subscript.is_some(),
            font: st.font().is_some(),
            size: st.font_size().is_some(),
            color: st.font_color().is_some(),
            highlight: st.highlight().is_some(),
        }
    }
}

/// OneNote's built-in paragraph styles (Normal, Heading 1–6, Page Title,
/// Citation, Quote, Code), compared case-insensitively. Their look is fixed
/// by OneNote and expressed by the block type (plain paragraph, heading,
/// quote, code), so their font, size, colour and weight are not run marks.
const BUILTIN_STYLES: &[&str] = &[
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pagetitle",
    "cite",
    "blockquote",
    "quote",
    "code",
];

/// Whether text runs inherit the character formatting of a paragraph style
/// with this style id: every style except OneNote's built-in ones.
///
/// OneNote stores a paragraph's base formatting in its paragraph style and
/// only the differences in its text runs, so formatting applied to a whole
/// paragraph (by a custom style or a third-party writer) can be in the
/// paragraph style alone.
pub fn inherits_paragraph_style(style_id: Option<&str>) -> bool {
    !style_id.is_some_and(|id| BUILTIN_STYLES.iter().any(|b| id.eq_ignore_ascii_case(b)))
}

/// A stored hyperlink in UTF-16 offsets (as the parser reports them).
pub struct Link16 {
    pub target: String,
    pub start: usize,
    pub end: usize,
}

/// Result of converting one paragraph.
#[derive(Debug, Default)]
pub struct Converted {
    pub rich: Rich,
    /// Every visible non-whitespace character is in a monospace font.
    pub all_monospace: bool,
    /// Every visible non-whitespace run is math (display equation).
    pub all_math: bool,
    /// Equations (maximal runs of math-formatted text) in the paragraph.
    pub equations: usize,
    pub links_web: usize,
    pub links_onenote: usize,
    /// Human-readable notes about lossy conversions.
    pub notes: Vec<String>,
}

#[derive(Clone, Copy, Default)]
pub struct Options {
    /// Suppress font family/size marks (headings, titles).
    pub suppress_fonts: bool,
}

/// Convert a parser `RichText` paragraph.
pub fn convert(rt: &RichText, opts: Options) -> Converted {
    let indices: Vec<usize> = rt.text_run_indices().iter().map(|i| *i as usize).collect();
    let ps = rt.paragraph_style();
    let base = inherits_paragraph_style(ps.style_id()).then(|| RunFmt::from_styling(ps));
    let mut fmts: Vec<Option<RunFmt>> = rt
        .text_run_formatting()
        .iter()
        .map(|s| {
            let own = RunFmt::from_styling(s);
            Some(match &base {
                Some(base) => own.inherit(Stored::of(s), base),
                None => own,
            })
        })
        .collect();
    if indices.is_empty() && fmts.is_empty() {
        fmts.push(Some(RunFmt::from_styling(rt.paragraph_style())));
    }
    let links: Vec<Link16> = rt
        .hyperlinks()
        .iter()
        .map(|l| Link16 {
            target: l.target().to_string(),
            start: l.start() as usize,
            end: l.end() as usize,
        })
        .collect();
    // Equations stay OneNote's plain linear text (Fortress has no math).
    convert_runs(rt.text(), &indices, &fmts, &links, opts)
}

/// Characters that are never text: C0/C1 control characters other than tab
/// and line feed (NUL, BEL, ESC, form feed…) and the noncharacters
/// U+xFFFE/U+xFFFF become U+FFFD, so the text is safe for XML, HTML and
/// databases. One character is replaced by one, so offsets are unchanged.
/// Bidirectional controls (U+202A–U+202E, U+2066–U+2069) are kept: they are
/// legitimate in right-to-left text.
pub fn clean_char(c: char) -> char {
    let nonchar = (c as u32) & 0xFFFE == 0xFFFE;
    if (c.is_control() && c != '\t' && c != '\n') || nonchar {
        '\u{FFFD}'
    } else {
        c
    }
}

/// UTF-16 [start, end) of each run. Run k ends at `indices[k]`; the last run
/// extends to the end of the text.
fn run_bounds(indices: &[usize], len16: usize) -> Vec<(usize, usize)> {
    let mut out = Vec::with_capacity(indices.len() + 1);
    let mut start = 0usize;
    for end in indices.iter().map(|i| (*i).min(len16)).chain([len16]) {
        let end = end.max(start);
        out.push((start, end));
        start = end;
    }
    out
}

static FIELD_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new("\u{FDDF}HYPERLINK\\s+\"([^\"]*)\"").expect("valid field regex"));

static URL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)\b(?:https?://|www\.)[^\s<>"]+"#).expect("valid url regex"));

/// Core conversion: split `text` into runs at UTF-16 `indices`, drop hidden
/// runs, and emit marks with code-point offsets into the visible text.
pub fn convert_runs(
    text: &str,
    indices: &[usize],
    fmts: &[Option<RunFmt>],
    links: &[Link16],
    opts: Options,
) -> Converted {
    let utf16: Vec<u16> = text.encode_utf16().collect();
    let bounds = run_bounds(indices, utf16.len());
    let fmt_of = |ri: usize| fmts.get(ri).and_then(|f| f.as_ref());

    // Hyperlink field markers (`\u{FDDF}HYPERLINK "url"`) that were not
    // flagged hidden: hide them and remember the URL for the next run.
    let mut forced_hidden = vec![false; utf16.len()];
    let mut field_links: Vec<(usize, String)> = Vec::new(); // (utf16 end, url)
    for c in FIELD_RE.captures_iter(text) {
        let m = c.get(0).expect("match");
        let s16 = text[..m.start()].encode_utf16().count();
        let e16 = s16 + m.as_str().encode_utf16().count();
        for h in forced_hidden.iter_mut().take(e16).skip(s16) {
            *h = true;
        }
        field_links.push((e16, c[1].to_string()));
    }

    // Visible chars (with their run) and a UTF-16 index -> visible code point map.
    let mut chars: Vec<(char, usize)> = Vec::new();
    let mut map16 = vec![0usize; utf16.len() + 1];
    let mut pos16 = 0usize;
    let mut ri = 0usize;
    for decoded in char::decode_utf16(utf16.iter().copied()) {
        let ch = decoded.unwrap_or('\u{FFFD}');
        let w = ch.len_utf16();
        while ri + 1 < bounds.len() && pos16 >= bounds[ri].1 {
            ri += 1;
        }
        for k in 0..w {
            map16[pos16 + k] = chars.len();
        }
        // OneNote uses the noncharacters U+FDD0..U+FDEF as structure markers
        // in equations (and U+FDDF for hyperlink fields); never show them.
        let marker = ('\u{FDD0}'..='\u{FDEF}').contains(&ch) || forced_hidden[pos16];
        if !marker && !fmt_of(ri).map(|f| f.hidden).unwrap_or(false) {
            let ch = match ch {
                '\u{000B}' | '\r' => '\n',
                c => clean_char(c),
            };
            chars.push((ch, ri));
        }
        pos16 += w;
    }
    map16[utf16.len()] = chars.len();

    let mut out = Converted::default();
    out.rich.text = chars.iter().map(|c| c.0).collect();

    // Visible [start, end) per run.
    let mut ranges: Vec<Option<(usize, usize)>> = vec![None; bounds.len()];
    for (i, (_, r)) in chars.iter().enumerate() {
        ranges[*r] = Some(match ranges[*r] {
            None => (i, i + 1),
            Some((s, _)) => (s, i + 1),
        });
    }

    let mut marks: Vec<Mark> = Vec::new();
    let mut any_text = false;
    let mut all_mono = true;
    let mut all_math = true;
    for (ri, range) in ranges.iter().enumerate() {
        let Some((s, e)) = *range else { continue };
        let has_text = chars[s..e].iter().any(|c| !c.0.is_whitespace());
        let Some(f) = fmt_of(ri) else {
            if has_text {
                any_text = true;
                all_mono = false;
                all_math = false;
            }
            continue;
        };
        if has_text {
            any_text = true;
            if !f.font.as_deref().map(is_monospace).unwrap_or(false) {
                all_mono = false;
            }
            if !f.math {
                all_math = false;
            }
        }
        if f.math {
            continue;
        }
        let mut push = |kind: MarkKind| {
            marks.push(Mark {
                start: s,
                end: e,
                kind,
            })
        };
        if f.bold {
            push(MarkKind::Bold);
        }
        if f.italic {
            push(MarkKind::Italic);
        }
        if f.underline {
            push(MarkKind::Underline);
        }
        if f.strikethrough {
            push(MarkKind::Strikethrough);
        }
        if f.superscript {
            push(MarkKind::Superscript);
        }
        if f.subscript {
            push(MarkKind::Subscript);
        }
        if let Some(color) = &f.highlight {
            push(MarkKind::Highlight {
                color: color.clone(),
            });
        }
        if let Some(color) = &f.color {
            push(MarkKind::Color {
                color: color.clone(),
            });
        }
        if !opts.suppress_fonts {
            if let Some(font) = &f.font
                && !font.trim().is_empty()
                && !font.eq_ignore_ascii_case(DEFAULT_FONT)
            {
                push(MarkKind::Font {
                    family: font.clone(),
                });
            }
            if let Some(pt) = f.size_pt
                && pt > 0.0
                && (pt - DEFAULT_FONT_SIZE_PT).abs() > 0.01
            {
                push(MarkKind::FontSize { pt });
            }
        }
    }
    out.all_monospace = any_text && all_mono;
    out.all_math = any_text && all_math;

    // Equations: maximal sequences of math-formatted runs with visible text.
    let mut ri = 0usize;
    while ri < bounds.len() {
        if !fmt_of(ri).map(|f| f.math).unwrap_or(false) {
            ri += 1;
            continue;
        }
        let first = ri;
        while ri < bounds.len() && fmt_of(ri).map(|f| f.math).unwrap_or(false) {
            ri += 1;
        }
        if map16[bounds[ri - 1].1] > map16[bounds[first].0] {
            out.equations += 1;
        }
    }

    // Stored hyperlinks.
    let mut link_ranges: Vec<(usize, usize)> = Vec::new();
    // Field markers the parser did not associate: link the visible display
    // run that follows the marker.
    let mut links: Vec<Link16> = links
        .iter()
        .map(|l| Link16 {
            target: l.target.clone(),
            start: l.start,
            end: l.end,
        })
        .collect();
    for (end16, url) in field_links {
        let Some(ri) = bounds.iter().position(|(s, e)| *s <= end16 && end16 < *e) else {
            continue;
        };
        // The display run starts at the marker end (same run) or is the next run.
        let (start, end) = if end16 > bounds[ri].0 && end16 < bounds[ri].1 {
            (end16, bounds[ri].1)
        } else {
            bounds[ri]
        };
        let is_display = fmt_of(ri)
            .map(|f| f.hyperlink && !f.hidden)
            .unwrap_or(false);
        let covered = links.iter().any(|l| l.start < end && l.end > start);
        if is_display && !covered && end > start {
            links.push(Link16 {
                target: url,
                start,
                end,
            });
        } else if !covered {
            out.notes.push(format!(
                "hyperlink field without display text dropped: {url}"
            ));
        }
    }
    for link in links {
        let s = map16[link.start.min(utf16.len())];
        let e = map16[link.end.min(utf16.len())];
        if e <= s {
            continue;
        }
        let href = link.target.trim().to_string();
        let onenote = parse_onenote_link(&href);
        if onenote.is_some() || is_onenote_link(&href) {
            out.links_onenote += 1;
        } else {
            out.links_web += 1;
        }
        link_ranges.push((s, e));
        marks.push(Mark {
            start: s,
            end: e,
            kind: MarkKind::Link {
                href,
                onenote,
                auto: false,
            },
        });
    }

    // Bare URLs in the text that are not covered by a stored link.
    let plain = &out.rich.text;
    let mut cp_of_byte = vec![0usize; plain.len() + 1];
    for (n, (b, _)) in plain.char_indices().enumerate() {
        cp_of_byte[b] = n;
    }
    cp_of_byte[plain.len()] = plain.chars().count();
    for m in URL_RE.find_iter(plain) {
        let mut url = m.as_str();
        while let Some(last) = url.chars().last() {
            let unbalanced_close = match last {
                ')' => url.matches('(').count() < url.matches(')').count(),
                ']' => url.matches('[').count() < url.matches(']').count(),
                _ => false,
            };
            if ".,;:!?}'>".contains(last) || unbalanced_close {
                url = &url[..url.len() - last.len_utf8()];
            } else {
                break;
            }
        }
        if url.len() < 8 {
            continue;
        }
        let s = cp_of_byte[m.start()];
        let e = cp_of_byte[m.start() + url.len()];
        if link_ranges.iter().any(|(ls, le)| s < *le && e > *ls) {
            continue;
        }
        let href = if url.to_ascii_lowercase().starts_with("www.") {
            format!("https://{url}")
        } else {
            url.to_string()
        };
        let onenote = parse_onenote_link(&href);
        if onenote.is_some() {
            out.links_onenote += 1;
        } else {
            out.links_web += 1;
        }
        marks.push(Mark {
            start: s,
            end: e,
            kind: MarkKind::Link {
                href,
                onenote,
                auto: true,
            },
        });
    }

    out.rich.marks = merge_marks(marks);
    out
}

/// Merge adjacent/overlapping spans of identical marks and sort them.
pub fn merge_marks(mut marks: Vec<Mark>) -> Vec<Mark> {
    marks.sort_by(|a, b| {
        mark_key(&a.kind)
            .cmp(&mark_key(&b.kind))
            .then(a.start.cmp(&b.start))
            .then(a.end.cmp(&b.end))
    });
    let mut out: Vec<Mark> = Vec::with_capacity(marks.len());
    for m in marks {
        if m.end <= m.start {
            continue;
        }
        if let Some(last) = out.last_mut()
            && last.kind == m.kind
            && m.start <= last.end
        {
            last.end = last.end.max(m.end);
            continue;
        }
        out.push(m);
    }
    out.sort_by(|a, b| {
        a.start
            .cmp(&b.start)
            .then(b.end.cmp(&a.end))
            .then(mark_key(&a.kind).cmp(&mark_key(&b.kind)))
    });
    out
}

fn mark_key(k: &MarkKind) -> String {
    match k {
        MarkKind::Bold => "0bold".into(),
        MarkKind::Italic => "1italic".into(),
        MarkKind::Underline => "2underline".into(),
        MarkKind::Strikethrough => "3strike".into(),
        MarkKind::Superscript => "4sup".into(),
        MarkKind::Subscript => "5sub".into(),
        MarkKind::Highlight { color } => format!("6hl{color}"),
        MarkKind::Color { color } => format!("7c{color}"),
        MarkKind::Font { family } => format!("8f{family}"),
        MarkKind::FontSize { pt } => format!("9s{pt}"),
        MarkKind::Link { href, .. } => format!("al{href}"),
    }
}

static CITATION_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?is)^\s*([^<>\n]{0,40}?)\s*<\s*((?:https?|ftp)://[^\s<>]+)\s*>\s*$")
        .expect("valid citation regex")
});

/// Detect OneNote's "Pasted from <url>" / "From <url>" citation line.
/// Returns the URL if the paragraph is such a line.
pub fn citation_url(rich: &Rich) -> Option<String> {
    let text = rich.text.trim();
    if text.is_empty() || text.len() > 2048 {
        return None;
    }
    if let Some(c) = CITATION_RE.captures(text) {
        let prefix = c.get(1).map(|m| m.as_str()).unwrap_or("");
        // Prefix must look like a short label ("Pasted from", "From",
        // "Eingefügt aus", "Collé à partir de", ...), not a sentence.
        if prefix.split_whitespace().count() <= 5 {
            return Some(c[2].to_string());
        }
    }
    // Without angle brackets only well-known labels count
    // ("Clipped from: https://..." is written by the OneNote Web Clipper).
    let lower = text.to_lowercase();
    for label in SOURCE_LABELS {
        if let Some(rest) = lower.strip_prefix(label) {
            let rest = rest.trim_start_matches([':', ' ', '\u{a0}']);
            let offset = text.len() - rest.len();
            let Some(url) = text.get(offset..).map(str::trim) else {
                continue;
            };
            if (url.starts_with("http://") || url.starts_with("https://"))
                && !url.contains(char::is_whitespace)
            {
                return Some(url.to_string());
            }
        }
    }
    None
}

/// Lower-case labels OneNote and its clippers put before a source URL.
const SOURCE_LABELS: &[&str] = &[
    "clipped from",
    "pasted from",
    "from",
    "source",
    "eingefügt aus",
    "quelle",
    "collé à partir de",
    "à partir de l’adresse",
    "à partir de l'adresse",
    "source :",
    "pegado desde",
    "copiado de",
    "incollato da",
    "geplakt vanuit",
];

#[cfg(test)]
mod tests {

    #[test]
    fn control_characters_are_replaced_one_for_one() {
        for c in [
            '\u{0}',
            '\u{7}',
            '\u{1B}',
            '\u{C}',
            '\u{7F}',
            '\u{85}',
            '\u{FFFE}',
            '\u{FFFF}',
            '\u{1FFFF}',
        ] {
            assert_eq!(clean_char(c), '\u{FFFD}', "{c:?}");
        }
        for c in ['\t', '\n', 'a', 'é', '\u{202E}', '\u{200D}', '😀'] {
            assert_eq!(clean_char(c), c, "{c:?}");
        }
    }

    use super::*;

    fn m(start: usize, end: usize, kind: MarkKind) -> Mark {
        Mark { start, end, kind }
    }

    #[test]
    fn merges_adjacent_identical_marks() {
        let merged = merge_marks(vec![
            m(0, 3, MarkKind::Bold),
            m(3, 5, MarkKind::Bold),
            m(7, 9, MarkKind::Bold),
            m(
                0,
                5,
                MarkKind::Highlight {
                    color: "#FFFF00".into(),
                },
            ),
            m(
                5,
                6,
                MarkKind::Highlight {
                    color: "#00FF00".into(),
                },
            ),
        ]);
        assert_eq!(
            merged,
            vec![
                m(0, 5, MarkKind::Bold),
                m(
                    0,
                    5,
                    MarkKind::Highlight {
                        color: "#FFFF00".into()
                    }
                ),
                m(
                    5,
                    6,
                    MarkKind::Highlight {
                        color: "#00FF00".into()
                    }
                ),
                m(7, 9, MarkKind::Bold),
            ]
        );
    }

    #[test]
    fn drops_empty_marks() {
        assert!(merge_marks(vec![m(3, 3, MarkKind::Italic)]).is_empty());
    }

    #[test]
    fn citation_detection() {
        let r = |t: &str| Rich {
            text: t.into(),
            marks: vec![],
        };
        assert_eq!(
            citation_url(&r("Pasted from <https://example.com/a?b=1>")).as_deref(),
            Some("https://example.com/a?b=1")
        );
        assert_eq!(
            citation_url(&r("From <https://en.wikipedia.org/wiki/Pho> ")).as_deref(),
            Some("https://en.wikipedia.org/wiki/Pho")
        );
        assert_eq!(
            citation_url(&r("Eingefügt aus <https://example.de/>")).as_deref(),
            Some("https://example.de/")
        );
        assert_eq!(
            citation_url(&r("Pasted from https://example.com/x")).as_deref(),
            Some("https://example.com/x")
        );
        assert_eq!(
            citation_url(&r("Clipped from: https://cdn.example/a%20b.pdf")).as_deref(),
            Some("https://cdn.example/a%20b.pdf")
        );
        assert_eq!(citation_url(&r("See also: https://example.com/")), None);
        assert_eq!(
            citation_url(&r(
                "I read this whole long sentence about soups somewhere <https://x.y/>"
            )),
            None
        );
        assert_eq!(citation_url(&r("Just text")), None);
    }

    fn fmt(f: impl FnOnce(&mut RunFmt)) -> Option<RunFmt> {
        let mut r = RunFmt::default();
        f(&mut r);
        Some(r)
    }

    #[test]
    fn offsets_are_code_points_and_hidden_runs_removed() {
        // "😀 " is 3 UTF-16 units but 2 code points.
        let marker = "\u{fddf}HYPERLINK \"https://example.com/\"";
        let text = format!("😀 {marker}link bold");
        let m16 = marker.encode_utf16().count();
        // Runs: [0,3) plain, [3,3+m16) hidden marker, [.., +4) link text, rest bold.
        let indices = vec![3, 3 + m16, 3 + m16 + 4];
        let fmts = vec![
            fmt(|_| {}),
            fmt(|f| f.hidden = true),
            fmt(|f| f.underline = true),
            fmt(|f| f.bold = true),
        ];
        let links = vec![Link16 {
            target: "https://example.com/".into(),
            start: 3 + m16,
            end: 3 + m16 + 4,
        }];
        let c = convert_runs(&text, &indices, &fmts, &links, Options::default());
        assert_eq!(c.rich.text, "😀 link bold");
        assert!(c.rich.marks.contains(&Mark {
            start: 2,
            end: 6,
            kind: MarkKind::Underline
        }));
        assert!(c.rich.marks.contains(&Mark {
            start: 6,
            end: 11,
            kind: MarkKind::Bold
        }));
        assert!(c.rich.marks.contains(&Mark {
            start: 2,
            end: 6,
            kind: MarkKind::Link {
                href: "https://example.com/".into(),
                onenote: None,
                auto: false
            }
        }));
        assert_eq!(c.links_web, 1);
    }

    #[test]
    fn unflagged_hyperlink_field_markers_are_hidden_and_linked() {
        let marker = "\u{fddf}HYPERLINK \"https://x.example/\"";
        let text = format!("see {marker}here now");
        let m16 = marker.encode_utf16().count();
        let fmts = vec![
            fmt(|_| {}),
            fmt(|_| {}), // marker run, not flagged hidden
            fmt(|f| f.hyperlink = true),
            fmt(|_| {}),
        ];
        let c = convert_runs(
            &text,
            &[4, 4 + m16, 4 + m16 + 4],
            &fmts,
            &[],
            Options::default(),
        );
        assert_eq!(c.rich.text, "see here now");
        assert_eq!(
            c.rich.marks,
            vec![Mark {
                start: 4,
                end: 8,
                kind: MarkKind::Link {
                    href: "https://x.example/".into(),
                    onenote: None,
                    auto: false
                }
            }]
        );
    }

    #[test]
    fn single_run_without_indices_and_fonts() {
        let fmts = vec![fmt(|f| {
            f.font = Some("Consolas".into());
            f.size_pt = Some(10.0);
            f.highlight = Some("#FFFF00".into());
        })];
        let c = convert_runs("let x = 1;", &[], &fmts, &[], Options::default());
        assert!(c.all_monospace);
        assert_eq!(c.rich.marks.len(), 3);
        let suppressed = convert_runs(
            "let x = 1;",
            &[],
            &fmts,
            &[],
            Options {
                suppress_fonts: true,
            },
        );
        assert_eq!(suppressed.rich.marks.len(), 1);
    }

    #[test]
    fn vertical_tab_becomes_newline_and_bare_urls_link() {
        let c = convert_runs(
            "see https://a.example/x.\u{000B}and www.b.example",
            &[],
            &[fmt(|_| {})],
            &[],
            Options::default(),
        );
        assert_eq!(c.rich.text, "see https://a.example/x.\nand www.b.example");
        let links: Vec<_> = c
            .rich
            .marks
            .iter()
            .filter_map(|m| match &m.kind {
                MarkKind::Link { href, auto, .. } => Some((m.start, m.end, href.clone(), *auto)),
                _ => None,
            })
            .collect();
        assert_eq!(
            links,
            vec![
                (4, 23, "https://a.example/x".to_string(), true),
                (29, 42, "https://www.b.example".to_string(), true)
            ]
        );
    }

    #[test]
    fn onenote_links_are_flagged() {
        let target = "onenote:#P&section-id={11111111-2222-3333-4444-555555555555}&page-id={AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}&end";
        let c = convert_runs(
            "go here",
            &[3],
            &[fmt(|_| {}), fmt(|_| {})],
            &[Link16 {
                target: target.into(),
                start: 3,
                end: 7,
            }],
            Options::default(),
        );
        assert_eq!(c.links_onenote, 1);
        match &c.rich.marks[0].kind {
            MarkKind::Link { onenote, .. } => assert_eq!(
                onenote.as_ref().unwrap().page_id.as_deref(),
                Some("{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}")
            ),
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn math_runs_form_groups() {
        let c = convert_runs(
            "x=a+b",
            &[],
            &[fmt(|f| f.math = true)],
            &[],
            Options::default(),
        );
        assert!(c.all_math);
        assert_eq!(c.equations, 1);
        assert_eq!(c.rich.text, "x=a+b");
        assert!(c.rich.marks.is_empty());
    }

    #[test]
    fn monospace_fonts() {
        assert!(is_monospace("Consolas"));
        assert!(is_monospace("Courier New"));
        assert!(!is_monospace("Calibri"));
    }

    #[test]
    fn color_formatting() {
        assert_eq!(
            color_hex(Some(ColorRef::Manual {
                r: 255,
                g: 0,
                b: 16
            }))
            .as_deref(),
            Some("#FF0010")
        );
        assert_eq!(color_hex(Some(ColorRef::Auto)), None);
    }

    /// A whole paragraph formatted through its paragraph style (bold, blue,
    /// 20 pt) with an unformatted text run keeps that formatting as marks.
    #[test]
    fn runs_inherit_paragraph_style_formatting() {
        let base = RunFmt {
            bold: true,
            italic: true,
            underline: true,
            strikethrough: true,
            superscript: true,
            font: Some("Georgia".into()),
            size_pt: Some(20.0),
            color: Some("#0000FF".into()),
            highlight: Some("#FFFF00".into()),
            ..RunFmt::default()
        };
        let run = RunFmt::default().inherit(Stored::default(), &base);
        let text = "Big bold paragraph";
        let c = convert_runs(text, &[], &[Some(run)], &[], Options::default());
        let n = text.chars().count();
        let kinds: Vec<MarkKind> = c
            .rich
            .marks
            .into_iter()
            .map(|m| {
                assert_eq!((m.start, m.end), (0, n));
                m.kind
            })
            .collect();
        for k in [
            MarkKind::Bold,
            MarkKind::Italic,
            MarkKind::Underline,
            MarkKind::Strikethrough,
            MarkKind::Superscript,
            MarkKind::Color {
                color: "#0000FF".into(),
            },
            MarkKind::Highlight {
                color: "#FFFF00".into(),
            },
            MarkKind::Font {
                family: "Georgia".into(),
            },
            MarkKind::FontSize { pt: 20.0 },
        ] {
            assert!(kinds.contains(&k), "{k:?} missing from {kinds:?}");
        }
    }

    /// What a run stores itself wins over the paragraph style, including an
    /// explicit "off" (not bold, automatic colour); run-only flags never
    /// come from the paragraph style.
    #[test]
    fn run_formatting_overrides_paragraph_style() {
        let base = RunFmt {
            bold: true,
            subscript: true,
            hidden: true,
            math: true,
            hyperlink: true,
            font: Some("Georgia".into()),
            color: Some("#0000FF".into()),
            size_pt: Some(20.0),
            ..RunFmt::default()
        };
        let own = RunFmt {
            italic: true,
            font: Some("Arial".into()),
            ..RunFmt::default()
        };
        let stored = Stored {
            bold: true,
            italic: true,
            font: true,
            color: true,
            ..Stored::default()
        };
        let f = own.inherit(stored, &base);
        assert!(!f.bold && f.italic && f.subscript);
        assert!(!f.hidden && !f.math && !f.hyperlink);
        assert_eq!(f.font.as_deref(), Some("Arial"));
        assert_eq!(f.color, None);
        assert_eq!(f.size_pt, Some(20.0));
    }

    #[test]
    fn builtin_paragraph_styles_are_not_inherited() {
        for id in ["p", "h1", "H6", "PageTitle", "cite", "blockquote", "code"] {
            assert!(!inherits_paragraph_style(Some(id)), "{id}");
        }
        for id in [Some("Style281"), Some("h7"), Some("Custom"), None] {
            assert!(inherits_paragraph_style(id), "{id:?}");
        }
    }
}
