import Foundation
import Testing

#if canImport(FortressKit)
@testable import FortressKit
private typealias BlockOp = FortressKit.BlockOperation
#else
@testable import Core
private typealias BlockOp = Core.BlockOperation
#endif

// Block ↔ editor mapping, inline marks and the diff that turns edits into operations.

private func node(
    _ id: String, _ type: String, markdown: String? = nil, blocknote: JSONValue? = nil,
    attrs: [String: JSONValue] = [:], children: [BlockNode] = [], extraContent: [String: JSONValue] = [:]
) -> BlockNode {
    var content = extraContent
    if let markdown { content["markdown"] = .string(markdown) }
    if let blocknote { content["blocknote"] = blocknote }
    return BlockNode(
        id: id, documentId: "doc", parentId: nil, position: 0, type: type, attrs: attrs,
        userAttrs: [:], content: .object(content), text: markdown ?? "", createdAt: "", updatedAt: "",
        children: children)
}

private func text(_ text: String, _ styles: [String: JSONValue] = [:]) -> JSONValue {
    .object(["type": .string("text"), "text": .string(text), "styles": .object(styles)])
}

/// Applies operations the way `backend/app/block_store.py` does (positions shift on
/// insert/move/delete, delete removes the subtree), so the diff can be checked end to end.
private struct ServerTree {
    var children: [String?: [String]] = [nil: []]
    var parent: [String: String?] = [:]
    var fields: [String: BlockFields] = [:]

    init(_ tree: SyncedTree) {
        children = tree.children
        parent = tree.parent
        fields = tree.fields
        if children[nil] == nil { children[nil] = [] }
    }

    mutating func apply(_ operations: [BlockOp]) throws {
        struct Rejected: Error { var reason: String }
        for op in operations {
            switch op.operation {
            case .insert:
                let id = try #require(op.blockId)
                guard parent[id] == nil else { throw Rejected(reason: "exists \(id)") }
                let p = op.parentId
                if let p, parent[p] == nil { throw Rejected(reason: "no parent \(p)") }
                var list = children[p] ?? []
                let position = op.position ?? list.count
                guard position <= list.count else { throw Rejected(reason: "gap at \(position)") }
                list.insert(id, at: position)
                children[p] = list
                children[id] = []
                parent.updateValue(p, forKey: id)
                fields[id] = BlockFields(type: op.type ?? "paragraph", attrs: op.attrs ?? [:], content: op.content ?? .object([:]), text: op.text ?? "")
            case .move:
                let id = try #require(op.blockId)
                guard let old = parent[id] else { throw Rejected(reason: "missing \(id)") }
                var cursor = op.parentId
                while let c = cursor {
                    if c == id { throw Rejected(reason: "cycle") }
                    cursor = parent[c] ?? nil
                }
                children[old]?.removeAll { $0 == id }
                var list = children[op.parentId] ?? []
                let position = op.position ?? list.count
                guard position <= list.count else { throw Rejected(reason: "gap") }
                list.insert(id, at: position)
                children[op.parentId] = list
                parent.updateValue(op.parentId, forKey: id)
            case .update:
                let id = try #require(op.blockId)
                guard var current = fields[id] else { throw Rejected(reason: "missing \(id)") }
                if let type = op.type { current.type = type }
                if let attrs = op.attrs { current.attrs = attrs }
                if let content = op.content { current.content = content }
                if let text = op.text { current.text = text }
                fields[id] = current
            case .delete:
                let id = try #require(op.blockId)
                guard let old = parent[id] else { throw Rejected(reason: "missing \(id)") }
                children[old]?.removeAll { $0 == id }
                func remove(_ id: String) {
                    for child in children[id] ?? [] { remove(child) }
                    children[id] = nil
                    parent[id] = nil
                    fields[id] = nil
                }
                remove(id)
            default:
                throw Rejected(reason: "unexpected \(op.operation)")
            }
        }
    }

    /// Document-order (id, parent, fields).
    func flattened() -> [String] {
        var output: [String] = []
        func visit(_ p: String?) {
            for id in children[p] ?? [] {
                output.append("\(id)<\(p ?? "-")")
                visit(id)
            }
        }
        visit(nil)
        return output
    }
}

private func expected(_ blocks: [EditorBlock]) -> [String] {
    BlockDiff.shape(blocks).map { "\($0.block.id)<\($0.parent ?? "-")" }
}

