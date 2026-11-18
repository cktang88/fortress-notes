import Foundation

// A table block's content as the editor edits it (spec §2a): BlockNote's `tableContent`
// extended with column widths, header rows, cell colours and nested cell blocks.
// Foundation-only so it can be unit-tested.

/// One cell: its first line of rich text, its props, and nested blocks kept verbatim.
public struct TableCell: Hashable, Sendable {
    public var runs: [InlineRun]
    /// `backgroundColor`, `textColor`, `textAlignment`, `colspan`, `rowspan`, and any others.
    public var props: [String: JSONValue]
    /// Nested blocks (OneNote cells hold lists, images, nested tables). Shown read-only
    /// and never changed by the editor.
    public var blocks: JSONValue?
    /// Keys the cell had that the editor doesn't manage.
    public var extra: [String: JSONValue]

    public init(runs: [InlineRun] = [], props: [String: JSONValue] = [:], blocks: JSONValue? = nil,
                extra: [String: JSONValue] = [:]) {
        self.runs = runs
        self.props = props
        self.blocks = blocks
        self.extra = extra
    }

    public var plainText: String { BlockCodec.plainText(runs) }

    private func color(_ key: String) -> String? {
        guard let value = props[key]?.stringValue, !value.isEmpty, value != "default" else { return nil }
        return value
    }

    public var backgroundColor: String? { color("backgroundColor") }
    public var textColor: String? { color("textColor") }
    /// left, center, right or justify.
    public var alignment: String { props["textAlignment"]?.stringValue ?? "left" }
    public var colspan: Int { max(1, props["colspan"]?.intValue ?? 1) }
    public var rowspan: Int { max(1, props["rowspan"]?.intValue ?? 1) }

    /// The nested blocks, if any.
    public var nestedBlocks: [JSONValue] {
        if case .array(let items)? = blocks { return items }
        return []
    }

    /// A fresh cell that looks like `template` (colour, alignment) but spans one column.
    static func empty(like template: TableCell?) -> TableCell {
        var props = template?.props ?? [:]
        props["colspan"] = nil
        props["rowspan"] = nil
        return TableCell(props: props)
    }
}

public struct TableRow: Hashable, Sendable {
    public var cells: [TableCell]
    public var extra: [String: JSONValue]

    public init(cells: [TableCell], extra: [String: JSONValue] = [:]) {
        self.cells = cells
        self.extra = extra
    }
}

public struct TableContent: Hashable, Sendable {
    public var rows: [TableRow]
    /// CSS pixels per column; nil = automatic.
    public var columnWidths: [Double?]
    public var headerRows: Int
    public var headerCols: Int
    /// Top-level keys the editor doesn't manage.
    public var extra: [String: JSONValue]
    /// Cells per row when the table was loaded. While the shape is the same, cells are
    /// saved without `blocks` and the server keeps their nested blocks (spec §2a);
    /// after rows or columns change, nested blocks are written out with their cells.
    public var loadedShape: [Int]

    public init(rows: [TableRow], columnWidths: [Double?] = [], headerRows: Int = 0, headerCols: Int = 0,
                extra: [String: JSONValue] = [:]) {
        self.rows = rows
        self.columnWidths = columnWidths
        self.headerRows = headerRows
        self.headerCols = headerCols
        self.extra = extra
        self.loadedShape = rows.map(\.cells.count)
    }

    /// A new table: `rows` × `columns` empty cells, the first row a header.
    public static func blank(rows: Int = 3, columns: Int = 3) -> TableContent {
        let cells = (0..<max(1, columns)).map { _ in TableCell() }
        return TableContent(rows: (0..<max(1, rows)).map { _ in TableRow(cells: cells) },
                            columnWidths: Array(repeating: nil, count: max(1, columns)), headerRows: 1)
    }

    // MARK: Decoding

    /// The table in a block's `content.blocknote`, or nil when it isn't a usable table.
    public init?(blockNote value: JSONValue?) {
        guard case .object(var object)? = value, case .array(let rawRows)? = object["rows"], !rawRows.isEmpty else { return nil }
        if let type = object["type"]?.stringValue, type != "tableContent" { return nil }
        var rows: [TableRow] = []
        for rawRow in rawRows {
            var extra: [String: JSONValue] = [:]
            let rawCells: [JSONValue]
            switch rawRow {
            case .object(var row):
                guard case .array(let cells)? = row["cells"] else { return nil }
                rawCells = cells
                row["cells"] = nil
                extra = row
            case .array(let cells):
                rawCells = cells
            default:
                return nil
            }
            guard !rawCells.isEmpty else { return nil }
            rows.append(TableRow(cells: rawCells.map(Self.cell), extra: extra))
        }
        var widths: [Double?] = []
        if case .array(let items)? = object["columnWidths"] {
            widths = items.map { if case .number(let width) = $0 { width } else { nil } }
        }
        func count(_ key: String) -> Int {
            switch object[key] {
            case .number(let value)?: max(0, Int(value))
            case .bool(true)?: 1
            default: 0
            }
        }
        let headerRows = count("headerRows")
        let headerCols = count("headerCols")
        for key in ["type", "rows", "columnWidths", "headerRows", "headerCols"] { object[key] = nil }
        self.init(rows: rows, columnWidths: widths, headerRows: headerRows, headerCols: headerCols, extra: object)
    }

