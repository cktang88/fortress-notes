import AppKit
import SwiftUI
import Testing

@testable import FortressKit

// The note's undo stack as a person uses it (UNDO_AUDIT.md): edits made with keys,
// Paste and menu commands, then ⌘Z and ⇧⌘Z through Edit ▸ Undo and Redo (the action
// goes up the responder chain from the window's first responder, as the menu sends it),
// checking the text, the blocks (ids included) and the menu's titles at every step.

@MainActor private var undoWindows: [NSWindow] = []

@MainActor
func settleRunLoop(_ seconds: Double = 0.05) {
    RunLoop.main.run(until: Date().addingTimeInterval(seconds))
}

/// A note in a window, driven like a person would.
@MainActor
final class UndoHarness {
    let controller: BlockEditorController
    let window: NSWindow

    init(_ blocks: [EditorBlock], controller: BlockEditorController? = nil) {
        let controller = controller ?? BlockEditorController(baseURL: URL(string: "http://127.0.0.1:9")!)
        self.controller = controller
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 700), styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = controller.scrollView
        controller.scrollView.frame = window.contentView!.bounds
        if !blocks.isEmpty { controller.load(blocks) }
        window.makeFirstResponder(controller.textView)
        controller.textView.layoutSubtreeIfNeeded()
        controller.textView.textLayoutManager?.textViewportLayoutController.layoutViewport()
        undoWindows.append(window)
    }

    var view: FortressTextView { controller.textView }
    /// The text view with keyboard focus (the note's, a table cell's, a column's).
    var focused: NSTextView { (window.firstResponder as? NSTextView) ?? view }

    /// Types as the keyboard does (at the selection, no replacement range).
    func type(_ text: String) {
        let target = focused
        for character in text {
            let before = target.selectedRange()
            target.insertText(String(character), replacementRange: NSRange(location: NSNotFound, length: 0))
            let after = target.selectedRange()
            if after.location != before.location + (String(character) as NSString).length {
                print("HARNESS caret \(before) → \(after) typing '\(character)' in \(Swift.type(of: target)): \(target.string.debugDescription)")
            }
        }
        settleRunLoop()
    }

    /// Lays the note out, so tables and columns are in the window.
    func layout() {
        view.layoutSubtreeIfNeeded()
        view.textLayoutManager?.textViewportLayoutController.layoutViewport()
        settleRunLoop()
    }

    func command(_ selector: Selector) {
        focused.doCommand(by: selector)
        settleRunLoop()
    }

    func paste(_ string: String, plain: Bool = false) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(string, forType: .string)
        if plain { focused.pasteAsPlainText(nil) } else { focused.paste(nil) }
        settleRunLoop()
    }

    func select(_ range: NSRange) {
        window.makeFirstResponder(view)
        view.setSelectedRange(range)
        settleRunLoop()
    }

    /// A menu command, sent as the menu sends it (to the first responder that takes it).
    func send(_ action: String) {
        let item = NSMenuItem(title: "", action: NSSelectorFromString(action), keyEquivalent: "")
        _ = (window.firstResponder ?? window).tryToPerform(NSSelectorFromString(action), with: item)
        settleRunLoop()
    }

    /// The menu item as it would show: its title and whether it is enabled.
    func menuItem(_ action: String) -> (title: String, enabled: Bool) {
        let item = NSMenuItem(title: "", action: NSSelectorFromString(action), keyEquivalent: "")
        var responder: NSResponder? = window.firstResponder ?? window
        while let current = responder {
            if current.responds(to: NSSelectorFromString(action)) {
                let enabled = (current as NSObject as? NSMenuItemValidation)?.validateMenuItem(item) ?? true
                return (item.title, enabled)
            }
            responder = current.nextResponder
        }
        return ("", false)
    }

    func undo() { send("undo:") }
    func redo() { send("redo:") }

    /// The first table or columns block's live controller.
    func live<T>(_ type: T.Type) -> T? {
        var found: T?
        let storage = controller.storage
        storage.enumerateAttribute(.attachment, in: NSRange(location: 0, length: storage.length), options: []) { value, _, stop in
            if let live = (value as? BlockAttachment)?.live as? T { found = live; stop.pointee = true }
        }
        return found
    }

    /// The note as blocks: id, kind, depth, checkbox, text (bold as **…**), table cells.
    func snapshot() -> [String] {
        controller.currentBlocks().map(Self.describe)
    }

    static func describe(_ block: EditorBlock) -> String {
        var line = "\(block.id) \(block.kind.rawValue) d\(block.depth)"
        if block.kind == .check { line += block.checked ? " [x]" : " [ ]" }
        if let table = block.table {
            line += " table " + table.rows.map { row in row.cells.map { "\($0.id)=\($0.plainText)" }.joined(separator: ",") }.joined(separator: " / ")
        }
        if let columns = block.columns {
            line += " columns " + columns.map { $0.blocks.map(describe).joined(separator: "; ") }.joined(separator: " || ")
        }
        line += " " + block.runs.map { run in run.marks.bold ? "**\(run.text)**" : run.text }.joined()
        return line
    }
}

