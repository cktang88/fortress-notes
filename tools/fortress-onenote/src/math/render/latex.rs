// LaTeX math renderer.
//
// The output is wrapped in `\(...\)` and assumes a Unicode-aware math backend
// downstream — MathJax or KaTeX in the browser, or `unicode-math` (with
// xelatex/lualatex) for typeset documents. We deliberately do *not* maintain
// hand-curated translation tables for the long tail of greek letters and
// operator symbols: those backends render bare Unicode (`α`, `≤`, `∂`, `2π`)
// natively, so a per-char `\alpha`/`\leq`/`\partial` map would be perpetual
// whack-a-mole against ~3500 Unicode-math symbols for no gain.
//
// We *do* still translate:
//   - Structural constructs (`\frac`, `\sqrt`, `\sum`, matrix envs, brackets,
//     accents, sub/sup, `\boxed`, …) — these *are* the LaTeX rendering and
//     have no Unicode equivalent.
//   - Styled Mathematical Alphanumeric Symbols (U+1D400…U+1D7FF) decoded back
//     to base ASCII / greek so that `\mathbf{X}` works in pdflatex (writing
//     `\mathbf{𝐗}` does not).
//   - LaTeX-syntactic characters (`\`, `{`, `}`, `$`, `&`, `#`, `_`, `^`, `%`)
//     escaped on output.
//   - N-ary operators, bracket delimiters, and accents — small fixed tables
//     because LaTeX needs different spelling there (`\sum` vs bare `∑` because
//     of `\limits`/sub-sup attachment; `\left\{` vs `\left{`; `\hat{x}` vs
//     bare combining U+0302).

use crate::math::ast::{
    BoxDisplay, BoxFlags, BoxSize, BoxedFormulaAlignment, BracketsAlignment, Equation,
    EquationArrayAlignment, MathOp, MatrixAlignment, MatrixBrackets, NAryAlignment, NAryDisplay,
    NAryOptions, PhantomDisplay, PhantomKind, StretchStackPosition, SubSupAlignment,
};
use crate::math::render::is_skippable_format;
use crate::math::text::TextType;
use anyhow::Result;
use anyhow::anyhow as eyre;
use itertools::Itertools;

pub(super) fn render_equation(equation: Equation, block: bool) -> Result<String> {
    if block {
        Ok(format!("\\[{}\\]", render_eq(equation)?))
    } else {
        Ok(format!("\\({}\\)", render_eq(equation)?))
    }
}

fn render_eq(eq: Equation) -> Result<String> {
    let mut content = String::new();
    for op in eq {
        content.push_str(&render_op(op)?);
    }
    Ok(content)
}

fn render_group(eq: Equation) -> Result<String> {
    match eq.len() {
        0 => Ok("{}".to_string()),
        1 => render_op(eq.into_iter().next().unwrap()),
        _ => Ok(format!("{{{}}}", render_eq(eq)?)),
    }
}

fn render_op(op: MathOp) -> Result<String> {
    match op {
        MathOp::Text(text) => render_text(text),
        MathOp::Accent { char, body } => render_accent(char, body),
        MathOp::Box { body, display } => render_box(body, display),
        MathOp::BoxedFormula { body, align } => render_boxed_formula(body, align),
        MathOp::Brackets {
            open,
            close,
            body,
            align,
        } => render_brackets(open, close, body, align),
        MathOp::BracketsWithSeps {
            open,
            close,
            sep,
            segments,
            align,
        } => render_brackets_with_seps(open, close, sep, segments, align),
        MathOp::EquationArray {
            align,
            columns,
            rows,
        } => render_equation_array(columns, rows, align),
        MathOp::Fraction { num, den, small } => render_fraction(num, den, small),
        MathOp::FunctionApply { func, body } => render_function_apply(func, body),
        MathOp::LeftSubSup { sub, sup, body } => render_left_sub_sup(sub, sup, body),
        MathOp::LowerLimit { body, limit } => render_lower_limit(body, limit),
        MathOp::Matrix {
            align,
            columns,
            brackets,
            items,
        } => render_matrix(columns, items, brackets, align),
        MathOp::NAry {
            op,
            sub,
            sup,
            body,
            display,
        } => render_nary(op, sub, sup, body, display),
        MathOp::OverBar { body } => render_over_bar(body),
        MathOp::Phantom {
            kind,
            display,
            body,
        } => render_phantom(kind, display, body),
        MathOp::Radical { degree, body } => render_radical(degree, body),
        MathOp::SlashedFraction { num, den, linear } => render_slashed_fraction(num, den, linear),
        MathOp::Stack { num, den } => render_stack(num, den),
        MathOp::StretchStack { char, pos, body } => render_stretch_stack(char, pos, body),
        MathOp::Subscript { sub, body } => render_subscript(sub, body),
        MathOp::SubSup {
            align,
            sub,
            sup,
            body,
        } => render_sub_sup(sub, sup, body, align),
        MathOp::Superscript { sup, body } => render_superscript(sup, body),
        MathOp::UnderBar { body } => render_under_bar(body),
        MathOp::UpperLimit { body, limit } => render_upper_limit(body, limit),
    }
}

