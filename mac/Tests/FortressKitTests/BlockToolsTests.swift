import AppKit
import SwiftUI
import Testing

@testable import FortressKit

// Blocks made visible and movable (DESIGN c, "Block interactions"): the handle beside a
// block and its menu, block selection with Esc, the `/` insert menu, dragging blocks,
// columns, Show Blocks. Each action is checked for the blocks it leaves (ids included),
// the block operations a save sends, and one named step on the note's undo stack.

@MainActor
enum BlockFixture {
    /// Plans (h1) · First paragraph · • Item one ( • Child ) · • Item two · Last
    static func blocks() -> [EditorBlock] {
        [
            EditorBlock(id: "h", kind: .heading1, runs: [InlineRun("Plans")]),
            EditorBlock(id: "p1", runs: [InlineRun("First paragraph")]),
            EditorBlock(id: "b1", kind: .bullet, runs: [InlineRun("Item one")]),
            EditorBlock(id: "b2", kind: .bullet, depth: 1, runs: [InlineRun("Child")]),
            EditorBlock(id: "b3", kind: .bullet, runs: [InlineRun("Item two")]),
            EditorBlock(id: "p2", runs: [InlineRun("Last")]),
        ]
    }

    static func harness(_ blocks: [EditorBlock] = blocks()) -> UndoHarness {
        let harness = UndoHarness(blocks)
        harness.controller.noteReference = { (id: "doc-1", title: "Trip [draft]") }
        harness.layout()
        return harness
    }
}

extension UndoHarness {
    /// The block ids and kinds, in order, with depths ("p1 paragraph d0").
    var outline: [String] {
        controller.currentBlocks().map { block in
            var line = "\(block.id) \(block.kind.rawValue) d\(block.depth)"
            if let columns = block.columns {
                line += " [" + columns.map { $0.blocks.map { "\($0.id)/d\($0.depth)" }.joined(separator: ",") }.joined(separator: " | ") + "]"
            }
            return line
        }
    }

    var ids: [String] { controller.currentBlocks().map(\.id) }

    /// A point on a block's first line (the note's text view), `dx` right of its text.
    func point(onBlock id: String, dx: CGFloat = 10, lower: Bool = false) -> CGPoint {
        let range = controller.range(ofBlock: id)!
        let frame = controller.selectionFrame(controller.paragraph(at: range.location))
        return CGPoint(x: controller.textView.textContainerOrigin.x + dx, y: lower ? frame.maxY - 2 : frame.minY + 4)
    }

    /// Hovers a block and opens its handle's menu (without showing it).
    func blockMenu(_ id: String) -> NSMenu? {
        controller.hover(at: point(onBlock: id))
        #expect(controller.blockHandle.target?.blockId == id, "the handle stands for \(id)")
        return controller.blockHandle.prepareMenu()
    }

    /// Chooses a menu item by its titles ("Turn Into", "Heading 2").
    func choose(_ menu: NSMenu?, _ path: String...) {
        var current = menu
        for (index, title) in path.enumerated() {
            guard let found = current?.items.first(where: { $0.title == title }) else {
                Issue.record("no menu item \(title)")
                return
            }
            if index == path.count - 1 {
                #expect(found.isEnabled, "\(title) is enabled")
                current?.performActionForItem(at: current!.index(of: found))
            } else {
                current = found.submenu
            }
        }
        settleRunLoop()
    }

    func key(_ characters: String, keyCode: UInt16, modifiers: NSEvent.ModifierFlags = []) {
        let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers, timestamp: 0,
                                     windowNumber: window.windowNumber, context: nil, characters: characters,
                                     charactersIgnoringModifiers: characters, isARepeat: false, keyCode: keyCode)!
        if modifiers.contains(.command) {
            if !(focused.performKeyEquivalent(with: event)) { focused.keyDown(with: event) }
        } else {
            focused.keyDown(with: event)
        }
        settleRunLoop()
    }

    /// The operations a save would send from `base` to what is shown.
    func operations(from base: [EditorBlock]) -> [FortressKit.BlockOperation] {
        BlockDiff.operations(from: SyncedTree(base), to: controller.currentBlocks()).operations
    }
}

