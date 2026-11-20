import AppKit
import SwiftUI
import Testing

@testable import FortressKit

// Editable tables and columns in the real text view, plus image/file/equation rendering,
// and gallery scenes of an imported OneNote page.

enum RichPage {
    static func run(_ text: String, _ marks: [String: JSONValue] = [:]) -> JSONValue {
        var object: [String: JSONValue] = ["type": .string("text"), "text": .string(text)]
        if !marks.isEmpty { object["marks"] = .object(marks) }
        return .object(object)
    }

    static func node(_ id: String, _ type: String, attrs: [String: JSONValue] = [:], content: [JSONValue]? = nil,
                     payload: JSONValue? = nil, text: String = "", children: [BlockNode] = []) -> BlockNode {
        BlockNode(id: id, documentId: "01M7ONENOTEPAGE00000000000", type: type, attrs: attrs,
                  content: content.map(JSONValue.array) ?? .null, payload: payload, text: text,
                  createdAt: "2024-03-11T09:00:00+00:00", updatedAt: "2026-11-16T09:00:00+00:00", children: children)
    }

    static func paragraph(_ id: String, _ runs: [JSONValue], type: String = "paragraph", attrs: [String: JSONValue] = [:]) -> BlockNode {
        node(id, type, attrs: attrs, content: runs, text: runs.map { BlockCodec.inlineText($0) }.joined())
    }

    /// A cell: its text in a paragraph, then any other blocks it holds.
    static func cell(_ id: String, _ text: String, _ attrs: [String: JSONValue] = [:], marks: [String: JSONValue] = [:],
                     more: [BlockNode] = []) -> BlockNode {
        node(id, "table_cell", attrs: attrs, children: [paragraph(id + "P", [run(text, marks)])] + more)
    }

    static let header: [String: JSONValue] = ["background": .string("#DEEAF6")]
    static let right: [String: JSONValue] = ["align": .string("right")]

    /// The id of the paragraph holding a cell's text.
    static func cellParagraph(row: Int, column: Int) -> String { "01M7ONENOTECELL000000\(row)\(column)P" }

    static var table: BlockNode {
        func id(_ row: Int, _ column: Int) -> String { "01M7ONENOTECELL000000\(row)\(column)" }
        return node("01M7ONENOTEBLOCK0000000002", "table", attrs: [
            "borders": .bool(true), "column_widths": .array([.number(150), .null, .number(110)]), "header_rows": .number(1),
        ], children: [
            node("01M7ONENOTEROW00000000000", "table_row", children: [
                cell(id(0, 0), "Sample", header), cell(id(0, 1), "Observation", header), cell(id(0, 2), "pH", right.merging(header) { $1 }),
            ]),
            node("01M7ONENOTEROW00000000001", "table_row", children: [
                cell(id(1, 0), "A · HCl 0.1 M"), cell(id(1, 1), "Turned pink at 24.6 mL"), cell(id(1, 2), "8.2", right),
            ]),
            node("01M7ONENOTEROW00000000002", "table_row", children: [
                cell(id(2, 0), "B · CH₃COOH"),
                cell(id(2, 1), "Faint pink, fades", marks: ["italic": .bool(true)],
                     more: [paragraph("01M7ONENOTENESTED00000001", [run("repeat with fresh indicator")], type: "bullet_item")]),
                cell(id(2, 2), "7.9", right.merging(["color": .string("#C00000")]) { $1 }),
            ]),
        ])
    }