private func check(_ base: [EditorBlock], _ target: [EditorBlock]) throws -> [BlockOp] {
    let tree = SyncedTree(base)
    let (operations, result) = BlockDiff.operations(from: tree, to: target)
    var server = ServerTree(tree)
    try server.apply(operations)
    #expect(server.flattened() == expected(target))
    #expect(server.fields == result.fields)
    return operations
}

private func kinds(_ operations: [BlockOp]) -> [BlockOperationKind] { operations.map { $0.operation } }

private func para(_ id: String, _ text: String, depth: Int = 0, kind: BlockKind = .paragraph) -> EditorBlock {
    EditorBlock(id: id, kind: kind, depth: depth, runs: [InlineRun(text)])
}

@Suite struct BlockMappingTests {
    @Test func decodesImportedMarkdownBlocks() {
        let blocks = BlockCodec.flatten([
            node("h", "heading", markdown: "## Plan"),
            node("l", "list", markdown: "- [x] Buy **JR** pass", attrs: ["checked": .bool(true)], children: [
                node("n", "list", markdown: "1. nested", attrs: ["ordered": .bool(true)]),
            ]),
            node("q", "quote", markdown: "> quoted\n> twice"),
            node("c", "code", markdown: "```swift\nlet a = 1\n```", attrs: ["language": .string("swift")]),
            node("i", "paragraph", markdown: "![Kyoto](/media/abc.png)"),
            node("t", "thematic_break", markdown: "---"),
            node("r", "raw", markdown: "<table></table>"),
        ])
        #expect(blocks.map(\.kind) == [.heading2, .check, .numbered, .quote, .code, .image, .divider, .embed])
        #expect(blocks.map(\.depth) == [0, 0, 1, 0, 0, 0, 0, 0])
        #expect(blocks[0].plainText == "Plan")
        #expect(blocks[1].checked)
        #expect(blocks[1].runs == [InlineRun("Buy "), InlineRun("JR", InlineMarks(bold: true)), InlineRun(" pass")])
        #expect(blocks[3].plainText == "quoted\ntwice")
        #expect(blocks[4].plainText == "let a = 1")
        #expect(blocks[4].language == "swift")
    }

    @Test func decodesBlockNoteInlineMarksAndLinks() {
        let content: JSONValue = .array([
            text("Hello ", ["bold": .bool(true), "textColor": .string("red"), "fontSize": .string("12px")]),
            .object(["type": .string("link"), "href": .string("https://example.com"), "content": .array([text("site", ["italic": .bool(true)])])]),
            text(" and "),
            .object(["type": .string("link"), "href": .string("[[N1|Packing list]]"), "content": .array([text("Packing list")])]),
            .object(["type": .string("mention"), "user": .string("me")]),
            text(" ", ["backgroundColor": .string("yellow"), "underline": .bool(true), "strike": .bool(true), "code": .bool(true)]),
        ])
        let block = BlockCodec.decode(node("p", "paragraph", blocknote: content))
        #expect(block.runs.count == 6)
        #expect(block.runs[0].marks == InlineMarks(bold: true, textColor: "red", extraStyles: ["fontSize": .string("12px")]))
        #expect(block.runs[1].marks == InlineMarks(italic: true, link: "https://example.com"))
        #expect(block.runs[3].marks.link == "[[N1|Packing list]]")
        #expect(block.runs[4].node != nil)
        #expect(block.runs[5].marks == InlineMarks(underline: true, strike: true, code: true, highlight: "yellow"))
    }

    @Test func roundTripsBlockNoteContentExactly() {
        let content: JSONValue = .array([
            text("Hello ", ["bold": .bool(true), "textColor": .string("red"), "fontSize": .string("12px")]),
            .object(["type": .string("link"), "href": .string("https://example.com"), "content": .array([text("site", ["italic": .bool(true)])])]),
            .object(["type": .string("mention"), "user": .string("me")]),
            text("line\nbreak"),
        ])
        let original = node("p", "paragraph", blocknote: content, attrs: ["textAlignment": .string("left"), "provenance": .object(["source": .string("onenote")])], extraContent: ["future": .bool(true)])
        let block = BlockCodec.decode(original)
        let fields = BlockCodec.encode(block)
        #expect(fields.type == "paragraph")
        #expect(fields.attrs == original.attrs)
        #expect(fields.content["blocknote"] == content)
        #expect(fields.content["future"] == .bool(true))
        #expect(fields.text == "Hello siteline\nbreak")
    }

