import AppKit
import Testing

@testable import FortressKit

// Note links and block references behave as one unit; tables draw their cells and edit
// one at a time; a long note opens its first screens and loads the rest; the save diff
// and the read-back take the quick path only when it gives the same answer.

@MainActor private var atomicWindows: [NSWindow] = []

@MainActor
private func linkEditor(_ blocks: [EditorBlock], titles: [String: String] = ["N1": "Packing list"]) -> (BlockEditorController, NSWindow) {
    let controller = BlockEditorController(baseURL: URL(string: "http://127.0.0.1:9")!)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 600), styleMask: [.titled], backing: .buffered, defer: true)
    window.isReleasedWhenClosed = false
    window.contentView = controller.scrollView
    controller.context.adoptResolvedLinks(ResolvedLinks(documents: titles.mapValues { .init(title: $0) }))
    controller.load(blocks)
    window.makeFirstResponder(controller.textView)
    controller.textView.layoutSubtreeIfNeeded()
    controller.textView.textLayoutManager?.textViewportLayoutController.layoutViewport()
    atomicWindows.append(window)
    return (controller, window)
}

private let link = NoteLinks.documentNode(id: "N1")
/// "See Packing list." — the label is characters 4..<16.
private let linked = [EditorBlock(id: "a", runs: [InlineRun("See "), .atomic(link), InlineRun(".")])]
private let label = NSRange(location: 4, length: 12)

private func nodes(_ block: EditorBlock) -> Int { block.runs.filter { $0.node != nil }.count }

@MainActor
@Suite(.serialized) struct InlineNodeTests {
    @Test func theCaretStepsOverALabel() {
        let (editor, _) = linkEditor(linked)
        let view = editor.textView
        view.setSelectedRange(NSRange(location: 4, length: 0))
        view.moveRight(nil)
        #expect(view.selectedRange() == NSRange(location: 16, length: 0))
        view.moveLeft(nil)
        #expect(view.selectedRange() == NSRange(location: 4, length: 0))
        // Put inside by a program: it goes to an edge, never in the middle.
        view.setSelectedRange(NSRange(location: 9, length: 0))
        #expect([4, 16].contains(view.selectedRange().location))
        // A selection takes the whole label.
        view.setSelectedRange(NSRange(location: 2, length: 4))
        #expect(view.selectedRange() == NSRange(location: 2, length: 14))
    }

    @Test func typingInsideALabelGoesAfterIt() {
        let (editor, _) = linkEditor(linked)
        editor.textView.insertText("!", replacementRange: NSRange(location: 9, length: 0))
        let runs = editor.currentBlocks()[0].runs
        #expect(runs == [InlineRun("See "), .atomic(link), InlineRun("!.")])
        #expect(editor.storage.string == "See Packing list!.\n")
    }

    @Test func deleteRemovesTheWholeNode() {
        let (editor, _) = linkEditor(linked)
        editor.textView.setSelectedRange(NSRange(location: 16, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.deleteBackward(_:)))
        #expect(editor.currentBlocks()[0].runs == [InlineRun("See .")])
        #expect(editor.textView.selectedRange() == NSRange(location: 4, length: 0))
        editor.textView.undoManager?.undo()
        #expect(editor.currentBlocks()[0].runs == linked[0].runs)
        // Forward delete in front of it, and a selection cutting into it.
        editor.textView.setSelectedRange(NSRange(location: 4, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.deleteForward(_:)))
        #expect(editor.currentBlocks()[0].runs == [InlineRun("See .")])
        editor.textView.undoManager?.undo()
        editor.textView.insertText("", replacementRange: NSRange(location: 10, length: 7))
        #expect(editor.currentBlocks()[0].runs == [InlineRun("See ")])
    }