/// Edits recorded with the state after each and its expected undo name; then walked back
/// with Edit ▸ Undo to the start and forward with Edit ▸ Redo to the end.
@MainActor
struct UndoScript {
    let harness: UndoHarness
    var states: [(name: String, blocks: [String])]

    init(_ harness: UndoHarness) {
        self.harness = harness
        states = [("", harness.snapshot())]
    }

    mutating func step(_ name: String, _ body: () -> Void) {
        body()
        states.append((name, harness.snapshot()))
    }

    func walkBackAndForth(sourceLocation: SourceLocation = #_sourceLocation) {
        for index in stride(from: states.count - 1, to: 0, by: -1) {
            let item = harness.menuItem("undo:")
            #expect(item.enabled, "step \(index) can be undone", sourceLocation: sourceLocation)
            #expect(item.title == "Undo \(states[index].name)", "step \(index)", sourceLocation: sourceLocation)
            harness.undo()
            #expect(harness.snapshot() == states[index - 1].blocks, "after undoing step \(index) (\(states[index].name))",
                    sourceLocation: sourceLocation)
        }
        #expect(!harness.menuItem("undo:").enabled, "back at the start", sourceLocation: sourceLocation)
        for index in 1..<states.count {
            let item = harness.menuItem("redo:")
            #expect(item.enabled, "step \(index) can be redone", sourceLocation: sourceLocation)
            #expect(item.title == "Redo \(states[index].name)", "redo step \(index)", sourceLocation: sourceLocation)
            harness.redo()
            #expect(harness.snapshot() == states[index].blocks, "after redoing step \(index) (\(states[index].name))",
                    sourceLocation: sourceLocation)
        }
        #expect(!harness.menuItem("redo:").enabled, "at the end again", sourceLocation: sourceLocation)
    }
}