fn render_text(text: String) -> Result<String> {
    let filtered: String = text.chars().filter(|c| !is_skippable_format(*c)).collect();
    if filtered.is_empty() {
        return Ok(String::new());
    }

    let mut parts: Vec<(Option<TextType>, String)> = vec![];
    let mut current = String::new();
    let mut current_type = None;

    for (i, c) in filtered.char_indices() {
        let text_type = TextType::from_char(&c)?;
        if Some(text_type) != current_type {
            if i != 0 {
                parts.push((current_type, current));
                current = String::new();
            }
            current_type = Some(text_type);
        }
        current.push(c);
    }
    parts.push((current_type, current));

    let mut rendered_parts = Vec::with_capacity(parts.len());
    for (text_type, text) in parts {
        let rendered = match text_type {
            None => return Err(eyre!("No text type for text `{}`", text)),
            Some(TextType::Bold) => format!("\\mathbf{{{}}}", decode_letters(&text, text_type)),
            Some(TextType::BoldItalic) => {
                format!("\\boldsymbol{{{}}}", decode_letters(&text, text_type))
            }
            Some(TextType::BoldScript) => format!(
                "\\boldsymbol{{\\mathcal{{{}}}}}",
                decode_letters(&text, text_type)
            ),
            Some(TextType::Double) => {
                format!("\\mathbb{{{}}}", decode_letters(&text, text_type))
            }
            // Double-struck italic operators (ⅅ, ⅆ, ⅇ, ⅈ, ⅉ): MathJax /
            // unicode-math render the codepoint directly. Preserve the thin
            // space the MathML backend inserts to keep `dx`-style typography.
            Some(TextType::DoubleOperator) => format!("\\,{}", text),
            Some(TextType::Fraktur) => {
                format!("\\mathfrak{{{}}}", decode_letters(&text, text_type))
            }
            Some(TextType::FrakturBold) => format!(
                "\\boldsymbol{{\\mathfrak{{{}}}}}",
                decode_letters(&text, text_type)
            ),
            Some(TextType::Identifier) => render_identifier(&text),
            Some(TextType::Mono) => format!("\\mathtt{{{}}}", decode_letters(&text, text_type)),
            // `<mi mathvariant="normal">…</mi>` text — upright in MathML.
            // Use `\operatorname{}`, not bare text or `\mathrm{}`:
            //   - bare `cos` italicises in math mode (`c·o·s`).
            //   - `\mathrm{cos}` is upright but ord-class, so neighbours
            //     stay flush (`2ab\mathrm{cos}C` → `2abcosC`).
            //   - `\operatorname{cos}` is upright AND mathop-class, so
            //     adjacent ord atoms get the same 3mu auto-spacing that
            //     MathJax inserts around multi-letter `<mi>` runs. This
            //     also matches the implicit spacing of LaTeX's built-in
            //     `\cos`/`\sin`/… without us shipping a per-name table.
            Some(TextType::Normal) => format!("\\operatorname{{{}}}", text),
            Some(TextType::Numeric) => text,
            Some(TextType::Operator) => render_operator_run(&text),
            Some(TextType::Raw) => text,
            Some(TextType::Sans) => format!("\\mathsf{{{}}}", decode_letters(&text, text_type)),
            Some(TextType::SansBold) => format!(
                "\\boldsymbol{{\\mathsf{{{}}}}}",
                decode_letters(&text, text_type)
            ),
            Some(TextType::SansBoldItalic) => format!(
                "\\boldsymbol{{\\mathsf{{{}}}}}",
                decode_letters(&text, text_type)
            ),
            Some(TextType::SansItalic) => {
                format!("\\mathsf{{{}}}", decode_letters(&text, text_type))
            }
            Some(TextType::Script) => {
                format!("\\mathcal{{{}}}", decode_letters(&text, text_type))
            }
            Some(TextType::Space) => "\\,".to_string(),
        };
        rendered_parts.push(rendered);
    }

    Ok(rendered_parts.iter().join(""))
}

