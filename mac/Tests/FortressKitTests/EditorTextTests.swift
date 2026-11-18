import AppKit
import SwiftUI
import Testing

@testable import FortressKit

private typealias BlockOp = FortressKit.BlockOperation

/// The rich fixture note, with its image pointing at the bundled picture.
@MainActor
func richDocument() throws -> BlockDocument {
    let url = try #require(Bundle.module.url(forResource: "editor-rich.json", withExtension: nil, subdirectory: "Fixtures"))
    let image = try #require(Bundle.module.url(forResource: "kyoto.jpg", withExtension: nil, subdirectory: "Fixtures"))
    let json = try String(contentsOf: url, encoding: .utf8).replacingOccurrences(of: "FIXTURE_IMAGE", with: image.absoluteString)
    return try JSONDecoder().decode(BlockDocument.self, from: Data(json.utf8))
}

@MainActor
private func controller(_ blocks: [EditorBlock]) -> BlockEditorController {
    let controller = BlockEditorController(baseURL: URL(string: "http://127.0.0.1:8000")!)
    controller.textView.frame = NSRect(x: 0, y: 0, width: 800, height: 600)
    controller.load(blocks)
    return controller
}

private func para(_ id: String, _ text: String, kind: BlockKind = .paragraph, depth: Int = 0) -> EditorBlock {
    EditorBlock(id: id, kind: kind, depth: depth, runs: [InlineRun(text)])
}

/// Spins the main run loop so deferred work (Markdown shortcuts) runs.
@MainActor
private func settle() {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
}

