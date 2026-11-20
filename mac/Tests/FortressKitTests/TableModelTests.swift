import Foundation
import Testing

#if canImport(FortressKit)
@testable import FortressKit
private typealias BlockOp = FortressKit.BlockOperation
#else
@testable import Core
private typealias BlockOp = Core.BlockOperation
#endif

// Tables, columns, unknown types and superscript: block content ⇄ editing model, and the
// operations edits turn into (spec §2a).

private func run(_ text: String, _ marks: [String: JSONValue] = [:]) -> JSONValue {
    var object: [String: JSONValue] = ["type": .string("text"), "text": .string(text)]
    if !marks.isEmpty { object["marks"] = .object(marks) }
    return .object(object)
}

private func blockNode(
    _ id: String, _ type: String, attrs: [String: JSONValue] = [:], content: [JSONValue]? = nil,
    payload: JSONValue? = nil, children: [BlockNode] = []
) -> BlockNode {
    BlockNode(id: id, documentId: "doc", type: type, attrs: attrs, content: content.map(JSONValue.array) ?? .null,
              payload: payload, children: children)
}

private func paragraph(_ id: String, _ text: String) -> BlockNode {
    blockNode(id, "paragraph", content: [run(text)])
}

private func cell(_ id: String, _ text: String, attrs: [String: JSONValue] = [:], more: [BlockNode] = []) -> BlockNode {
    blockNode(id, "table_cell", attrs: attrs, children: [paragraph(id + "p", text)] + more)
}

/// The OneNote-style table of docs/block-model.md §2: widths, a header row, colours,
/// a nested list in a cell.
private func tableNode(_ id: String = "t1", attrs: [String: JSONValue] = ["borders": .bool(true)]) -> BlockNode {
    var tableAttrs = attrs
    tableAttrs["column_widths"] = .array([.number(200), .null])
    tableAttrs["header_rows"] = .number(1)
    return blockNode(id, "table", attrs: tableAttrs, children: [
        blockNode("r0", "table_row", children: [
            cell("c00", "Name", attrs: ["background": .string("#DEEAF6")]),
            cell("c01", "Qty", attrs: ["background": .string("#DEEAF6")]),
        ]),
        blockNode("r1", "table_row", children: [
            cell("c10", "Apples", attrs: ["color": .string("red")]),
            cell("c11", "3", attrs: ["align": .string("center")], more: [blockNode("n1", "bullet_item", content: [run("in cell")])]),
        ]),
    ])
}

