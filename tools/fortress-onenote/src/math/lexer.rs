use anyhow::Result;
use onenote_parser::contents::{MathInlineObject, MathObjectType};
use std::collections::VecDeque;

// See https://learn.microsoft.com/en-us/archive/blogs/murrays/officemath for the origin of those
// constants
const TOK_START: &str = "\u{fdd0}";
const TOK_SEP: &str = "\u{fdee}";
const TOK_END: &str = "\u{fdef}";

#[derive(PartialEq, Debug)]
pub(super) enum Token {
    Text(String),
    Start(MathInlineObject),
    Sep(MathObjectType),
    End(MathObjectType),
    Eof,
}

pub(super) struct Lexer {
    tokens: VecDeque<Token>,
}

impl Lexer {
    /// fortress-onenote change: tokenise every start/separator/end mark
    /// wherever it sits in a text run, and take the object type of a
    /// separator or end from the innermost open object. OneNote puts each
    /// mark in a run of its own object's formatting; other writers (e.g.
    /// NoteCraft) put a whole equation's arguments in one plain math run,
    /// which the upstream lexer could not read (`latex: null`).
    pub(super) fn new(segments: Vec<(String, MathInlineObject)>) -> Lexer {
        let mut tokens = VecDeque::new();
        let mut open: Vec<MathObjectType> = Vec::new();
        for (text, object) in segments {
            let mut buf = String::new();
            for ch in text.chars() {
                let mark = [TOK_START, TOK_SEP, TOK_END]
                    .iter()
                    .position(|t| t.starts_with(ch));
                let Some(mark) = mark else {
                    buf.push(ch);
                    continue;
                };
                if !buf.is_empty() {
                    tokens.push_back(Token::Text(std::mem::take(&mut buf)));
                }
                let current = open.last().copied().unwrap_or(object.object_type());
                match mark {
                    0 => {
                        open.push(object.object_type());
                        tokens.push_back(Token::Start(object));
                    }
                    1 => tokens.push_back(Token::Sep(current)),
                    _ => {
                        open.pop();
                        tokens.push_back(Token::End(current));
                    }
                }
            }
            if !buf.is_empty() {
                tokens.push_back(Token::Text(buf));
            }
        }
        Lexer { tokens }
    }

    pub(super) fn next(&mut self) -> Result<Token> {
        Ok(self.tokens.pop_front().unwrap_or(Token::Eof))
    }
}
