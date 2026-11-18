//! Reading-order layout of a page's positioned items (outlines, floating
//! images, files, ink).
//!
//! OneNote stores only each item's top-left offset and (often) a width; the
//! height is never stored, so it is estimated by the caller. The algorithm:
//!
//! 1. Sort boxes by top, then left.
//! 2. Sweep them into horizontal *bands*: a box joins the current band when
//!    its vertical range overlaps the band's range by more than
//!    [`V_TOLERANCE`]; the band grows to the union (so overlap is transitive).
//! 3. Inside a band, cluster boxes into *columns* by horizontal overlap
//!    (more than [`H_TOLERANCE`]), again transitively, ordered left to right.
//!    *Bridges* are set aside first: a box that overlaps (in both
//!    directions) two side-by-side boxes, such as a picture floating across
//!    the gap between two columns or an ink drawing over a page area, would
//!    otherwise merge their columns. The rest of the band is laid out on its
//!    own and each bridge becomes a row of its own at its top position.
//! 4. A band with one box is a plain row. A band with one column (boxes that
//!    overlap in both directions) is emitted as stacked rows in top order. A
//!    band with two or more columns becomes a `columns` row; each column's
//!    boxes are laid out recursively (so a column can stack several outlines
//!    or contain its own columns).
//! 5. Bands with more than [`MAX_COLUMNS`] columns are flagged `scattered`,
//!    unless they are a grid: at most [`GRID_MAX_COLUMNS`] columns whose
//!    tops are aligned.
//!
//! Background images are excluded from banding (they would merge every band)
//! and emitted as their own rows at their vertical position.

use crate::manifest::{LayoutColumn, LayoutRow};

/// Minimum vertical overlap (geometry units, 1 = half an inch) for two boxes
/// to be considered side by side.
pub const V_TOLERANCE: f32 = 0.15;
/// Minimum horizontal overlap for two boxes to share a column.
pub const H_TOLERANCE: f32 = 0.1;
/// More columns than this in one band is treated as "scattered notes",
/// unless they form a grid (see [`GRID_MAX_COLUMNS`]).
pub const MAX_COLUMNS: usize = 4;
/// Up to this many columns whose first boxes start at the same height
/// (within [`GRID_TOP_TOLERANCE`]) are a deliberate grid, not scattered.
pub const GRID_MAX_COLUMNS: usize = 5;
/// Geometry units.
pub const GRID_TOP_TOLERANCE: f32 = 0.5;
const MAX_DEPTH: usize = 6;

#[derive(Debug, Clone)]
pub struct LBox {
    pub id: String,
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
    pub background: bool,
}

impl LBox {
    fn right(&self) -> f32 {
        self.x + self.w.max(0.01)
    }
    fn bottom(&self) -> f32 {
        self.y + self.h.max(0.01)
    }
}

/// Compute rows for a page. Returns the rows and the number of scattered bands.
pub fn compute(boxes: &[LBox]) -> (Vec<LayoutRow>, usize) {
    let (bg, fg): (Vec<&LBox>, Vec<&LBox>) = boxes.iter().partition(|b| b.background);
    let mut scattered = 0usize;
    let mut rows = layout(&fg, 0, &mut scattered);
    if !bg.is_empty() {
        // Merge background rows by their top position.
        let tops: Vec<f32> = rows.iter().map(|r| row_top(r, boxes)).collect();
        let mut keyed: Vec<(f32, usize, LayoutRow)> = rows
            .drain(..)
            .enumerate()
            .map(|(i, r)| (tops[i], i, r))
            .collect();
        for (k, b) in bg.iter().enumerate() {
            keyed.push((
                b.y,
                usize::MAX - bg.len() + k,
                LayoutRow::Item { item: b.id.clone() },
            ));
        }
        keyed.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
        rows = keyed.into_iter().map(|(_, _, r)| r).collect();
    }
    (rows, scattered)
}

