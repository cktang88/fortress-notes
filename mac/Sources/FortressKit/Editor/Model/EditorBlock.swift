import Foundation

// The editor's model of a note: a flat list of blocks (one per paragraph in the text
// view) with a nesting depth. It is the bridge between the backend's block tree
// (`BlockNode`) and the text system, and is Foundation-only so it can be unit-tested.

/// How a block is presented and edited.
public enum BlockKind: String, Sendable, Hashable, CaseIterable {
    case paragraph, heading1, heading2, heading3, bullet, numbered, check, quote, code
    /// Atomic blocks: one attachment character, no inline text.
    case divider, image, file
    /// Atomic blocks with their own editing inside the attachment: a table grid, side-by-side
    /// columns (each with its own blocks), and math (shown, saved untouched).
    case table, columns, math
    /// Any block type the editor doesn't edit (tables, columns, raw HTML, math, types
    /// added later…): shown read-only and saved back untouched.
    case embed

    public var hasInlineText: Bool {
        switch self {
        case .divider, .image, .file, .embed, .table, .columns, .math: false
        default: true
        }
    }

    /// Keeps its children to itself: nothing in the main flow nests under it.
    public var holdsOwnChildren: Bool { self == .embed || self == .columns }

    public var isList: Bool { self == .bullet || self == .numbered || self == .check }

    public var headingLevel: Int? {
        switch self {
        case .heading1: 1
        case .heading2: 2
        case .heading3: 3
        default: nil
        }
    }

    public static func heading(_ level: Int) -> BlockKind {
        switch level {
        case ...1: .heading1
        case 2: .heading2
        default: .heading3
        }
    }

    /// The BlockNote type the web editor writes for this kind.
    public var canonicalType: String {
        switch self {
        case .paragraph: "paragraph"
        case .heading1, .heading2, .heading3: "heading"
        case .bullet: "bulletListItem"
        case .numbered: "numberedListItem"
        case .check: "checkListItem"
        case .quote: "quote"
        case .code: "codeBlock"
        case .divider: "divider"
        case .image: "image"
        case .file: "file"
        case .table: "table"
        case .columns: "columns"
        case .math: "math"
        case .embed: "embed"
        }
    }
}

/// Inline formatting of a run of text. Mirrors BlockNote's `styles` plus links.
public struct InlineMarks: Hashable, Sendable {
    public var bold = false
    public var italic = false
    public var underline = false
    public var strike = false
    public var code = false
    public var sup = false
    public var sub = false
    /// A BlockNote color name ("red", "blue", …) or a hex color ("#B4532A").
    public var textColor: String?
    /// Background (highlight) color, same vocabulary as `textColor`.
    public var highlight: String?
    /// Link target: a URL, `/media/…`, or a note link `[[id|Title]]`.
    public var link: String?
    /// Style keys this editor doesn't know, kept so saving never drops them.
    public var extraStyles: [String: JSONValue] = [:]

    public init(
        bold: Bool = false, italic: Bool = false, underline: Bool = false, strike: Bool = false,
        code: Bool = false, textColor: String? = nil, highlight: String? = nil, link: String? = nil,
        sup: Bool = false, sub: Bool = false, extraStyles: [String: JSONValue] = [:]
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
        self.extraStyles = extraStyles
    }

    public var isPlain: Bool { self == InlineMarks() }

    /// The same marks without the link (BlockNote keeps links as wrapper nodes).
    var withoutLink: InlineMarks {
        var copy = self
        copy.link = nil
        return copy
    }
}

/// The object replacement character: stands in for an atomic inline node or block.
public let objectReplacement = "\u{FFFC}"
/// A soft line break inside one block (a "\n" inside BlockNote text).
public let softBreak = "\u{2028}"

/// A run of inline content: text with marks, or an atomic inline node kept verbatim
/// (a block reference, a mention, anything unknown), whose text is `objectReplacement`.
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

/// The block as the server sent it. Kept so saving an edited block preserves its type,
/// attributes and content keys the editor doesn't manage, and so untouched atomic
/// blocks are written back verbatim.
public struct BlockOrigin: Hashable, Sendable {
    public var type: String
    public var attrs: [String: JSONValue]
    public var userAttrs: [String: JSONValue]
    public var content: JSONValue
    public var text: String
    /// The kind it decoded to; a different kind on save means the person converted it.
    public var kind: BlockKind
    /// Saved by a rich editor (has `content.blocknote`), so its type is a real editor type.
    public var native: Bool
    public var createdAt: String
    public var updatedAt: String
    /// Children of an embed (they are not flattened into the text).
    public var children: [BlockNode]
    /// Where the block came from (spec §7a). Read-only here: never sent on update.
    public var provenance: JSONValue?
    /// Note revision that last changed the block.
    public var revision: Int?

    public init(
        type: String, attrs: [String: JSONValue] = [:], userAttrs: [String: JSONValue] = [:],
        content: JSONValue = .object([:]), text: String = "", kind: BlockKind, native: Bool,
        createdAt: String = "", updatedAt: String = "", children: [BlockNode] = [],
        provenance: JSONValue? = nil, revision: Int? = nil
    ) {
        self.provenance = provenance
        self.revision = revision
        self.type = type
        self.attrs = attrs
        self.userAttrs = userAttrs
        self.content = content
        self.text = text
        self.kind = kind
        self.native = native
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.children = children
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

    public init(
        id: String = EditorBlock.newId(), kind: BlockKind = .paragraph, depth: Int = 0,
        checked: Bool = false, language: String? = nil, runs: [InlineRun] = [],
        origin: BlockOrigin? = nil, newProvenance: JSONValue? = nil, table: TableContent? = nil,
        columns: [EditorColumn]? = nil
    ) {
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

/// The fields a block transaction writes for a block.
public struct BlockFields: Hashable, Sendable {
    public var type: String
    public var attrs: [String: JSONValue]
    public var content: JSONValue
    public var text: String
}
