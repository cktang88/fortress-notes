//! Parsing of OneNote-internal links (`onenote:` URLs and their web variants).

use crate::manifest::OneNoteLink;
use percent_encoding::percent_decode_str;

/// Normalise a GUID to `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}` (upper case,
/// braces), which is the format of `Page::link_target_id()`.
/// Returns `None` if the input is not a GUID.
pub fn normalize_guid(raw: &str) -> Option<String> {
    let s = raw.trim().trim_start_matches('{').trim_end_matches('}');
    let hex: String = s.chars().filter(|c| *c != '-').collect();
    if hex.len() != 32 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let h = hex.to_ascii_uppercase();
    Some(format!(
        "{{{}-{}-{}-{}-{}}}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    ))
}

fn decode(s: &str) -> String {
    percent_decode_str(s).decode_utf8_lossy().into_owned()
}

/// Whether a URL points into a OneNote notebook.
pub fn is_onenote_link(href: &str) -> bool {
    let lower = href.trim().to_ascii_lowercase();
    lower.starts_with("onenote:")
        || ((lower.contains(".one#") || lower.contains(".one%23")) && lower.contains("page-id="))
}

/// Extract the value of `key=` from the `&`-separated fragment part.
fn param<'a>(fragment: &'a str, key: &str) -> Option<&'a str> {
    let lower = fragment.to_ascii_lowercase();
    let mut search_from = 0;
    while let Some(pos) = lower[search_from..].find(key) {
        let abs = search_from + pos;
        // Must start a parameter: preceded by '&', '#', '?' or start.
        let ok = abs == 0 || matches!(fragment.as_bytes()[abs - 1], b'&' | b'#' | b'?' | b';');
        if ok {
            let rest = &fragment[abs + key.len()..];
            let end = rest.find('&').unwrap_or(rest.len());
            return Some(&rest[..end]);
        }
        search_from = abs + key.len();
    }
    None
}

