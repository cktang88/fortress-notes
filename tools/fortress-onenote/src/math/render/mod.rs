use super::ast::Equation;
use anyhow::Result;
use finl_unicode::categories::CharacterCategories;

mod latex;

pub(crate) fn latex(equation: Equation) -> Result<String> {
    latex::render_equation(equation)
}

pub(super) fn is_skippable_format(c: char) -> bool {
    c.is_format() && c != '\u{2061}'
}