    @Test func keepsUnknownBlocksVerbatim() {
        let table: JSONValue = .object(["type": .string("tableContent"), "rows": .array([])])
        let original = node("t", "table", blocknote: table, attrs: ["x": .number(1)], children: [node("c", "paragraph", markdown: "inside")])
        let blocks = BlockCodec.flatten([original])
        #expect(blocks.count == 1)
        #expect(blocks[0].kind == .embed)
        #expect(blocks[0].origin?.children.count == 1)
        let fields = BlockCodec.encode(blocks[0])
        #expect(fields.type == "table")
        #expect(fields.content == original.content)
        #expect(fields.attrs == original.attrs)
    }

    @Test func convertingKindWritesBlockNoteTypes() {
        var block = BlockCodec.decode(node("l", "list", markdown: "- [ ] task", attrs: ["checked": .bool(false)]))
        block.checked = true
        var fields = BlockCodec.encode(block)
        #expect(fields.type == "checkListItem")
        #expect(fields.attrs["checked"] == .bool(true))
        #expect(fields.content["markdown"] == .string("- [x] task"))
        block.kind = .heading2
        fields = BlockCodec.encode(block)
        #expect(fields.type == "heading")
        #expect(fields.attrs["level"] == .number(2))
        #expect(fields.attrs["checked"] == nil)
    }

    @Test func rendersMarkdownMirror() {
        let runs = [
            InlineRun("a "), InlineRun("bold", InlineMarks(bold: true)), InlineRun(" "),
            InlineRun("link", InlineMarks(link: "https://x.y")), InlineRun(" "),
            InlineRun("Note", InlineMarks(link: "[[N1|Note]]")), InlineRun(" "),
            InlineRun("x", InlineMarks(code: true)),
        ]
        #expect(InlineMarkdown.render(runs) == "a **bold** [link](https://x.y) [[N1|Note]] `x`")
        #expect(InlineMarkdown.parse("a **bold** [link](https://x.y) [[N1|Note]] `x`") == BlockCodec.normalized(runs))
    }

    @Test func parsesEmphasisVariants() {
        #expect(InlineMarkdown.parse("*it* and _it_ and snake_case ~~gone~~") == [
            InlineRun("it", InlineMarks(italic: true)), InlineRun(" and "),
            InlineRun("it", InlineMarks(italic: true)), InlineRun(" and snake_case "),
            InlineRun("gone", InlineMarks(strike: true)),
        ])
        #expect(InlineMarkdown.parse("2 * 3 * 4") == [InlineRun("2 * 3 * 4")])
        #expect(InlineMarkdown.parse(#"not \*em\*"#) == [InlineRun("not *em*")])
    }

    @Test func noteLinkFormatMatchesBackend() {
        #expect(InlineMarkdown.documentLinkHref(id: "01ABC", title: "A | b]") == "[[01ABC|A b]]")
        #expect(InlineMarkdown.documentLinkTarget("[[01ABC|Kyoto trip]]")?.id == "01ABC")
        #expect(InlineMarkdown.blockReferenceTarget("(( 01XYZ \"label\" ))")?.id == "01XYZ")
        let runs = InlineMarkdown.parse("See [[01M7|Packing list]].")
        #expect(runs[1] == InlineRun("Packing list", InlineMarks(link: "[[01M7|Packing list]]")))
    }

    @Test func autoTitleMatchesBackendRule() {
        #expect(BlockCodec.firstLineTitle(["", "  ", "Plan for Kyoto"]) == "Plan for Kyoto")
        #expect(BlockCodec.firstLineTitle([]) == "Untitled")
        let long = String(repeating: "word ", count: 30)
        #expect(BlockCodec.firstLineTitle([long]).hasSuffix("…"))
    }
}

@Suite struct BlockDiffTests {
    let base = [
        para("a", "Alpha"),
        para("b", "Bravo"),
        para("b1", "child", depth: 1, kind: .bullet),
        para("b2", "child 2", depth: 1, kind: .bullet),
        para("c", "Charlie"),
    ]

    @Test func noChangesNoOperations() throws {
        #expect(try check(base, base).isEmpty)
    }