/// Parse a OneNote link. Returns `None` for ordinary web links.
///
/// Handles the common shapes:
/// - `onenote:https://d.docs.live.net/.../Notebook/Section.one#Title&section-id={..}&page-id={..}&end`
/// - `onenote:///C:\path\Section.one#Title&section-id={..}&page-id={..}&object-id={..}&3C`
/// - `onenote:#Title&section-id={..}&page-id={..}&end` (same section)
/// - `https://.../Section.one#Title&section-id=...&page-id=...` (web)
pub fn parse_onenote_link(href: &str) -> Option<OneNoteLink> {
    if !is_onenote_link(href) {
        return None;
    }
    let href = href.trim();
    let body = if href.len() >= 8 && href[..8].eq_ignore_ascii_case("onenote:") {
        &href[8..]
    } else {
        href
    };
    // Some producers percent-encode the '#'.
    let (file_part, fragment) = match body.find('#') {
        Some(i) => (&body[..i], &body[i + 1..]),
        None => match body.to_ascii_lowercase().find("%23") {
            Some(i) => (&body[..i], &body[i + 3..]),
            None => (body, ""),
        },
    };
    // The fragment itself may be percent-encoded as a whole (`%26page-id%3D`).
    let decoded_fragment;
    let fragment = if !fragment.to_ascii_lowercase().contains("page-id=")
        && !fragment.to_ascii_lowercase().contains("section-id=")
    {
        decoded_fragment = decode(fragment);
        decoded_fragment.as_str()
    } else {
        fragment
    };

    let get_guid = |key: &str| param(fragment, key).and_then(|v| normalize_guid(&decode(v)));

    let page_id = get_guid("page-id=");
    let section_id = get_guid("section-id=");
    let object_id = get_guid("object-id=");
    // The object's counter follows as a bare hexadecimal parameter:
    // `object-id={GUID}&3C`.
    let object_ref = param(fragment, "object-id=").and_then(|v| {
        let lower = fragment.to_ascii_lowercase();
        let start = lower.find(&format!("object-id={}", v.to_ascii_lowercase()))?;
        let after = &fragment[start + "object-id=".len() + v.len()..];
        let suffix = after.strip_prefix('&')?;
        let suffix = &suffix[..suffix.find('&').unwrap_or(suffix.len())];
        crate::objectids::object_ref(&decode(v), Some(suffix))
    });

    let lower = fragment.to_ascii_lowercase();
    let first_param = ["section-id=", "page-id=", "object-id="]
        .iter()
        .filter_map(|k| {
            if lower.starts_with(k) {
                Some(0)
            } else {
                lower.find(&format!("&{k}"))
            }
        })
        .min()
        .unwrap_or(fragment.len());
    let title = decode(&fragment[..first_param]);
    let page_title = Some(title).filter(|t| !t.is_empty());

    let section_file = Some(decode(file_part))
        .map(|s| s.trim_start_matches("///").to_string())
        .filter(|s| !s.is_empty());

    Some(OneNoteLink {
        page_id,
        section_id,
        object_id,
        object_ref,
        section_file,
        page_title,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn guid_normalisation() {
        assert_eq!(
            normalize_guid("{2b1b6b5d-1234-4a5b-9c8d-0123456789ab}").as_deref(),
            Some("{2B1B6B5D-1234-4A5B-9C8D-0123456789AB}")
        );
        assert_eq!(
            normalize_guid("2B1B6B5D12344A5B9C8D0123456789AB").as_deref(),
            Some("{2B1B6B5D-1234-4A5B-9C8D-0123456789AB}")
        );
        assert_eq!(normalize_guid("{nope}"), None);
        assert_eq!(normalize_guid(""), None);
    }

    #[test]
    fn web_onenote_link() {
        let l = parse_onenote_link(
            "onenote:https://d.docs.live.net/abc123/Documents/My%20Notebook/Recipes.one#Pho%20soup&section-id={11111111-2222-3333-4444-555555555555}&page-id={aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}&end",
        )
        .unwrap();
        assert_eq!(
            l.page_id.as_deref(),
            Some("{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}")
        );
        assert_eq!(
            l.section_id.as_deref(),
            Some("{11111111-2222-3333-4444-555555555555}")
        );
        assert_eq!(l.object_id, None);
        assert_eq!(l.page_title.as_deref(), Some("Pho soup"));
        assert_eq!(
            l.section_file.as_deref(),
            Some("https://d.docs.live.net/abc123/Documents/My Notebook/Recipes.one")
        );
    }

    #[test]
    fn desktop_paragraph_link() {
        let l = parse_onenote_link(
            r"onenote:///D:\Notes\Work\Meetings.one#Weekly&section-id={11111111-2222-3333-4444-555555555555}&page-id={AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}&object-id={99999999-8888-7777-6666-555555555555}&1A",
        )
        .unwrap();
        assert_eq!(
            l.page_id.as_deref(),
            Some("{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}")
        );
        assert_eq!(
            l.object_id.as_deref(),
            Some("{99999999-8888-7777-6666-555555555555}")
        );
        assert_eq!(
            l.object_ref.as_deref(),
            Some("{99999999-8888-7777-6666-555555555555}&1A")
        );
        assert_eq!(l.page_title.as_deref(), Some("Weekly"));
        assert_eq!(
            l.section_file.as_deref(),
            Some(r"D:\Notes\Work\Meetings.one")
        );
    }

    #[test]
    fn paragraph_link_counter() {
        let l = parse_onenote_link(
            "onenote:#T&section-id={11111111-2222-3333-4444-555555555555}&page-id={AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}&object-id={99999999-8888-7777-6666-555555555555}&end",
        )
        .unwrap();
        assert!(l.object_id.is_some());
        assert_eq!(l.object_ref, None);
        let l = parse_onenote_link(
            "onenote:#T&page-id={AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}&object-id=%7B99999999-8888-7777-6666-555555555555%7D&2f&end",
        )
        .unwrap();
        assert_eq!(
            l.object_ref.as_deref(),
            Some("{99999999-8888-7777-6666-555555555555}&2F")
        );
    }

    #[test]
    fn same_section_link_and_encoded_guid() {
        let l = parse_onenote_link(
            "onenote:#Title&section-id=%7B11111111-2222-3333-4444-555555555555%7D&page-id=%7Baaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee%7D&end",
        )
        .unwrap();
        assert_eq!(
            l.page_id.as_deref(),
            Some("{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}")
        );
        assert_eq!(l.section_file, None);
    }

    #[test]
    fn section_only_link() {
        let l = parse_onenote_link(
            "onenote:https://d.docs.live.net/x/Notebook/Sec.one#section-id={11111111-2222-3333-4444-555555555555}&end",
        )
        .unwrap();
        assert_eq!(l.page_id, None);
        assert!(l.section_id.is_some());
    }

    #[test]
    fn web_links_are_not_onenote() {
        assert!(parse_onenote_link("https://example.com/page#page-id=1").is_none());
        assert!(parse_onenote_link("mailto:a@b.c").is_none());
        assert!(
            parse_onenote_link(
                "https://onedrive.live.com/x/Sec.one#T&section-id={11111111-2222-3333-4444-555555555555}&page-id={AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}&end"
            )
            .is_some()
        );
    }

    #[test]
    fn uppercase_scheme_and_params() {
        let l = parse_onenote_link(
            "OneNote:#X&SECTION-ID={11111111-2222-3333-4444-555555555555}&PAGE-ID={aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}",
        )
        .unwrap();
        assert!(l.page_id.is_some());
        assert_eq!(l.page_title.as_deref(), Some("X"));
    }
}