    static func document(image: URL) -> BlockDocument {
        let blocks: [BlockNode] = [
            paragraph("01M7ONENOTEBLOCK0000000001", [run("Titrated three samples against 0.1 M NaOH. Water is H"), run("2", ["sub": .bool(true)]),
                                                      run("O; the fit is y = ax"), run("2", ["sup": .bool(true)]), run(" + b.")]),
            table,
            paragraph("01M7ONENOTEBLOCK0000000003", [run("Setup")], type: "heading", attrs: ["level": .number(2)]),
            node("01M7ONENOTEBLOCK0000000004", "columns", attrs: ["onenote_y": .number(3)], children: [
                node("01M7ONENOTEBLOCK0000000005", "column", attrs: ["width": .number(0.45)], children: [
                    paragraph("01M7ONENOTEBLOCK0000000006", [run("Burette rinsed twice; "), run("phenolphthalein", ["bold": .bool(true)]), run(" as indicator.")]),
                    paragraph("01M7ONENOTEBLOCK0000000007", [run("Swirl continuously")], type: "bullet_item"),
                    paragraph("01M7ONENOTEBLOCK0000000008", [run("Read at eye level")], type: "bullet_item"),
                ]),
                node("01M7ONENOTEBLOCK0000000009", "column", attrs: ["width": .number(0.55)], children: [
                    node("01M7ONENOTEBLOCK0000000010", "image", attrs: [
                        "url": .string(image.absoluteString), "alt": .string("Whiteboard: burette readings 24.6, 24.4, 24.7"),
                        "caption": .string("Readings on the lab whiteboard"), "width": .number(640), "height": .number(427)]),
                ]),
            ]),
            // An equation, imported as its LaTeX (Fortress has no math blocks).
            paragraph("01M7ONENOTEBLOCK0000000011", [run(#"c_{acid} = \frac{c_{base} \cdot V_{base}}{V_{acid}} \approx 0.098"#)],
                      type: "code", attrs: ["language": .string("latex")]),
            node("01M7ONENOTEBLOCK0000000012", "file", attrs: ["url": .string("/media/titration-printout.pdf"), "name": .string("Titration printout.pdf"),
                                                              "size": .number(482_133), "mime": .string("application/pdf")], text: "Titration printout.pdf"),
        ]
        return BlockDocument(id: "01M7ONENOTEPAGE00000000000", revision: 4, titleAuto: false,
                             title: "Titration — week 12", createdAt: "2024-03-11T09:00:00+00:00",
                             updatedAt: "2026-11-16T09:00:00+00:00", children: blocks)
    }

    @MainActor static func load() throws -> BlockDocument {
        let image = try #require(Bundle.module.url(forResource: "kyoto.jpg", withExtension: nil, subdirectory: "Fixtures"))
        return document(image: image)
    }
}

@MainActor
private func editor(_ document: BlockDocument, width: CGFloat = 900) -> (BlockEditorController, NSWindow) {
    let controller = BlockEditorController(baseURL: URL(string: "http://127.0.0.1:8000")!)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: 700), styleMask: [.titled], backing: .buffered, defer: false)
    window.contentView = controller.scrollView
    controller.scrollView.frame = window.contentView!.bounds
    controller.load(BlockCodec.flatten(document.children))
    controller.textView.layoutSubtreeIfNeeded()
    return (controller, window)
}

@MainActor
private func live<T>(_ controller: BlockEditorController, _ type: T.Type) -> T? {
    var found: T?
    let storage = controller.storage
    storage.enumerateAttribute(.attachment, in: NSRange(location: 0, length: storage.length), options: []) { value, _, stop in
        if let live = (value as? BlockAttachment)?.live as? T { found = live; stop.pointee = true }
    }
    return found
}

@MainActor
@Suite(.serialized) struct EditorRichBlocksTests {
    @Test func untouchedPageSavesNothing() throws {
        let document = try RichPage.load()
        let (controller, _) = editor(document)
        // Read the text back, not the blocks kept from loading.
        controller.discardReadCache()
        let blocks = controller.currentBlocks()
        #expect(blocks.map(\.kind) == [.paragraph, .table, .heading2, .columns, .code, .file])
        #expect(blocks[3].columns?.map { $0.blocks.count } == [3, 1])
        let (operations, _) = BlockDiff.operations(from: SyncedTree(BlockCodec.flatten(document.children)), to: blocks)
        #expect(operations.isEmpty, "unexpected: \(operations.map { "\($0.operation) \($0.blockId ?? "")" })")
        #expect(blocks[0].runs.contains { $0.marks.sub })
    }