@MainActor
@Suite(.serialized) struct BlockToolsTests {
    // MARK: The handle and its menu

    @Test func theHandleShowsBesideTheBlockUnderThePointer() throws {
        let harness = BlockFixture.harness()
        let controller = harness.controller
        controller.hover(at: harness.point(onBlock: "b1", dx: 40))
        let hover = controller.blockHandle
        #expect(hover.target?.blockId == "b1")
        #expect(!hover.grip.isHidden && !hover.plus.isHidden)
        // Left of the text, the grip nearer it than the +.
        let text = try #require(hover.target?.frame)
        #expect(hover.grip.frame.maxX <= text.minX && hover.plus.frame.maxX <= hover.grip.frame.minX + 1)
        // Moving to the handle keeps it; leaving the view hides it.
        controller.hover(at: CGPoint(x: hover.grip.frame.midX, y: hover.grip.frame.midY))
        #expect(hover.target?.blockId == "b1")
        controller.hover(at: nil)
        #expect(hover.grip.isHidden)
        // VoiceOver: a button named for the block, with the moves as actions.
        controller.hover(at: harness.point(onBlock: "h"))
        #expect(hover.grip.accessibilityRole() == .button)
        #expect(hover.grip.accessibilityLabel() == "Heading 1 block actions")
        #expect(hover.grip.accessibilityCustomActions()?.map(\.name) == ["Move Up", "Move Down", "Duplicate", "Delete"])
        #expect(hover.plus.accessibilityLabel() == "Add Block Below")
    }

    @Test func turnIntoFromTheMenuIsOneNamedStepEach() throws {
        let harness = BlockFixture.harness()
        let start = harness.controller.currentBlocks()
        var script = UndoScript(harness)
        script.step("Heading 2") { harness.choose(harness.blockMenu("p1"), "Turn Into", "Heading 2") }
        #expect(harness.controller.currentBlocks()[1].kind == .heading2)
        script.step("Callout") { harness.choose(harness.blockMenu("p1"), "Turn Into", "Callout") }
        let callout = harness.controller.currentBlocks()[1]
        #expect(callout.kind == .quote && callout.container?.type == "callout")
        // Saved as a callout container holding the paragraph (docs/block-model.md §2).
        let operations = harness.operations(from: start)
        let container = try #require(operations.first { $0.operation == .insert && $0.type == "callout" })
        #expect(operations.contains { $0.operation == .move && $0.blockId == "p1" && $0.parentId == container.blockId })
        script.step("Code Block") { harness.choose(harness.blockMenu("p1"), "Turn Into", "Code Block") }
        script.step("Quote") { harness.choose(harness.blockMenu("b3"), "Turn Into", "Quote") }
        script.step("Checklist") { harness.choose(harness.blockMenu("p2"), "Turn Into", "Checklist") }
        script.step("Body") { harness.choose(harness.blockMenu("h"), "Turn Into", "Text") }
        #expect(harness.controller.currentBlocks().map(\.kind) == [.paragraph, .code, .bullet, .bullet, .quote, .check])
        // A block with text can't become a divider (its text would be lost).
        let menu = harness.blockMenu("p2")
        let divider = menu?.items.first { $0.title == "Turn Into" }?.submenu?.items.first { $0.title == "Divider" }
        #expect(divider?.isEnabled == false)
        harness.controller.endBlockSelection(caret: 0)
        script.walkBackAndForth()
    }

    @Test func turnIntoTableAndDivider() throws {
        let harness = BlockFixture.harness(BlockFixture.blocks() + [EditorBlock(id: "e", runs: [])])
        var script = UndoScript(harness)
        script.step("Divider") { harness.choose(harness.blockMenu("e"), "Turn Into", "Divider") }
        #expect(harness.controller.currentBlocks().last?.kind == .divider)
        script.step("Insert Table") { harness.choose(harness.blockMenu("p2"), "Turn Into", "Table") }
        let table = try #require(harness.controller.currentBlocks().first { $0.kind == .table }?.table)
        #expect(table.cell(row: 0, column: 0)?.plainText == "Last")
        #expect(!harness.ids.contains("p2"))
        harness.controller.endBlockSelection(caret: 0)
        script.walkBackAndForth()
    }