fn decode_letters(text: &str, text_type: Option<TextType>) -> String {
    let base = text_type.and_then(range_start).map(|c| c as u32);
    text.chars()
        .map(|c| {
            if let Some(base) = base {
                let offset = (c as u32).wrapping_sub(base);
                if offset <= 25 {
                    return char::from_u32(b'A' as u32 + offset).unwrap_or(c);
                }
                if (26..=51).contains(&offset) {
                    return char::from_u32(b'a' as u32 + offset - 26).unwrap_or(c);
                }
            }
            // Fall back to the generic styled-letter decoder so that e.g. a
            // bold-italic greek char in a `Bold` run still gets unwrapped to
            // its base codepoint before we apply `\mathbf{...}`.
            decode_styled_letter(c)
        })
        .collect()
}

fn range_start(text_type: TextType) -> Option<char> {
    Some(match text_type {
        TextType::Bold => '\u{1d400}',
        TextType::BoldItalic => '\u{1d468}',
        TextType::BoldScript => '\u{1d4d0}',
        TextType::Double => '\u{1d538}',
        TextType::Fraktur => '\u{1d504}',
        TextType::FrakturBold => '\u{1d56c}',
        TextType::Mono => '\u{1d670}',
        TextType::Sans => '\u{1d5a0}',
        TextType::SansBold => '\u{1d5d4}',
        TextType::SansBoldItalic => '\u{1d63c}',
        TextType::SansItalic => '\u{1d608}',
        TextType::Script => '\u{1d49c}',
        _ => return None,
    })
}

fn render_identifier(text: &str) -> String {
    text.chars().map(decode_styled_letter).collect()
}

/// Decode a styled Mathematical Alphanumeric Symbol back to its base char.
/// Math-italic Latin letters become plain ASCII (so that math mode applies its
/// default italic rendering), and styled greek letters become their base greek
/// codepoints (which MathJax/unicode-math render natively). Other styled forms
/// are left to the per-style helpers (`\mathbf{...}` etc.) which apply their
/// own decoding before wrapping.
fn decode_styled_letter(c: char) -> char {
    let code = c as u32;

    // Italic Latin: U+1D434..=U+1D467 (A..Z then a..z)
    if (0x1d434..=0x1d44d).contains(&code) {
        return char::from_u32(b'A' as u32 + (code - 0x1d434)).unwrap_or(c);
    }
    if (0x1d44e..=0x1d467).contains(&code) {
        return char::from_u32(b'a' as u32 + (code - 0x1d44e)).unwrap_or(c);
    }
    // Italic small h hole substitute
    if c == '\u{210E}' {
        return 'h';
    }

    // Styled greek: 5 ranges of 58 chars each, identical layout.
    // U+1D6A8 bold, U+1D6E2 italic, U+1D71C bold-italic,
    // U+1D756 sans-bold, U+1D790 sans-bold-italic.
    for &start in &[0x1d6a8u32, 0x1d6e2, 0x1d71c, 0x1d756, 0x1d790] {
        if (start..start + 58).contains(&code) {
            return greek_offset_to_base((code - start) as u8).unwrap_or(c);
        }
    }

    c
}

