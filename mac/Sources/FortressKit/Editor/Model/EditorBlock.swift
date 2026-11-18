import Foundation

// The editor's model of a note: a flat list of blocks (one per paragraph in the text
// view) with a nesting depth. It is the bridge between the backend's block tree
// (`BlockNode`) and the text system, and is Foundation-only so it can be unit-tested.

/// How a block is presented and edited.
public enum BlockKind: String, Sendable, Hashable, CaseIterable {
    case paragraph, heading1, heading2, heading3, heading4, heading5, heading6, bullet, numbered, check, code
    /// A paragraph inside a `quote` (or `callout`) container: each line of a quote is
    /// one of its paragraph children (docs/block-model.md §2).
    case quote
    /// Atomic blocks: one attachment character, no inline text.
    case divider, image, file
    /// Blocks with their own editing inside the attachment: a table grid, side-by-side
    /// columns (each with its own blocks), and math (shown, saved untouched).
    case table, columns, math
    /// Any block the editor doesn't edit (raw HTML, types added later, quotes holding
    /// more than lines of text…): shown read-only and never rewritten.
    case embed

    public var hasInlineText: Bool {
        switch self {
        case .divider, .image, .file, .embed, .table, .columns, .math: false
        default: true
        }
    }

    /// Keeps its children to itself: nothing in the main flow nests under it.
    public var holdsOwnChildren: Bool { self == .embed || self == .columns || self == .table }

    /// No block nests under it in the main flow (atomic blocks, containers shown as one
    /// attachment, and quote lines, whose container holds only them).
    public var takesNoChildren: Bool {
        switch self {
        case .divider, .image, .file, .math, .embed, .table, .columns, .quote, .code: true
        default: false
        }
    }

    public var isList: Bool { self == .bullet || self == .numbered || self == .check }

    public var headingLevel: Int? {
        switch self {
        case .heading1: 1
        case .heading2: 2
        case .heading3: 3
        case .heading4: 4
        case .heading5: 5
        case .heading6: 6
        default: nil
        }
    }

    /// The heading kind for `level` (1–6; never clamped below 6).
    public static func heading(_ level: Int) -> BlockKind {
        switch level {
        case ...1: .heading1
        case 2: .heading2
        case 3: .heading3
        case 4: .heading4
        case 5: .heading5
        default: .heading6
        }
    }

    /// The canonical type written for this kind (a quote line is a paragraph; its
    /// container is written separately).
    public var canonicalType: String {
        switch self {
        case .paragraph, .quote: "paragraph"
        case .heading1, .heading2, .heading3, .heading4, .heading5, .heading6: "heading"
        case .bullet: "bullet_item"
        case .numbered: "numbered_item"
        case .check: "check_item"
        case .code: "code"
        case .divider: "divider"
        case .image: "image"
        case .file: "file"
        case .table: "table"
        case .columns: "columns"
        case .math: "math"
        case .embed: "html"
        }
    }
}

/// Inline formatting of a run of text: the marks of docs/block-model.md §3 plus the
/// href of the `link` node around it.
public struct InlineMarks: Hashable, Sendable {
    public var bold = false
    public var italic = false
    public var underline = false
    public var strike = false
    public var code = false
    public var sup = false
    public var sub = false
    /// Text colour (`color` mark): a colour name ("red", …) or CSS colour ("#B4532A").
    public var textColor: String?
    /// Background (`highlight` mark), same vocabulary as `textColor`.
    public var highlight: String?
    /// Link target (`href` of a `link` node): a URL or `/media/…`.
    public var link: String?

    public init(
        bold: Bool = false, italic: Bool = false, underline: Bool = false, strike: Bool = false,
        code: Bool = false, textColor: String? = nil, highlight: String? = nil, link: String? = nil,
        sup: Bool = false, sub: Bool = false
    ) {
        self.sup = sup
        self.sub = sub
        self.bold = bold
        self.italic = italic
        self.underline = underline
        self.strike = strike
        self.code = code
        self.textColor = textColor
        self.highlight = highlight
        self.link = link
    }

    public var isPlain: Bool { self == InlineMarks() }

    /// The same marks without the link (links are wrapper nodes).
    var withoutLink: InlineMarks {
        var copy = self
        copy.link = nil
        return copy
    }
}

/// The object replacement character: stands in for an atomic inline node or block.
public let objectReplacement = "\u{FFFC}"
/// A line break inside one block (a "\n" inside a text node).
public let softBreak = "\u{2028}"

/// A run of inline content: text with marks, or an atomic inline node kept verbatim
/// (`docLink`, `blockRef`, anything newer), whose text is `objectReplacement`.
public struct InlineRun: Hashable, Sendable {
    public var text: String
    public var marks: InlineMarks
    public var node: JSONValue?

    public init(_ text: String, _ marks: InlineMarks = InlineMarks(), node: JSONValue? = nil) {
        self.text = text
        self.marks = marks
        self.node = node
    }

