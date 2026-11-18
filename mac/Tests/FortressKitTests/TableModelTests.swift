import Foundation
import Testing

#if canImport(FortressKit)
@testable import FortressKit
private typealias BlockOp = FortressKit.BlockOperation
#else
@testable import Core
private typealias BlockOp = Core.BlockOperation
#endif

// Tables, columns, math and superscript: block content ⇄ editing model, and the
// operations edits turn into (spec §2a).

private func run(_ text: String, _ styles: [String: JSONValue] = [:]) -> JSONValue {
    .object(["type": .string("text"), "text": .string(text), "styles": .object(styles)])
}

private func cell(_ text: String, props: [String: JSONValue] = [:], blocks: JSONValue? = nil) -> JSONValue {
    var object: [String: JSONValue] = ["type": .string("tableCell"), "props": .object(props), "content": .array([run(text)])]
    if let blocks { object["blocks"] = blocks }
    return .object(object)
}

private func row(_ cells: JSONValue...) -> JSONValue { .object(["cells": .array(cells)]) }

private func blockNode(
    _ id: String, _ type: String, attrs: [String: JSONValue] = [:], content: [String: JSONValue] = [:],
    text: String = "", children: [BlockNode] = []
) -> BlockNode {
    BlockNode(id: id, documentId: "doc", parentId: nil, position: 0, type: type, attrs: attrs, userAttrs: [:],
              content: .object(content), text: text, createdAt: "", updatedAt: "", children: children)
}

private func paragraph(_ id: String, _ text: String) -> BlockNode {
    blockNode(id, "paragraph", content: ["blocknote": .array([run(text)])], text: text)
}

/// The OneNote-style table from spec §2a: widths, a header row, colours, nested blocks.
let oneNoteTable: JSONValue = .object([
    "type": .string("tableContent"),
    "columnWidths": .array([.number(200), .null]),
    "headerRows": .number(1),
    "rows": .array([
        row(cell("Name", props: ["backgroundColor": .string("#DEEAF6")]), cell("Qty", props: ["backgroundColor": .string("#DEEAF6")])),
        row(cell("Apples", props: ["textColor": .string("red")]),
            cell("3", props: ["textAlignment": .string("center")],
                 blocks: .array([.object(["type": .string("list"), "attrs": .object([:]),
                                          "content": .object(["blocknote": .array([run("in cell")])])])]))),
    ]),
])

private func tableNode(_ id: String = "t1", attrs: [String: JSONValue] = ["borders": .bool(true)]) -> BlockNode {
    blockNode(id, "table", attrs: attrs, content: ["blocknote": oneNoteTable, "markdown": .string("<table>…</table>")],
              text: "Name\tQty\nApples\t3 in cell")
}

@Suite struct TableModelTests {
    @Test func decodesTheSpecTable() throws {
        let block = BlockCodec.decode(tableNode())
        #expect(block.kind == .table)
        let table = try #require(block.table)
        #expect(table.rows.count == 2)
        #expect(table.columnCount == 2)
        #expect(table.columnWidths == [200, nil])
        #expect(table.headerRows == 1)
        #expect(table.cell(row: 0, column: 0)?.backgroundColor == "#DEEAF6")
        #expect(table.cell(row: 1, column: 0)?.textColor == "red")
        #expect(table.cell(row: 1, column: 1)?.alignment == "center")
        #expect(table.cell(row: 1, column: 1)?.nestedBlocks.count == 1)
        #expect(table.plainText == "Name\tQty\nApples\t3 in cell")
    }