fn greek_offset_to_base(offset: u8) -> Option<char> {
    // Layout per Unicode Mathematical Alphanumeric Symbols, greek block:
    // 0..=24: Α Β Γ Δ Ε Ζ Η Θ Ι Κ Λ Μ Ν Ξ Ο Π Ρ ϴ Σ Τ Υ Φ Χ Ψ Ω
    // 25:    ∇
    // 26..=50: α β γ δ ε ζ η θ ι κ λ μ ν ξ ο π ρ ς σ τ υ φ χ ψ ω
    // 51:    ∂
    // 52..=57: ϵ ϑ ϰ ϕ ϱ ϖ
    const CAPS: &str = "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡϴΣΤΥΦΧΨΩ";
    const LOWERS: &str = "αβγδεζηθικλμνξοπρςστυφχψω";
    const EXTRA: &str = "ϵϑϰϕϱϖ";
    match offset {
        0..=24 => CAPS.chars().nth(offset as usize),
        25 => Some('∇'),
        26..=50 => LOWERS.chars().nth(offset as usize - 26),
        51 => Some('∂'),
        52..=57 => EXTRA.chars().nth(offset as usize - 52),
        _ => None,
    }
}

fn render_operator_run(text: &str) -> String {
    text.chars()
        .map(decode_styled_letter)
        .map(escape_operator_char)
        .collect()
}

/// Escape only the characters that LaTeX itself parses specially in math mode.
/// Everything else (`α`, `≤`, `∂`, `≠`, …) passes through as Unicode and is
/// rendered by MathJax / unicode-math.
fn escape_operator_char(c: char) -> String {
    match c {
        // Function-application invisible operator: the parser consumes
        // these into a structured `MathOp::FunctionApply`, so this branch
        // is for the rare case where one slipped through as raw text.
        // Drop it — `render_function_apply` supplies the visible spacing.
        '\u{2061}' => String::new(),
        '\\' => "\\backslash ".to_string(),
        '{' => "\\{".to_string(),
        '}' => "\\}".to_string(),
        '$' => "\\$".to_string(),
        '#' => "\\#".to_string(),
        '%' => "\\%".to_string(),
        '_' => "\\_".to_string(),
        '^' => "\\hat{}".to_string(),
        '&' => "\\&".to_string(),
        _ => c.to_string(),
    }
}

fn render_accent(char: char, body: Equation) -> Result<String> {
    let body = render_group(body)?;
    if let Some(cmd) = accent_command(char) {
        Ok(format!("{}{{{}}}", cmd, body))
    } else {
        Ok(format!("\\overset{{{}}}{{{}}}", char, body))
    }
}

fn accent_command(c: char) -> Option<&'static str> {
    Some(match c {
        // Combining marks
        '\u{0300}' => "\\grave",
        '\u{0301}' => "\\acute",
        '\u{0302}' => "\\hat",
        '\u{0303}' => "\\tilde",
        '\u{0304}' => "\\bar",
        '\u{0306}' => "\\breve",
        '\u{0307}' => "\\dot",
        '\u{0308}' => "\\ddot",
        '\u{030A}' => "\\mathring",
        '\u{030C}' => "\\check",
        '\u{20D7}' => "\\vec",
        // Spacing variants sometimes seen
        '\u{02C6}' => "\\hat",
        '\u{02DC}' => "\\tilde",
        '\u{00AF}' => "\\bar",
        '\u{02D9}' => "\\dot",
        '\u{00A8}' => "\\ddot",
        '\u{02C7}' => "\\check",
        '\u{02D8}' => "\\breve",
        '\u{02DA}' => "\\mathring",
        _ => return None,
    })
}

fn render_box(body: Equation, display: Option<BoxDisplay>) -> Result<String> {
    let mut content = render_group(body)?;
    let Some(display) = display else {
        return Ok(content);
    };

    content = match display.size {
        BoxSize::Script => format!("{{\\scriptstyle {}}}", content),
        BoxSize::ScriptScript => format!("{{\\scriptscriptstyle {}}}", content),
        _ => content,
    };

    if display.flags.contains(BoxFlags::NoBreak) {
        content = format!("\\nobreak {}", content);
    }

    Ok(content)
}