    @Test func copyPastedNextToItselfIsASecondNode() throws {
        let (editor, _) = linkEditor(linked)
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("atomic-\(UUID().uuidString)"))
        pasteboard.clearContents()
        editor.textView.setSelectedRange(label)
        #expect(editor.writeBlocks(to: pasteboard))
        editor.textView.setSelectedRange(NSRange(location: 16, length: 0))
        #expect(editor.pasteBlocks(from: pasteboard))
        let block = editor.currentBlocks()[0]
        #expect(nodes(block) == 2)
        #expect(block.runs == [InlineRun("See "), .atomic(link), .atomic(link), InlineRun(".")])
        let operations = BlockDiff.operations(from: SyncedTree(linked), to: editor.currentBlocks()).operations
        #expect(operations.count == 1)
        // The two are separate: deleting one keeps the other.
        editor.textView.setSelectedRange(NSRange(location: 28, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.deleteBackward(_:)))
        #expect(nodes(editor.currentBlocks()[0]) == 1)
    }

    @Test func aCopyOfTheSameAttributesIsASecondNodeToo() {
        let (editor, _) = linkEditor(linked)
        // The label's characters with its very attributes (its box), put right after it.
        let copy = editor.storage.attributedSubstring(from: label)
        editor.replace(NSRange(location: 16, length: 0), with: copy)
        #expect(nodes(editor.currentBlocks()[0]) == 2)
        // And a copy elsewhere in the paragraph.
        editor.replace(NSRange(location: 0, length: 0), with: copy)
        #expect(nodes(editor.currentBlocks()[0]) == 3)
    }

    @Test func typingInACellLinkGoesAfterIt() throws {
        let table = TableContent(rows: [TableRow(cells: [TableCell(runs: [InlineRun("See "), .atomic(link)]), TableCell()])])
        let (editor, _) = linkEditor([EditorBlock(id: "t", kind: .table, table: table)])
        let live = try #require((editor.storage.attribute(.attachment, at: 0, effectiveRange: nil) as? BlockAttachment)?.live as? TableBlockController)
        let cell = try #require(live.editor(row: 0, column: 0))
        cell.insertText("x", replacementRange: NSRange(location: 8, length: 0))
        #expect(live.table.cell(row: 0, column: 0)?.runs == [InlineRun("See "), .atomic(link), InlineRun("x")])
        cell.setSelectedRange(NSRange(location: 6, length: 0))
        #expect([4, 16].contains(cell.selectedRange().location))
    }
}

@MainActor
@Suite(.serialized) struct TableDrawingTests {
    private func table(rows: Int = 6, columns: Int = 4) throws -> (BlockEditorController, NSWindow, TableBlockController) {
        var content = TableContent.blank(rows: rows, columns: columns)
        for r in 0..<rows { for c in 0..<columns { content.setRuns([InlineRun("R\(r)C\(c)")], row: r, column: c) } }
        let (editor, window) = linkEditor([EditorBlock(id: "p", runs: [InlineRun("Above")]), EditorBlock(id: "t", kind: .table, table: content),
                                           EditorBlock(id: "q", runs: [InlineRun("Below")])])
        let range = try #require(editor.range(ofBlock: "t"))
        let live = try #require((editor.storage.attribute(.attachment, at: range.location, effectiveRange: nil) as? BlockAttachment)?.live as? TableBlockController)
        _ = live.height(forWidth: 600)
        return (editor, window, live)
    }

    @Test func cellsAreDrawnAndOneIsEditedAtATime() throws {
        let (_, window, live) = try table()
        #expect(live.grid.subviews.filter { $0 is CellTextView }.isEmpty)
        live.focus(row: 2, column: 1, atEnd: true)
        let first = try #require(live.editor)
        #expect(window.firstResponder === first)
        #expect(first.string == "R2C1")
        #expect(first.frame == live.textFrame(row: 2, column: 1))
        live.focus(row: 3, column: 2, atEnd: true)
        #expect(live.grid.subviews.filter { $0 is CellTextView }.count == 1)
        #expect(live.editor?.string == "R3C2")
    }

    @Test func arrowsAndTabMoveBetweenCells() throws {
        let (_, _, live) = try table()
        live.focus(row: 1, column: 1, atEnd: true)
        try #require(live.editor).doCommand(by: #selector(NSResponder.moveDown(_:)))
        #expect(live.editor?.row == 2 && live.editor?.column == 1)
        try #require(live.editor).doCommand(by: #selector(NSResponder.moveUp(_:)))
        #expect(live.editor?.row == 1 && live.editor?.column == 1)
        try #require(live.editor).doCommand(by: #selector(NSResponder.insertBacktab(_:)))
        #expect(live.editor?.row == 1 && live.editor?.column == 0)
    }