    @Test func duplicateDeleteMoveAndCopyLinkFromTheMenu() throws {
        let harness = BlockFixture.harness()
        let start = harness.controller.currentBlocks()
        var script = UndoScript(harness)
        // Duplicate takes the children along, with new ids, right after the block.
        script.step("Duplicate") { harness.choose(harness.blockMenu("b1"), "Duplicate") }
        let afterDuplicate = harness.controller.currentBlocks()
        #expect(afterDuplicate.count == 8)
        #expect(Array(afterDuplicate.map(\.id)[0..<4]) == ["h", "p1", "b1", "b2"])
        #expect(afterDuplicate[4].plainText == "Item one" && afterDuplicate[5].plainText == "Child" && afterDuplicate[5].depth == 1)
        #expect(!["b1", "b2"].contains(afterDuplicate[4].id))
        #expect(harness.operations(from: start).filter { $0.operation == .insert }.count == 2)
        // Move Down past the copy (a sibling with its child), then back up.
        script.step("Move") { harness.choose(harness.blockMenu("b1"), "Move Down") }
        #expect(Array(harness.ids[2..<6]) == [afterDuplicate[4].id, afterDuplicate[5].id, "b1", "b2"])
        script.step("Move") { harness.choose(harness.blockMenu("b1"), "Move Up") }
        #expect(Array(harness.ids[2..<4]) == ["b1", "b2"])
        script.step("Delete") { harness.choose(harness.blockMenu("b1"), "Delete") }
        #expect(!harness.ids.contains("b1") && !harness.ids.contains("b2"))
        // Copy Link to Block: a block reference, and Obsidian's form as text.
        harness.choose(harness.blockMenu("p1"), "Copy Link to Block")
        let pasteboard = NSPasteboard.general
        #expect(pasteboard.string(forType: .string) == "[[Trip  draft #^p1]]")
        #expect(pasteboard.data(forType: BlockEditorController.blockReferencePasteboardType) != nil)
        harness.controller.endBlockSelection(caret: 0)
        script.walkBackAndForth()
    }

    @Test func aCopiedBlockLinkPastesAsABlockReference() throws {
        let harness = BlockFixture.harness()
        harness.select(NSRange(location: 0, length: 0))
        harness.controller.copyLinkToBlock()
        let last = try #require(harness.controller.range(ofBlock: "p2"))
        harness.select(NSRange(location: NSMaxRange(last) - 1, length: 0))
        var script = UndoScript(harness)
        script.step("Paste") { harness.focused.paste(nil); settleRunLoop() }
        let runs = harness.controller.currentBlocks().last?.runs ?? []
        #expect(runs.contains { $0.node.flatMap(NoteLinks.blockTarget) == "h" })
        script.walkBackAndForth()
    }

    // MARK: The + button and the / menu