    public static func atomic(_ node: JSONValue) -> InlineRun {
        InlineRun(objectReplacement, InlineMarks(), node: node)
    }
}

/// The block as the server sent it. Kept so saving an edited block keeps attributes the
/// editor doesn't manage (extension attributes are kept by the attrs patch anyway), and
/// so blocks the editor doesn't edit are never rewritten.
public struct BlockOrigin: Hashable, Sendable {
    public var type: String
    public var attrs: [String: JSONValue]
    public var userAttrs: [String: JSONValue]
    public var content: JSONValue
    public var payload: JSONValue?
    public var text: String
    /// The kind it decoded to; a different kind on save means the person converted it.
    public var kind: BlockKind
    public var createdAt: String
    public var updatedAt: String
    /// Children of a block that keeps them to itself (an embed); never flattened.
    public var children: [BlockNode]
    /// Where the block came from (spec §7a). Read-only here: never sent on update.
    public var provenance: JSONValue?
    /// Note revision that last changed the block.
    public var revision: Int?

    public init(
        type: String, attrs: [String: JSONValue] = [:], userAttrs: [String: JSONValue] = [:],
        content: JSONValue = .null, payload: JSONValue? = nil, text: String = "", kind: BlockKind,
        createdAt: String = "", updatedAt: String = "", children: [BlockNode] = [],
        provenance: JSONValue? = nil, revision: Int? = nil
    ) {
        self.provenance = provenance
        self.revision = revision
        self.type = type
        self.attrs = attrs
        self.userAttrs = userAttrs
        self.content = content
        self.payload = payload
        self.text = text
        self.kind = kind
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.children = children
    }

    public init(_ node: BlockNode, kind: BlockKind, keepingChildren: Bool = false) {
        self.init(type: node.type, attrs: node.attrs, userAttrs: node.userAttrs, content: node.content,
                  payload: node.payload, text: node.text, kind: kind, createdAt: node.createdAt,
                  updatedAt: node.updatedAt, children: keepingChildren ? node.children : [],
                  provenance: node.provenance, revision: node.revision)
    }
}

/// The `quote` or `callout` container a quote line belongs to.
public struct QuoteContainer: Hashable, Sendable {
    public var id: String
    public var type: String
    public var attrs: [String: JSONValue]

    public init(id: String, type: String = "quote", attrs: [String: JSONValue] = [:]) {
        self.id = id
        self.type = type
        self.attrs = attrs
    }
}

/// One paragraph of the editor.
public struct EditorBlock: Hashable, Sendable {
    public var id: String
    public var kind: BlockKind
    public var depth: Int
    public var checked: Bool
    /// Code block language.
    public var language: String?
    public var runs: [InlineRun]
    public var origin: BlockOrigin?
    /// Provenance to record when this block is first saved (e.g. pasted from a web page).
    public var newProvenance: JSONValue?
    /// A table's rows and cells (kind `.table`).
    public var table: TableContent?
    /// A columns block's columns, each with its blocks (kind `.columns`).
    public var columns: [EditorColumn]?
    /// The container of a quote line (kind `.quote`); nil for a line that just became one
    /// (it joins the quote above, or a new container is made for it).
    public var container: QuoteContainer?

    public init(
        id: String = EditorBlock.newId(), kind: BlockKind = .paragraph, depth: Int = 0,
        checked: Bool = false, language: String? = nil, runs: [InlineRun] = [],
        origin: BlockOrigin? = nil, newProvenance: JSONValue? = nil, table: TableContent? = nil,
        columns: [EditorColumn]? = nil, container: QuoteContainer? = nil
    ) {
        self.container = container
        self.table = table
        self.columns = columns
        self.newProvenance = newProvenance
        self.id = id
        self.kind = kind
        self.depth = depth
        self.checked = checked
        self.language = language
        self.runs = runs
        self.origin = origin
    }

    /// New block IDs are lowercase UUIDs (the backend accepts UUIDs and ULIDs).
    public static func newId() -> String { UUID().uuidString.lowercased() }

    public var plainText: String { BlockCodec.plainText(runs) }
}

/// What a transaction writes for a block: its canonical type, attrs, inline content
/// and payload (docs/block-model.md §1). The server derives everything else.
public struct BlockFields: Hashable, Sendable {
    public var type: String
    public var attrs: [String: JSONValue]
    /// Inline nodes (text blocks) or `.null`.
    public var content: JSONValue
    public var payload: JSONValue?

    public init(type: String, attrs: [String: JSONValue] = [:], content: JSONValue = .null, payload: JSONValue? = nil) {
        self.type = type
        self.attrs = attrs
        self.content = content
        self.payload = payload
    }

    /// The fields of a block as the server sent it.
    public init(_ node: BlockNode) {
        self.init(type: node.type, attrs: node.attrs, content: node.content, payload: node.payload)
    }
}
