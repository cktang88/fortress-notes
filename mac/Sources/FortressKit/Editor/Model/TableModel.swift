import Foundation

// A table as the editor edits it (docs/block-model.md §2): `table → table_row →
// table_cell → blocks`, all ordinary blocks with ids. Each cell's text is its first
// paragraph; whatever else a cell holds (lists, pictures, nested tables from OneNote)
// is kept as it came and shown read-only. Foundation-only so it can be unit-tested.

/// One `table_cell`: its attrs, the text of its first paragraph, and its other blocks.
public struct TableCell: Hashable, Sendable {
    public var id: String
    /// `colspan`, `rowspan`, `background`, `color`, `align`, and extension attributes.
    public var attrs: [String: JSONValue]
    /// The paragraph holding the cell's text. It exists on the server when `hasParagraph`;
    /// otherwise it is inserted once the cell has text.
    public var paragraphId: String
    public var hasParagraph: Bool
    /// The paragraph's own attrs (extension attributes from an import).
    public var paragraphAttrs: [String: JSONValue]
    public var runs: [InlineRun]
    /// The cell's other blocks, read-only and never rewritten.
    public var nested: [BlockNode]

    public init(id: String = EditorBlock.newId(), runs: [InlineRun] = [], attrs: [String: JSONValue] = [:],
                paragraphId: String = EditorBlock.newId(), hasParagraph: Bool = false,
                paragraphAttrs: [String: JSONValue] = [:], nested: [BlockNode] = []) {
        self.id = id
        self.runs = runs
        self.attrs = attrs
        self.paragraphId = paragraphId
        self.hasParagraph = hasParagraph
        self.paragraphAttrs = paragraphAttrs
        self.nested = nested
    }

    public var plainText: String { BlockCodec.plainText(runs) }

    private func color(_ key: String) -> String? {
        guard let value = attrs[key]?.stringValue, !value.isEmpty, value != "default" else { return nil }
        return value
    }

    public var backgroundColor: String? { color("background") }
    public var textColor: String? { color("color") }
    /// left, center, right or justify.
    public var alignment: String { attrs["align"]?.stringValue ?? "left" }
    public var colspan: Int { max(1, attrs["colspan"]?.intValue ?? 1) }
    public var rowspan: Int { max(1, attrs["rowspan"]?.intValue ?? 1) }

    /// The paragraph written for the cell's text, if it has one (or should get one).
    public var paragraphFields: BlockFields? {
        guard hasParagraph || !runs.isEmpty else { return nil }
        return BlockFields(type: "paragraph", attrs: paragraphAttrs, content: BlockCodec.inlineContent(runs))
    }

    /// A fresh cell that looks like `template` (colour, alignment) but spans one column.
    static func empty(like template: TableCell?) -> TableCell {
        var attrs = template?.attrs ?? [:]
        attrs["colspan"] = nil
        attrs["rowspan"] = nil
        return TableCell(attrs: attrs)
    }

    /// The cell as the server holds it.
    init(node: BlockNode) {
        var children = node.children
        var paragraph: BlockNode?
        if let first = children.first, first.type == "paragraph", first.children.isEmpty {
            paragraph = first
            children.removeFirst()
        }
        self.init(id: node.id, runs: paragraph.map { BlockCodec.runs(fromContent: $0.content) } ?? [],
                  attrs: node.attrs, paragraphId: paragraph?.id ?? EditorBlock.newId(),
                  hasParagraph: paragraph != nil, paragraphAttrs: paragraph?.attrs ?? [:], nested: children)
    }
}

/// One `table_row`.
public struct TableRow: Hashable, Sendable {
    public var id: String
    public var attrs: [String: JSONValue]
    public var cells: [TableCell]

    public init(id: String = EditorBlock.newId(), cells: [TableCell], attrs: [String: JSONValue] = [:]) {
        self.id = id
        self.cells = cells
        self.attrs = attrs
    }
}

public struct TableContent: Hashable, Sendable {
    public var rows: [TableRow]
    /// CSS pixels per column; nil = automatic (`column_widths`).
    public var columnWidths: [Double?]
    public var headerRows: Int
    public var headerCols: Int
    /// The table's other attrs (`borders`, extension attributes).
    public var extra: [String: JSONValue]
    /// Which of the managed attrs the table had, so an untouched table writes back
    /// exactly what it read.
    var stored: Set<String> = []

    public init(rows: [TableRow], columnWidths: [Double?] = [], headerRows: Int = 0, headerCols: Int = 0,
                extra: [String: JSONValue] = [:]) {
        self.rows = rows
        self.columnWidths = columnWidths
        self.headerRows = headerRows
        self.headerCols = headerCols
        self.extra = extra
    }

    /// A new table: `rows` × `columns` empty cells, the first row a header.
    public static func blank(rows: Int = 3, columns: Int = 3) -> TableContent {
        TableContent(rows: (0..<max(1, rows)).map { _ in TableRow(cells: (0..<max(1, columns)).map { _ in TableCell() }) },
                     columnWidths: Array(repeating: nil, count: max(1, columns)), headerRows: 1)
    }

    // MARK: Decoding

    /// The table a `table` block holds, or nil when its children aren't rows of cells
    /// (then it is shown read-only).
    public init?(node: BlockNode) {
        guard node.type == "table", !node.children.isEmpty,
              node.children.allSatisfy({ $0.type == "table_row" && !$0.children.isEmpty && $0.children.allSatisfy { $0.type == "table_cell" } })
        else { return nil }
        var extra = node.attrs
        var stored: Set<String> = []
        var widths: [Double?] = []
        if case .array(let items)? = extra["column_widths"] {
            widths = items.map { if case .number(let width) = $0 { width } else { nil } }
            stored.insert("column_widths")
        }
        func count(_ key: String) -> Int {
            guard let value = extra[key] else { return 0 }
            stored.insert(key)
            return max(0, value.intValue ?? 0)
        }
        let headerRows = count("header_rows")
        let headerCols = count("header_cols")
        for key in ["column_widths", "header_rows", "header_cols"] { extra[key] = nil }
        self.init(rows: node.children.map { row in TableRow(id: row.id, cells: row.children.map(TableCell.init(node:)), attrs: row.attrs) },
                  columnWidths: widths, headerRows: headerRows, headerCols: headerCols, extra: extra)
        self.stored = stored
    }

    // MARK: Encoding

    /// The `table` block's attrs.
    public var attrs: [String: JSONValue] {
        var attrs = extra
        if stored.contains("column_widths") || columnWidths.contains(where: { $0 != nil }) || !columnWidths.isEmpty {
            attrs["column_widths"] = .array(columnWidths.map { $0.map(JSONValue.number) ?? .null })
        }
        if stored.contains("header_rows") || headerRows > 0 { attrs["header_rows"] = .number(Double(headerRows)) }
        if stored.contains("header_cols") || headerCols > 0 { attrs["header_cols"] = .number(Double(headerCols)) }
        return attrs
    }

    /// What search reads: rows on lines, cells separated by tabs.
    public var plainText: String {
        rows.map { row in
            row.cells.map { cell in
                ([cell.plainText] + cell.nested.map(Self.nestedText)).filter { !$0.isEmpty }.joined(separator: " ")
                    .replacingOccurrences(of: "\n", with: " ")
            }.joined(separator: "\t")
        }.joined(separator: "\n")
    }

    static func nestedText(_ node: BlockNode) -> String {
        ([node.text] + node.children.map(nestedText)).filter { !$0.isEmpty }.joined(separator: " ")
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
        return BlockFields(type: "column", attrs: attrs)
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