    private static func cell(_ value: JSONValue) -> TableCell {
        switch value {
        case .object(var object) where object["type"]?.stringValue == "tableCell" || object["content"] != nil || object["props"] != nil:
            var props: [String: JSONValue] = [:]
            if case .object(let raw)? = object["props"] { props = raw }
            let runs = cellRuns(object["content"])
            let blocks = object["blocks"]
            for key in ["type", "props", "content", "blocks"] { object[key] = nil }
            return TableCell(runs: runs, props: props, blocks: blocks, extra: object)
        default:
            // Older BlockNote tables store a cell as its inline content.
            return TableCell(runs: cellRuns(value))
        }
    }

    private static func cellRuns(_ value: JSONValue?) -> [InlineRun] {
        let runs: [InlineRun]
        switch value {
        case .array(let items)?: runs = BlockCodec.runs(fromBlockNote: items)
        case .string(let text)?: runs = [InlineRun(text)]
        case .object?: runs = BlockCodec.runs(fromBlockNote: [value!])
        default: runs = []
        }
        return BlockCodec.normalized(runs.map { run in
            var run = run
            if run.node == nil { run.text = BlockCodec.softBreaks(run.text) }
            return run
        })
    }

    // MARK: Encoding

    /// Whether rows or columns were added or removed since loading.
    public var shapeChanged: Bool { rows.map(\.cells.count) != loadedShape }

    /// `content.blocknote` to save. Nested cell blocks are written only when the shape
    /// changed (otherwise the server keeps them).
    public var blockNote: JSONValue {
        let includeBlocks = shapeChanged
        var object = extra
        object["type"] = .string("tableContent")
        let columns = columnCount
        var widths = Array(columnWidths.prefix(columns))
        widths += Array(repeating: nil, count: max(0, columns - widths.count))
        object["columnWidths"] = .array(widths.map { $0.map(JSONValue.number) ?? .null })
        if headerRows > 0 { object["headerRows"] = .number(Double(headerRows)) }
        if headerCols > 0 { object["headerCols"] = .number(Double(headerCols)) }
        object["rows"] = .array(rows.map { row in
            var rowObject = row.extra
            rowObject["cells"] = .array(row.cells.map { cell in
                var cellObject = cell.extra
                cellObject["type"] = .string("tableCell")
                cellObject["props"] = .object(cell.props)
                cellObject["content"] = BlockCodec.blockNote(cell.runs)
                if includeBlocks, let blocks = cell.blocks { cellObject["blocks"] = blocks }
                return .object(cellObject)
            })
            return .object(rowObject)
        })
        return .object(object)
    }

    /// What search reads: rows on lines, cells separated by tabs.
    public var plainText: String {
        rows.map { row in
            row.cells.map { cell in
                ([cell.plainText] + cell.nestedBlocks.map(Self.nestedText)).filter { !$0.isEmpty }.joined(separator: "\n")
                    .replacingOccurrences(of: "\n", with: " ")
            }.joined(separator: "\t")
        }.joined(separator: "\n")
    }

    static func nestedText(_ block: JSONValue) -> String {
        var parts: [String] = []
        if let blockNote = block["content"]?["blocknote"] {
            if case .array = blockNote {
                parts.append(BlockCodec.inlineText(blockNote))
            } else if let table = TableContent(blockNote: blockNote) {
                parts.append(table.plainText)
            }
        }
        if let latex = block["content"]?["latex"]?.stringValue { parts.append(latex) }
        for key in ["alt", "caption", "name"] {
            if let value = block["attrs"]?[key]?.stringValue, !value.isEmpty { parts.append(value) }
        }
        if case .array(let children)? = block["children"] { parts += children.map(nestedText) }
        return parts.filter { !$0.isEmpty }.joined(separator: "\n")
    }

    // MARK: Shape

    /// Columns, counting spans.
    public var columnCount: Int {
        rows.map { $0.cells.reduce(0) { $0 + $1.colspan } }.max() ?? 0
    }

    public func cell(row: Int, column: Int) -> TableCell? {
        guard rows.indices.contains(row), rows[row].cells.indices.contains(column) else { return nil }
        return rows[row].cells[column]
    }

    public mutating func setRuns(_ runs: [InlineRun], row: Int, column: Int) {
        guard rows.indices.contains(row), rows[row].cells.indices.contains(column) else { return }
        rows[row].cells[column].runs = BlockCodec.normalized(runs)
    }

