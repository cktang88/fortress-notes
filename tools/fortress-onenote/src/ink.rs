//! Ink (drawings / handwriting) to standalone SVG.

use onenote_parser::contents::{Ink, InkStroke};
use std::collections::HashSet;
use std::fmt::Write;

/// Ink units (0.01 mm) per CSS pixel at 96 dpi.
pub const INK_UNITS_PER_PX: f32 = 2540.0 / 96.0;

pub struct RenderedInk {
    pub svg: String,
    /// Size in geometry units (half inches).
    pub width: f32,
    pub height: f32,
    pub stroke_count: usize,
    pub recognized_text: Option<String>,
}

fn collect<'a>(ink: &'a Ink, out: &mut Vec<&'a InkStroke>) {
    out.extend(ink.ink_strokes().iter());
    for child in ink.child_groups() {
        collect(child, out);
    }
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// Render one or more ink objects into a single SVG. Returns `None` when
/// there are no strokes.
pub fn render(inks: &[&Ink]) -> Option<RenderedInk> {
    let mut strokes = Vec::new();
    for ink in inks {
        collect(ink, &mut strokes);
    }
    let paths: Vec<(Vec<(f32, f32)>, &InkStroke)> = strokes
        .iter()
        .map(|s| {
            (
                s.absolute_path()
                    .iter()
                    .map(|p| (p.x(), p.y()))
                    .filter(|(x, y)| x.is_finite() && y.is_finite())
                    .collect::<Vec<_>>(),
                *s,
            )
        })
        .filter(|(p, _)| !p.is_empty())
        .collect();
    if paths.is_empty() {
        return None;
    }
    let (mut minx, mut miny, mut maxx, mut maxy) = (f32::MAX, f32::MAX, f32::MIN, f32::MIN);
    let mut max_w: f32 = 0.0;
    for (pts, s) in &paths {
        max_w = max_w.max(s.width()).max(s.height());
        for (x, y) in pts {
            minx = minx.min(*x);
            miny = miny.min(*y);
            maxx = maxx.max(*x);
            maxy = maxy.max(*y);
        }
    }
    let pad = (max_w / 2.0).max(INK_UNITS_PER_PX);
    minx -= pad;
    miny -= pad;
    let w = (maxx - minx + pad).max(1.0);
    let h = (maxy - miny + pad).max(1.0);
    let wpx = w / INK_UNITS_PER_PX;
    let hpx = h / INK_UNITS_PER_PX;

    // Recognised words, deduplicated by id in stroke order.
    let mut seen = HashSet::new();
    let words: Vec<String> = strokes
        .iter()
        .filter_map(|s| s.recognized_word())
        .filter(|w| seen.insert(w.id()))
        .filter_map(|w| w.text().map(str::to_string))
        .collect();
    let recognized = Some(words.join(" ")).filter(|s| !s.trim().is_empty());

    let mut svg = String::new();
    let _ = write!(
        svg,
        r#"<svg xmlns="http://www.w3.org/2000/svg" width="{:.1}" height="{:.1}" viewBox="{:.0} {:.0} {:.0} {:.0}">"#,
        wpx, hpx, minx, miny, w, h
    );
    if let Some(t) = &recognized {
        let _ = write!(svg, "<title>{}</title>", esc(t));
    }
    for (pts, s) in &paths {
        let color = s
            .color()
            .map(|v| {
                format!(
                    "#{:02X}{:02X}{:02X}",
                    v & 0xFF,
                    (v >> 8) & 0xFF,
                    (v >> 16) & 0xFF
                )
            })
            .unwrap_or_else(|| "#000000".into());
        let opacity = (255 - s.transparency().unwrap_or(0)) as f32 / 255.0;
        let width = s.width().max(s.height()).max(1.0);
        let cap = if s.pen_tip().unwrap_or(0) == 0 {
            "round"
        } else {
            "square"
        };
        let mut d = String::new();
        let (x0, y0) = pts[0];
        let _ = write!(d, "M{x0:.0} {y0:.0}");
        if pts.len() == 1 {
            let _ = write!(d, "l0 0");
        }
        for (x, y) in &pts[1..] {
            let _ = write!(d, "L{x:.0} {y:.0}");
        }
        let _ = write!(
            svg,
            r#"<path d="{d}" fill="none" stroke="{color}" stroke-width="{width:.0}" stroke-linecap="{cap}" stroke-linejoin="round""#
        );
        if opacity < 0.999 {
            let _ = write!(svg, r#" stroke-opacity="{opacity:.2}""#);
        }
        svg.push_str("/>");
    }
    svg.push_str("</svg>");
    Some(RenderedInk {
        svg,
        width: wpx / 48.0,
        height: hpx / 48.0,
        stroke_count: paths.len(),
        recognized_text: recognized,
    })
}