fn row_top(row: &LayoutRow, boxes: &[LBox]) -> f32 {
    match row {
        LayoutRow::Item { item } => boxes
            .iter()
            .find(|b| &b.id == item)
            .map(|b| b.y)
            .unwrap_or(0.0),
        LayoutRow::Columns { columns, .. } => columns
            .iter()
            .flat_map(|c| c.rows.iter())
            .map(|r| row_top(r, boxes))
            .fold(f32::INFINITY, f32::min),
    }
}

fn sorted_by_position<'a>(boxes: &[&'a LBox]) -> Vec<&'a LBox> {
    let mut v = boxes.to_vec();
    v.sort_by(|a, b| a.y.total_cmp(&b.y).then(a.x.total_cmp(&b.x)));
    v
}

fn layout(boxes: &[&LBox], depth: usize, scattered: &mut usize) -> Vec<LayoutRow> {
    let sorted = sorted_by_position(boxes);
    if sorted.len() <= 1 || depth >= MAX_DEPTH {
        return sorted
            .iter()
            .map(|b| LayoutRow::Item { item: b.id.clone() })
            .collect();
    }

    // Bands by vertical overlap.
    let mut bands: Vec<(f32, Vec<&LBox>)> = Vec::new(); // (bottom, members)
    for b in sorted {
        match bands.last_mut() {
            Some((bottom, members)) if b.y < *bottom - V_TOLERANCE => {
                *bottom = bottom.max(b.bottom());
                members.push(b);
            }
            _ => bands.push((b.bottom(), vec![b])),
        }
    }

    let mut rows = Vec::new();
    for (_, members) in bands {
        if members.len() == 1 {
            rows.push(LayoutRow::Item {
                item: members[0].id.clone(),
            });
            continue;
        }
        let bridges = bridges(&members);
        if !bridges.is_empty() {
            rows.extend(without_bridges(&members, &bridges, depth, scattered));
            continue;
        }
        // Columns by horizontal overlap.
        let mut by_x = members.clone();
        by_x.sort_by(|a, b| a.x.total_cmp(&b.x).then(a.y.total_cmp(&b.y)));
        let mut cols: Vec<(f32, f32, Vec<&LBox>)> = Vec::new(); // (left, right, members)
        for b in by_x {
            match cols.last_mut() {
                Some((_, right, m)) if b.x < *right - H_TOLERANCE => {
                    *right = right.max(b.right());
                    m.push(b);
                }
                _ => cols.push((b.x, b.right(), vec![b])),
            }
        }
        if cols.len() == 1 {
            // Overlapping in both directions: stack by position.
            for b in sorted_by_position(&members) {
                rows.push(LayoutRow::Item { item: b.id.clone() });
            }
            continue;
        }
        let total: f32 = cols.iter().map(|(l, r, _)| (r - l).max(0.01)).sum();
        let is_scattered = is_scattered(&cols);
        if is_scattered {
            *scattered += 1;
        }
        let columns = cols
            .into_iter()
            .map(|(l, r, m)| LayoutColumn {
                width: round3((r - l).max(0.01) / total),
                x: round3(l),
                span: round3(r - l),
                rows: layout(&m, depth + 1, scattered),
            })
            .collect();
        rows.push(LayoutRow::Columns {
            columns,
            scattered: is_scattered,
        });
    }
    rows
}

/// Whether a band's columns are scattered notes: more than [`MAX_COLUMNS`],
/// except for a grid of up to [`GRID_MAX_COLUMNS`] columns with aligned tops.
fn is_scattered(cols: &[(f32, f32, Vec<&LBox>)]) -> bool {
    if cols.len() <= MAX_COLUMNS {
        return false;
    }
    let tops: Vec<f32> = cols
        .iter()
        .map(|(_, _, m)| m.iter().map(|b| b.y).fold(f32::INFINITY, f32::min))
        .collect();
    let spread = tops.iter().copied().fold(f32::NEG_INFINITY, f32::max)
        - tops.iter().copied().fold(f32::INFINITY, f32::min);
    cols.len() > GRID_MAX_COLUMNS || spread > GRID_TOP_TOLERANCE
}