fn render_boxed_formula(body: Equation, align: Option<BoxedFormulaAlignment>) -> Result<String> {
    if align.is_some() {
        math_warn!(
            "Math feature not implemented: boxed-formula alignment in LaTeX. Please provide a sample at https://github.com/msiemens/one2html/issues."
        );
    }

    Ok(format!("\\boxed{{{}}}", render_group(body)?))
}

fn render_brackets(
    open: Option<char>,
    close: Option<char>,
    body: Equation,
    align: Option<BracketsAlignment>,
) -> Result<String> {
    let body = render_group(body)?;
    let open_str = bracket_open_str(open);
    let close_str = bracket_close_str(close);

    Ok(match align {
        Some(BracketsAlignment::DontGrow) => {
            format!("{}{}{}", open_str, body, close_str)
        }
        Some(BracketsAlignment::TeXbig) => {
            format!("\\bigl{} {} \\bigr{}", open_str, body, close_str)
        }
        Some(BracketsAlignment::TeXBig) => {
            format!("\\Bigl{} {} \\Bigr{}", open_str, body, close_str)
        }
        Some(BracketsAlignment::TeXbigg) => {
            format!("\\biggl{} {} \\biggr{}", open_str, body, close_str)
        }
        Some(BracketsAlignment::TeXBigg) => {
            format!("\\Biggl{} {} \\Biggr{}", open_str, body, close_str)
        }
        None => format!("\\left{} {} \\right{}", open_str, body, close_str),
    })
}

fn render_brackets_with_seps(
    open: Option<char>,
    close: Option<char>,
    sep: char,
    segments: Vec<Equation>,
    align: Option<BracketsAlignment>,
) -> Result<String> {
    let open_str = bracket_open_str(open);
    let close_str = bracket_close_str(close);
    let sep_str = bracket_delim_str(sep);

    let segments = segments
        .into_iter()
        .map(render_group)
        .collect::<Result<Vec<_>>>()?;

    let joined = segments.join(&format!(" \\,\\middle{}\\, ", sep_str));

    Ok(match align {
        Some(BracketsAlignment::DontGrow) => {
            format!("{}{}{}", open_str, joined, close_str)
        }
        Some(BracketsAlignment::TeXbig) => {
            format!("\\bigl{} {} \\bigr{}", open_str, joined, close_str)
        }
        Some(BracketsAlignment::TeXBig) => {
            format!("\\Bigl{} {} \\Bigr{}", open_str, joined, close_str)
        }
        Some(BracketsAlignment::TeXbigg) => {
            format!("\\biggl{} {} \\biggr{}", open_str, joined, close_str)
        }
        Some(BracketsAlignment::TeXBigg) => {
            format!("\\Biggl{} {} \\Biggr{}", open_str, joined, close_str)
        }
        None => format!("\\left{} {} \\right{}", open_str, joined, close_str),
    })
}

fn bracket_open_str(c: Option<char>) -> String {
    match c {
        None => ".".to_string(),
        Some(c) => bracket_delim_str(c),
    }
}

fn bracket_close_str(c: Option<char>) -> String {
    match c {
        None => ".".to_string(),
        Some(c) => bracket_delim_str(c),
    }
}

fn bracket_delim_str(c: char) -> String {
    match c {
        '{' => "\\{".to_string(),
        '}' => "\\}".to_string(),
        '⟨' => "\\langle ".to_string(),
        '⟩' => "\\rangle ".to_string(),
        '⌊' => "\\lfloor ".to_string(),
        '⌋' => "\\rfloor ".to_string(),
        '⌈' => "\\lceil ".to_string(),
        '⌉' => "\\rceil ".to_string(),
        '‖' => "\\|".to_string(),
        _ => c.to_string(),
    }
}

fn render_equation_array(
    _columns: u8,
    rows: Vec<Equation>,
    align: Option<EquationArrayAlignment>,
) -> Result<String> {
    if align.is_some() {
        math_warn!(
            "Math feature not implemented: equation-array alignment in LaTeX. Please provide a sample at https://github.com/msiemens/one2html/issues."
        );
    }

    let rows = rows
        .into_iter()
        .map(render_eq)
        .collect::<Result<Vec<_>>>()?
        .join(" \\\\ ");

    Ok(format!("\\begin{{aligned}} {} \\end{{aligned}}", rows))
}