    @Test func editingTextIsOneUpdate() throws {
        var target = base
        target[0].runs = [InlineRun("Alpha!")]
        let operations: [BlockOp] = try check(base, target)
        #expect(operations.count == 1)
        #expect(operations[0].operation == .update)
        #expect(operations[0].blockId == "a")
        #expect(operations[0].text == "Alpha!")
    }

    @Test func splitInsertsAfterWithPosition() throws {
        var target = base
        target[0].runs = [InlineRun("Al")]
        target.insert(para("new", "pha"), at: 1)
        let operations: [BlockOp] = try check(base, target)
        #expect(kinds(operations) == [BlockOperationKind.update, .insert])
        #expect(operations[1].position == 1)
        #expect(operations[1].parentId == nil)
    }

    @Test func mergeUpdatesAndDeletes() throws {
        var target = base
        target[3].runs = [InlineRun("child 2Charlie")]
        target.remove(at: 4)
        let operations: [BlockOp] = try check(base, target)
        #expect(kinds(operations) == [BlockOperationKind.delete, .update])
    }

    @Test func indentAndOutdentMoveWithParents() throws {
        var target = base
        target[4].depth = 2  // clamps to 2 under b2 (prev depth 1 + 1)
        target[1].depth = 1  // b under a; b1/b2 stay one level below b
        target[2].depth = 2
        target[3].depth = 2
        _ = try check(base, target)
        var outdented = base
        outdented[2].depth = 0
        outdented[3].depth = 0
        let operations: [BlockOp] = try check(base, outdented)
        #expect(kinds(operations).allSatisfy { $0 == BlockOperationKind.move })
        #expect(operations.first?.parentId == nil)
    }

    @Test func deletingParentKeepsSurvivingChildren() throws {
        var target = base
        target.remove(at: 1)  // b goes, b1 and b2 now follow a at depth 1
        let operations: [BlockOp] = try check(base, target)
        let moves = operations.filter { $0.operation == BlockOperationKind.move }
        #expect(operations.last?.operation == BlockOperationKind.delete)
        #expect(moves.filter { $0.blockId?.hasPrefix("b") == true }.allSatisfy { $0.parentId == "a" })
    }

    @Test func reorderingAndWholesaleChanges() throws {
        let target = [para("c", "Charlie"), para("b2", "x", kind: .check), para("a", "Alpha", depth: 1), para("z", "new", depth: 2)]
        _ = try check(base, target)
        _ = try check(base, [para("only", "one")])
        _ = try check([para("only", "one")], base)
    }

    @Test func nothingNestsUnderAnEmbed() {
        let embed = EditorBlock(id: "e", kind: .embed, origin: BlockOrigin(type: "table", kind: .embed, native: true))
        let shaped = BlockDiff.shape([embed, para("p", "after", depth: 1)])
        #expect(shaped[1].parent == nil)
    }

    @Test func randomEditsAlwaysReachTarget() throws {
        var generator = SeededGenerator(seed: 7)
        for _ in 0..<300 {
            var current = (0..<Int.random(in: 1...8, using: &generator)).map { index in
                para("s\(index)", "text \(index)", depth: Int.random(in: 0...2, using: &generator))
            }
            current = BlockDiff.shape(current).map(\.block)
            var target = current
            for step in 0..<Int.random(in: 1...6, using: &generator) {
                switch Int.random(in: 0...4, using: &generator) {
                case 0: target.insert(para("n\(step)", "new", depth: Int.random(in: 0...3, using: &generator)), at: Int.random(in: 0...target.count, using: &generator))
                case 1 where target.count > 1: target.remove(at: Int.random(in: 0..<target.count, using: &generator))
                case 2: let i = Int.random(in: 0..<target.count, using: &generator); target[i].depth = Int.random(in: 0...3, using: &generator)
                case 3: let i = Int.random(in: 0..<target.count, using: &generator); target[i].runs = [InlineRun("edited \(step)")]
                default:
                    let i = Int.random(in: 0..<target.count, using: &generator)
                    let moved = target.remove(at: i)
                    target.insert(moved, at: Int.random(in: 0...target.count, using: &generator))
                }
            }
            _ = try check(current, target)
        }
    }
}

struct SeededGenerator: RandomNumberGenerator {
    var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> UInt64 {
        state = state &* 6364136223846793005 &+ 1442695040888963407
        return state
    }
}