    @Test func typingInACellUpdatesTheTable() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        let base = SyncedTree(controller.currentBlocks())
        let table = try #require(live(controller, TableBlockController.self))
        #expect(table.table.rows.count == 3)
        let cell = try #require(table.editor(row: 1, column: 1))
        window.makeFirstResponder(cell)
        cell.setSelectedRange(NSRange(location: cell.textStorage!.length, length: 0))
        cell.insertText(" (pale)", replacementRange: cell.selectedRange())
        let (operations, _) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        // One update of the cell's paragraph; nothing else (the nested list stays).
        #expect(operations.count == 1)
        #expect(operations.first?.blockId == RichPage.cellParagraph(row: 1, column: 1))
        #expect(operations.first?.plainText == "Turned pink at 24.6 mL (pale)")
    }

    @Test func tabMovesBetweenCellsAndAddsARowAtTheEnd() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        let table = try #require(live(controller, TableBlockController.self))
        table.focus(row: 0, column: 0, atEnd: false)
        try #require(table.editor).doCommand(by: #selector(NSResponder.insertTab(_:)))
        #expect(table.focusedCell?.row == 0 && table.focusedCell?.column == 1)
        #expect(window.firstResponder === table.editor)
        // One cell editor at a time: the others are drawn.
        #expect(table.grid.subviews.filter { $0 is CellTextView }.count == 1)
        table.focus(row: 2, column: 2, atEnd: true)
        try #require(table.editor).doCommand(by: #selector(NSResponder.insertTab(_:)))
        #expect(table.table.rows.count == 4)
        #expect(table.focusedCell?.row == 3 && table.focusedCell?.column == 0)
        #expect(window.firstResponder === table.editor)
        // ⇧Tab from the first cell leaves the table.
        table.focus(row: 0, column: 0, atEnd: false)
        try #require(table.editor).doCommand(by: #selector(NSResponder.insertBacktab(_:)))
        #expect(window.firstResponder === controller.textView)
    }

    @Test func rowsAndColumnsAreAddedAndUndone() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        defer { withExtendedLifetime(window) {} }  // The window owns the undo manager.
        let base = SyncedTree(controller.currentBlocks())
        let table = try #require(live(controller, TableBlockController.self))
        table.perform("Insert Column") { $0.insertColumn(at: 1); return true }
        #expect(table.table.rows[0].cells.count == 4)
        let (operations, _) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        // The widths, and a new (empty) cell in each row; nothing deleted or rewritten.
        #expect(operations.map(\.operation) == [.update, .insert, .insert, .insert])
        #expect(operations.dropFirst().allSatisfy { $0.type == "table_cell" })
        #expect(operations[1].afterId == "01M7ONENOTECELL00000000")
        controller.textView.undoManager?.undo()  // The note's undo stack.
        #expect(table.table.columnCount == 3)
    }

    @Test func boldInACell() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        let table = try #require(live(controller, TableBlockController.self))
        let cell = try #require(table.editor(row: 1, column: 0))
        window.makeFirstResponder(cell)
        cell.setSelectedRange(NSRange(location: 0, length: 1))
        table.toggleMark(\.bold, in: cell)
        let blocks = controller.currentBlocks()
        #expect(blocks[1].table?.cell(row: 1, column: 0)?.runs.first == InlineRun("A", InlineMarks(bold: true)))
    }

    @Test func editingInAColumnUpdatesThatBlock() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        let base = SyncedTree(controller.currentBlocks())
        let columns = try #require(live(controller, ColumnsBlockController.self))
        #expect(columns.editors.count == 2)
        let left = columns.editors[0]
        window.makeFirstResponder(left.textView)
        // End of the first bullet, then Return makes a new bullet in the column.
        let bullet = try #require(left.range(ofBlock: "01M7ONENOTEBLOCK0000000007"))
        left.textView.setSelectedRange(NSRange(location: NSMaxRange(bullet) - 1, length: 0))
        left.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
        left.textView.insertText("Note the volume", replacementRange: left.textView.selectedRange())
        let (operations, result) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        #expect(operations.map(\.operation) == [.insert])
        #expect(operations.first?.afterId == "01M7ONENOTEBLOCK0000000007")
        #expect(operations.first?.type == "bullet_item")
        #expect(operations.first?.plainText == "Note the volume")
        #expect(result.parent[operations.first?.blockId ?? ""] == .some("01M7ONENOTEBLOCK0000000005"))
    }

    @Test func tableRowsFitTheirText() throws {
        let document = try RichPage.load()
        let (controller, _) = editor(document)
        let table = try #require(live(controller, TableBlockController.self))
        let height = table.height(forWidth: 640)
        // The height measured from the string is what the cell's editor lays out.
        let cell = try #require(table.editor(row: 1, column: 1))
        let frame = table.textFrame(row: 1, column: 1)
        let fitting = cell.fittingTextHeight(width: frame.width)
        let info = "rows \(table.rowEdges) columns \(table.columnEdges) text frame \(frame) fitting \(fitting)"
        #expect(abs(fitting - frame.height) < 1, "\(info)")
        #expect(cell.frame == frame, "\(info)")
        #expect(height < 200, "\(info)")
        #expect(table.rowEdges.count == 4)
        #expect(table.rowEdges[1] < 50, "\(info)")
    }

    @Test func columnsLayOutSideBySide() throws {
        let document = try RichPage.load()
        let (controller, _) = editor(document)
        let columns = try #require(live(controller, ColumnsBlockController.self))
        let height = columns.height(forWidth: 640)
        #expect(height > 60)
        #expect(columns.frames.count == 2)
        #expect(columns.frames[0].maxX < columns.frames[1].minX)
        #expect(abs(columns.frames[0].width / columns.frames[1].width - 0.45 / 0.55) < 0.05)
    }

    @Test func insertTableMakesANewBlock() throws {
        let (controller, _) = editor(BlockDocument(id: "d", revision: 1, titleAuto: true, title: "",
                                                   createdAt: "", updatedAt: "", children: [RichPage.paragraph("p", [RichPage.run("Hello")])]))
        let base = SyncedTree(controller.currentBlocks())
        controller.textView.setSelectedRange(NSRange(location: 2, length: 0))
        controller.insertTable()
        let (operations, _) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        // The table, its three rows and nine (empty) cells, in that order.
        #expect(operations.count == 13)
        #expect(operations.allSatisfy { $0.operation == .insert })
        #expect(operations.first?.type == "table")
        #expect(operations.first?.attrs?["header_rows"] == .number(1))
        #expect(operations.filter { $0.type == "table_cell" }.count == 9)
    }

    // MARK: Gallery

    @Test func gallery() throws {
        let document = try RichPage.load()
        let model = AppModel()
        model.connectsOnAppear = false
        model.navigation = NavigationResponse(items: [.folder(NavigationFolder(
            id: "lab", parentId: nil, name: "Lab", position: 0,
            children: [.document(NavigationDocument(id: document.id, title: document.title,
                                                    updatedAt: document.updatedAt, createdAt: document.createdAt,
                                                    snippet: "", folderId: "lab", position: 0))]))], recent: [])
        for (name, appearance) in [("editor-onenote-light", NSAppearance.Name.aqua), ("editor-onenote-dark", .darkAqua)] {
            try snapshot(NoteEditorView(session: EditorSession(document: document)).environment(model), named: name, appearance: appearance)
        }
        // Scrolled down to the columns, equation and file.
        for (name, appearance) in [("editor-onenote-lower-light", NSAppearance.Name.aqua), ("editor-onenote-lower-dark", .darkAqua)] {
            try snapshot(NoteEditorView(session: EditorSession(document: document), focusBlockId: "01M7ONENOTEBLOCK0000000011").environment(model),
                         named: name, appearance: appearance)
        }
    }
}