fn render_fraction(num: Equation, den: Equation, small: bool) -> Result<String> {
    let cmd = if small { "\\tfrac" } else { "\\frac" };
    Ok(format!(
        "{}{{{}}}{{{}}}",
        cmd,
        render_eq(num)?,
        render_eq(den)?
    ))
}

fn render_function_apply(func: Equation, body: Equation) -> Result<String> {
    // The U+2061 invisible function-application operator in the source
    // MathML carries the visible thin space between e.g. `cos` and its
    // argument. We don't need to inject one here: the function name is
    // rendered via `\operatorname{}` (mathop-class), so adjacent atoms
    // pick up TeX's automatic 3mu spacing — same look as `\cos x`.
    Ok(format!("{}{}", render_group(func)?, render_group(body)?))
}

fn render_left_sub_sup(sub: Equation, sup: Equation, body: Equation) -> Result<String> {
    let sub = if sub.is_empty() {
        "{}".to_string()
    } else {
        format!("{{{}}}", render_eq(sub)?)
    };
    let sup = if sup.is_empty() {
        "{}".to_string()
    } else {
        format!("{{{}}}", render_eq(sup)?)
    };
    Ok(format!("{{}}_{}^{}{}", sub, sup, render_group(body)?))
}

fn render_lower_limit(body: Equation, limit: Equation) -> Result<String> {
    Ok(format!(
        "\\underset{{{}}}{{{}}}",
        render_eq(limit)?,
        render_eq(body)?
    ))
}

fn render_matrix(
    columns: u8,
    items: Vec<Equation>,
    brackets: Option<MatrixBrackets>,
    align: Option<MatrixAlignment>,
) -> Result<String> {
    let show_placeholder = matches!(align, Some(MatrixAlignment::ShowMatPlaceHldr));
    let env = match brackets {
        Some(MatrixBrackets::Parentheses) => "pmatrix",
        Some(MatrixBrackets::VerticalBars) => "vmatrix",
        Some(MatrixBrackets::DoubleVerticalBars) => "Vmatrix",
        None => "matrix",
    };

    let rows = items
        .into_iter()
        .chunks(columns as usize)
        .into_iter()
        .map(|row| {
            let mut cells = row.collect_vec();
            while cells.len() < columns as usize {
                cells.push(Vec::new());
            }
            render_matrix_row(cells, show_placeholder)
        })
        .collect::<Result<Vec<_>>>()?
        .join(" \\\\ ");

    Ok(format!("\\begin{{{0}}} {1} \\end{{{0}}}", env, rows))
}

fn render_matrix_row(items: Vec<Equation>, show_placeholder: bool) -> Result<String> {
    items
        .into_iter()
        .map(|eq| {
            if eq.is_empty() && show_placeholder {
                Ok("\\square".to_string())
            } else if eq.is_empty() {
                Ok(String::new())
            } else {
                render_eq(eq)
            }
        })
        .collect::<Result<Vec<_>>>()
        .map(|cells| cells.join(" & "))
}

fn render_nary(
    op: char,
    sub: Equation,
    sup: Equation,
    body: Equation,
    display: Option<NAryDisplay>,
) -> Result<String> {
    let op_cmd = nary_op_command(op);
    let sub_is_empty = sub.is_empty();
    let sup_is_empty = sup.is_empty();

    let mut sub_content = if sub_is_empty {
        String::new()
    } else {
        render_eq(sub)?
    };
    let mut sup_content = if sup_is_empty {
        String::new()
    } else {
        render_eq(sup)?
    };

    let mut limits = "";

    if let Some(display) = display {
        if display.options.contains(NAryOptions::ShowLLimPlaceHldr) && sub_is_empty {
            sub_content = "\\square".to_string();
        }
        if display.options.contains(NAryOptions::ShowULimPlaceHldr) && sup_is_empty {
            sup_content = "\\square".to_string();
        }
        if display.options.contains(NAryOptions::LimitsOpposite) {
            std::mem::swap(&mut sub_content, &mut sup_content);
        }

        limits = match display.align {
            NAryAlignment::LimitsSubSup | NAryAlignment::UpperLimitAsSuperScript => "\\nolimits",
            NAryAlignment::LimitsUnderOver => "\\limits",
            NAryAlignment::LimitsDefault => "",
        };
    }

    let sub_part = if sub_content.is_empty() {
        String::new()
    } else {
        format!("_{{{}}}", sub_content)
    };
    let sup_part = if sup_content.is_empty() {
        String::new()
    } else {
        format!("^{{{}}}", sup_content)
    };

    Ok(format!(
        "{}{}{}{} {}",
        op_cmd,
        limits,
        sub_part,
        sup_part,
        render_group(body)?
    ))
}