    @Test func editsInALeftCellAreKeptDrawnAndUndoable() throws {
        let (editor, window, live) = try table()
        live.focus(row: 0, column: 0, atEnd: true)
        let cell = try #require(live.editor)
        // Undo while the cell is being edited.
        cell.insertText(" x", replacementRange: cell.selectedRange())
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        window.undoManager?.undo()
        #expect(live.table.cell(row: 0, column: 0)?.plainText == "R0C0", "undo in the cell: \(window.undoManager?.canUndo ?? false)")
        cell.setSelectedRange(NSRange(location: cell.string.utf16.count, length: 0))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        cell.insertText(" edited", replacementRange: cell.selectedRange())
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        // Leave the table: the cell is drawn again, from its new text.
        window.makeFirstResponder(editor.textView)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        #expect(live.editor == nil)
        #expect(live.text(row: 0, column: 0).string == "R0C0 edited")
        #expect(editor.currentBlocks()[1].table?.cell(row: 0, column: 0)?.plainText == "R0C0 edited")
        window.undoManager?.undo()
        #expect(live.table.cell(row: 0, column: 0)?.plainText == "R0C0")
        #expect(live.text(row: 0, column: 0).string == "R0C0")
    }

    @Test func accessibilitySeesRowsOfCells() throws {
        let (_, _, live) = try table(rows: 3, columns: 2)
        let rows = try #require(live.grid.accessibilityChildren() as? [NSAccessibilityElement])
        #expect(rows.count == 3)
        let cells = try #require(rows[1].accessibilityChildren() as? [NSAccessibilityElement])
        #expect(cells.count == 2)
        #expect(cells[1].accessibilityRole() == .cell)
        #expect(cells[1].accessibilityValue() as? String == "R1C1")
        #expect(cells[1].accessibilityPerformPress())
        #expect(live.editor?.row == 1 && live.editor?.column == 1)
    }

    @Test func copyFromACellEditor() throws {
        let (_, _, live) = try table()
        live.focus(row: 4, column: 3, atEnd: false)
        let cell = try #require(live.editor)
        #expect(cell.selectedRange() == NSRange(location: 0, length: 4))
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("cell-\(UUID().uuidString)"))
        pasteboard.clearContents()
        let types = cell.writablePasteboardTypes
        #expect(cell.writeSelection(to: pasteboard, types: types), "\(types)")
        #expect(pasteboard.string(forType: .string) == "R4C3", "\(pasteboard.types ?? [])")
    }
}

@MainActor
@Suite(.serialized) struct LongNoteLoadingTests {
    private static func blocks(_ count: Int) -> [EditorBlock] {
        (0..<count).map { index in
            index % 40 == 20 ? EditorBlock(id: "t\(index)", kind: .table, table: .blank(rows: 3, columns: 2))
                : EditorBlock(id: "p\(index)", kind: index % 9 == 0 ? .heading2 : .paragraph, runs: [InlineRun("Paragraph \(index) with a few words.")])
        }
    }

    @Test func theFirstScreensShowFirstAndTheRestFollows() throws {
        let blocks = Self.blocks(600)
        let (editor, _) = linkEditor(blocks)
        #expect(!editor.pendingBlocks.isEmpty)
        #expect(editor.storage.string.components(separatedBy: "\n").count - 1 < 600)
        // Saving sees the whole note meanwhile.
        #expect(editor.currentBlocks().map(\.id) == blocks.map(\.id))
        let base = SyncedTree(editor.currentBlocks(), reusing: nil, lazily: true)
        // An edit in the first screens while the rest loads.
        editor.textView.setSelectedRange(NSRange(location: 3, length: 0))
        editor.textView.insertText("Z", replacementRange: editor.textView.selectedRange())
        var turns = 0
        while !editor.pendingBlocks.isEmpty && turns < 200 {
            RunLoop.main.run(mode: .default, before: Date())
            turns += 1
        }
        #expect(editor.pendingBlocks.isEmpty)
        let read = editor.currentBlocks()
        #expect(read.map(\.id) == blocks.map(\.id))
        let operations = BlockDiff.operations(from: base, to: read).operations
        #expect(operations.map(\.blockId) == ["p0"])
        // Read back from scratch: the same.
        editor.discardReadCache()
        #expect(editor.currentBlocks() == read)
    }

    @Test func findingABlockFurtherDownFinishesLoading() throws {
        let (editor, _) = linkEditor(Self.blocks(600))
        #expect(!editor.pendingBlocks.isEmpty)
        #expect(editor.range(ofBlock: "p599") != nil)
        #expect(editor.pendingBlocks.isEmpty)
        editor.textView.setSelectedRange(NSRange(location: 0, length: 0))
        let (other, _) = linkEditor(Self.blocks(600))
        other.textView.selectAll(nil)
        #expect(other.pendingBlocks.isEmpty)
        #expect(other.textView.selectedRange().length == other.storage.length - 1)
    }
}

