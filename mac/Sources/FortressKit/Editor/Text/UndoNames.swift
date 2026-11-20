import Foundation

/// The names Edit ▸ Undo and Redo show for the editor's own edits ("Undo Bold", "Redo
/// Move"). Typing, Paste, Cut, Drag, Replace and spelling are named by NSTextView.
enum UndoNames {
    static let typing = "Typing"
    static let paste = "Paste"
    static let drop = "Drop"
    static let move = "Move"
    static let indent = "Indent"
    static let outdent = "Outdent"
    static let checkbox = "Checkbox"
    static let insertTable = "Insert Table"
    static let link = "Link"
    static let removeLink = "Remove Link"
    static let noteLink = "Note Link"
    static let highlight = "Highlight"
    static let textColor = "Text Color"
    static let divider = "Divider"
    static let callout = "Callout"
    static let columns = "Columns"
    static let addColumn = "Add Column"
    static let removeColumn = "Remove Column"
    static let unwrapColumns = "Unwrap Columns"
    static let duplicate = "Duplicate"
    static let delete = "Delete"
    static let cut = "Cut"
    static let insertBlock = "Insert Block"
    static let math = "Math"
    static let title = "Title Change"
    static let status = "Status Change"

    static func mark(_ keyPath: WritableKeyPath<InlineMarks, Bool>) -> String {
        switch keyPath {
        case \InlineMarks.bold: "Bold"
        case \InlineMarks.italic: "Italic"
        case \InlineMarks.underline: "Underline"
        case \InlineMarks.strike: "Strikethrough"
        case \InlineMarks.code: "Inline Code"
        case \InlineMarks.sup: "Superscript"
        case \InlineMarks.sub: "Subscript"
        default: "Format"
        }
    }

    /// A paragraph style change ("Undo Bulleted List").
    static func kind(_ kind: BlockKind) -> String {
        switch kind {
        case .paragraph: "Body"
        case .heading1: "Heading 1"
        case .heading2: "Heading 2"
        case .heading3: "Heading 3"
        case .heading4: "Heading 4"
        case .heading5: "Heading 5"
        case .heading6: "Heading 6"
        case .bullet: "Bulleted List"
        case .numbered: "Numbered List"
        case .check: "Checklist"
        case .code: "Code Block"
        case .quote: "Quote"
        case .divider: "Divider"
        default: "Paragraph Style"
        }
    }
}