    @Test func untouchedTableSavesNothing() {
        let blocks = BlockCodec.flatten([tableNode(), paragraph("p", "after")])
        let fields = BlockCodec.encode(blocks[0])
        #expect(fields.content == tableNode().content)
        #expect(fields.attrs == ["borders": .bool(true)])
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: blocks)
        #expect(operations.isEmpty)
    }

    @Test func editingACellIsOneUpdateThatKeepsNestedBlocksOnTheServer() throws {
        let blocks = BlockCodec.flatten([tableNode()])
        var edited = blocks
        edited[0].table?.setRuns([InlineRun("Pears", InlineMarks(bold: true)), InlineRun("²", InlineMarks(sup: true))], row: 1, column: 0)
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations.count == 1)
        let update = try #require(operations.first)
        #expect(update.operation == .update)
        #expect(update.type == "table")
        #expect(update.attrs == ["borders": .bool(true)])
        let content = try #require(update.content?["blocknote"])
        #expect(content["headerRows"] == .number(1))
        #expect(content["columnWidths"] == .array([.number(200), .null]))
        guard case .array(let rows)? = content["rows"], case .array(let cells)? = rows[1]["cells"] else {
            Issue.record("no rows"); return
        }
        // Same shape: cells go without `blocks`, so the server keeps the nested list.
        #expect(cells[1]["blocks"] == nil)
        #expect(cells[0]["props"] == .object(["textColor": .string("red")]))
        #expect(cells[0]["content"] == .array([run("Pears", ["bold": .bool(true)]), run("²", ["superscript": .bool(true)])]))
        // The server derives Markdown for tables; nothing stale is sent.
        #expect(update.content?["markdown"] == nil)
        #expect(update.text == "Name\tQty\nPears²\t3 in cell")
    }

    @Test func addingRowsAndColumnsWritesNestedBlocksWithTheirCells() throws {
        var table = try #require(TableContent(blockNote: oneNoteTable))
        table.insertRow(at: 2)
        #expect(table.rows.count == 3)
        #expect(table.rows[2].cells.count == 2)
        // A new body row looks like the body row above it, not the header.
        #expect(table.rows[2].cells[0].textColor == "red")
        #expect(table.rows[2].cells[0].plainText == "")
        table.insertColumn(at: 1)
        #expect(table.columnCount == 3)
        #expect(table.columnWidths == [200, nil, nil])
        // A header cell added to the header row keeps the header colour.
        #expect(table.rows[0].cells[1].backgroundColor == "#DEEAF6")
        #expect(table.shapeChanged)
        guard case .array(let rows)? = table.blockNote["rows"], case .array(let cells)? = rows[1]["cells"] else {
            Issue.record("no rows"); return
        }
        #expect(cells.count == 3)
        #expect(cells[2]["blocks"] != nil)  // the nested list moved with its cell
        #expect(table.blockNote["columnWidths"] == .array([.number(200), .null, .null]))
    }

    @Test func removingKeepsAtLeastOneRowAndColumn() throws {
        var table = try #require(TableContent(blockNote: oneNoteTable))
        let removedColumn = table.removeColumn(at: 0)
        #expect(removedColumn)
        #expect(table.columnWidths == [nil])
        #expect(table.rows.map(\.cells.count) == [1, 1])
        let removedLastColumn = table.removeColumn(at: 0)
        #expect(!removedLastColumn)
        let removedRow = table.removeRow(at: 0)
        #expect(removedRow)
        #expect(table.headerRows == 0)
        let removedLastRow = table.removeRow(at: 0)
        #expect(!removedLastRow)
        #expect(table.rows.count == 1)
    }

    @Test func headerRowFollowsInsertsAbove() throws {
        var table = try #require(TableContent(blockNote: oneNoteTable))
        table.insertRow(at: 0)
        #expect(table.headerRows == 2)
        table.toggleHeaderRow()
        #expect(table.headerRows == 0)
        #expect(table.blockNote["headerRows"] == nil)
    }

    @Test func readsOlderBlockNoteCells() throws {
        // The web editor's older tables: a cell is its runs.
        let legacy: JSONValue = .object(["type": .string("tableContent"), "rows": .array([
            .object(["cells": .array([.array([run("Day")]), .array([run("Area")])])]),
        ])])
        let block = BlockCodec.decode(blockNode("t", "table", content: ["blocknote": legacy]))
        #expect(block.kind == .table)
        #expect(block.table?.cell(row: 0, column: 1)?.plainText == "Area")
        // Untouched: saved back exactly as it was.
        #expect(BlockCodec.encode(block).content == .object(["blocknote": legacy]))
        var edited = block
        edited.table?.setRuns([InlineRun("Where")], row: 0, column: 1)
        let cells = BlockCodec.encode(edited).content["blocknote"]?["rows"]
        guard case .array(let rows)? = cells, case .array(let first)? = rows[0]["cells"] else { Issue.record("no rows"); return }
        #expect(first[1]["type"] == .string("tableCell"))
        #expect(first[1]["content"] == .array([run("Where")]))
    }

    @Test func emptyOrBrokenTablesStayEmbeds() {
        let empty = BlockCodec.decode(blockNode("t", "table", content: ["blocknote": .object(["type": .string("tableContent"), "rows": .array([])])]))
        #expect(empty.kind == .embed)
        let broken = BlockCodec.decode(blockNode("t", "table", content: ["blocknote": .string("nope")]))
        #expect(broken.kind == .embed)
    }

    @Test func softBreaksInCellsRoundTrip() throws {
        let content: JSONValue = .object(["type": .string("tableContent"), "rows": .array([row(cell("one\ntwo"))])])
        var table = try #require(TableContent(blockNote: content))
        #expect(table.cell(row: 0, column: 0)?.runs == [InlineRun("one\u{2028}two")])
        table.setRuns([InlineRun("one\u{2028}two!")], row: 0, column: 0)
        #expect(table.blockNote["rows"].flatMap { if case .array(let rows) = $0 { rows[0]["cells"] } else { nil } }
                .flatMap { if case .array(let cells) = $0 { cells[0]["content"] } else { nil } } == .array([run("one\ntwo!")]))
    }

    @Test func blankTableHasAHeaderRow() {
        let table = TableContent.blank(rows: 2, columns: 3)
        #expect(table.rows.count == 2)
        #expect(table.columnCount == 3)
        #expect(table.headerRows == 1)
        #expect(table.blockNote["headerRows"] == .number(1))
    }
}