@MainActor
@Suite(.serialized) struct UndoStackTests {
    /// The sequence from the request: type, paste ×3 (one of them several paragraphs),
    /// Return, a list by Markdown shortcut, a table with a cell typed in and a row added,
    /// bold, a block moved, a checklist checked, an indent. Every step undoes one at a
    /// time back to the original note and redoes to the end, named in the menu.
    @Test func everyEditUndoesAndRedoesOneStepAtATime() throws {
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun("Hello")])])
        let start = harness.controller.currentBlocks()
        harness.select(NSRange(location: 5, length: 0))
        var script = UndoScript(harness)
        script.step("Typing") { harness.type(" world") }
        script.step("Paste") { harness.paste(" one") }
        script.step("Paste") { harness.paste(" two") }
        script.step("Paste") { harness.paste(" three\nfour") }
        script.step("Typing") { harness.command(#selector(NSResponder.insertNewline(_:))) }
        // "- " is typed (one step), then becomes a list item (a step of its own).
        harness.type("- ")
        var typed = script.states[script.states.count - 1].blocks
        typed[typed.count - 1] += "- "
        script.states.append(("Typing", typed))
        script.states.append(("Bulleted List", harness.snapshot()))
        #expect(harness.controller.currentBlocks().last?.kind == .bullet)
        script.step("Typing") { harness.type("item") }
        script.step("Insert Table") { harness.send("fortressInsertTable:") }
        let table = try #require(harness.live(TableBlockController.self))
        #expect(harness.window.firstResponder is CellTextView, "the new table's first cell is being edited")
        if !(harness.window.firstResponder is CellTextView) {
            harness.layout()
            table.focus(row: 0, column: 0, atEnd: false)
            settleRunLoop()
        }
        script.step("Typing") { harness.type("cell") }
        script.step("Insert Row") { table.insertRowBelow(nil); settleRunLoop() }
        script.step("Bold") {
            harness.select(NSRange(location: 6, length: 5))  // "world"
            harness.send("fortressToggleBold:")
        }
        let item = try #require(harness.controller.range(ofBlock: harness.controller.currentBlocks()[2].id))
        script.step("Move") {
            harness.select(NSRange(location: item.location + 1, length: 0))
            harness.send("fortressMoveBlockUp:")
        }
        script.step("Checklist") { harness.send("fortressChecklist:") }
        script.step("Checkbox") { harness.send("fortressToggleCheckbox:") }
        script.step("Indent") { harness.command(#selector(NSResponder.insertTab(_:))) }

        let final = harness.controller.currentBlocks()
        #expect(final.map(\.kind) == [.paragraph, .check, .paragraph, .table])
        #expect(final[1].depth == 1 && final[1].checked)
        #expect(final[3].table?.rows.count == 4)
        #expect(final[3].table?.cell(row: 0, column: 0)?.plainText == "cell")
        // Distinct states: nothing was a no-op step.
        for index in 1..<script.states.count {
            #expect(script.states[index].blocks != script.states[index - 1].blocks, "step \(index) changed the note")
        }

        script.walkBackAndForth()
        // What is shown is what would be saved: redone to the end, the same blocks.
        #expect(harness.controller.currentBlocks() == final)
        // And undone to the start, nothing to save.
        while harness.view.undoManager?.canUndo == true { harness.undo() }
        #expect(BlockDiff.operations(from: SyncedTree(start), to: harness.controller.currentBlocks()).operations.isEmpty)
    }

    /// Typing joins one step until the caret moves, Return is pressed, or another command
    /// runs in between (as NSTextView joins it); the steps undo in order.
    @Test func typingStepsBreakAtCaretMovesAndReturn() {
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun("")])])
        var script = UndoScript(harness)
        script.step("Typing") { harness.type("abc def") }
        script.step("Typing") { harness.command(#selector(NSResponder.insertNewline(_:))) }
        script.step("Typing") { harness.type("ghi") }
        script.step("Typing") {
            harness.select(NSRange(location: 1, length: 0))
            harness.type("X")
        }
        script.step("Typing") {
            harness.select(NSRange(location: 4, length: 0))
            harness.command(#selector(NSResponder.deleteBackward(_:)))
        }
        script.walkBackAndForth()
    }

    /// Block structure edits from the keyboard: Backspace merging blocks, Delete at the
    /// end of a block, Return on an empty list item, and Markdown shortcuts.
    @Test func structuralKeysAreStepsOfTheirOwn() {
        let harness = UndoHarness([
            EditorBlock(id: "a", runs: [InlineRun("one")]), EditorBlock(id: "b", runs: [InlineRun("two")]),
            EditorBlock(id: "c", kind: .bullet, runs: [InlineRun("")]),
        ])
        var script = UndoScript(harness)
        script.step("Typing") {
            harness.select(NSRange(location: 4, length: 0))
            harness.command(#selector(NSResponder.deleteBackward(_:)))
        }
        script.step("Typing") {
            harness.select(NSRange(location: 6, length: 0))
            harness.command(#selector(NSResponder.deleteForward(_:)))
        }
        script.step("Typing") { harness.command(#selector(NSResponder.insertNewline(_:))) }
        harness.select(NSRange(location: 0, length: 0))
        harness.type("# ")
        var typedState = script.states[script.states.count - 1].blocks
        typedState[0] = typedState[0].replacingOccurrences(of: "paragraph d0 ", with: "paragraph d0 # ")
        script.states.append(("Typing", typedState))
        script.states.append(("Heading 1", harness.snapshot()))
        #expect(harness.controller.currentBlocks()[0].kind == .heading1)
        script.walkBackAndForth()
    }

    /// Pasting text with Windows line ends (Word, Outlook): one step, undone exactly and
    /// redone without an extra empty paragraph.
    @Test func pasteWithWindowsLineEndsUndoesAndRedoesExactly() {
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun("XY")])])
        harness.select(NSRange(location: 1, length: 0))
        var script = UndoScript(harness)
        script.step("Paste") { harness.paste("a\r\nb\r\nc") }
        #expect(harness.controller.currentBlocks().map(\.plainText) == ["Xa", "b", "cY"])
        script.step("Paste") { harness.paste("p\u{2029}q", plain: true) }
        #expect(harness.controller.currentBlocks().map(\.plainText) == ["Xa", "b", "cp", "qY"])
        script.walkBackAndForth()
        #expect(harness.controller.storage.string == "Xa\nb\ncp\nqY\n")
    }

    /// Paste and Match Style, and Cut: named as the menu names them.
    @Test func pasteAndMatchStyleAndCut() {
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun("Hello world")])])
        harness.select(NSRange(location: 5, length: 0))
        harness.paste(" big", plain: true)
        let pasteName = harness.view.undoManager?.undoActionName ?? ""
        #expect(["Paste", "Paste and Match Style"].contains(pasteName), "\(pasteName)")
        harness.select(NSRange(location: 0, length: 6))
        harness.send("cut:")
        #expect(harness.controller.storage.string == "big world\n")
        #expect(harness.menuItem("undo:").title == "Undo Cut")
        harness.undo()
        #expect(harness.controller.storage.string == "Hello big world\n")
        harness.undo()
        #expect(harness.controller.storage.string == "Hello world\n")
    }

    /// Typing in a table's cells, a shape change in between, more typing: each undone in
    /// order (the cells' edits used to be lost once a row was added).
    @Test func cellEditsStayOnTheNoteStackAcrossShapeChanges() throws {
        let harness = UndoHarness([EditorBlock(id: "t", kind: .table, table: .blank(rows: 2, columns: 2)),
                                   EditorBlock(id: "p", runs: [InlineRun("after")])])
        let table = try #require(harness.live(TableBlockController.self))
        var script = UndoScript(harness)
        table.focus(row: 0, column: 0, atEnd: true)
        settleRunLoop()
        script.step("Typing") { harness.type("first") }
        script.step("Insert Column") { table.insertColumnRight(nil); settleRunLoop() }
        script.step("Typing") {
            table.focus(row: 1, column: 2, atEnd: true)
            settleRunLoop()
            harness.type("second")
        }
        script.step("Bold") {
            harness.focused.setSelectedRange(NSRange(location: 0, length: 3))
            harness.send("fortressToggleBold:")
        }
        script.step("Typing") {
            harness.focused.setSelectedRange(NSRange(location: 6, length: 0))
            harness.command(#selector(NSResponder.deleteBackward(_:)))
            harness.command(#selector(NSResponder.deleteBackward(_:)))
        }
        // Typing in the note's text, then back in a cell.
        script.step("Typing") {
            harness.select(NSRange(location: harness.controller.storage.length - 1, length: 0))
            harness.type(" text")
        }
        script.step("Typing") {
            table.focus(row: 0, column: 1, atEnd: true)
            settleRunLoop()
            harness.type("third")
        }
        script.walkBackAndForth()
    }

    /// Typing in a column, in the note, and in the column again: one stack, in order. A
    /// column's editor coming into being (scrolled to, entered) never clears it.
    @Test func columnEditsShareTheNoteStack() throws {
        let document = try RichPage.load()
        let harness = UndoHarness(BlockCodec.flatten(document.children))
        let columns = try #require(harness.live(ColumnsBlockController.self))
        var script = UndoScript(harness)
        script.step("Typing") {
            harness.select(NSRange(location: 0, length: 0))
            harness.type("Start ")
        }
        let column = columns.editors[0]
        #expect(harness.view.undoManager?.canUndo == true, "building the columns kept the note's steps")
        #expect(column.textView.undoManager === harness.view.undoManager)
        script.step("Typing") {
            column.focusStart()
            settleRunLoop()
            harness.type("Column ")
        }
        script.step("Bulleted List") { harness.send("fortressBulletedList:") }
        script.step("Typing") {
            harness.select(NSRange(location: 6, length: 0))
            harness.type("more ")
        }
        // The column's view leaves the window and comes back (TextKit does this as it
        // lays out): its steps stay.
        column.textView.removeFromSuperview()
        columns.container.addSubview(column.textView)
        script.step("Typing") {
            columns.editors[1].focusStart()
            settleRunLoop()
            harness.window.makeFirstResponder(column.textView)
            column.textView.setSelectedRange(NSRange(location: 0, length: 0))
            harness.type("again ")
        }
        script.walkBackAndForth()
    }

    /// Showing the rest of a long note (the one append after opening) and saving add
    /// nothing to the stack and take nothing off it.
    @Test func loadingTheRestOfALongNoteKeepsTheStack() {
        let blocks = (0..<600).map { EditorBlock(id: "p\($0)", runs: [InlineRun("Paragraph \($0) with some words in it")]) }
        let controller = BlockEditorController(baseURL: URL(string: "http://127.0.0.1:9")!)
        controller.load(blocks)
        #expect(!controller.pendingBlocks.isEmpty, "a long note loads its first screens first")
        let harness = UndoHarness([], controller: controller)
        harness.select(NSRange(location: 3, length: 0))
        harness.type("XYZ")
        controller.finishLoading()
        settleRunLoop()
        #expect(controller.pendingBlocks.isEmpty)
        #expect(harness.menuItem("undo:").title == "Undo Typing")
        harness.undo()
        #expect(!(harness.view.undoManager?.canUndo ?? true), "loading added no step")
        #expect(controller.currentBlocks().map(\.plainText) == blocks.map(\.plainText))
        harness.redo()
        #expect(controller.currentBlocks()[0].plainText == "ParXYZagraph 0 with some words in it")
    }

    /// Undo of an inserted table while a cell has focus leaves focus in the note, so the
    /// next ⌘Z still undoes the note (not the window's list actions).
    @Test func undoingATableWhileInItsCellKeepsFocusInTheNote() throws {
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun("text")])])
        harness.select(NSRange(location: 4, length: 0))
        harness.type("!")
        harness.send("fortressInsertTable:")
        #expect(harness.window.firstResponder is CellTextView)
        harness.undo()
        #expect(harness.live(TableBlockController.self) == nil)
        #expect(harness.menuItem("undo:").title == "Undo Typing", "the next ⌘Z is the note's")
        harness.undo()
        #expect(harness.controller.storage.string == "text\n")
    }

    /// A pasted screenshot becomes a picture block once uploaded: one "Paste" step, and
    /// Backspace removing a divider is a step that brings the same block back.
    @Test func pastedPicturesAndDeletedAttachmentsAreSteps() async throws {
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun("one")]), EditorBlock(id: "d", kind: .divider),
                                   EditorBlock(id: "b", runs: [InlineRun("two")])])
        harness.controller.uploadFile = { _, name, _ in ("/media/shot.png", name) }
        var script = UndoScript(harness)
        script.step("Typing") {
            let two = harness.controller.range(ofBlock: "b")!
            harness.select(NSRange(location: two.location, length: 0))
            harness.command(#selector(NSResponder.deleteBackward(_:)))
        }
        #expect(!harness.controller.currentBlocks().contains { $0.kind == .divider })
        let image = NSImage(size: NSSize(width: 4, height: 4))
        image.lockFocus()
        NSColor.red.setFill()
        NSRect(x: 0, y: 0, width: 4, height: 4).fill()
        image.unlockFocus()
        let png = try #require(image.tiffRepresentation.flatMap { NSBitmapImageRep(data: $0)?.representation(using: .png, properties: [:]) })
        harness.select(NSRange(location: 1, length: 0))
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setData(png, forType: .png)
        harness.view.paste(nil)
        // The upload is a task on the main actor: let it run.
        for _ in 0..<100 where !harness.controller.currentBlocks().contains(where: { $0.kind == .image }) {
            try await Task.sleep(for: .milliseconds(20))
        }
        settleRunLoop()
        script.states.append(("Paste", harness.snapshot()))
        #expect(harness.controller.currentBlocks().map(\.kind) == [.paragraph, .image, .paragraph])
        script.walkBackAndForth()
    }

    /// Title and status changes are steps on the note's stack too.
    @Test func titleAndStatusChangesAreUndoable() {
        let node = BlockNode.paragraph("p1", "Body")
        let document = BlockDocument(id: "d", revision: 1, titleAuto: false, canUndo: false, canRedo: false, title: "Kyoto",
                                     status: .rough, createdAt: "2026-11-18T00:00:00+00:00", updatedAt: "2026-11-18T00:00:00+00:00",
                                     children: [node])
        let session = EditorSession(document: document)
        let undo = UndoManager()
        undo.groupsByEvent = false
        session.undoManager = undo
        undo.beginUndoGrouping()
        session.titleDidChange("Kyoto t")
        session.titleDidChange("Kyoto trip")
        undo.endUndoGrouping()
        #expect(undo.undoActionName == "Title Change")
        undo.beginUndoGrouping()
        session.changeStatus(.polished)
        undo.endUndoGrouping()
        #expect(undo.undoActionName == "Status Change")
        undo.undo()
        #expect(session.status == .rough)
        undo.undo()
        #expect(session.title == "Kyoto")
        undo.redo()
        #expect(session.title == "Kyoto trip")
        undo.redo()
        #expect(session.status == .polished)
    }

    /// Going to another note and back: the editor (and its undo stack) is the one left.
    @Test func eachNoteKeepsItsStackWhenTheWindowShowsAnotherNote() throws {
        func document(_ id: String, _ text: String) -> BlockDocument {
            BlockDocument(id: id, revision: 1, titleAuto: false, canUndo: false, canRedo: false, title: text,
                          status: .rough, createdAt: "2026-11-18T00:00:00+00:00", updatedAt: "2026-11-18T00:00:00+00:00",
                          children: [BlockNode.paragraph("\(id)-p", text, documentId: id)])
        }
        let model = AppModel(api: APIClient(baseURL: URL(string: "http://127.0.0.1:9")!))
        let first = EditorSession(document: document("A", "Alpha"), api: model.api)
        let second = EditorSession(document: document("B", "Beta"), api: model.api)
        func editor(_ session: EditorSession) -> AnyView {
            AnyView(BlockTextEditor(session: session, model: model, focusBlockId: nil).id(session.documentId).environment(model))
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 700), styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        undoWindows.append(window)
        let host = NSHostingView(rootView: editor(first))
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        settleRunLoop()
        let controller = try #require(model.noteEditors.controller(for: first))
        window.makeFirstResponder(controller.textView)
        controller.textView.setSelectedRange(NSRange(location: 5, length: 0))
        controller.textView.insertText(" one", replacementRange: controller.textView.selectedRange())
        settleRunLoop()
        controller.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
        settleRunLoop()

        host.rootView = editor(second)
        host.layoutSubtreeIfNeeded()
        settleRunLoop()
        #expect(controller.textView.window == nil, "the other note is shown")
        host.rootView = editor(first)
        host.layoutSubtreeIfNeeded()
        settleRunLoop()

        #expect(model.noteEditors.controller(for: first) === controller)
        #expect(controller.textView.window === window, "the same editor is shown again")
        #expect(controller.currentBlocks().map(\.plainText) == ["Alpha one", ""])
        let undo = try #require(controller.textView.undoManager)
        #expect(undo.undoActionName == "Typing")
        undo.undo()
        undo.undo()
        #expect(controller.currentBlocks().map(\.plainText) == ["Alpha"])
        // Its edits still reach its session (saving is wired to the editor shown).
        #expect(first.hasUnsavedChanges)
    }

    @Test func theCacheKeepsTheLatestNotesOnly() {
        let cache = NoteEditorCache()
        cache.limit = 2
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:9")!)
        let sessions = ["A", "B", "C"].map { id in
            EditorSession(document: BlockDocument(id: id, revision: 1, titleAuto: false, canUndo: false, canRedo: false, title: id,
                                                  status: .rough, createdAt: "2026-11-18T00:00:00+00:00",
                                                  updatedAt: "2026-11-18T00:00:00+00:00", children: []), api: api)
        }
        for session in sessions { cache.keep(session, BlockEditorController(baseURL: api.baseURL)) }
        #expect(cache.session(for: "A") == nil)
        #expect(cache.session(for: "B") === sessions[1])
        #expect(cache.controller(for: sessions[2]) != nil)
    }
}