fn h_overlap(a: &LBox, b: &LBox) -> bool {
    a.x.max(b.x) < a.right().min(b.right()) - H_TOLERANCE
}

fn v_overlap(a: &LBox, b: &LBox) -> bool {
    a.y.max(b.y) < a.bottom().min(b.bottom()) - V_TOLERANCE
}

/// Boxes of a band that *bridge* side-by-side boxes: each overlaps (in both
/// directions) two other boxes that are side by side, i.e. overlap each
/// other vertically but not horizontally. Typical bridges are a picture
/// floating across the gap between two columns, an ink drawing over a whole
/// page area, or a wide outline whose estimated height reaches into columns
/// below it. Left in, a bridge merges the columns it touches into one, and
/// the band is stacked instead of laid out side by side.
///
/// Widest boxes are considered first; a box already found to be a bridge
/// does not count as one of the side-by-side pair of another.
fn bridges<'a>(members: &[&'a LBox]) -> Vec<&'a LBox> {
    if members.len() < 3 {
        return Vec::new();
    }
    let mut by_width = members.to_vec();
    by_width.sort_by(|a, b| b.w.total_cmp(&a.w).then(a.y.total_cmp(&b.y)));
    let mut found: Vec<&LBox> = Vec::new();
    for b in by_width {
        let touching: Vec<&LBox> = members
            .iter()
            .copied()
            .filter(|o| {
                !std::ptr::eq(*o, b)
                    && !found.iter().any(|f| std::ptr::eq(*f, *o))
                    && h_overlap(o, b)
                    && v_overlap(o, b)
            })
            .collect();
        let bridges_pair = touching.iter().enumerate().any(|(i, p)| {
            touching[i + 1..]
                .iter()
                .any(|q| v_overlap(p, q) && !h_overlap(p, q))
        });
        if bridges_pair {
            found.push(b);
        }
    }
    found
}

/// Lay out a band without its bridges (which may split it into several
/// bands and columns), then put each bridge in a row of its own at its top
/// position: before the rows that start below it, after the others.
fn without_bridges(
    members: &[&LBox],
    bridges: &[&LBox],
    depth: usize,
    scattered: &mut usize,
) -> Vec<LayoutRow> {
    let rest: Vec<&LBox> = members
        .iter()
        .copied()
        .filter(|m| !bridges.iter().any(|b| std::ptr::eq(*b, *m)))
        .collect();
    let owned: Vec<LBox> = members.iter().map(|b| (*b).clone()).collect();
    let mut keyed: Vec<(f32, usize, LayoutRow)> = layout(&rest, depth + 1, scattered)
        .into_iter()
        .enumerate()
        .map(|(i, r)| (row_top(&r, &owned), i, r))
        .collect();
    let n = keyed.len();
    for (k, b) in sorted_by_position(bridges).into_iter().enumerate() {
        keyed.push((b.y, n + k, LayoutRow::Item { item: b.id.clone() }));
    }
    // Stable: equal tops keep the bridged content first.
    keyed.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
    keyed.into_iter().map(|(_, _, r)| r).collect()
}

