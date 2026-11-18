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
}

/// Identity and kind of one paragraph's block. Immutable; replace it to change a block.
final class BlockInfo: NSObject, @unchecked Sendable {
    let id: String
    let kind: BlockKind
    let depth: Int
    let checked: Bool
    let language: String?
    let origin: OriginBox?
    /// Provenance to record when the block is first saved (pasted web content).
    let newProvenance: JSONValue?

    init(id: String = EditorBlock.newId(), kind: BlockKind = .paragraph, depth: Int = 0,
         checked: Bool = false, language: String? = nil, origin: OriginBox? = nil,
         newProvenance: JSONValue? = nil) {
        self.newProvenance = newProvenance
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
                  newProvenance: block.newProvenance)
    }

    func with(id: String? = nil, kind: BlockKind? = nil, depth: Int? = nil, checked: Bool? = nil) -> BlockInfo {
        BlockInfo(id: id ?? self.id, kind: kind ?? self.kind, depth: depth ?? self.depth,
                  checked: checked ?? self.checked, language: language, origin: origin,
                  newProvenance: newProvenance)
    }

    func with(newProvenance: JSONValue?) -> BlockInfo {
        BlockInfo(id: id, kind: kind, depth: depth, checked: checked, language: language, origin: origin,
                  newProvenance: newProvenance)
    }

    /// A fresh block following this one (Return): new id, no server history.
    func successor(kind: BlockKind) -> BlockInfo {
        BlockInfo(kind: kind, depth: depth, checked: false, language: kind == .code ? language : nil)
    }

    override func isEqual(_ object: Any?) -> Bool {
        guard let other = object as? BlockInfo else { return false }
        return id == other.id && kind == other.kind && depth == other.depth
            && checked == other.checked && language == other.language && origin === other.origin
            && newProvenance == other.newProvenance
    }

    override var hash: Int { id.hashValue }
}

/// The server's version of a block (boxed so attribute comparisons stay cheap).
final class OriginBox: NSObject, @unchecked Sendable {
    let origin: BlockOrigin
    init(_ origin: BlockOrigin) { self.origin = origin }
}

final class MarksBox: NSObject, @unchecked Sendable {
    let marks: InlineMarks
    init(_ marks: InlineMarks) { self.marks = marks }
    override func isEqual(_ object: Any?) -> Bool { (object as? MarksBox)?.marks == marks }
    override var hash: Int { marks.hashValue }
}

final class NodeBox: NSObject, @unchecked Sendable {
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

extension NSString {
    /// The paragraph range containing `index`, terminator included.
    func paragraphRange(at index: Int) -> NSRange {
        paragraphRange(for: NSRange(location: min(max(0, index), max(0, length - 1)), length: 0))
    }
}
