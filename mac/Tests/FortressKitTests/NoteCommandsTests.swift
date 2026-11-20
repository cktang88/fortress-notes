import AppKit
import PDFKit
import Testing

@testable import FortressKit

/// File ▸ Print… and Edit ▸ Find from inside a table cell or a column: both are the note's.
@MainActor
@Suite(.serialized) struct NoteCommandsTests {
    private static func blocks() -> [EditorBlock] {
        var table = TableContent.blank(rows: 2, columns: 2)
        table.setRuns([InlineRun("Cellwombat")], row: 0, column: 0)
        table.setRuns([InlineRun("B")], row: 0, column: 1)
        var blocks = [EditorBlock(id: "h", kind: .heading1, runs: [InlineRun("Plans")]),
                      EditorBlock(id: "t", kind: .table, table: table),
                      EditorBlock(id: "k", kind: .columns, columns: [
                          EditorColumn(id: "k1", blocks: [EditorBlock(id: "k1p", runs: [InlineRun("Leftcolumnword")])]),
                          EditorColumn(id: "k2", blocks: [EditorBlock(id: "k2p", runs: [InlineRun("Rightcolumnword")])]),
                      ])]
        // Long enough for several pages.
        for index in 0..<160 {
            blocks.append(EditorBlock(id: "p\(index)", runs: [InlineRun("Paragraph number \(index) of the trip notes")]))
        }
        blocks.append(EditorBlock(id: "end", runs: [InlineRun("Finalsentinel")]))
        return blocks
    }

    private static func harness() -> UndoHarness {
        let harness = UndoHarness(blocks())
        harness.controller.noteReference = { (id: "doc-1", title: "Kyoto trip") }
        harness.layout()
        return harness
    }

    /// The operation's pages as a PDF, and its text.
    private static func printedText(_ operation: NSPrintOperation) throws -> (pages: Int, text: String) {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("print-\(UUID().uuidString).pdf")
        defer { try? FileManager.default.removeItem(at: url) }
        let info = operation.printInfo
        info.jobDisposition = .save
        info.dictionary()[NSPrintInfo.AttributeKey.jobSavingURL] = url
        operation.showsPrintPanel = false
        operation.showsProgressPanel = false
        #expect(operation.run())
        let document = try #require(PDFDocument(url: url))
        return (document.pageCount, document.string ?? "")
    }

    @Test func printFromATableCellPrintsTheWholeNote() throws {
        let harness = Self.harness()
        let grid = try #require(harness.live(TableBlockController.self))
        grid.focus(row: 0, column: 0, atEnd: true)
        settleRunLoop()
        #expect(harness.window.firstResponder is CellTextView)
        var printed: NSPrintOperation?
        harness.controller.runPrintOperation = { printed = $0 }
        harness.send("print:")
        let operation = try #require(printed, "Print in a cell prints the note")
        #expect(operation.view !== harness.view, "a copy, not the editor on screen")
        let (pages, text) = try Self.printedText(operation)
        #expect(pages > 1)
        for word in ["Kyoto trip", "Plans", "Cellwombat", "Leftcolumnword", "Rightcolumnword", "Paragraph number 80", "Finalsentinel"] {
            #expect(text.contains(word), "printed: \(word)")
        }
        // The note on screen is as it was.
        #expect(harness.window.firstResponder is CellTextView)
    }

    @Test func printFromAColumnPrintsTheWholeNote() throws {
        let harness = Self.harness()
        let columns = try #require(harness.live(ColumnsBlockController.self))
        let left = columns.editors[0]
        harness.window.makeFirstResponder(left.textView)
        left.textView.setSelectedRange(NSRange(location: 2, length: 0))
        settleRunLoop()
        var printed: NSPrintOperation?
        harness.controller.runPrintOperation = { printed = $0 }
        harness.send("print:")
        let operation = try #require(printed)
        let (_, text) = try Self.printedText(operation)
        #expect(text.contains("Plans") && text.contains("Finalsentinel"))
    }

    @Test func findInATableCellUsesTheNotesFindBar() throws {
        let harness = Self.harness()
        let grid = try #require(harness.live(TableBlockController.self))
        grid.focus(row: 0, column: 0, atEnd: true)
        settleRunLoop()
        let cell = try #require(harness.window.firstResponder as? CellTextView)
        let find = NSMenuItem(title: "Find…", action: #selector(NSTextView.performTextFinderAction(_:)), keyEquivalent: "f")
        find.tag = NSTextFinder.Action.showFindInterface.rawValue
        #expect(cell.validateMenuItem(find))
        cell.performTextFinderAction(find)
        settleRunLoop(0.2)
        #expect(harness.window.firstResponder !== cell, "the cell let go")
        #expect(harness.view.enclosingScrollView?.isFindBarVisible == true, "the note's find bar shows")
        // From where the table is: the next match after it, not from the top.
        let tableAt = try #require(harness.controller.range(ofBlock: "t")).location
        #expect(abs(harness.view.selectedRange().location - tableAt) <= 1)
    }

    @Test func findInAColumnUsesTheNotesFindBar() throws {
        let harness = Self.harness()
        let columns = try #require(harness.live(ColumnsBlockController.self))
        let left = columns.editors[0].textView
        harness.window.makeFirstResponder(left)
        settleRunLoop()
        let find = NSMenuItem(title: "Find…", action: #selector(NSTextView.performTextFinderAction(_:)), keyEquivalent: "f")
        find.tag = NSTextFinder.Action.showFindInterface.rawValue
        left.performTextFinderAction(find)
        settleRunLoop(0.2)
        #expect(harness.window.firstResponder !== left)
        #expect(harness.view.enclosingScrollView?.isFindBarVisible == true)
    }

    @Test func useSelectionForFindInACellSharesTheFindString() throws {
        let harness = Self.harness()
        let grid = try #require(harness.live(TableBlockController.self))
        grid.focus(row: 0, column: 0, atEnd: true)
        settleRunLoop()
        let cell = try #require(harness.window.firstResponder as? CellTextView)
        cell.setSelectedRange(NSRange(location: 0, length: 4))
        let item = NSMenuItem(title: "Use Selection for Find", action: #selector(NSTextView.performTextFinderAction(_:)), keyEquivalent: "e")
        item.tag = NSTextFinder.Action.setSearchString.rawValue
        cell.performTextFinderAction(item)
        #expect(NSPasteboard(name: .find).string(forType: .string) == "Cell")
        #expect(harness.window.firstResponder === cell, "⌘E keeps the cell")
    }
}