fn round3(v: f32) -> f32 {
    (v * 1000.0).round() / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bx(id: &str, x: f32, y: f32, w: f32, h: f32) -> LBox {
        LBox {
            id: id.into(),
            x,
            y,
            w,
            h,
            background: false,
        }
    }

    /// Compact textual form: `a` item, `[a|b]` columns, `,` between rows.
    fn show(rows: &[LayoutRow]) -> String {
        rows.iter()
            .map(|r| match r {
                LayoutRow::Item { item } => item.clone(),
                LayoutRow::Columns { columns, scattered } => format!(
                    "{}[{}]",
                    if *scattered { "!" } else { "" },
                    columns
                        .iter()
                        .map(|c| show(&c.rows))
                        .collect::<Vec<_>>()
                        .join("|")
                ),
            })
            .collect::<Vec<_>>()
            .join(",")
    }

    fn run(boxes: &[LBox]) -> String {
        show(&compute(boxes).0)
    }

    #[test]
    fn empty_and_single() {
        assert_eq!(run(&[]), "");
        assert_eq!(run(&[bx("a", 1.0, 2.0, 10.0, 3.0)]), "a");
    }

    #[test]
    fn stacked_outlines_in_top_order() {
        let boxes = [
            bx("b", 1.0, 6.0, 12.0, 2.0),
            bx("a", 1.0, 1.0, 12.0, 3.0),
            bx("c", 1.2, 9.0, 8.0, 1.0),
        ];
        assert_eq!(run(&boxes), "a,b,c");
    }

    #[test]
    fn two_columns_left_to_right_with_widths() {
        let boxes = [
            bx("right", 9.0, 1.1, 3.0, 5.0),
            bx("left", 1.0, 1.0, 6.0, 4.0),
        ];
        let (rows, _) = compute(&boxes);
        assert_eq!(show(&rows), "[left|right]");
        match &rows[0] {
            LayoutRow::Columns { columns, .. } => {
                assert!((columns[0].width - 6.0 / 9.0).abs() < 0.01);
                assert!((columns[1].width - 3.0 / 9.0).abs() < 0.01);
                assert_eq!(columns[0].x, 1.0);
            }
            _ => panic!(),
        }
    }

    #[test]
    fn staggered_three_columns_then_footer() {
        let boxes = [
            bx("a", 0.0, 0.0, 4.0, 6.0),
            bx("b", 5.0, 1.0, 4.0, 3.0),
            bx("c", 10.0, 2.5, 4.0, 2.0),
            bx("footer", 0.0, 8.0, 14.0, 1.0),
        ];
        assert_eq!(run(&boxes), "[a|b|c],footer");
    }

    #[test]
    fn column_with_stacked_items() {
        // Tall left outline; two outlines stacked on the right.
        let boxes = [
            bx("left", 0.0, 0.0, 6.0, 10.0),
            bx("r2", 7.0, 5.0, 5.0, 2.0),
            bx("r1", 7.0, 0.5, 5.0, 3.0),
        ];
        assert_eq!(run(&boxes), "[left|r1,r2]");
    }

    #[test]
    fn nested_columns_inside_a_column() {
        let boxes = [
            bx("left", 0.0, 0.0, 6.0, 12.0),
            bx("top", 7.0, 0.0, 10.0, 2.0),
            bx("m1", 7.0, 3.0, 4.0, 2.0),
            bx("m2", 12.0, 3.0, 5.0, 2.0),
        ];
        assert_eq!(run(&boxes), "[left|top,[m1|m2]]");
    }

    #[test]
    fn floating_image_beside_text() {
        let boxes = [
            bx("text", 1.0, 1.0, 10.0, 6.0),
            bx("img", 11.5, 2.0, 4.0, 3.0),
        ];
        assert_eq!(run(&boxes), "[text|img]");
    }

    #[test]
    fn tiny_offsets_do_not_create_columns() {
        // Second outline is slightly indented and starts right where the
        // estimated height of the first ends: stacked, not columns.
        let boxes = [bx("a", 1.0, 1.0, 12.0, 3.0), bx("b", 1.05, 3.9, 12.0, 2.0)];
        assert_eq!(run(&boxes), "a,b");
        // Just touching vertically, horizontally disjoint: still rows.
        let boxes = [bx("a", 0.0, 0.0, 4.0, 2.0), bx("b", 6.0, 1.9, 4.0, 2.0)];
        assert_eq!(run(&boxes), "a,b");
        // Columns that merely touch horizontally are still separate columns.
        let boxes = [bx("a", 0.0, 0.0, 4.0, 2.0), bx("b", 4.05, 0.0, 4.0, 2.0)];
        assert_eq!(run(&boxes), "[a|b]");
    }

    #[test]
    fn overlapping_boxes_stack() {
        let boxes = [bx("a", 1.0, 1.0, 10.0, 5.0), bx("b", 3.0, 2.0, 10.0, 5.0)];
        assert_eq!(run(&boxes), "a,b");
    }

    #[test]
    fn scattered_band_flagged() {
        let boxes: Vec<LBox> = (0..6)
            .map(|i| bx(&format!("n{i}"), i as f32 * 3.0, 0.0, 2.0, 1.0))
            .collect();
        let (rows, scattered) = compute(&boxes);
        assert_eq!(scattered, 1);
        assert_eq!(show(&rows), "![n0|n1|n2|n3|n4|n5]");
    }

    #[test]
    fn picture_across_the_gap_does_not_merge_columns() {
        // gen-layout-columns.one: a floating picture overlaps columns 2 and 3.
        let mut boxes: Vec<LBox> = (0..4)
            .map(|i| bx(&format!("c{i}"), 0.5 + i as f32 * 4.2, 2.8, 3.9, 6.7))
            .collect();
        boxes.push(bx("img", 4.7, 3.1, 5.6, 3.3));
        assert_eq!(run(&boxes), "[c0|c1|c2|c3],img");
    }

    #[test]
    fn drawing_over_side_by_side_outlines_does_not_stack_them() {
        // Cornell notes: an ink drawing (the template's lines) covers the
        // cue column and the note-taking area.
        let boxes = [
            bx("summary", 1.25, 22.9, 15.8, 0.5),
            bx("cue", 1.0, 2.4, 3.6, 3.3),
            bx("notes", 6.0, 2.4, 10.8, 9.1),
            bx("ink", 1.0, 2.42, 16.2, 13.0),
            bx("area", 6.0, 15.9, 6.7, 0.9),
        ];
        assert_eq!(run(&boxes), "[cue|notes],ink,area,summary");
    }

    #[test]
    fn wide_outline_reaching_into_columns_goes_first() {
        // The header's estimated height overlaps the two columns below it.
        let boxes = [
            bx("header", 0.0, 0.0, 20.0, 3.0),
            bx("a", 0.0, 2.5, 8.0, 4.0),
            bx("b", 10.0, 2.5, 8.0, 4.0),
        ];
        assert_eq!(run(&boxes), "header,[a|b]");
    }

    #[test]
    fn a_box_over_stacked_boxes_is_no_bridge() {
        // Overlapping both of two stacked outlines (not side by side).
        let boxes = [
            bx("a", 0.0, 0.0, 10.0, 3.0),
            bx("b", 0.0, 3.0, 10.0, 3.0),
            bx("img", 2.0, 1.0, 4.0, 4.0),
        ];
        assert_eq!(run(&boxes), "a,img,b");
    }

    #[test]
    fn aligned_grid_of_five_columns_is_not_scattered() {
        let boxes: Vec<LBox> = (0..5)
            .map(|i| bx(&format!("c{i}"), i as f32 * 4.2, 2.8, 3.9, 6.7))
            .collect();
        let (rows, scattered) = compute(&boxes);
        assert_eq!((show(&rows).as_str(), scattered), ("[c0|c1|c2|c3|c4]", 0));
        // Too many columns, even aligned: scattered.
        let boxes: Vec<LBox> = (0..6)
            .map(|i| bx(&format!("c{i}"), i as f32 * 4.2, 2.8, 3.9, 6.7))
            .collect();
        assert_eq!(compute(&boxes).1, 1);
        // Five columns at different heights: scattered.
        let boxes: Vec<LBox> = (0..5)
            .map(|i| bx(&format!("c{i}"), i as f32 * 4.2, i as f32 * 0.4, 3.9, 6.7))
            .collect();
        assert_eq!(compute(&boxes).1, 1);
    }

    #[test]
    fn background_images_do_not_merge_bands() {
        let mut bg = bx("bg", 0.0, 0.0, 30.0, 40.0);
        bg.background = true;
        let boxes = [
            bg,
            bx("a", 1.0, 1.0, 10.0, 2.0),
            bx("b", 1.0, 5.0, 10.0, 2.0),
        ];
        assert_eq!(run(&boxes), "bg,a,b");
    }
}