// MARK: Columns

private func columnsNode() -> BlockNode {
    blockNode("cols", "columns", attrs: ["onenote_y": .number(3)], children: [
        blockNode("c1", "column", attrs: ["width": .number(0.4)], children: [paragraph("l1", "Left one"), paragraph("l2", "Left two")]),
        blockNode("c2", "column", attrs: ["width": .number(0.6)], children: [
            blockNode("r1", "image", attrs: ["url": .string("/media/a.png"), "alt": .string("OCR"), "width": .number(300)]),
        ]),
    ])
}

@Suite struct ColumnsModelTests {
    @Test func decodesColumnsWithTheirBlocks() throws {
        let blocks = BlockCodec.flatten([paragraph("p0", "before"), columnsNode(), paragraph("p9", "after")])
        #expect(blocks.map(\.id) == ["p0", "cols", "p9"])
        let columns = try #require(blocks[1].columns)
        #expect(blocks[1].kind == .columns)
        #expect(columns.map(\.id) == ["c1", "c2"])
        #expect(columns.map(\.width) == [0.4, 0.6])
        #expect(columns[0].blocks.map(\.plainText) == ["Left one", "Left two"])
        #expect(columns[1].blocks.first?.kind == .image)
        #expect(EditorColumn.fractions(columns) == [0.4, 0.6])
    }

    @Test func syncedTreeHoldsColumnChildren() {
        let blocks = BlockCodec.flatten([columnsNode()])
        let tree = SyncedTree(blocks)
        #expect(tree.order == ["cols", "c1", "l1", "l2", "c2", "r1"])
        #expect(tree.parent["c1"] == .some("cols"))
        #expect(tree.parent["l2"] == .some("c1"))
        #expect(tree.parent["r1"] == .some("c2"))
        #expect(tree.fields["c1"]?.type == "column")
        #expect(tree.fields["c1"]?.attrs == ["width": .number(0.4)])
        #expect(tree.fields["cols"]?.attrs == ["onenote_y": .number(3)])
        let (operations, _) = BlockDiff.operations(from: tree, to: blocks)
        #expect(operations.isEmpty)
    }