@MainActor
@Suite struct QuickPathTests {
    /// The read-back after edits equals reading everything back from scratch.
    @Test func readingOnlyEditedBlocksMatchesAFullRead() {
        let blocks = (0..<60).map { EditorBlock(id: "p\($0)", kind: $0 % 5 == 0 ? .bullet : .paragraph, runs: [InlineRun("Line \($0)")]) }
        let (editor, _) = linkEditor(blocks)
        _ = editor.currentBlocks()
        let view = editor.textView
        func check(_ step: String) {
            let quick = editor.currentBlocks()
            editor.discardReadCache()
            #expect(quick == editor.currentBlocks(), "\(step)")
        }
        view.insertText("x", replacementRange: NSRange(location: 2, length: 0)); check("type")
        view.setSelectedRange(NSRange(location: 20, length: 0)); view.doCommand(by: #selector(NSResponder.insertNewline(_:))); check("return")
        view.setSelectedRange(NSRange(location: 21, length: 0)); view.doCommand(by: #selector(NSResponder.deleteBackward(_:))); check("join")
        view.setSelectedRange(NSRange(location: 40, length: 3)); editor.toggleMark(\.bold); check("bold")
        view.setSelectedRange(NSRange(location: 50, length: 0)); editor.setKind(.heading1); check("kind")
        view.setSelectedRange(NSRange(location: 60, length: 0)); editor.indent(by: 1); check("indent")
        view.undoManager?.undo(); check("undo")
        view.insertText("a\nb\nc", replacementRange: NSRange(location: 70, length: 5)); check("paste lines")
    }

    /// The diff patched from the last tree gives what the full diff gives.
    @Test func patchedDiffMatchesTheFullDiff() throws {
        var blocks = (0..<80).map { index -> EditorBlock in
            if index % 20 == 10 { return EditorBlock(id: "t\(index)", kind: .table, table: .blank(rows: 3, columns: 3)) }
            if index % 7 == 3 { return EditorBlock(id: "q\(index)", kind: .quote, runs: [InlineRun("Quoted \(index)")], container: QuoteContainer(id: "c\(index)")) }
            return EditorBlock(id: "p\(index)", kind: index % 4 == 0 ? .bullet : .paragraph, depth: index % 4 == 1 ? 1 : 0, runs: [InlineRun("Block \(index)")])
        }
        var base = SyncedTree(blocks, reusing: nil, lazily: true)
        func check(_ step: String) {
            let (quick, result) = BlockDiff.operations(from: base, to: blocks)
            let full = BlockDiff.operations(from: base, to: SyncedTree(blocks, reusing: nil), root: nil)
            #expect(quick == full, "\(step)")
            base = result
        }
        blocks[5].runs = [InlineRun("Edited")]; check("text")
        blocks[6].kind = .heading2; check("kind within the text group")
        blocks[10].table?.setRuns([InlineRun("cell")], row: 1, column: 1); check("table cell")
        blocks[3].runs = [InlineRun("Quote edited")]; check("quote line")
        blocks[8].kind = .check; blocks[8].checked = true; check("check")
        blocks[9].kind = .image; blocks[9].runs = []; check("type change across groups")
        blocks[12].depth = 1; check("indent")
        blocks.remove(at: 14); check("delete")
        blocks.insert(EditorBlock(id: "new", runs: [InlineRun("New")]), at: 2); check("insert")
        blocks.swapAt(20, 21); check("move")
        let table = try #require(blocks.firstIndex { $0.id == "t10" })
        blocks[table].table?.setRuns([InlineRun("cell again")], row: 1, column: 1); check("table cell again")
        blocks[table].table?.rows[2].cells[0].attrs["background"] = .string("red"); check("cell colour")
        blocks[table].table?.insertRow(at: 1); check("row added")
        // From a baseline that never made the table's rows (as a note's baseline is).
        base = SyncedTree(blocks, reusing: nil, lazily: true)
        blocks[table].table?.setRuns([InlineRun("from a baseline")], row: 0, column: 0); check("cell text, lazy baseline")
        blocks[table].table?.rows[0].cells[1].attrs["align"] = .string("right"); check("cell attr after")
        base = SyncedTree(blocks, reusing: nil, lazily: true)
        blocks[table].table?.rows[0].cells[2].attrs["align"] = .string("center"); blocks[5].runs = [InlineRun("x")]
        check("cell attr and text, lazy baseline")
    }
}
