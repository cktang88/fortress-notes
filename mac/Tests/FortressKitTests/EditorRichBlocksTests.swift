import AppKit
import SwiftUI
import Testing

@testable import FortressKit

// Editable tables and columns in the real text view, plus image/file/math rendering,
// and gallery scenes of an imported OneNote page.

enum RichPage {
    static func run(_ text: String, _ styles: [String: JSONValue] = [:]) -> JSONValue {
        .object(["type": .string("text"), "text": .string(text), "styles": .object(styles)])
    }

    static func node(_ id: String, _ type: String, attrs: [String: JSONValue] = [:], content: [String: JSONValue] = [:],
                     text: String = "", children: [BlockNode] = []) -> BlockNode {
        BlockNode(id: id, documentId: "01M7ONENOTEPAGE00000000000", parentId: nil, position: 0, type: type, attrs: attrs,
                  userAttrs: [:], content: .object(content), text: text, createdAt: "2024-03-11T09:00:00+00:00",
                  updatedAt: "2026-11-16T09:00:00+00:00", children: children,
                  provenance: .object(["source": .string("onenote_import"), "source_path": .string("Lab › Week 12 › Titration")]))
    }

    static func paragraph(_ id: String, _ runs: [JSONValue], type: String = "paragraph", attrs: [String: JSONValue] = [:]) -> BlockNode {
        node(id, type, attrs: attrs, content: ["blocknote": .array(runs)], text: runs.map(BlockCodec.inlineText).joined())
    }

    static func cell(_ text: String, _ props: [String: JSONValue] = [:], styles: [String: JSONValue] = [:], blocks: JSONValue? = nil) -> JSONValue {
        var object: [String: JSONValue] = ["type": .string("tableCell"), "props": .object(props), "content": .array([run(text, styles)])]
        if let blocks { object["blocks"] = blocks }
        return .object(object)
    }

    static let header: [String: JSONValue] = ["backgroundColor": .string("#DEEAF6")]

    static var table: JSONValue {
        .object([
            "type": .string("tableContent"), "columnWidths": .array([.number(150), .null, .number(110)]), "headerRows": .number(1),
            "rows": .array([
                .object(["cells": .array([cell("Sample", header), cell("Observation", header), cell("pH", header, ["textAlignment": .string("right")].merging(header) { $1 })])]),
                .object(["cells": .array([cell("A · HCl 0.1 M"), cell("Turned pink at 24.6 mL"), cell("8.2", ["textAlignment": .string("right")])])]),
                .object(["cells": .array([cell("B · CH₃COOH"), cell("Faint pink, fades", styles: ["italic": .bool(true)],
                    blocks: .array([.object(["type": .string("list"), "attrs": .object([:]), "content": .object(["blocknote": .array([run("repeat with fresh indicator")])])])])),
                    cell("7.9", ["textAlignment": .string("right"), "textColor": .string("#C00000")])])]),
            ]),
        ])
    }