    /// A row of empty cells at `index` (0…rows.count), styled like the body row next to it.
    public mutating func insertRow(at index: Int) {
        let index = min(max(0, index), rows.count)
        let neighbours = [index, index - 1].filter { rows.indices.contains($0) && $0 >= headerRows }
        let template = neighbours.first.map { rows[$0] }
        let count = max(1, template?.cells.count ?? columnCount)
        let cells = (0..<count).map { column in TableCell.empty(like: template?.cells[column]) }
        rows.insert(TableRow(cells: cells), at: index)
        if index < headerRows { headerRows += 1 }
    }

    /// Removes a row; the last row stays.
    @discardableResult
    public mutating func removeRow(at index: Int) -> Bool {
        guard rows.count > 1, rows.indices.contains(index) else { return false }
        rows.remove(at: index)
        if index < headerRows { headerRows -= 1 }
        return true
    }

    /// An empty column at cell index `index` in every row.
    public mutating func insertColumn(at index: Int) {
        let index = max(0, index)
        for row in rows.indices {
            let at = min(index, rows[row].cells.count)
            let neighbour = rows[row].cells.indices.contains(at) ? rows[row].cells[at] : rows[row].cells.last
            rows[row].cells.insert(TableCell.empty(like: row < headerRows ? neighbour : nil), at: at)
        }
        while columnWidths.count < index { columnWidths.append(nil) }
        columnWidths.insert(nil, at: min(index, columnWidths.count))
    }

    /// Removes the cell at `index` from every row that has one; every row keeps a cell.
    @discardableResult
    public mutating func removeColumn(at index: Int) -> Bool {
        guard index >= 0, rows.allSatisfy({ $0.cells.count > 1 || !$0.cells.indices.contains(index) }),
              rows.contains(where: { $0.cells.indices.contains(index) }) else { return false }
        for row in rows.indices where rows[row].cells.indices.contains(index) {
            rows[row].cells.remove(at: index)
        }
        if columnWidths.indices.contains(index) { columnWidths.remove(at: index) }
        return true
    }

    public mutating func toggleHeaderRow() {
        headerRows = headerRows > 0 ? 0 : 1
    }

    /// Column widths in points for `available` width: stored widths (CSS px ≈ points) as
    /// given, automatic columns share what is left; everything shrinks to fit, but no
    /// column below `minimum` unless even that doesn't fit. A table of fixed columns
    /// narrower than the space stays that narrow.
    public func layoutWidths(available: Double, minimum: Double = 64) -> [Double] {
        let count = max(1, columnCount)
        let stored = (0..<count).map { index in
            columnWidths.indices.contains(index) ? columnWidths[index].flatMap { $0 > 0 ? $0 : nil } : nil
        }
        let fixedTotal = stored.compactMap { $0 }.reduce(0, +)
        let automatic = stored.filter { $0 == nil }.count
        var widths: [Double]
        if automatic == 0 {
            let scale = fixedTotal > available ? available / fixedTotal : 1
            widths = stored.map { ($0 ?? 0) * scale }
        } else {
            let left = available - fixedTotal
            if left >= Double(automatic) * minimum {
                widths = stored.map { $0 ?? left / Double(automatic) }
            } else {
                let room = max(0, available - Double(automatic) * minimum)
                let scale = fixedTotal > 0 ? min(1, room / fixedTotal) : 1
                widths = stored.map { $0.map { $0 * scale } ?? minimum }
            }
        }
        let total = widths.reduce(0, +)
        if total > available, total > 0 { widths = widths.map { $0 * available / total } }
        return widths
    }
}

// MARK: Columns

/// One `column` of a `columns` block: a relative width and its own blocks.
public struct EditorColumn: Hashable, Sendable {
    public var id: String
    /// Relative width (the widths of a row of columns add up to about 1).
    public var width: Double?
    /// The column block as the server sent it.
    public var origin: BlockOrigin?
    /// The blocks in the column, flat with depths like the main flow.
    public var blocks: [EditorBlock]

    public init(id: String = EditorBlock.newId(), width: Double? = nil, origin: BlockOrigin? = nil, blocks: [EditorBlock] = []) {
        self.id = id
        self.width = width
        self.origin = origin
        self.blocks = blocks
    }

    /// The fields a transaction writes for the column block itself.
    public var fields: BlockFields {
        var attrs = origin?.attrs ?? [:]
        if let width { attrs["width"] = .number(width) }
        if let origin, attrs == origin.attrs {
            return BlockFields(type: origin.type, attrs: origin.attrs, content: origin.content, text: origin.text)
        }
        return BlockFields(type: "column", attrs: attrs, content: .object([:]), text: "")
    }

    /// Relative widths that add up to 1 (missing ones share equally).
    public static func fractions(_ columns: [EditorColumn]) -> [Double] {
        guard !columns.isEmpty else { return [] }
        let given = columns.compactMap { $0.width.flatMap { $0 > 0 ? $0 : nil } }
        let missing = columns.count - given.count
        let fallback = given.isEmpty ? 1.0 : given.reduce(0, +) / Double(given.count)
        let raw = columns.map { column -> Double in
            if let width = column.width, width > 0 { return width }
            return missing > 0 ? fallback : 1
        }
        let total = raw.reduce(0, +)
        return raw.map { $0 / total }
    }
}