fn nary_op_command(c: char) -> String {
    match c {
        '∑' => "\\sum".to_string(),
        '∏' => "\\prod".to_string(),
        '∐' => "\\coprod".to_string(),
        '∫' => "\\int".to_string(),
        '∬' => "\\iint".to_string(),
        '∭' => "\\iiint".to_string(),
        '⨌' => "\\iiiint".to_string(),
        '∮' => "\\oint".to_string(),
        '∯' => "\\oiint".to_string(),
        '∰' => "\\oiiint".to_string(),
        '⋃' => "\\bigcup".to_string(),
        '⋂' => "\\bigcap".to_string(),
        '⨁' => "\\bigoplus".to_string(),
        '⨂' => "\\bigotimes".to_string(),
        '⨄' => "\\biguplus".to_string(),
        '⨆' => "\\bigsqcup".to_string(),
        '⋁' => "\\bigvee".to_string(),
        '⋀' => "\\bigwedge".to_string(),
        _ => c.to_string(),
    }
}

fn render_over_bar(body: Equation) -> Result<String> {
    Ok(format!("\\overline{{{}}}", render_eq(body)?))
}

fn render_phantom(
    kind: PhantomKind,
    display: Option<PhantomDisplay>,
    body: Equation,
) -> Result<String> {
    let mut show = matches!(
        kind,
        PhantomKind::AscentSmash
            | PhantomKind::DescentSmash
            | PhantomKind::HorizontalSmash
            | PhantomKind::VerticalSmash
    );
    let mut transparent = false;
    let mut zero_width = false;
    let mut zero_ascent = false;
    let mut zero_descent = false;

    if let Some(display) = display {
        show |= display.contains(PhantomDisplay::PhantomShow);
        transparent |= display.contains(PhantomDisplay::PhantomTransparent);
        zero_width |= display.contains(PhantomDisplay::PhantomZeroWidth);
        zero_ascent |= display.contains(PhantomDisplay::PhantomZeroAscent);
        zero_descent |= display.contains(PhantomDisplay::PhantomZeroDescent);
    }

    match kind {
        PhantomKind::FullOrCustom => {}
        PhantomKind::HorizontalPhantom => {
            zero_ascent = true;
            zero_descent = true;
        }
        PhantomKind::VerticalPhantom => {
            zero_width = true;
        }
        PhantomKind::AscentSmash => {
            zero_ascent = true;
        }
        PhantomKind::DescentSmash => {
            zero_descent = true;
        }
        PhantomKind::HorizontalSmash => {
            zero_width = true;
        }
        PhantomKind::VerticalSmash => {
            zero_ascent = true;
            zero_descent = true;
        }
    }

    let body = render_group(body)?;

    if !show {
        if zero_width && zero_ascent && zero_descent {
            return Ok(format!("\\phantom{{{}}}", body));
        }
        if zero_ascent && zero_descent && !zero_width {
            return Ok(format!("\\hphantom{{{}}}", body));
        }
        if zero_width && !zero_ascent && !zero_descent {
            return Ok(format!("\\vphantom{{{}}}", body));
        }
        return Ok(format!("\\phantom{{{}}}", body));
    }

    let mut content = body;

    if zero_ascent && zero_descent {
        content = format!("\\smash{{{}}}", content);
    } else if zero_ascent {
        content = format!("\\smash[t]{{{}}}", content);
    } else if zero_descent {
        content = format!("\\smash[b]{{{}}}", content);
    }

    if transparent {
        content = format!("{{\\color{{transparent}}{}}}", content);
    }

    Ok(content)
}