    @Test func plusAddsABlockBelowAndOpensTheInsertMenu() throws {
        let harness = BlockFixture.harness()
        harness.controller.hover(at: harness.point(onBlock: "b1"))
        harness.controller.blockHandle.addBlock()
        settleRunLoop()
        let blocks = harness.controller.currentBlocks()
        // Below b1 and its child, at b1's depth, holding the "/" the menu filters from.
        #expect(Array(blocks.map(\.id)[0..<4]) == ["h", "p1", "b1", "b2"])
        #expect(blocks[4].plainText == "/" && blocks[4].depth == 0)
        #expect(harness.controller.slashMenu.isActive)
        harness.type("h3")
        #expect(harness.controller.slashMenu.model.items.map(\.title) == ["Heading 3"])
        harness.command(#selector(NSResponder.insertNewline(_:)))
        let made = harness.controller.currentBlocks()[4]
        #expect(made.kind == .heading3 && made.plainText.isEmpty)
        #expect(!harness.controller.slashMenu.isActive)
        harness.undo()
        #expect(harness.controller.currentBlocks()[4].plainText == "/h3")
    }

    @Test func slashMenuFilters() {
        func titles(_ query: String, inColumn: Bool = false) -> [String] { SlashItem.items(matching: query, inColumn: inColumn).map(\.title) }
        #expect(titles("").count == SlashItem.catalog.count)
        #expect(titles("head") == ["Heading 1", "Heading 2", "Heading 3", "Heading 4", "Heading 5", "Heading 6"])
        #expect(titles("h2") == ["Heading 2"])
        #expect(titles("todo") == ["Checklist"])
        #expect(titles("bul") == ["Bulleted List"])
        #expect(titles("col") == ["2 Columns", "3 Columns"])
        #expect(titles("col", inColumn: true).isEmpty, "columns don't nest")
        #expect(titles("latex").isEmpty, "no math blocks")
        #expect(titles("table 4x5") == ["Table 4 × 5"])
        #expect(titles("3×2") == ["Table 3 × 2"])
        #expect(titles("zzz").isEmpty)
        #expect(SlashItem.tableSize("table 40x5") == nil)
    }

    @Test func slashOpensOnlyAtTheStartOrAfterASpace() {
        let harness = BlockFixture.harness([
            EditorBlock(id: "a", runs: []),
            EditorBlock(id: "c", kind: .code, runs: [InlineRun("let x")]),
            EditorBlock(id: "l", runs: [InlineRun("see ", InlineMarks()), InlineRun("example", InlineMarks(link: "https://example.com"))]),
        ])
        let menu = harness.controller.slashMenu
        harness.select(NSRange(location: 0, length: 0))
        harness.type("/")
        #expect(menu.isActive, "at the start of an empty block")
        // Esc closes it and leaves the "/" as text.
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        #expect(!menu.isActive)
        #expect(harness.controller.currentBlocks()[0].plainText == "/")
        #expect(harness.controller.blockSelection == nil, "Esc went to the menu, not to block selection")
        harness.type(" a/")
        #expect(!menu.isActive, "not in the middle of a word")
        harness.type(" https:/")
        harness.type("/")
        #expect(!menu.isActive, "not inside a URL")
        harness.type(" /")
        #expect(menu.isActive, "after a space")
        harness.type("zzz ")
        #expect(!menu.isActive, "a space after nothing that matches: just writing")
        let code = harness.controller.range(ofBlock: "c")!
        harness.select(NSRange(location: NSMaxRange(code) - 1, length: 0))
        harness.type(" /")
        #expect(!menu.isActive, "not in a code block")
        let link = harness.controller.range(ofBlock: "l")!
        harness.select(NSRange(location: link.location + 6, length: 0))
        harness.type("/")
        #expect(!menu.isActive, "not inside a link")
    }

    @Test func slashMenuInsertsBlocksAsOneStepEach() throws {
        let harness = BlockFixture.harness([EditorBlock(id: "a", runs: [InlineRun("Notes")]), EditorBlock(id: "e", runs: [])])
        let empty = harness.controller.range(ofBlock: "e")!
        harness.select(NSRange(location: empty.location, length: 0))
        var script = UndoScript(harness)
        harness.type("/quo")
        script.states.append(("Typing", harness.snapshot()))
        // ↓ and ↑ move in the list; ↩ chooses.
        #expect(harness.controller.slashMenu.model.items.first?.title == "Quote")
        script.step("Quote") { harness.command(#selector(NSResponder.insertNewline(_:))) }
        #expect(harness.controller.currentBlocks()[1].kind == .quote && harness.controller.currentBlocks()[1].plainText.isEmpty)
        // After text and a space: a new block below, the text stays.
        harness.type("Said so /table 2x4")
        script.states.append(("Typing", harness.snapshot()))
        script.step("Insert Table") { harness.command(#selector(NSResponder.insertTab(_:))) }
        let blocks = harness.controller.currentBlocks()
        #expect(blocks[1].plainText == "Said so ")
        let table = try #require(blocks.first { $0.kind == .table }?.table)
        #expect(table.rows.count == 2 && table.rows[0].cells.count == 4)
        #expect(blocks.last?.kind == .paragraph, "a paragraph after the table to type in")
        script.walkBackAndForth()
    }

    @Test func slashColumns() throws {
        let harness = BlockFixture.harness([EditorBlock(id: "e", runs: [])])
        harness.select(NSRange(location: 0, length: 0))
        harness.type("/3 col")
        harness.command(#selector(NSResponder.insertNewline(_:)))
        let columns = try #require(harness.controller.currentBlocks().first?.columns)
        #expect(columns.count == 3)
        // Saved as columns → column ×3 → paragraph each.
        let operations = harness.operations(from: [EditorBlock(id: "e", runs: [])])
        #expect(operations.filter { $0.type == "column" }.count == 3)
        #expect(operations.contains { $0.type == "columns" })
    }

    // MARK: Block selection (Esc)

    @Test func escSelectsBlocksAndTheKeysActOnThem() throws {
        let harness = BlockFixture.harness()
        let controller = harness.controller
        let p1 = controller.range(ofBlock: "p1")!
        harness.select(NSRange(location: p1.location + 3, length: 0))
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        #expect(controller.blockSelection?.anchor == "p1" && controller.blockSelection?.head == "p1")
        #expect(controller.blockSelectionOverlay?.superview === controller.textView)
        // ⇧↓ takes in the next block with its child; ↓ alone moves to the block below.
        harness.command(#selector(NSResponder.moveDownAndModifySelection(_:)))
        #expect(controller.blockSelection?.head == "b1")
        #expect(controller.blockSelectionRange() == NSUnionRange(p1, controller.range(ofBlock: "b2")!))
        harness.command(#selector(NSResponder.moveUpAndModifySelection(_:)))
        #expect(controller.blockSelection?.head == "p1")
        harness.command(#selector(NSResponder.moveDown(_:)))
        #expect(controller.blockSelection?.anchor == "b1" && controller.blockSelection?.head == "b1")
        harness.command(#selector(NSResponder.moveUpAndModifySelection(_:)))
        #expect(controller.blockSelection?.head == "p1")

        var script = UndoScript(harness)
        // ⌥⇧↓ moves the selected blocks (with b1's child) below the next block.
        script.step("Move") { harness.command(#selector(NSResponder.moveParagraphForwardAndModifySelection(_:))) }
        #expect(harness.ids == ["h", "b3", "p1", "b1", "b2", "p2"])
        #expect(controller.blockSelection != nil, "still selected after the move")
        script.step("Move") { harness.command(#selector(NSResponder.moveParagraphBackwardAndModifySelection(_:))) }
        #expect(harness.ids == ["h", "p1", "b1", "b2", "b3", "p2"])
        // Tab indents them (as far as the block above allows); ⇧Tab takes it back.
        script.step("Indent") { harness.command(#selector(NSResponder.insertTab(_:))) }
        #expect(harness.controller.currentBlocks()[1].depth == 1)
        script.step("Outdent") { harness.command(#selector(NSResponder.insertBacktab(_:))) }
        #expect(harness.controller.currentBlocks()[1].depth == 0)
        // ⌘D duplicates them; the copies are selected.
        script.step("Duplicate") { harness.key("d", keyCode: 2, modifiers: .command) }
        #expect(harness.ids.count == 9)
        #expect(controller.blockSelection?.anchor != "p1")
        // ⌫ deletes the selected blocks.
        script.step("Delete") { harness.command(#selector(NSResponder.deleteBackward(_:))) }
        #expect(harness.ids == ["h", "p1", "b1", "b2", "b3", "p2"])
        #expect(controller.blockSelection == nil)
        script.walkBackAndForth()
    }

    @Test func copyingSelectedBlocksPutsBlocksMarkdownRichAndPlainText() throws {
        let harness = BlockFixture.harness()
        let controller = harness.controller
        let b1 = controller.range(ofBlock: "b1")!
        harness.select(NSRange(location: b1.location, length: 0))
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        harness.focused.copy(nil)
        let types = NSPasteboard.general.types ?? []
        for type in [BlockEditorController.blocksPasteboardType, BlockEditorController.markdownPasteboardType, .rtf, .string] {
            #expect(types.contains(type), "\(type.rawValue) on the pasteboard")
        }
        #expect(NSPasteboard.general.string(forType: BlockEditorController.markdownPasteboardType)?.contains("- Item one") == true)
        #expect(NSPasteboard.general.string(forType: BlockEditorController.markdownPasteboardType)?.contains("Child") == true)
        // The last block of the note copies whole too (not as part of a paragraph).
        let last = controller.range(ofBlock: "p2")!
        harness.select(NSRange(location: last.location, length: 0))
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        harness.focused.copy(nil)
        let data = try #require(NSPasteboard.general.data(forType: BlockEditorController.blocksPasteboardType))
        let payload = try JSONDecoder().decode([[String: JSONValue]].self, from: data)
        #expect(payload.first?["partial"]?.boolValue == false)
        // ⌘X: copied, then removed as blocks.
        var script = UndoScript(harness)
        script.step("Cut") { harness.focused.cut(nil); settleRunLoop() }
        #expect(!harness.ids.contains("p2"))
        script.walkBackAndForth()
    }

    @Test func typingOrReturnEndsTheBlockSelection() throws {
        let harness = BlockFixture.harness()
        let controller = harness.controller
        let p1 = controller.range(ofBlock: "p1")!
        harness.select(NSRange(location: p1.location, length: 0))
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        #expect(controller.blockSelection != nil)
        // A typed character: the caret at the end of the block, the character typed there.
        harness.key("x", keyCode: 7)
        #expect(controller.blockSelection == nil)
        #expect(controller.currentBlocks()[1].plainText == "First paragraphx")
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        harness.key("\r", keyCode: 36)
        #expect(controller.blockSelection == nil)
        #expect(controller.textView.selectedRange().length == 0)
        // Esc again lets go too.
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        #expect(controller.blockSelection == nil)
        #expect(harness.ids == ["h", "p1", "b1", "b2", "b3", "p2"])
    }

    // MARK: Dragging

    @Test func draggingMovesBlocksWithTheirChildrenAndNests() throws {
        let harness = BlockFixture.harness()
        let controller = harness.controller
        let start = controller.currentBlocks()
        let x = harness.point(onBlock: "b1").x
        var script = UndoScript(harness)
        // b1 (with b2) dropped below the last block.
        script.step("Move") {
            let drag = BlockDrag(source: controller, firstId: "b1", lastId: "b2", startX: x)
            #expect(controller.dropBlocks(drag, at: harness.point(onBlock: "p2", lower: true)))
            settleRunLoop()  // one event per drop, as a person drags
        }
        #expect(harness.ids == ["h", "p1", "b3", "p2", "b1", "b2"])
        #expect(controller.currentBlocks()[5].depth == 1, "the child came along")
        // Saved as moves (ids kept), nothing inserted or deleted.
        let operations = harness.operations(from: start)
        #expect(operations.allSatisfy { $0.operation == .move })
        // Dragged right of where it started: nested under the block above.
        script.step("Move") {
            let drag = BlockDrag(source: controller, firstId: "p2", lastId: "p2", startX: x)
            #expect(controller.dropBlocks(drag, at: CGPoint(x: x + controller.theme.indentStep * 1.2, y: harness.point(onBlock: "b3", lower: true).y)))
            settleRunLoop()  // one event per drop, as a person drags
        }
        #expect(harness.ids == ["h", "p1", "b3", "p2", "b1", "b2"])
        #expect(controller.currentBlocks()[3].depth == 1)
        #expect(BlockDiff.shape(controller.currentBlocks())[3].parent == "b3")
        script.walkBackAndForth()
    }

    @Test func draggingBesideABlockMakesColumnsAndInAndOutOfThem() throws {
        let harness = BlockFixture.harness()
        let controller = harness.controller
        let start = controller.currentBlocks()
        let width = controller.textView.textContainer!.size.width
        var script = UndoScript(harness)
        // p2 dropped at the right edge of p1: p1 | p2 side by side.
        script.step("Move") {
            let drag = BlockDrag(source: controller, firstId: "p2", lastId: "p2", startX: 0)
            #expect(controller.dropBlocks(drag, at: harness.point(onBlock: "p1", dx: width - 10)))
            settleRunLoop()  // one event per drop, as a person drags
        }
        harness.layout()
        #expect(harness.outline.contains { $0.hasSuffix("[p1/d0 | p2/d0]") })
        let operations = harness.operations(from: start)
        let columns = try #require(operations.first { $0.type == "columns" })
        #expect(operations.filter { $0.type == "column" }.allSatisfy { $0.parentId == columns.blockId || $0.afterId != nil })
        #expect(operations.contains { $0.operation == .move && $0.blockId == "p1" })
        #expect(operations.contains { $0.operation == .move && $0.blockId == "p2" })
        // b3 dragged into the second column, below p2.
        let columnsId = try #require(controller.currentBlocks().first { $0.kind == .columns }?.id)
        let live = try #require(harness.live(ColumnsBlockController.self))
        let second = live.editors[1]
        let p2InColumn = second.selectionFrame(second.paragraph(at: second.range(ofBlock: "p2")!.location))
        let below = controller.textView.convert(CGPoint(x: p2InColumn.minX + 10, y: p2InColumn.maxY - 2), from: second.textView)
        script.step("Move") {
            let drag = BlockDrag(source: controller, firstId: "b3", lastId: "b3", startX: 0)
            #expect(controller.dropBlocks(drag, at: below))
            settleRunLoop()  // one event per drop, as a person drags
        }
        harness.layout()
        #expect(harness.outline.contains { $0.hasSuffix("[p1/d0 | p2/d0,b3/d0]") })
        // p1 dragged out of its column to the end of the note: one column left, unwrapped.
        let liveAgain = try #require(harness.live(ColumnsBlockController.self))
        script.step("Move") {
            let drag = BlockDrag(source: liveAgain.editors[0], firstId: "p1", lastId: "p1", startX: 0)
            let end = harness.point(onBlock: try! #require(controller.currentBlocks().last?.id), lower: true)
            #expect(controller.dropBlocks(drag, at: end))
            settleRunLoop()  // one event per drop, as a person drags
        }
        #expect(!harness.outline.contains { $0.contains("columns") }, "unwrapped: \(harness.outline)")
        #expect(harness.ids == ["h", "p2", "b3", "b1", "b2", "p1"], "the columns unwrapped where they were")
        #expect(!harness.ids.contains(columnsId))
        script.walkBackAndForth()
    }

    @Test func draggingTextIntoATableCell() throws {
        var blocks = BlockFixture.blocks()
        var table = TableContent.blank(rows: 2, columns: 2)
        table.setRuns([InlineRun("A")], row: 0, column: 0)
        blocks.insert(EditorBlock(id: "t", kind: .table, table: table), at: 2)
        let harness = BlockFixture.harness(blocks)
        let controller = harness.controller
        let grid = try #require(harness.live(TableBlockController.self))
        var script = UndoScript(harness)
        script.step("Move") {
            let cell = controller.textView.convert(grid.cellFrame(row: 0, column: 1), from: grid.grid)
            let drag = BlockDrag(source: controller, firstId: "p2", lastId: "p2", startX: 0)
            #expect(controller.dropBlocks(drag, at: CGPoint(x: cell.midX, y: cell.midY)))
            settleRunLoop()  // one event per drop, as a person drags
        }
        let after = try #require(controller.currentBlocks().first { $0.kind == .table }?.table)
        #expect(after.cell(row: 0, column: 1)?.plainText == "Last")
        #expect(!harness.ids.contains("p2"))
        script.walkBackAndForth()
    }

    // MARK: Columns from the menus

    @Test func columnsTurnAddRemoveAndUnwrap() throws {
        let harness = BlockFixture.harness()
        let controller = harness.controller
        let start = controller.currentBlocks()
        // p1, b1 (+ child) and b3 selected: three blocks into two columns.
        let p1 = controller.range(ofBlock: "p1")!
        harness.select(NSRange(location: p1.location, length: 0))
        harness.command(#selector(NSResponder.cancelOperation(_:)))
        harness.command(#selector(NSResponder.moveDownAndModifySelection(_:)))
        harness.command(#selector(NSResponder.moveDownAndModifySelection(_:)))
        var script = UndoScript(harness)
        script.step("Columns") { harness.send("fortressTurnIntoTwoColumns:") }
        harness.layout()
        #expect(harness.outline.contains { $0.hasSuffix("[p1/d0,b1/d0,b2/d1 | b3/d0]") }, "\(harness.outline)")
        // Saved: the columns and two column blocks inserted, the blocks moved in (b2 stays under b1).
        let operations = harness.operations(from: start)
        #expect(operations.filter { $0.operation == .insert }.compactMap(\.type).sorted() == ["column", "column", "columns"])
        #expect(Set(operations.filter { $0.operation == .move }.compactMap(\.blockId)) == ["p1", "b1", "b3"])
        #expect(operations.allSatisfy { $0.operation != .delete })
        // The columns block is selected: Add Column, Remove Column, Unwrap.
        script.step("Add Column") { harness.send("fortressAddColumn:") }
        #expect(controller.currentBlocks().first { $0.kind == .columns }?.columns?.count == 3)
        script.step("Remove Column") { harness.send("fortressRemoveColumn:") }
        #expect(controller.currentBlocks().first { $0.kind == .columns }?.columns?.count == 2)
        script.step("Unwrap Columns") { harness.send("fortressUnwrapColumns:") }
        #expect(harness.ids == ["h", "p1", "b1", "b2", "b3", "p2"])
        script.walkBackAndForth()
        // Undone to the start: nothing to save.
        while harness.view.undoManager?.canUndo == true { harness.undo() }
        #expect(harness.operations(from: start).isEmpty)
    }

    // MARK: Show Blocks

    @Test func showBlocksOutlinesEveryBlock() throws {
        let harness = BlockFixture.harness()
        defer { UserDefaults.standard.removeObject(forKey: BlockOutlines.key) }
        func outlined() -> Bool {
            guard let manager = harness.controller.textView.textLayoutManager else { return false }
            harness.layout()
            return (manager.textLayoutFragment(for: manager.documentRange.location) as? BlockLayoutFragment)?.showsBlocks ?? false
        }
        #expect(!outlined(), "off by default")
        UserDefaults.standard.set(true, forKey: BlockOutlines.key)
        settleRunLoop()
        #expect(outlined())
        UserDefaults.standard.set(false, forKey: BlockOutlines.key)
        settleRunLoop()
        #expect(!outlined())
    }

    @Test func blockShortcutsKeepTheSystemsKeys() {
        #expect(Shortcuts.showBlocks.chord == "⌥⌘B")
        #expect(Shortcuts.systemReserved["⌥⌘B"] == nil)
        // ⌘D is the HIG's Duplicate: used for Duplicate (of blocks), its own meaning.
        #expect(Shortcuts.duplicateBlock.chord == "⌘D" && Shortcuts.sameMeaning.contains("⌘D"))
        // ⌥⇧↑/↓ select to the paragraph's start/end in text: the menu's Move Up/Down keep
        // ⌃⌘↑/↓, and ⌥⇧↑/↓ move blocks only while blocks are selected.
        let chords = Set(Shortcuts.all.map(\.chord))
        #expect(!chords.contains("⌥⇧↑") && !chords.contains("⌥⇧↓"))
        #expect(Shortcuts.systemReserved["⌥⇧↑"] != nil)
    }
}
