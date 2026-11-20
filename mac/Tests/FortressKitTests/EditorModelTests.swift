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
    _ id: String, _ type: String, content: [JSONValue]? = nil, payload: JSONValue? = nil,
    attrs: [String: JSONValue] = [:], children: [BlockNode] = []
) -> BlockNode {
    BlockNode(id: id, documentId: "doc", type: type, attrs: attrs, content: content.map(JSONValue.array) ?? .null,
              payload: payload, children: children)
}

private func text(_ text: String, _ marks: [String: JSONValue] = [:]) -> JSONValue {
    var object: [String: JSONValue] = ["type": .string("text"), "text": .string(text)]
    if !marks.isEmpty { object["marks"] = .object(marks) }
    return .object(object)
}

/// Applies operations the way `backend/app/block_store.py` does (placement after or
/// before a sibling or at the end of a parent, attrs as a patch, delete removes the
/// subtree), so the diff can be checked end to end.
private struct ServerTree {
    var children: [String?: [String]] = [nil: []]
    var parent: [String: String?] = [:]
    var fields: [String: BlockFields] = [:]

    init(_ tree: SyncedTree) {
        children = tree.children
        parent = tree.parent
        for id in tree.order { fields[id] = tree.fields(of: id) }
        if children[nil] == nil { children[nil] = [] }
    }

    struct Rejected: Error { var reason: String }

    /// (parent, index) for a placement.
    func place(_ op: BlockOp, moving id: String? = nil) throws -> (String?, Int) {
        if op.afterId != nil && op.beforeId != nil { throw Rejected(reason: "both after and before") }
        if let anchor = op.afterId ?? op.beforeId {
            guard anchor != id, let p = parent[anchor] else { throw Rejected(reason: "no anchor \(anchor)") }
            if let given = op.parentId, given != p { throw Rejected(reason: "parent disagrees") }
            let index = try #require(children[p]?.firstIndex(of: anchor))
            return (p, op.afterId != nil ? index + 1 : index)
        }
        if let p = op.parentId, parent[p] == nil { throw Rejected(reason: "no parent \(p)") }
        return (op.parentId, children[op.parentId]?.count ?? 0)
    }

