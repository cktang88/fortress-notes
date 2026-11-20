import AppKit

// Attributes the editor keeps in its text storage. The semantic ones (block identity,
// inline marks, atomic nodes) are the source of truth; fonts, colors and paragraph
// styles are derived from them by `EditorTheme.restyle`.

extension NSAttributedString.Key {
    /// `BlockInfo`: which block a paragraph is, on every character of it (incl. its "\n").
    static let fnBlock = NSAttributedString.Key("fortress.block")
    /// `MarksBox`: inline formatting; absent means plain.
    static let fnMarks = NSAttributedString.Key("fortress.marks")
    /// `NodeBox`: an atomic inline node (block reference, mention, …) on its U+FFFC.
    static let fnNode = NSAttributedString.Key("fortress.node")
    /// Set by `restyle`; text without it came from outside (a paste) and is cleaned up.
    static let fnStyled = NSAttributedString.Key("fortress.styled")
    /// On a U+2029 that stands for a space of a long block (`SoftBreaks`): the block goes
    /// on past it. A U+2029 without it came from outside and becomes a "\n".
    static let fnSoft = NSAttributedString.Key("fortress.soft")
}

/// Identity and kind of one paragraph's block. Immutable; replace it to change a block.
/// (This and the boxes below are checked `Sendable`: final NSObject subclasses with only
/// `let` properties of Sendable types.)
final class BlockInfo: NSObject, Sendable {
    let id: String
    let kind: BlockKind
    let depth: Int
    let checked: Bool
    let language: String?
    let origin: OriginBox?
    /// The quote (or callout) a quote line belongs to.
    let container: QuoteContainer?

    init(id: String = EditorBlock.newId(), kind: BlockKind = .paragraph, depth: Int = 0,
         checked: Bool = false, language: String? = nil, origin: OriginBox? = nil,
         container: QuoteContainer? = nil) {
        self.container = container
        self.id = id
        self.kind = kind
        self.depth = depth
        self.checked = checked
        self.language = language
        self.origin = origin
    }

    convenience init(_ block: EditorBlock) {
        self.init(id: block.id, kind: block.kind, depth: block.depth, checked: block.checked,
                  language: block.language, origin: block.origin.map(OriginBox.init),
                  container: block.container)
    }

    func with(id: String? = nil, kind: BlockKind? = nil, depth: Int? = nil, checked: Bool? = nil) -> BlockInfo {
        BlockInfo(id: id ?? self.id, kind: kind ?? self.kind, depth: depth ?? self.depth,
                  checked: checked ?? self.checked, language: language, origin: origin,
                  container: container)
    }

    /// A quote line of `container` (or another kind keeping its container).
    func with(kind: BlockKind, container: QuoteContainer?) -> BlockInfo {
        BlockInfo(id: id, kind: kind, depth: depth, checked: false, language: language, origin: origin,
                  container: container)
    }

    /// The same block info under a new id (a duplicate from a paste).
    func renamed() -> BlockInfo {
        BlockInfo(kind: kind, depth: depth, checked: checked, language: language, origin: origin,
                  container: container)
    }

    /// A fresh block following this one (Return): new id, no server history.
    func successor(kind: BlockKind) -> BlockInfo {
        BlockInfo(kind: kind, depth: depth, checked: false, language: kind == .code ? language : nil,
                  container: kind == .quote && self.kind == .quote ? container : nil)
    }

    override func isEqual(_ object: Any?) -> Bool {
        guard let other = object as? BlockInfo else { return false }
        return id == other.id && kind == other.kind && depth == other.depth
            && checked == other.checked && language == other.language && origin === other.origin
            && container == other.container
    }

    override var hash: Int { id.hashValue }
}

/// The server's version of a block (boxed so attribute comparisons stay cheap).
final class OriginBox: NSObject, Sendable {
    let origin: BlockOrigin
    init(_ origin: BlockOrigin) { self.origin = origin }
}

final class MarksBox: NSObject, Sendable {
    let marks: InlineMarks
    init(_ marks: InlineMarks) { self.marks = marks }
    override func isEqual(_ object: Any?) -> Bool { (object as? MarksBox)?.marks == marks }
    override var hash: Int { marks.hashValue }
}

final class NodeBox: NSObject, Sendable {
    let node: JSONValue
    init(_ node: JSONValue) { self.node = node }
}

extension NSAttributedString {
    func blockInfo(at index: Int) -> BlockInfo? {
        guard index >= 0, index < length else { return nil }
        return attribute(.fnBlock, at: index, effectiveRange: nil) as? BlockInfo
    }

    func marks(at index: Int) -> InlineMarks {
        guard index >= 0, index < length else { return InlineMarks() }
        return (attribute(.fnMarks, at: index, effectiveRange: nil) as? MarksBox)?.marks ?? InlineMarks()
    }
}

extension NSAttributedString {
    /// The characters as an NSString, without copying them.
    ///
    /// `string` is a Swift `String`, and bridging a *mutable* NSString to Swift copies it
    /// (`CFStringCreateCopy`): for the note's NSTextStorage that is the whole note, on every
    /// call. The editor reads the text this way many times per keystroke (paragraph ranges,
    /// selection snapping, normalizing, reading blocks back, numbering), so a 600,000-
    /// character note spent milliseconds per key just copying it. A mutable attributed
    /// string answers with its live `mutableString` instead (no copy; it shows later edits
    /// of the characters, so don't hold it across one expecting the old text).
    var nsString: NSString {
        if let mutable = self as? NSMutableAttributedString { return mutable.mutableString }
        return string as NSString
    }
}

extension NSString {
    /// The block's paragraph containing `index`, its "\n" included: the text's paragraph
    /// with the soft paragraphs before and after it (`SoftBreaks`).
    func paragraphRange(at index: Int) -> NSRange {
        var range = paragraphRange(for: NSRange(location: min(max(0, index), max(0, length - 1)), length: 0))
        while range.location > 0, character(at: range.location - 1) == SoftBreaks.character {
            range = NSUnionRange(paragraphRange(for: NSRange(location: range.location - 1, length: 0)), range)
        }
        while range.length > 0, NSMaxRange(range) < length, character(at: NSMaxRange(range) - 1) == SoftBreaks.character {
            range = NSUnionRange(range, paragraphRange(for: NSRange(location: NSMaxRange(range), length: 0)))
        }
        return range
    }
}