fn render_radical(degree: Equation, body: Equation) -> Result<String> {
    if degree.is_empty() {
        Ok(format!("\\sqrt{{{}}}", render_eq(body)?))
    } else {
        Ok(format!(
            "\\sqrt[{}]{{{}}}",
            render_eq(degree)?,
            render_eq(body)?
        ))
    }
}

fn render_slashed_fraction(num: Equation, den: Equation, linear: bool) -> Result<String> {
    if linear {
        Ok(format!("{}/{}", render_group(num)?, render_group(den)?))
    } else {
        Ok(format!(
            "{{}}^{{{}}}\\!/\\!_{{{}}}",
            render_eq(num)?,
            render_eq(den)?
        ))
    }
}

fn render_stack(num: Equation, den: Equation) -> Result<String> {
    Ok(format!(
        "\\substack{{{} \\\\ {}}}",
        render_eq(num)?,
        render_eq(den)?
    ))
}

fn render_stretch_stack(char: char, pos: StretchStackPosition, body: Equation) -> Result<String> {
    let body_str = render_group(body)?;

    // Specific stretchable mappings
    let cmd = match (char, &pos) {
        ('\u{23DE}', _) | ('\u{FE37}', _) => Some("\\overbrace"),
        ('\u{23DF}', _) | ('\u{FE38}', _) => Some("\\underbrace"),
        ('\u{23B4}', _) => Some("\\overbracket"),
        ('\u{23B5}', _) => Some("\\underbracket"),
        _ => None,
    };

    if let Some(cmd) = cmd {
        return Ok(format!("{}{{{}}}", cmd, body_str));
    }

    // Stretchy arrows on top/bottom
    let xarrow = match char {
        '→' => Some("\\xrightarrow"),
        '←' => Some("\\xleftarrow"),
        '↔' => Some("\\xleftrightarrow"),
        '⇒' => Some("\\xRightarrow"),
        '⇐' => Some("\\xLeftarrow"),
        '⇔' => Some("\\xLeftrightarrow"),
        _ => None,
    };

    if let Some(cmd) = xarrow {
        return Ok(format!("{}{{{}}}", cmd, body_str));
    }

    let setter = match pos {
        StretchStackPosition::CharBelow => "\\underset",
        StretchStackPosition::CharAbove => "\\overset",
        StretchStackPosition::BaseBelow => "\\overset",
        StretchStackPosition::BaseAbove => "\\underset",
    };

    Ok(format!("{}{{{}}}{{{}}}", setter, char, body_str))
}

fn render_subscript(sub: Equation, body: Equation) -> Result<String> {
    let sub = if sub.is_empty() {
        "{\\square}".to_string()
    } else {
        format!("{{{}}}", render_eq(sub)?)
    };
    Ok(format!("{}_{}", render_group(body)?, sub))
}

fn render_sub_sup(
    sub: Equation,
    sup: Equation,
    body: Equation,
    align: Option<SubSupAlignment>,
) -> Result<String> {
    if align.is_some() {
        math_warn!(
            "Math feature not implemented: sub-sup alignment in LaTeX. Please provide a sample at https://github.com/msiemens/one2html/issues."
        );
    }

    let sub = if sub.is_empty() {
        "{\\square}".to_string()
    } else {
        format!("{{{}}}", render_eq(sub)?)
    };
    let sup = if sup.is_empty() {
        "{\\square}".to_string()
    } else {
        format!("{{{}}}", render_eq(sup)?)
    };

    Ok(format!("{}_{}^{}", render_group(body)?, sub, sup))
}

fn render_superscript(sup: Equation, body: Equation) -> Result<String> {
    let sup = if sup.is_empty() {
        "{\\square}".to_string()
    } else {
        format!("{{{}}}", render_eq(sup)?)
    };
    Ok(format!("{}^{}", render_group(body)?, sup))
}

fn render_under_bar(body: Equation) -> Result<String> {
    Ok(format!("\\underline{{{}}}", render_eq(body)?))
}

fn render_upper_limit(body: Equation, limit: Equation) -> Result<String> {
    Ok(format!(
        "\\overset{{{}}}{{{}}}",
        render_eq(limit)?,
        render_eq(body)?
    ))
}