    @Test func editingInsideAColumnUpdatesThatBlock() throws {
        let blocks = BlockCodec.flatten([columnsNode()])
        var edited = blocks
        edited[0].columns?[0].blocks[1].runs = [InlineRun("Left two, edited")]
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations.count == 1)
        #expect(operations.first?.operation == .update)
        #expect(operations.first?.blockId == "l2")
        #expect(operations.first?.text == "Left two, edited")
    }

    @Test func newBlockInAColumnIsInsertedUnderTheColumn() throws {
        let blocks = BlockCodec.flatten([columnsNode()])
        var edited = blocks
        edited[0].columns?[1].blocks.append(EditorBlock(id: "new", runs: [InlineRun("caption below")]))
        edited[0].columns?[0].blocks.remove(at: 0)
        let (operations, result) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations.map(\.operation) == [.delete, .insert])
        #expect(operations[0].blockId == "l1")
        #expect(operations[1].blockId == "new")
        #expect(operations[1].parentId == "c2")
        #expect(operations[1].position == 1)
        #expect(result.parent["new"] == .some("c2"))
    }

    @Test func movingABlockIntoAColumnIsAMove() {
        let blocks = BlockCodec.flatten([paragraph("p0", "loose"), columnsNode()])
        var edited = blocks
        let moving = edited.remove(at: 0)
        edited[0].columns?[0].blocks.insert(moving, at: 0)
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations.allSatisfy { $0.operation == .move })
        let intoColumn = operations.first { $0.blockId == "p0" }
        #expect(intoColumn?.parentId == "c1")
        #expect(intoColumn?.position == 0)
    }

    @Test func nothingNestsUnderColumnsInTheMainFlow() {
        var blocks = BlockCodec.flatten([columnsNode(), paragraph("p", "after")])
        blocks[1].depth = 1
        let shaped = BlockDiff.shape(blocks)
        #expect(shaped[1].parent == nil)
    }

    @Test func columnWidthChangeUpdatesTheColumn() {
        let blocks = BlockCodec.flatten([columnsNode()])
        var edited = blocks
        edited[0].columns?[0].width = 0.5
        edited[0].columns?[1].width = 0.5
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations.map(\.blockId) == ["c1", "c2"])
        #expect(operations.first?.attrs == ["width": .number(0.5)])
        #expect(operations.first?.type == "column")
    }

    @Test func columnsWithStrayChildrenStayEmbeds() {
        let node = blockNode("cols", "columns", children: [paragraph("p", "not a column")])
        let blocks = BlockCodec.flatten([node])
        #expect(blocks.count == 1)
        #expect(blocks[0].kind == .embed)
    }

    @Test func fractionsFillMissingWidths() {
        let columns = [EditorColumn(id: "a", width: nil), EditorColumn(id: "b", width: nil)]
        #expect(EditorColumn.fractions(columns) == [0.5, 0.5])
        let mixed = [EditorColumn(id: "a", width: 0.3), EditorColumn(id: "b", width: nil)]
        #expect(EditorColumn.fractions(mixed) == [0.5, 0.5])
    }
}

// MARK: Math, images, marks

@Suite struct RichBlockTests {
    @Test func mathIsKeptVerbatim() {
        let node = blockNode("m", "math", content: ["latex": .string("E = mc^2"), "mathml": .string("<math/>")], text: "E = mc^2")
        let block = BlockCodec.decode(node)
        #expect(block.kind == .math)
        #expect(BlockCodec.encode(block).content == node.content)
        let (operations, _) = BlockDiff.operations(from: SyncedTree([block]), to: [block])
        #expect(operations.isEmpty)
    }

    @Test func imageAttrsSurviveUntouched() {
        let attrs: [String: JSONValue] = ["url": .string("/media/x.png"), "alt": .string("OCR text"), "caption": .string("Whiteboard"),
                                          "width": .number(480), "height": .number(320), "name": .string("scan.png")]
        let block = BlockCodec.decode(blockNode("i", "image", attrs: attrs, content: ["markdown": .string("<img>")]))
        #expect(block.kind == .image)
        #expect(BlockCodec.encode(block).attrs == attrs)
    }

    @Test func superscriptAndSubscriptRoundTrip() {
        let content: JSONValue = .array([run("H"), run("2", ["subscript": .bool(true)]), run("O x"), run("2", ["superscript": .bool(true)])])
        let block = BlockCodec.decode(blockNode("p", "paragraph", content: ["blocknote": content]))
        #expect(block.runs[1].marks.sub)
        #expect(block.runs[3].marks.sup)
        #expect(block.runs[1].marks.extraStyles.isEmpty)
        #expect(BlockCodec.encode(block).content["blocknote"] == content)
        #expect(BlockCodec.markdown(block) == "H<sub>2</sub>O x<sup>2</sup>")
        #expect(InlineMarkdown.parse("x<sup>2</sup>") == [InlineRun("x"), InlineRun("2", InlineMarks(sup: true))])
    }

    @Test func provenanceAndUnknownAttrsSurviveTableEdits() {
        var node = tableNode(attrs: ["borders": .bool(false), "onenote": .object(["layout": .string("fixed")])])
        node.provenance = .object(["source": .string("onenote_import"), "external_id": .string("{T1}")])
        var block = BlockCodec.decode(node)
        block.table?.setRuns([InlineRun("changed")], row: 0, column: 0)
        let fields = BlockCodec.encode(block)
        #expect(fields.attrs == node.attrs)
        let (operations, _) = BlockDiff.operations(from: SyncedTree([BlockCodec.decode(node)]), to: [block])
        // Provenance is never sent on update (the server keeps it).
        #expect(operations.first?.provenance == nil)
    }
}
