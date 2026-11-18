//! OneNote equation (linear-format) parser and LaTeX / MathML renderers.
//!
//! Vendored from one2html v2.0.1 (`src/page/math`), Copyright 2020 Markus
//! Siemens, MIT licence (see `LICENSES/one2html-MIT.txt`). Changes: error
//! type switched to `anyhow`, logging replaced by a no-op macro, renderer entry
//! points adapted to this crate; the lexer finds structure marks anywhere in a
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

use anyhow::Result;
use onenote_parser::contents::MathInlineObject;

/// Rendered forms of one equation.
pub struct RenderedMath {
    pub latex: Option<String>,
    pub mathml: Option<String>,
}

/// Render one math group (consecutive math-formatted text runs together with
/// their inline math objects) to LaTeX and MathML. Each part is best effort.
pub fn render(segments: &[(String, MathInlineObject)], block: bool) -> RenderedMath {
    let parse = || -> Result<ast::Equation> { parser::Parser::new(segments.to_vec())?.parse() };
    let latex = parse()
        .and_then(|eq| render::latex(eq, block))
        .ok()
        .filter(|s| !s.trim().is_empty());
    let mathml = parse()
        .and_then(|eq| render::mathml(eq, block))
        .ok()
        .filter(|s| !s.trim().is_empty());
    let clean = |s: String| -> String { s.chars().filter(|c| !c.is_control()).collect() };
    RenderedMath {
        latex: latex.map(clean),
        mathml: mathml.map(clean),
    }
}
