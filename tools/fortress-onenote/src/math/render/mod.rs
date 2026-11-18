use super::ast::Equation;
use anyhow::Result;
use finl_unicode::categories::CharacterCategories;

mod latex;
mod mathml;

pub(crate) fn latex(equation: Equation, block: bool) -> Result<String> {
    latex::render_equation(equation, block)
}

pub(crate) fn mathml(equation: Equation, block: bool) -> Result<String> {
    mathml::render_equation(equation, block)
}

pub(super) fn is_skippable_format(c: char) -> bool {
    c.is_format() && c != '\u{2061}'
}