@MainActor
@Suite(.serialized) struct EditorTextTests {
    @Test func richNoteRoundTripsThroughTheText() throws {
        let document = try richDocument()
        let blocks = BlockCodec.flatten(document.children)
        let editor = controller(blocks)
        // Read the text back, not the blocks kept from loading.
        editor.discardReadCache()
        let read = editor.currentBlocks()
        #expect(read.map(\.id) == blocks.map(\.id))
        #expect(read.map(\.kind) == blocks.map(\.kind))
        #expect(read.map(\.depth) == blocks.map(\.depth))
        // Nothing was edited, so nothing would be saved.
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: read)
        #expect(operations.isEmpty, "unexpected: \(operations.map { "\($0.operation) \($0.blockId ?? "")" })")
    }

    @Test func returnSplitsIntoANewBlock() {
        let editor = controller([para("a", "Hello world")])
        editor.textView.setSelectedRange(NSRange(location: 5, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
        let blocks = editor.currentBlocks()
        #expect(blocks.map(\.plainText) == ["Hello", " world"])
        #expect(blocks[0].id == "a")
        #expect(blocks[1].id != "a")
        let (operations, _) = BlockDiff.operations(from: SyncedTree([para("a", "Hello world")]), to: blocks)
        #expect(operations.map(\.operation) == [BlockOperationKind.update, .insert])
    }

    @Test func returnInListContinuesAndEmptyItemExits() {
        let editor = controller([para("a", "milk", kind: .bullet)])
        editor.textView.setSelectedRange(NSRange(location: 4, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
        #expect(editor.currentBlocks().map(\.kind) == [.bullet, .bullet])
        editor.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
        #expect(editor.currentBlocks().map(\.kind) == [.bullet, .paragraph])
    }

    @Test func backspaceMergesAndKeepsUpperBlock() {
        let editor = controller([para("a", "One", kind: .heading2), para("b", "two")])
        editor.textView.setSelectedRange(NSRange(location: 4, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.deleteBackward(_:)))
        let blocks = editor.currentBlocks()
        #expect(blocks.count == 1)
        #expect(blocks[0].id == "a")
        #expect(blocks[0].kind == .heading2)
        #expect(blocks[0].plainText == "Onetwo")
    }

    @Test func backspaceAtListStartMakesAParagraph() {
        let editor = controller([para("a", "item", kind: .check)])
        editor.textView.setSelectedRange(NSRange(location: 0, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.deleteBackward(_:)))
        #expect(editor.currentBlocks()[0].kind == .paragraph)
        #expect(editor.currentBlocks()[0].id == "a")
    }

    @Test func markdownShortcutsConvertAndUndoBack() {
        for (typed, kind) in [("# ", BlockKind.heading1), ("## ", .heading2), ("- ", .bullet), ("1. ", .numbered), ("[ ] ", .check), ("> ", .quote)] {
            let editor = controller([para("a", "")])
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 600), styleMask: [.titled], backing: .buffered, defer: true)
            window.contentView = editor.scrollView
            window.makeFirstResponder(editor.textView)
            for character in typed {
                editor.textView.insertText(String(character), replacementRange: editor.textView.selectedRange())
                settle()
            }
            settle()
            let block = editor.currentBlocks()[0]
            #expect(block.kind == kind, "\(typed)")
            #expect(block.plainText == "", "\(typed)")
            editor.textView.undoManager?.undo()
            settle()
            #expect(editor.currentBlocks()[0].kind == .paragraph, "undo \(typed)")
            #expect(editor.currentBlocks()[0].plainText == typed, "undo \(typed)")
        }
    }

    @Test func noShortcutDuringComposition() {
        let editor = controller([para("a", "")])
        editor.textView.insertText("#", replacementRange: NSRange(location: 0, length: 0))
        editor.textView.setMarkedText(" ", selectedRange: NSRange(location: 1, length: 0), replacementRange: NSRange(location: 1, length: 0))
        settle()
        #expect(editor.textView.hasMarkedText())
        #expect(editor.currentBlocks()[0].kind == .paragraph)
    }

    @Test func boldTogglesOnSelection() {
        let editor = controller([para("a", "make this bold")])
        editor.textView.setSelectedRange(NSRange(location: 5, length: 4))
        editor.toggleMark(\.bold)
        let runs = editor.currentBlocks()[0].runs
        #expect(runs == [InlineRun("make "), InlineRun("this", InlineMarks(bold: true)), InlineRun(" bold")])
        editor.toggleMark(\.bold)
        #expect(editor.currentBlocks()[0].runs == [InlineRun("make this bold")])
    }

    @Test func tabIndentsUnderThePreviousBlock() {
        let editor = controller([para("a", "one", kind: .bullet), para("b", "two", kind: .bullet)])
        editor.textView.setSelectedRange(NSRange(location: 5, length: 0))
        editor.textView.doCommand(by: #selector(NSResponder.insertTab(_:)))
        #expect(editor.currentBlocks().map(\.depth) == [0, 1])
        let (operations, _) = BlockDiff.operations(from: SyncedTree([para("a", "one", kind: .bullet), para("b", "two", kind: .bullet)]), to: editor.currentBlocks())
        #expect(operations.first?.operation == .move)
        #expect(operations.first?.parentId == "a")
        // A second Tab can't go two levels deeper than the block above.
        editor.textView.doCommand(by: #selector(NSResponder.insertTab(_:)))
        #expect(editor.currentBlocks().map(\.depth) == [0, 1])
    }

    @Test func longNotesLoadQuickly() {
        let blocks = (0..<800).map { index in
            para("p\(index)", "Paragraph \(index) with enough words to wrap across a line or two in the editor column.", kind: index % 7 == 0 ? .bullet : .paragraph)
        }
        let start = Date()
        let editor = controller(blocks)
        // Read the text back, not the blocks kept from loading.
        editor.discardReadCache()
        let read = editor.currentBlocks()
        #expect(read.count == 800)
        #expect(Date().timeIntervalSince(start) < 3)
    }

    // MARK: Gallery

    @Test func galleryEditor() throws {
        let document = try richDocument()
        let model = AppModel()
        model.navigation = NavigationResponse(items: [.folder(NavigationFolder(
            id: "f", parentId: nil, name: "Travel", position: 0,
            children: [.document(NavigationDocument(id: document.id, title: document.title, status: .polished,
                                                    updatedAt: document.updatedAt, createdAt: document.createdAt,
                                                    snippet: "", folderId: "f", position: 0))]))], recent: [])
        for (name, appearance) in [("editor-light", NSAppearance.Name.aqua), ("editor-dark", .darkAqua)] {
            let session = EditorSession(document: document)
            try snapshot(NoteEditorView(session: session).environment(model), named: name, appearance: appearance)
        }
        // Scrolled to the code block: image, quote, code, divider, table, file chip.
        for (name, appearance) in [("editor-lower-light", NSAppearance.Name.aqua), ("editor-lower-dark", .darkAqua)] {
            let session = EditorSession(document: document)
            try snapshot(NoteEditorView(session: session, focusBlockId: "01M7RICH000000000000000014").environment(model),
                         named: name, appearance: appearance)
        }
        let narrow = EditorSession(document: document)
        try snapshot(NoteEditorView(session: narrow).environment(model), named: "editor-narrow-light",
                     size: CGSize(width: 620, height: 660))
    }
}