    static func document(image: URL) -> BlockDocument {
        let blocks: [BlockNode] = [
            paragraph("01M7ONENOTEBLOCK0000000001", [run("Titrated three samples against 0.1 M NaOH. Water is H"), run("2", ["subscript": .bool(true)]),
                                                      run("O; the fit is y = ax"), run("2", ["superscript": .bool(true)]), run(" + b.")]),
            node("01M7ONENOTEBLOCK0000000002", "table", attrs: ["borders": .bool(true)], content: ["blocknote": table],
                 text: "Sample\tObservation\tpH"),
            paragraph("01M7ONENOTEBLOCK0000000003", [run("Setup", [:])], type: "heading", attrs: ["level": .number(2)]),
            node("01M7ONENOTEBLOCK0000000004", "columns", attrs: ["onenote_y": .number(3)], children: [
                node("01M7ONENOTEBLOCK0000000005", "column", attrs: ["width": .number(0.45)], children: [
                    paragraph("01M7ONENOTEBLOCK0000000006", [run("Burette rinsed twice; ", [:]), run("phenolphthalein", ["bold": .bool(true)]), run(" as indicator.")]),
                    paragraph("01M7ONENOTEBLOCK0000000007", [run("Swirl continuously")], type: "bulletListItem"),
                    paragraph("01M7ONENOTEBLOCK0000000008", [run("Read at eye level")], type: "bulletListItem"),
                ]),
                node("01M7ONENOTEBLOCK0000000009", "column", attrs: ["width": .number(0.55)], children: [
                    node("01M7ONENOTEBLOCK0000000010", "image", attrs: [
                        "url": .string(image.absoluteString), "alt": .string("Whiteboard: burette readings 24.6, 24.4, 24.7"),
                        "caption": .string("Readings on the lab whiteboard"), "width": .number(640), "height": .number(427)]),
                ]),
            ]),
            node("01M7ONENOTEBLOCK0000000011", "math", content: ["latex": .string(#"c_{acid} = \frac{c_{base} \cdot V_{base}}{V_{acid}} \approx 0.098"#)],
                 text: "c_acid"),
            node("01M7ONENOTEBLOCK0000000012", "file", attrs: ["url": .string("/media/titration-printout.pdf"), "name": .string("Titration printout.pdf"),
                                                              "size": .number(482_133), "mime": .string("application/pdf")], text: "Titration printout.pdf"),
        ]
        return BlockDocument(id: "01M7ONENOTEPAGE00000000000", revision: 4, titleAuto: false, canUndo: false, canRedo: false,
                             title: "Titration — week 12", status: .rough, createdAt: "2024-03-11T09:00:00+00:00",
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
        let blocks = controller.currentBlocks()
        #expect(blocks.map(\.kind) == [.paragraph, .table, .heading2, .columns, .math, .file])
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
        #expect(table.cells.count == 3)
        let cell = table.cells[1][1]
        window.makeFirstResponder(cell)
        cell.setSelectedRange(NSRange(location: cell.textStorage!.length, length: 0))
        cell.insertText(" (pale)", replacementRange: cell.selectedRange())
        let (operations, _) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        #expect(operations.count == 1)
        #expect(operations.first?.blockId == "01M7ONENOTEBLOCK0000000002")
        #expect(operations.first?.text?.contains("Turned pink at 24.6 mL (pale)") == true)
        guard case .array(let rows)? = operations.first?.content?["blocknote"]?["rows"], case .array(let cells)? = rows[2]["cells"] else {
            Issue.record("no rows"); return
        }
        // Same shape: the nested list stays on the server.
        #expect(cells[1]["blocks"] == nil)
    }

    @Test func tabMovesBetweenCellsAndAddsARowAtTheEnd() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        let table = try #require(live(controller, TableBlockController.self))
        window.makeFirstResponder(table.cells[0][0])
        table.cells[0][0].doCommand(by: #selector(NSResponder.insertTab(_:)))
        #expect(window.firstResponder === table.cells[0][1])
        let last = table.cells[2][2]
        window.makeFirstResponder(last)
        last.doCommand(by: #selector(NSResponder.insertTab(_:)))
        #expect(table.table.rows.count == 4)
        #expect(window.firstResponder === table.cells[3][0])
        // ⇧Tab from the first cell leaves the table.
        window.makeFirstResponder(table.cells[0][0])
        table.cells[0][0].doCommand(by: #selector(NSResponder.insertBacktab(_:)))
        #expect(window.firstResponder === controller.textView)
    }

    @Test func rowsAndColumnsAreAddedAndUndone() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        let base = SyncedTree(controller.currentBlocks())
        let table = try #require(live(controller, TableBlockController.self))
        table.perform("Insert Column") { $0.insertColumn(at: 1); return true }
        #expect(table.cells[0].count == 4)
        let (operations, _) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        #expect(operations.count == 1)
        guard case .array(let rows)? = operations.first?.content?["blocknote"]?["rows"], case .array(let cells)? = rows[2]["cells"] else {
            Issue.record("no rows"); return
        }
        // The shape changed: nested blocks travel with their cell.
        #expect(cells[2]["blocks"] != nil)
        window.undoManager?.undo()
        #expect(table.table.columnCount == 3)
    }

    @Test func boldInACell() throws {
        let document = try RichPage.load()
        let (controller, window) = editor(document)
        let table = try #require(live(controller, TableBlockController.self))
        let cell = table.cells[1][0]
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
        #expect(operations.first?.parentId == "01M7ONENOTEBLOCK0000000005")
        #expect(operations.first?.position == 2)
        #expect(operations.first?.type == "bulletListItem")
        #expect(operations.first?.text == "Note the volume")
        #expect(result.parent[operations.first?.blockId ?? ""] == .some("01M7ONENOTEBLOCK0000000005"))
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
        let (controller, _) = editor(BlockDocument(id: "d", revision: 1, titleAuto: true, canUndo: false, canRedo: false, title: "",
                                                   status: .rough, createdAt: "", updatedAt: "", children: [RichPage.paragraph("p", [RichPage.run("Hello")])]))
        let base = SyncedTree(controller.currentBlocks())
        controller.textView.setSelectedRange(NSRange(location: 2, length: 0))
        controller.insertTable()
        let (operations, _) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        #expect(operations.count == 1)
        #expect(operations.first?.operation == .insert)
        #expect(operations.first?.type == "table")
        #expect(operations.first?.content?["blocknote"]?["headerRows"] == .number(1))
    }

    // MARK: Gallery

    @Test func gallery() throws {
        let document = try RichPage.load()
        let model = AppModel()
        model.connectsOnAppear = false
        model.navigation = NavigationResponse(items: [.folder(NavigationFolder(
            id: "lab", parentId: nil, name: "Lab", position: 0,
            children: [.document(NavigationDocument(id: document.id, title: document.title, status: .rough,
                                                    updatedAt: document.updatedAt, createdAt: document.createdAt,
                                                    snippet: "", folderId: "lab", position: 0))]))], recent: [])
        for (name, appearance) in [("editor-onenote-light", NSAppearance.Name.aqua), ("editor-onenote-dark", .darkAqua)] {
            try snapshot(NoteEditorView(session: EditorSession(document: document)).environment(model), named: name, appearance: appearance)
        }
        // Scrolled down to the columns, math and file.
        for (name, appearance) in [("editor-onenote-lower-light", NSAppearance.Name.aqua), ("editor-onenote-lower-dark", .darkAqua)] {
            try snapshot(NoteEditorView(session: EditorSession(document: document), focusBlockId: "01M7ONENOTEBLOCK0000000011").environment(model),
                         named: name, appearance: appearance)
        }
    }
}