    mutating func apply(_ operations: [BlockOp]) throws {
        for op in operations {
            switch op.operation {
            case .insert:
                let id = try #require(op.blockId)
                guard parent[id] == nil else { throw Rejected(reason: "exists \(id)") }
                let (p, index) = try place(op)
                children[p, default: []].insert(id, at: index)
                children[id] = []
                parent.updateValue(p, forKey: id)
                fields[id] = BlockFields(type: try #require(op.type), attrs: op.attrs ?? [:], content: op.content ?? .null, payload: op.payload)
            case .move:
                let id = try #require(op.blockId)
                guard let old = parent[id] else { throw Rejected(reason: "missing \(id)") }
                children[old]?.removeAll { $0 == id }
                let (p, index) = try place(op, moving: id)
                var cursor = p
                while let c = cursor {
                    if c == id { throw Rejected(reason: "cycle") }
                    cursor = parent[c] ?? nil
                }
                children[p, default: []].insert(id, at: index)
                parent.updateValue(p, forKey: id)
            case .update:
                let id = try #require(op.blockId)
                guard var current = fields[id] else { throw Rejected(reason: "missing \(id)") }
                guard op.type != nil || op.attrs != nil || op.content != nil || op.payload != nil else { throw Rejected(reason: "empty update") }
                if let type = op.type { current.type = type }
                for (key, value) in op.attrs ?? [:] { current.attrs[key] = value == .null ? nil : value }
                if let content = op.content { current.content = content }
                if let payload = op.payload { current.payload = payload == .null ? nil : payload }
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

private func check(_ base: [EditorBlock], _ target: [EditorBlock]) throws -> [BlockOp] {
    let tree = SyncedTree(base)
    let (operations, result) = BlockDiff.operations(from: tree, to: target)
    var server = ServerTree(tree)
    try server.apply(operations)
    #expect(server.flattened() == ServerTree(result).flattened())
    #expect(server.fields == ServerTree(result).fields)
    // Every block sits under the parent its depth gives it (a quote line under its
    // container, which sits there).
    for item in BlockDiff.shape(target) {
        let parent = result.parent[item.block.id] ?? nil
        #expect((item.block.kind == .quote ? parent.flatMap { result.parent[$0] ?? nil } : parent) == item.parent)
    }
    return operations
}

private func kinds(_ operations: [BlockOp]) -> [BlockOperationKind] { operations.map { $0.operation } }

private func para(_ id: String, _ text: String, depth: Int = 0, kind: BlockKind = .paragraph) -> EditorBlock {
    EditorBlock(id: id, kind: kind, depth: depth, runs: [InlineRun(text)])
}

@Suite struct BlockMappingTests {
    @Test func decodesCanonicalBlocks() {
        let blocks = BlockCodec.flatten([
            node("h", "heading", content: [text("Plan")], attrs: ["level": .number(5)]),
            node("l", "check_item", content: [text("Buy "), text("JR", ["bold": .bool(true)]), text(" pass")], attrs: ["checked": .bool(true)], children: [
                node("n", "numbered_item", content: [text("nested")]),
            ]),
            node("q", "quote", children: [node("q1", "paragraph", content: [text("quoted")]), node("q2", "paragraph", content: [text("twice")])]),
            node("c", "code", content: [text("let a = 1\nlet b = 2")], attrs: ["language": .string("swift")]),
            node("i", "image", attrs: ["url": .string("/media/abc.png")]),
            node("t", "divider"),
            // Fortress has no math: a type it doesn't know is shown read-only.
            node("m", "math", payload: .object(["latex": .string("x^2")])),
            node("r", "html", payload: .object(["html": .string("<b>x</b>")])),
            node("u", "something_newer", content: [text("future")]),
        ])
        #expect(blocks.map(\.kind) == [.heading5, .check, .numbered, .quote, .quote, .code, .image, .divider, .embed, .embed, .embed])
        #expect(blocks.map(\.depth) == [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0])
        #expect(blocks[0].plainText == "Plan")
        #expect(blocks[1].checked)
        #expect(blocks[1].runs == [InlineRun("Buy "), InlineRun("JR", InlineMarks(bold: true)), InlineRun(" pass")])
        #expect(blocks[3].container == QuoteContainer(id: "q"))
        #expect(blocks[3].id == "q1" && blocks[4].id == "q2")
        #expect(blocks[5].plainText == "let a = 1\nlet b = 2")
        #expect(blocks[5].language == "swift")
        #expect(blocks[8].origin?.payload?["latex"] == .string("x^2"))
    }

    @Test func headingLevelsAreNeverClamped() {
        for level in 1...6 {
            let block = BlockCodec.decode(node("h", "heading", content: [text("x")], attrs: ["level": .number(Double(level))]))
            #expect(block.kind.headingLevel == level)
            #expect(BlockCodec.encode(block).attrs["level"] == .number(Double(level)))
        }
    }

    @Test func decodesInlineMarksLinksAndNoteLinks() {
        let content: [JSONValue] = [
            text("Hello ", ["bold": .bool(true), "color": .string("red")]),
            .object(["type": .string("link"), "href": .string("https://example.com"), "content": .array([text("site", ["italic": .bool(true)])])]),
            text(" and "),
            .object(["type": .string("docLink"), "id": .string("N1")]),
            .object(["type": .string("blockRef"), "id": .string("B1")]),
            text(" ", ["highlight": .string("yellow"), "underline": .bool(true), "strike": .bool(true), "code": .bool(true)]),
        ]
        let block = BlockCodec.decode(node("p", "paragraph", content: content))
        #expect(block.runs.count == 6)
        #expect(block.runs[0].marks == InlineMarks(bold: true, textColor: "red"))
        #expect(block.runs[1].marks == InlineMarks(italic: true, link: "https://example.com"))
        #expect(block.runs[3].node.flatMap(NoteLinks.documentTarget) == "N1")
        #expect(block.runs[4].node.flatMap(NoteLinks.blockTarget) == "B1")
        #expect(block.runs[5].marks == InlineMarks(underline: true, strike: true, code: true, highlight: "yellow"))
        let resolved = ResolvedLinks(documents: ["N1": .init(title: "Packing list")], blocks: ["B1": .init(text: "a block")])
        #expect(BlockCodec.plainText(block.runs, resolved: resolved) == "Hello site and Packing lista block ")
    }

    @Test func roundTripsCanonicalContentExactly() {
        let content: [JSONValue] = [
            text("Hello ", ["bold": .bool(true), "color": .string("red")]),
            .object(["type": .string("link"), "href": .string("https://example.com"), "content": .array([text("site", ["italic": .bool(true)]), text("!")])]),
            .object(["type": .string("docLink"), "id": .string("N1")]),
            text("line\nbreak", ["sup": .bool(true)]),
        ]
        let original = node("p", "paragraph", content: content, attrs: ["onenote_tags": .array([.string("todo")])])
        let block = BlockCodec.decode(original)
        let fields = BlockCodec.encode(block)
        #expect(fields == BlockFields(original))
    }

    @Test func keepsUnknownBlocksVerbatim() {
        let original = node("t", "something_newer", content: [text("x")], attrs: ["x": .number(1)], children: [node("c", "paragraph", content: [text("inside")])])
        let blocks = BlockCodec.flatten([original])
        #expect(blocks.count == 1)
        #expect(blocks[0].kind == .embed)
        #expect(blocks[0].origin?.children.count == 1)
        #expect(BlockCodec.encode(blocks[0]) == BlockFields(original))
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: blocks)
        #expect(operations.isEmpty)
    }

    @Test func quotesHoldingMoreThanLinesAreShownAsTheyAre() {
        let quote = node("q", "quote", children: [node("p", "paragraph", content: [text("a")]), node("l", "bullet_item", content: [text("b")])])
        let blocks = BlockCodec.flatten([quote])
        #expect(blocks.map(\.kind) == [.embed])
        #expect(blocks[0].origin?.children.count == 2)
    }

    @Test func convertingKindWritesCanonicalTypes() {
        var block = BlockCodec.decode(node("l", "check_item", content: [text("task")], attrs: ["checked": .bool(false), "onenote_tags": .bool(true)]))
        block.checked = true
        var fields = BlockCodec.encode(block)
        #expect(fields.type == "check_item")
        #expect(fields.attrs == ["checked": .bool(true), "onenote_tags": .bool(true)])
        block.kind = .heading2
        fields = BlockCodec.encode(block)
        #expect(fields.type == "heading")
        #expect(fields.attrs == ["level": .number(2), "onenote_tags": .bool(true)])
        block.kind = .bullet
        #expect(BlockCodec.encode(block).type == "bullet_item")
        block.kind = .numbered
        #expect(BlockCodec.encode(block).type == "numbered_item")
        block.kind = .code
        block.runs = [InlineRun("x", InlineMarks(bold: true))]
        #expect(BlockCodec.encode(block).content == .array([text("x")]))
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
        #expect(operations[0].content == .array([text("Alpha!")]))
        #expect(operations[0].type == nil && operations[0].attrs == nil)
    }

    @Test func splitInsertsAfterTheBlockBefore() throws {
        var target = base
        target[0].runs = [InlineRun("Al")]
        target.insert(para("new", "pha"), at: 1)
        let operations: [BlockOp] = try check(base, target)
        #expect(kinds(operations) == [BlockOperationKind.update, .insert])
        #expect(operations[1].afterId == "a")
        #expect(operations[1].parentId == nil && operations[1].beforeId == nil)
        // A new first block goes before the old first one; a first child is appended.
        let first: [BlockOp] = try check(base, [para("top", "Top")] + base)
        #expect(first.first?.beforeId == "a")
        var child = base
        child.insert(para("kid", "kid", depth: 1), at: 1)
        let appended: [BlockOp] = try check(base, child)
        #expect(appended.first?.parentId == "a" && appended.first?.afterId == nil)
    }

    @Test func kindChangesAreTypeAndAttrsPatches() throws {
        var target = base
        target[0].kind = .heading4
        var operations: [BlockOp] = try check(base, target)
        #expect(operations.count == 1)
        #expect(operations[0].type == "heading" && operations[0].attrs == ["level": .number(4)] && operations[0].content == nil)
        let heading = target
        target[0].kind = .paragraph
        operations = try check(heading, target)
        #expect(operations[0].type == "paragraph" && operations[0].attrs == ["level": .null])
    }

    @Test func crossCategoryChangeIsDeleteAndInsert() throws {
        var target = base
        target[4].kind = .divider
        target[4].runs = []
        let operations: [BlockOp] = try check(base, target)
        #expect(kinds(operations) == [BlockOperationKind.delete, .insert])
        #expect(operations[1].blockId == "c" && operations[1].type == "divider")
    }

    @Test func quoteLinesShareTheirContainer() throws {
        let quoted = [para("a", "Alpha"), para("q1", "one", kind: .quote), para("q2", "two", kind: .quote), para("c", "Charlie")]
        let operations: [BlockOp] = try check(base.filter { $0.depth == 0 }, quoted)
        let container = try #require(operations.first { $0.type == "quote" })
        #expect(operations.filter { $0.blockId == "q1" || $0.blockId == "q2" }.allSatisfy { $0.parentId == container.blockId || $0.afterId == "q1" })
        // The same lines give the same container in the next diff (nothing to send).
        #expect(try check(quoted, quoted).isEmpty)
        // A line that stops being a quote leaves the container.
        var unquoted = quoted
        unquoted[2].kind = .paragraph
        let moves: [BlockOp] = try check(quoted, unquoted)
        #expect(kinds(moves) == [BlockOperationKind.move])
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
        #expect(operations.first?.afterId == "b")
    }

    @Test func deletingParentKeepsSurvivingChildren() throws {
        var target = base
        target.remove(at: 1)  // b goes, b1 and b2 now follow a at depth 1
        let operations: [BlockOp] = try check(base, target)
        let moves = operations.filter { $0.operation == BlockOperationKind.move }
        #expect(operations.last?.operation == BlockOperationKind.delete)
        #expect(moves.first { $0.blockId == "b1" }?.parentId == "a")
        #expect(moves.first { $0.blockId == "b2" }?.afterId == "b1")
    }

    @Test func reorderingAndWholesaleChanges() throws {
        let target = [para("c", "Charlie"), para("b2", "x", kind: .check), para("a", "Alpha", depth: 1), para("z", "new", depth: 2)]
        _ = try check(base, target)
        _ = try check(base, [para("only", "one")])
        _ = try check([para("only", "one")], base)
    }

    @Test func nothingNestsUnderAnEmbed() {
        let embed = EditorBlock(id: "e", kind: .embed, origin: BlockOrigin(type: "html", kind: .embed))
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
                switch Int.random(in: 0...5, using: &generator) {
                case 0: target.insert(para("n\(step)", "new", depth: Int.random(in: 0...3, using: &generator)), at: Int.random(in: 0...target.count, using: &generator))
                case 1 where target.count > 1: target.remove(at: Int.random(in: 0..<target.count, using: &generator))
                case 2: let i = Int.random(in: 0..<target.count, using: &generator); target[i].depth = Int.random(in: 0...3, using: &generator)
                case 3: let i = Int.random(in: 0..<target.count, using: &generator); target[i].runs = [InlineRun("edited \(step)")]
                case 4:
                    let i = Int.random(in: 0..<target.count, using: &generator)
                    target[i].kind = [.quote, .paragraph, .heading3, .bullet][Int.random(in: 0...3, using: &generator)]
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
