//! OneNote equation (linear-format) parser and LaTeX renderer.
//!
//! Fortress has no math blocks: an equation is imported as its LaTeX source,
//! in a code block (display) or as inline code (inline).
//!
//! Vendored from one2html v2.0.1 (`src/page/math`), Copyright 2020 Markus
//! Siemens, MIT licence (see `LICENSES/one2html-MIT.txt`). Changes: error
//! type switched to `anyhow`, logging replaced by a no-op macro, renderer entry
//! points adapted to this crate, the MathML renderer removed; the lexer finds structure marks anywhere in a
//! run and takes separator/end types from the open object (equations written
//! by other tools), and missing matrix brackets / equation-array column
//! counts get defaults.
#![allow(clippy::all)]

macro_rules! math_warn {
    ($($t:tt)*) => {{
        let _ = format_args!($($t)*);
    }};
}

mod ast;
mod lexer;
mod parser;
mod render;
mod text;

use onenote_parser::contents::MathInlineObject;

/// Render one math group (consecutive math-formatted text runs together with
/// their inline math objects) to LaTeX without delimiters, best effort.
pub fn latex(segments: &[(String, MathInlineObject)]) -> Option<String> {
    let equation = parser::Parser::new(segments.to_vec())
        .and_then(|mut p| p.parse())
        .ok()?;
    render::latex(equation)
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.chars().filter(|c| !c.is_control()).collect())
}