private func specTable() throws -> TableContent { try #require(TableContent(node: tableNode())) }

@Suite struct TableModelTests {
    @Test func decodesTheSpecTable() throws {
        let block = BlockCodec.decode(tableNode())
        #expect(block.kind == .table)
        let table = try #require(block.table)
        #expect(table.rows.count == 2)
        #expect(table.rows.map(\.id) == ["r0", "r1"])
        #expect(table.columnCount == 2)
        #expect(table.columnWidths == [200, nil])
        #expect(table.headerRows == 1)
        #expect(table.extra == ["borders": .bool(true)])
        #expect(table.cell(row: 0, column: 0)?.backgroundColor == "#DEEAF6")
        #expect(table.cell(row: 1, column: 0)?.textColor == "red")
        #expect(table.cell(row: 1, column: 1)?.alignment == "center")
        #expect(table.cell(row: 1, column: 1)?.paragraphId == "c11p")
        #expect(table.cell(row: 1, column: 1)?.nested.map(\.id) == ["n1"])
        #expect(table.plainText == "Name\tQty\nApples\t3")
    }

    @Test func untouchedTableSavesNothing() {
        let blocks = BlockCodec.flatten([tableNode(), paragraph("p", "after")])
        let fields = BlockCodec.encode(blocks[0])
        #expect(fields == BlockFields(tableNode()))
        let tree = SyncedTree(blocks)
        #expect(tree.order == ["t1", "p"])
        #expect(tree.tables["t1"]?.order == ["r0", "c00", "c00p", "c01", "c01p", "r1", "c10", "c10p", "c11", "c11p", "n1"])
        let (operations, _) = BlockDiff.operations(from: tree, to: blocks)
        #expect(operations.isEmpty)
    }

    @Test func editingACellIsOneUpdateOfItsParagraph() throws {
        let blocks = BlockCodec.flatten([tableNode()])
        var edited = blocks
        edited[0].table?.setRuns([InlineRun("Pears", InlineMarks(bold: true)), InlineRun("²", InlineMarks(sup: true))], row: 1, column: 0)
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations.count == 1)
        let update = try #require(operations.first)
        #expect(update.operation == .update)
        #expect(update.blockId == "c10p")
        #expect(update.type == nil && update.attrs == nil)
        #expect(update.content == .array([run("Pears", ["bold": .bool(true)]), run("²", ["sup": .bool(true)])]))
    }

    @Test func addingRowsAndColumnsInsertsRowsCellsAndParagraphs() throws {
        let blocks = BlockCodec.flatten([tableNode()])
        var edited = blocks
        var table = try #require(edited[0].table)
        table.insertRow(at: 2)
        #expect(table.rows.count == 3)
        #expect(table.rows[2].cells.count == 2)
        // A new body row looks like the body row above it, not the header.
        #expect(table.rows[2].cells[0].textColor == "red")
        #expect(table.rows[2].cells[0].plainText == "")
        table.setRuns([InlineRun("Pears")], row: 2, column: 0)
        table.insertColumn(at: 1)
        #expect(table.columnCount == 3)
        #expect(table.columnWidths == [200, nil, nil])
        // A header cell added to the header row keeps the header colour.
        #expect(table.rows[0].cells[1].backgroundColor == "#DEEAF6")
        edited[0].table = table
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        // The table's widths, a cell in each old row, the new row with its cells, and the
        // one new cell with text gets a paragraph.
        #expect(operations.first == BlockOp(operation: .update, blockId: "t1", attrs: ["column_widths": .array([.number(200), .null, .null])]))
        let inserts = operations.filter { $0.operation == .insert }
        #expect(inserts.filter { $0.type == "table_row" }.count == 1)
        #expect(inserts.filter { $0.type == "table_cell" }.count == 5)
        #expect(inserts.filter { $0.type == "paragraph" }.count == 1)
        #expect(inserts.first { $0.type == "table_row" }?.afterId == "r1")
        #expect(inserts.first { $0.type == "table_cell" && $0.afterId == "c00" } != nil)
    }

    @Test func removingKeepsAtLeastOneRowAndColumn() throws {
        var table = try specTable()
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

    @Test func removingARowDeletesIt() throws {
        let blocks = BlockCodec.flatten([tableNode()])
        var edited = blocks
        _ = edited[0].table?.removeRow(at: 1)
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations == [BlockOp(operation: .delete, blockId: "r1")])
    }

    @Test func headerRowFollowsInsertsAbove() throws {
        var table = try specTable()
        table.insertRow(at: 0)
        #expect(table.headerRows == 2)
        table.toggleHeaderRow()
        #expect(table.headerRows == 0)
        // It had the attribute, so it is written as 0 (not dropped).
        #expect(table.attrs["header_rows"] == .number(0))
    }

    @Test func emptyOrBrokenTablesStayEmbeds() {
        let empty = BlockCodec.decode(blockNode("t", "table"))
        #expect(empty.kind == .embed)
        let broken = BlockCodec.decode(blockNode("t", "table", children: [paragraph("p", "not a row")]))
        #expect(broken.kind == .embed)
        #expect(BlockCodec.encode(broken) == BlockFields(blockNode("t", "table", children: [paragraph("p", "not a row")])))
    }

    @Test func lineBreaksInCellsRoundTrip() throws {
        let node = blockNode("t", "table", children: [blockNode("r", "table_row", children: [cell("c", "one\ntwo")])])
        var table = try #require(TableContent(node: node))
        #expect(table.cell(row: 0, column: 0)?.runs == [InlineRun("one\u{2028}two")])
        table.setRuns([InlineRun("one\u{2028}two!")], row: 0, column: 0)
        #expect(table.cell(row: 0, column: 0)?.paragraphFields?.content == .array([run("one\ntwo!")]))
    }

    @Test func cellWithoutAParagraphGetsOneWhenTyped() throws {
        let node = blockNode("t", "table", children: [blockNode("r", "table_row", children: [
            blockNode("c", "table_cell", children: [blockNode("img", "image", attrs: ["url": .string("/media/a.png")])]),
        ])])
        let blocks = BlockCodec.flatten([node])
        #expect(blocks[0].table?.cell(row: 0, column: 0)?.hasParagraph == false)
        #expect(BlockDiff.operations(from: SyncedTree(blocks), to: blocks).operations.isEmpty)
        var edited = blocks
        edited[0].table?.setRuns([InlineRun("caption")], row: 0, column: 0)
        let (operations, _) = BlockDiff.operations(from: SyncedTree(blocks), to: edited)
        #expect(operations.count == 1)
        #expect(operations.first?.operation == .insert && operations.first?.beforeId == "img")
    }

    @Test func columnWidthsFitTheSpace() throws {
        let table = try specTable()
        // 200 stored, one automatic column takes the rest.
        #expect(table.layoutWidths(available: 640) == [200, 440])
        // Too narrow: the automatic column keeps its minimum, the stored one shrinks.
        #expect(table.layoutWidths(available: 200, minimum: 64) == [136, 64])
        var fixed = table
        fixed.columnWidths = [100, 150]
        #expect(fixed.layoutWidths(available: 640) == [100, 150])
        #expect(fixed.layoutWidths(available: 125) == [50, 75])
        var automatic = table
        automatic.columnWidths = []
        #expect(automatic.layoutWidths(available: 600) == [300, 300])
    }

    @Test func blankTableHasAHeaderRow() {
        let table = TableContent.blank(rows: 2, columns: 3)
        #expect(table.rows.count == 2)
        #expect(table.columnCount == 3)
        #expect(table.headerRows == 1)
        #expect(table.attrs["header_rows"] == .number(1))
        #expect(table.attrs["column_widths"] == .array([.null, .null, .null]))
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
        #expect(operations.first?.content == .array([run("Left two, edited")]))
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
        #expect(operations[1].afterId == "r1")
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
        #expect(intoColumn?.beforeId == "l1")
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
        #expect(operations.first?.type == nil)
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

// MARK: Unknown types, images, marks

@Suite struct RichBlockTests {
    @Test func unknownTypesAreShownReadOnlyAndKeptVerbatim() {
        // Fortress has no math; a type the app doesn't know (an older server's math
        // block, a newer type) is shown read-only and saved untouched.
        let node = blockNode("m", "math", payload: .object(["latex": .string("E = mc^2"), "mathml": .string("<math/>")]))
        let block = BlockCodec.decode(node)
        #expect(block.kind == .embed)
        #expect(BlockCodec.encode(block) == BlockFields(node))
        let (operations, _) = BlockDiff.operations(from: SyncedTree([block]), to: [block])
        #expect(operations.isEmpty)
    }

    @Test func imageAttrsSurviveUntouched() {
        let attrs: [String: JSONValue] = ["url": .string("/media/x.png"), "alt": .string("OCR text"), "caption": .string("Whiteboard"),
                                          "width": .number(480), "height": .number(320), "name": .string("scan.png")]
        let block = BlockCodec.decode(blockNode("i", "image", attrs: attrs))
        #expect(block.kind == .image)
        #expect(BlockCodec.encode(block).attrs == attrs)
    }

    @Test func superscriptAndSubscriptRoundTrip() {
        let content: [JSONValue] = [run("H"), run("2", ["sub": .bool(true)]), run("O x"), run("2", ["sup": .bool(true)])]
        let block = BlockCodec.decode(blockNode("p", "paragraph", content: content))
        #expect(block.runs[1].marks.sub)
        #expect(block.runs[3].marks.sup)
        #expect(BlockCodec.encode(block).content == .array(content))
    }

    @Test func unknownAttrsSurviveTableEdits() {
        let node = tableNode(attrs: ["borders": .bool(false), "onenote": .object(["layout": .string("fixed")])])
        var block = BlockCodec.decode(node)
        block.table?.setRuns([InlineRun("changed")], row: 0, column: 0)
        let fields = BlockCodec.encode(block)
        #expect(fields.attrs == node.attrs)
        let (operations, _) = BlockDiff.operations(from: SyncedTree([BlockCodec.decode(node)]), to: [block])
        #expect(operations.count == 1)
    }
}
