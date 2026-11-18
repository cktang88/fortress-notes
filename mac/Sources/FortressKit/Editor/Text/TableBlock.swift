import AppKit
import SwiftUI

/// An editable table inside the note: a grid of small rich-text views, one per cell.
/// Tab and ⇧Tab move between cells (Tab in the last cell adds a row), ↑/↓ at a cell's
/// edge move to the cell above or below, Esc leaves the table. Rows and columns are
/// added and removed from the context menu, as one undo step each. Nested cell blocks
/// (OneNote lists and pictures in cells) show read-only under the cell's text and are
/// saved untouched.
@MainActor
final class TableBlockController: NSObject, LiveBlock, NSTextViewDelegate {
    private(set) var table: TableContent
    let attrs: [String: JSONValue]
    weak var context: AttachmentContext?
    weak var attachment: BlockAttachment?
    let grid = TableGridView()
    var view: NSView { grid }

    /// The cell editors, made the first time the table is laid out or entered: a note
    /// with twenty 50 × 8 tables would otherwise make 8,000 text views when it opens,
    /// most of them never scrolled to.
    var cells: [[CellTextView]] {
        buildIfNeeded()
        return cellViews
    }
    private var cellViews: [[CellTextView]] = []
    private var isBuilt = false
    private var nested: [[NSView?]] = []
    /// Each cell's text height and nested blocks' height at the measured width, so an
    /// edit re-measures one cell (and moves the rows below), not the whole table.
    private var textHeights: [[CGFloat]] = []
    private var nestedHeights: [[CGFloat]] = []
    /// Logical column edges (count + 1) and row edges, in grid coordinates.
    private(set) var columnEdges: [CGFloat] = []
    private(set) var rowEdges: [CGFloat] = []
    private var measuredWidth: CGFloat = -1
    private var measuredHeight: CGFloat = 0
    private(set) var focusedCell: CellTextView?

    init(table: TableContent, attrs: [String: JSONValue], context: AttachmentContext, attachment: BlockAttachment) {
        self.table = table
        self.attrs = attrs
        self.context = context
        self.attachment = attachment
        super.init()
        grid.controller = self
    }

    private func buildIfNeeded() {
        guard !isBuilt else { return }
        isBuilt = true
        rebuild()
    }

    var theme: EditorTheme { context?.theme ?? EditorTheme() }
    var borders: Bool { attrs["borders"]?.boolValue ?? true }
    var padding: CGSize { CGSize(width: 10 * theme.scale, height: 7 * theme.scale) }
    var tableWidth: CGFloat { columnEdges.last ?? 0 }

    func write(into block: inout EditorBlock) {
        block.table = table
    }

    // MARK: Cells

    private static let cellInfo = BlockInfo(id: "table-cell", kind: .paragraph)

    func isHeader(row: Int, column: Int) -> Bool {
        row < table.headerRows || column < table.headerCols
    }

    func cellTheme(row: Int, column: Int) -> EditorTheme {
        var cellTheme = theme
        cellTheme.baseSize = 14 * theme.scale
        cellTheme.isCompact = true
        cellTheme.emphasized = isHeader(row: row, column: column)
        let cell = table.cell(row: row, column: column)
        cellTheme.textColorName = cell?.textColor
        cellTheme.alignment = switch cell?.alignment ?? "left" {
        case "center": .center
        case "right": .right
        case "justify": .justified
        default: .natural
        }
        return cellTheme
    }

    /// Creates the cell views for the current table (on first layout or a shape change).
    private func rebuild() {
        let focus = focusedCell.map { ($0.row, $0.column) }
        for row in cellViews { for cell in row { cell.removeFromSuperview() } }
        for row in nested { for view in row { view?.removeFromSuperview() } }
        cellViews = []
        nested = []
        for (r, row) in table.rows.enumerated() {
            var rowViews: [CellTextView] = []
            var rowNested: [NSView?] = []
            for (c, cell) in row.cells.enumerated() {
                let view = CellTextView.make()
                view.table = self
                view.row = r
                view.column = c
                view.cellTheme = cellTheme(row: r, column: c)
                view.delegate = self
                view.setAccessibilityLabel("Row \(r + 1), column \(c + 1)")
                load(cell.runs, into: view)
                grid.addSubview(view)
                rowViews.append(view)
                if cell.nestedBlocks.isEmpty {
                    rowNested.append(nil)
                } else {
                    let hosting = NSHostingView(rootView: NestedCellBlocks(blocks: cell.nestedBlocks, context: context, scale: theme.scale))
                    hosting.sizingOptions = []
                    grid.addSubview(hosting)
                    rowNested.append(hosting)
                }
            }
            cellViews.append(rowViews)
            nested.append(rowNested)
        }
        measuredWidth = -1
        if let focus { self.focus(row: min(focus.0, cellViews.count - 1), column: focus.1, atEnd: true) }
    }

    private func load(_ runs: [InlineRun], into view: CellTextView) {
        guard let context, let storage = view.textStorage else { return }
        let text = NSMutableAttributedString(attributedString: DocumentText.inline(runs, info: Self.cellInfo, context: context))
        if text.length == 0 {
            storage.setAttributedString(NSAttributedString())
        } else {
            view.cellTheme.restyle(text, range: NSRange(location: 0, length: text.length))
            storage.setAttributedString(text)
        }
        view.typingAttributes = typingAttributes(for: view, marks: InlineMarks())
    }

    private func typingAttributes(for view: CellTextView, marks: InlineMarks) -> [NSAttributedString.Key: Any] {
        var attributes = view.cellTheme.attributes(info: Self.cellInfo, marks: marks,
                                                   paragraphStyle: view.cellTheme.paragraphStyle(for: Self.cellInfo))
        attributes[.fnBlock] = Self.cellInfo
        if !marks.isPlain { attributes[.fnMarks] = MarksBox(marks) }
        attributes[.link] = nil
        return attributes
    }

    // MARK: Layout

    func height(forWidth width: CGFloat) -> CGFloat {
        layout(width: width)
        return measuredHeight
    }

    /// Lays the grid out for `width` (cached until something changes).
    func layout(width: CGFloat) {
        guard abs(width - measuredWidth) > 0.5 else { return }
        buildIfNeeded()
        measuredWidth = width
        let widths = table.layoutWidths(available: Double(max(80, width - 1)), minimum: Double(56 * theme.scale))
        columnEdges = [0]
        for value in widths { columnEdges.append((columnEdges.last ?? 0) + CGFloat(value).rounded(.down)) }
        textHeights = []
        nestedHeights = []
        for (r, row) in cellViews.enumerated() {
            var texts: [CGFloat] = []
            var nesteds: [CGFloat] = []
            for (c, cell) in row.enumerated() {
                let inner = innerWidth(row: r, column: c)
                texts.append(textHeight(cell, width: inner))
                nesteds.append(nested[r][c] == nil ? 0 : nestedHeight(row: r, column: c, width: inner))
            }
            textHeights.append(texts)
            nestedHeights.append(nesteds)
        }
        updateRowEdges(from: 0)
        placeViews(fromRow: 0)
        grid.needsDisplay = true
    }

    /// Re-measures after an edit; tells the note when the table's height changed.
    private func remeasure() {
        let before = measuredHeight
        let width = measuredWidth
        measuredWidth = -1
        layout(width: width < 0 ? grid.bounds.width : width)
        if abs(before - measuredHeight) > 0.5, let attachment {
            context?.attachmentDidResize?(attachment)
        }
    }

    /// Re-measures one edited cell: its row, and the rows below only if its height changed.
    private func remeasure(row: Int, column: Int) {
        guard measuredWidth > 0, textHeights.indices.contains(row), textHeights[row].indices.contains(column),
              cellViews.indices.contains(row), cellViews[row].indices.contains(column) else { remeasure(); return }
        let before = measuredHeight
        let rowBefore = rowEdges[row + 1] - rowEdges[row]
        textHeights[row][column] = textHeight(cellViews[row][column], width: innerWidth(row: row, column: column))
        if abs(rowHeight(row) - rowBefore) > 0.5 {
            updateRowEdges(from: row)
            placeViews(fromRow: row)
            grid.needsDisplay = true
        } else {
            placeViews(row: row)
        }
        if abs(before - measuredHeight) > 0.5, let attachment {
            context?.attachmentDidResize?(attachment)
        }
    }

    private func innerWidth(row: Int, column: Int) -> CGFloat {
        max(20, horizontalSpan(row: row, column: column).width - 2 * padding.width)
    }

    private func rowHeight(_ row: Int) -> CGFloat {
        var height = theme.lineHeight(for: .paragraph) * 0.875 + 2 * padding.height
        for column in textHeights[row].indices {
            var cell = textHeights[row][column]
            if nested[row][column] != nil { cell += 4 * theme.scale + nestedHeights[row][column] }
            height = max(height, ceil(cell) + 2 * padding.height)
        }
        return height
    }

    /// Row edges from `row` down, from the measured cell heights.
    private func updateRowEdges(from row: Int) {
        if row <= 0 || rowEdges.count <= row {
            rowEdges = [0]
        } else {
            rowEdges.removeSubrange((row + 1)...)
        }
        for r in (rowEdges.count - 1)..<textHeights.count {
            rowEdges.append((rowEdges.last ?? 0) + rowHeight(r))
        }
        measuredHeight = (rowEdges.last ?? 0) + 2
    }

    /// The read-only nested blocks' height at `width`.
    private func nestedHeight(row: Int, column: Int, width: CGFloat) -> CGFloat {
        guard let cell = table.cell(row: row, column: column), !cell.nestedBlocks.isEmpty else { return 0 }
        let controller = NSHostingController(rootView: NestedCellBlocks(blocks: cell.nestedBlocks, context: context, scale: theme.scale))
        return ceil(controller.sizeThatFits(in: CGSize(width: width, height: 10_000)).height)
    }

    private func textHeight(_ view: CellTextView, width: CGFloat) -> CGFloat {
        let lineHeight = view.cellTheme.lineHeight(for: .paragraph)
        guard (view.textStorage?.length ?? 0) > 0 else {
            if abs(view.frame.width - width) > 0.5 { view.setFrameSize(NSSize(width: width, height: lineHeight)) }
            return lineHeight
        }
        return max(lineHeight, view.fittingTextHeight(width: width))
    }

    /// The cell's rectangle (spans counted from the cells before it in the row).
    func cellFrame(row: Int, column: Int) -> CGRect {
        guard rowEdges.count > row + 1 else { return .zero }
        let span = horizontalSpan(row: row, column: column)
        return CGRect(x: span.x, y: rowEdges[row], width: span.width, height: rowEdges[row + 1] - rowEdges[row])
    }

    /// Where a cell sits across the table (known before row heights are).
    func horizontalSpan(row: Int, column: Int) -> (x: CGFloat, width: CGFloat) {
        guard table.rows.indices.contains(row) else { return (0, 0) }
        let cellsInRow = table.rows[row].cells
        let start = cellsInRow.prefix(column).reduce(0) { $0 + $1.colspan }
        let span = column < cellsInRow.count ? cellsInRow[column].colspan : 1
        let lastEdge = columnEdges.count - 1
        guard lastEdge > 0 else { return (0, 0) }
        let x0 = columnEdges[min(start, lastEdge)]
        // The last cell of a short row reaches the table's right edge.
        let end = column == cellsInRow.count - 1 ? lastEdge : min(start + span, lastEdge)
        return (x0, max(0, columnEdges[end] - x0))
    }

    private func placeViews(fromRow start: Int) {
        guard start < cellViews.count else { return }
        for row in start..<cellViews.count { placeViews(row: row) }
    }

    /// Frames for one row's cell views, from the measured heights (no measuring here).
    private func placeViews(row r: Int) {
        guard textHeights.indices.contains(r) else { return }
        for (c, cell) in cellViews[r].enumerated() where textHeights[r].indices.contains(c) {
            let frame = cellFrame(row: r, column: c)
            let inner = frame.insetBy(dx: padding.width, dy: padding.height)
            let width = max(20, inner.width)
            let textFrame = CGRect(x: inner.minX, y: inner.minY, width: width, height: textHeights[r][c])
            if cell.frame != textFrame { cell.frame = textFrame }
            if let extra = nested[r][c] {
                extra.frame = CGRect(x: inner.minX, y: inner.minY + textHeights[r][c] + 4 * theme.scale, width: width,
                                     height: nestedHeights[r][c])
            }
        }
    }


    // MARK: Editing (NSTextViewDelegate)

    func textDidChange(_ notification: Notification) {
        guard let view = notification.object as? CellTextView, let storage = view.textStorage else { return }
        let full = NSRange(location: 0, length: storage.length)
        if storage.length > 0 {
            // Pasted rich text becomes marks; presentation follows the marks.
            storage.beginEditing()
            view.cellTheme.restyle(storage, range: full)
            storage.endEditing()
        }
        table.setRuns(DocumentText.runs(in: storage, range: full), row: view.row, column: view.column)
        remeasure(row: view.row, column: view.column)
        context?.contentDidChange?()
    }

    func textViewDidChangeSelection(_ notification: Notification) {
        guard let view = notification.object as? CellTextView, let storage = view.textStorage else { return }
        let location = view.selectedRange().location
        guard view.selectedRange().length == 0 else { return }
        var marks = location > 0 && location <= storage.length ? storage.marks(at: location - 1) : InlineMarks()
        if marks.link != nil, location >= storage.length || storage.marks(at: location).link != marks.link { marks.link = nil }
        view.typingAttributes = typingAttributes(for: view, marks: marks)
    }

    func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        guard let cell = textView as? CellTextView else { return false }
        switch selector {
        case #selector(NSResponder.insertTab(_:)):
            move(from: cell, by: 1)
        case #selector(NSResponder.insertBacktab(_:)):
            move(from: cell, by: -1)
        case #selector(NSResponder.insertNewline(_:)), #selector(NSResponder.insertLineBreak(_:)),
             #selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)):
            // A line break inside the cell (a cell holds one paragraph).
            textView.insertText(softBreak, replacementRange: textView.selectedRange())
        case #selector(NSResponder.moveUp(_:)):
            guard cell.caretOnFirstLine else { return false }
            moveVertically(from: cell, by: -1)
        case #selector(NSResponder.moveDown(_:)):
            guard cell.caretOnLastLine else { return false }
            moveVertically(from: cell, by: 1)
        case #selector(NSResponder.cancelOperation(_:)):
            leave(forward: true)
        default:
            return false
        }
        return true
    }

    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        let href = (link as? URL)?.absoluteString ?? (link as? String) ?? ""
        if let target = InlineMarkdown.documentLinkTarget(href) {
            context?.openNote?(target.id)
            return true
        }
        if let url = context?.url(for: href) {
            context?.openURL?(url)
            return true
        }
        return false
    }

    func textView(_ view: NSTextView, menu: NSMenu, for event: NSEvent, at charIndex: Int) -> NSMenu? {
        guard let cell = view as? CellTextView else { return menu }
        addTableItems(to: menu, row: cell.row, column: cell.column)
        return menu
    }

    // MARK: Moving between cells

    func focus(atEnd: Bool) {
        if atEnd, let row = cells.indices.last, let column = cells[row].indices.last {
            focus(row: row, column: column, atEnd: true)
        } else {
            focus(row: 0, column: 0, atEnd: false)
        }
    }

    func focus(row: Int, column: Int, atEnd: Bool) {
        guard cells.indices.contains(row), !cells[row].isEmpty else { return }
        let cell = cells[row][min(max(0, column), cells[row].count - 1)]
        cell.window?.makeFirstResponder(cell)
        let length = cell.textStorage?.length ?? 0
        cell.setSelectedRange(atEnd ? NSRange(location: length, length: 0) : NSRange(location: 0, length: length))
    }

    private func move(from cell: CellTextView, by step: Int) {
        let flat = cells.flatMap { $0 }
        guard let index = flat.firstIndex(where: { $0 === cell }) else { return }
        let target = index + step
        if target >= flat.count {
            // Tab in the last cell adds a row, like Numbers and Pages.
            perform("Add Row") { $0.insertRow(at: $0.rows.count); return true }
            focus(row: cells.count - 1, column: 0, atEnd: false)
        } else if target < 0 {
            leave(forward: false)
        } else {
            let next = flat[target]
            focus(row: next.row, column: next.column, atEnd: false)
        }
    }

    private func moveVertically(from cell: CellTextView, by step: Int) {
        let row = cell.row + step
        if row < 0 { leave(forward: false); return }
        if row >= cells.count { leave(forward: true); return }
        focus(row: row, column: cell.column, atEnd: step < 0)
    }

    func leave(forward: Bool) {
        guard let attachment else { return }
        context?.leaveAttachment?(attachment, forward)
    }

    func cellDidFocus(_ cell: CellTextView?) {
        focusedCell = cell
        grid.needsDisplay = true
    }

    /// The cell under a point in grid coordinates (for clicks between cells).
    func cell(at point: CGPoint) -> CellTextView? {
        for (r, row) in cells.enumerated() {
            for (c, cell) in row.enumerated() where cellFrame(row: r, column: c).contains(point) {
                return cell
            }
        }
        return nil
    }

    // MARK: Rows and columns

    func addTableItems(to menu: NSMenu, row: Int, column: Int) {
        let items: [(String, Selector)] = [
            ("Insert Row Above", #selector(insertRowAbove(_:))),
            ("Insert Row Below", #selector(insertRowBelow(_:))),
            ("Insert Column Left", #selector(insertColumnLeft(_:))),
            ("Insert Column Right", #selector(insertColumnRight(_:))),
        ]
        var index = 0
        func add(_ item: NSMenuItem) {
            menu.insertItem(item, at: index)
            index += 1
        }
        for (title, action) in items {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            item.representedObject = [row, column]
            add(item)
        }
        add(.separator())
        for (title, action, enabled) in [
            ("Delete Row", #selector(deleteRow(_:)), table.rows.count > 1),
            ("Delete Column", #selector(deleteColumn(_:)), table.rows.allSatisfy { $0.cells.count > 1 }),
        ] {
            let item = NSMenuItem(title: title, action: enabled ? action : nil, keyEquivalent: "")
            item.target = self
            item.representedObject = [row, column]
            item.isEnabled = enabled
            add(item)
        }
        add(.separator())
        let header = NSMenuItem(title: "Header Row", action: #selector(toggleHeaderRow(_:)), keyEquivalent: "")
        header.target = self
        header.state = table.headerRows > 0 ? .on : .off
        add(header)
        add(.separator())
    }

    private func position(_ sender: Any?) -> (row: Int, column: Int) {
        let values = ((sender as? NSMenuItem)?.representedObject as? [Int]) ?? [focusedCell?.row ?? 0, focusedCell?.column ?? 0]
        return (values.first ?? 0, values.count > 1 ? values[1] : 0)
    }

    @objc func insertRowAbove(_ sender: Any?) {
        let at = position(sender)
        perform("Insert Row") { $0.insertRow(at: at.row); return true }
        focus(row: at.row, column: at.column, atEnd: false)
    }

    @objc func insertRowBelow(_ sender: Any?) {
        let at = position(sender)
        perform("Insert Row") { $0.insertRow(at: at.row + 1); return true }
        focus(row: at.row + 1, column: at.column, atEnd: false)
    }

    @objc func insertColumnLeft(_ sender: Any?) {
        let at = position(sender)
        perform("Insert Column") { $0.insertColumn(at: at.column); return true }
        focus(row: at.row, column: at.column, atEnd: false)
    }

    @objc func insertColumnRight(_ sender: Any?) {
        let at = position(sender)
        perform("Insert Column") { $0.insertColumn(at: at.column + 1); return true }
        focus(row: at.row, column: at.column + 1, atEnd: false)
    }

    @objc func deleteRow(_ sender: Any?) {
        let at = position(sender)
        perform("Delete Row") { $0.removeRow(at: at.row) }
        focus(row: min(at.row, table.rows.count - 1), column: at.column, atEnd: true)
    }

    @objc func deleteColumn(_ sender: Any?) {
        let at = position(sender)
        perform("Delete Column") { $0.removeColumn(at: at.column) }
        focus(row: at.row, column: max(0, at.column - 1), atEnd: true)
    }

    @objc func toggleHeaderRow(_ sender: Any?) {
        perform("Header Row") { $0.toggleHeaderRow(); return true }
    }

    /// A shape change as one undoable step.
    func perform(_ name: String, _ change: (inout TableContent) -> Bool) {
        var updated = table
        guard change(&updated) else { NSSound.beep(); return }
        replaceTable(with: updated, actionName: name)
    }

    private func replaceTable(with updated: TableContent, actionName: String) {
        let previous = table
        let undoManager = grid.window?.undoManager
        table = updated
        focusedCell = nil
        isBuilt = true
        rebuild()
        remeasureAfterShapeChange()
        undoManager?.registerUndo(withTarget: self) { target in
            MainActor.assumeIsolated { target.replaceTable(with: previous, actionName: actionName) }
        }
        undoManager?.setActionName(actionName)
    }

    private func remeasureAfterShapeChange() {
        let width = grid.bounds.width > 0 ? grid.bounds.width : 600
        measuredWidth = -1
        layout(width: width)
        if let attachment { context?.attachmentDidResize?(attachment) }
        context?.contentDidChange?()
    }

    // MARK: Marks in cells

    func toggleMark(_ keyPath: WritableKeyPath<InlineMarks, Bool>, in view: CellTextView) {
        guard let storage = view.textStorage else { return }
        let selection = view.selectedRange()
        if selection.length == 0 {
            var marks = (view.typingAttributes[.fnMarks] as? MarksBox)?.marks ?? InlineMarks()
            marks[keyPath: keyPath].toggle()
            if keyPath == \InlineMarks.sup, marks.sup { marks.sub = false }
            if keyPath == \InlineMarks.sub, marks.sub { marks.sup = false }
            view.typingAttributes = typingAttributes(for: view, marks: marks)
            return
        }
        var allOn = true
        storage.enumerateAttribute(.fnMarks, in: selection, options: []) { value, _, stop in
            if (value as? MarksBox)?.marks[keyPath: keyPath] != true { allOn = false; stop.pointee = true }
        }
        guard view.shouldChangeText(in: selection, replacementString: nil) else { return }
        storage.beginEditing()
        storage.enumerateAttribute(.fnMarks, in: selection, options: []) { value, run, _ in
            var marks = (value as? MarksBox)?.marks ?? InlineMarks()
            marks[keyPath: keyPath] = !allOn
            if !allOn, keyPath == \InlineMarks.sup { marks.sub = false }
            if !allOn, keyPath == \InlineMarks.sub { marks.sup = false }
            if marks.isPlain {
                storage.removeAttribute(.fnMarks, range: run)
            } else {
                storage.addAttribute(.fnMarks, value: MarksBox(marks), range: run)
            }
        }
        storage.endEditing()
        view.didChangeText()
        view.setSelectedRange(selection)
    }

    func handleKeyEquivalent(_ event: NSEvent, in view: CellTextView) -> Bool {
        guard !view.hasMarkedText() else { return false }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask).subtracting([.capsLock, .numericPad, .function])
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        switch (flags, key) {
        case ([.command], "b"): toggleMark(\.bold, in: view)
        case ([.command], "i"): toggleMark(\.italic, in: view)
        case ([.command], "u"): toggleMark(\.underline, in: view)
        case ([.command], "e"): toggleMark(\.code, in: view)
        case ([.command, .shift], "x"): toggleMark(\.strike, in: view)
        case ([.command, .control], "="), ([.command, .control], "+"): toggleMark(\.sup, in: view)
        case ([.command, .control], "-"): toggleMark(\.sub, in: view)
        default: return false
        }
        return true
    }
}

/// One cell's text: a TextKit 2 text view without chrome; the table controller is its
/// delegate.
final class CellTextView: NSTextView {
    weak var table: TableBlockController?
    var row = 0
    var column = 0
    var cellTheme = EditorTheme()

    static func make() -> CellTextView {
        let view = CellTextView(usingTextLayoutManager: true)
        view.isRichText = true
        view.importsGraphics = false
        view.allowsUndo = true
        view.drawsBackground = false
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        view.textContainerInset = .zero
        view.textContainer?.lineFragmentPadding = 0
        view.textContainer?.widthTracksTextView = true
        view.isAutomaticQuoteSubstitutionEnabled = true
        view.isAutomaticDashSubstitutionEnabled = true
        view.isAutomaticLinkDetectionEnabled = false
        view.isContinuousSpellCheckingEnabled = true
        view.focusRingType = .none
        view.displaysLinkToolTips = true
        view.linkTextAttributes = [.foregroundColor: NSColor.controlAccentColor, .cursor: NSCursor.pointingHand]
        return view
    }

    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted { table?.cellDidFocus(self) }
        return accepted
    }

    override func resignFirstResponder() -> Bool {
        let accepted = super.resignFirstResponder()
        if accepted, table?.focusedCell === self { table?.cellDidFocus(nil) }
        return accepted
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if window?.firstResponder === self, let table, table.handleKeyEquivalent(event, in: self) { return true }
        return super.performKeyEquivalent(with: event)
    }

    /// Whether the caret is on the first (last) line, so ↑ (↓) leaves the cell.
    var caretOnFirstLine: Bool { caretLine(atEnd: false) }
    var caretOnLastLine: Bool { caretLine(atEnd: true) }

    private func caretLine(atEnd: Bool) -> Bool {
        let length = textStorage?.length ?? 0
        guard length > 0 else { return true }
        let caret = firstRect(forCharacterRange: NSRange(location: selectedRange().location, length: 0), actualRange: nil)
        let edge = firstRect(forCharacterRange: NSRange(location: atEnd ? length : 0, length: 0), actualRange: nil)
        return abs(caret.midY - edge.midY) < 2
    }

    @objc func fortressToggleBold(_ sender: Any?) { table?.toggleMark(\.bold, in: self) }
    @objc func fortressToggleItalic(_ sender: Any?) { table?.toggleMark(\.italic, in: self) }
    @objc func fortressToggleUnderline(_ sender: Any?) { table?.toggleMark(\.underline, in: self) }
    @objc func fortressToggleStrikethrough(_ sender: Any?) { table?.toggleMark(\.strike, in: self) }
    @objc func fortressToggleInlineCode(_ sender: Any?) { table?.toggleMark(\.code, in: self) }
    @objc func fortressToggleSuperscript(_ sender: Any?) { table?.toggleMark(\.sup, in: self) }
    @objc func fortressToggleSubscript(_ sender: Any?) { table?.toggleMark(\.sub, in: self) }
    @objc override func underline(_ sender: Any?) { table?.toggleMark(\.underline, in: self) }
}

/// Draws the table: cell backgrounds, the header tint, and borders (when the table has
/// them; borderless tables show faint guides only while a cell is being edited).
final class TableGridView: NSView {
    weak var controller: TableBlockController?

    override var isFlipped: Bool { true }

    override func layout() {
        super.layout()
        controller?.layout(width: bounds.width)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let controller, controller.tableWidth > 0 else { return }
        let table = controller.table
        let outline = CGRect(x: 0.5, y: 0.5, width: controller.tableWidth - 1, height: (controller.rowEdges.last ?? 0) - 1)
        let radius: CGFloat = 6
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(roundedRect: outline, xRadius: radius, yRadius: radius).addClip()
        for (r, row) in table.rows.enumerated() {
            for (c, cell) in row.cells.enumerated() {
                let frame = controller.cellFrame(row: r, column: c)
                if let name = cell.backgroundColor, let color = EditorTheme.cellBackground(named: name) {
                    color.setFill()
                    frame.fill()
                } else if controller.isHeader(row: r, column: c) {
                    NSColor.labelColor.withAlphaComponent(0.045).setFill()
                    frame.fill()
                }
            }
        }
        NSGraphicsContext.restoreGraphicsState()

        let editing = controller.focusedCell != nil
        guard controller.borders || editing else { return }
        let line = controller.borders ? NSColor.separatorColor : NSColor.tertiaryLabelColor.withAlphaComponent(0.25)
        line.setStroke()
        let grid = NSBezierPath()
        grid.lineWidth = 1
        if !controller.borders { grid.setLineDash([3, 3], count: 2, phase: 0) }
        for (r, row) in table.rows.enumerated() {
            for c in row.cells.indices {
                let frame = controller.cellFrame(row: r, column: c)
                if c > 0 {
                    grid.move(to: CGPoint(x: frame.minX + 0.5, y: frame.minY))
                    grid.line(to: CGPoint(x: frame.minX + 0.5, y: frame.maxY))
                }
            }
            if r > 0 {
                let y = controller.rowEdges[r] + 0.5
                grid.move(to: CGPoint(x: 0, y: y))
                grid.line(to: CGPoint(x: controller.tableWidth, y: y))
            }
        }
        grid.stroke()
        let border = NSBezierPath(roundedRect: outline, xRadius: radius, yRadius: radius)
        border.lineWidth = 1
        if !controller.borders { border.setLineDash([3, 3], count: 2, phase: 0) }
        border.stroke()
    }

    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        if let cell = controller?.cell(at: point) {
            controller?.focus(row: cell.row, column: cell.column, atEnd: true)
        } else {
            super.mouseDown(with: event)
        }
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        let point = convert(event.locationInWindow, from: nil)
        guard let controller, let cell = controller.cell(at: point) else { return super.menu(for: event) }
        let menu = NSMenu()
        controller.addTableItems(to: menu, row: cell.row, column: cell.column)
        return menu
    }

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .table }
    override func accessibilityLabel() -> String? { "Table" }
}

/// What a OneNote cell holds besides its first line: list items, more paragraphs,
/// pictures, nested tables, math. Read-only; saved untouched.
struct NestedCellBlocks: View {
    let blocks: [JSONValue]
    let context: AttachmentContext?
    let scale: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 3 * scale) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                row(block)
            }
        }
        .font(.system(size: 13 * scale))
        .frame(maxWidth: .infinity, alignment: .leading)
        .help("Shown as imported")
    }

    @ViewBuilder private func row(_ block: JSONValue) -> some View {
        let type = block["type"]?.stringValue ?? "paragraph"
        let runs = block["content"]?["blocknote"]
        switch type {
        case "image":
            if let url = block["attrs"]?["url"]?.stringValue.flatMap({ context?.url(for: $0) }), let image = context?.image(for: url) {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fit)
                    .frame(maxWidth: 160 * scale, maxHeight: 120 * scale, alignment: .leading)
                    .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    .accessibilityLabel(block["attrs"]?["alt"]?.stringValue ?? "Image")
            } else {
                Label(block["attrs"]?["alt"]?.stringValue ?? "Image", systemImage: "photo")
                    .foregroundStyle(.secondary).lineLimit(2)
            }
        case "table":
            Label(TableContent(blockNote: runs).map { "Table · \($0.rows.count) rows" } ?? "Table", systemImage: "tablecells")
                .foregroundStyle(.secondary)
                .help(TableContent(blockNote: runs)?.plainText ?? "")
        case "math":
            Text(MathText.attributed(block["content"]?["latex"]?.stringValue ?? "", size: 14 * scale))
        case "file":
            Label(block["attrs"]?["name"]?.stringValue ?? "File", systemImage: "doc")
                .foregroundStyle(.secondary)
        case "thematic_break", "divider":
            Divider()
        default:
            let text = runs.map(BlockCodec.inlineText) ?? block["text"]?.stringValue ?? ""
            HStack(alignment: .firstTextBaseline, spacing: 6 * scale) {
                if let marker = marker(for: block) {
                    Text(marker).foregroundStyle(.secondary)
                }
                Text(text)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func marker(for block: JSONValue) -> String? {
        let type = block["type"]?.stringValue ?? ""
        let attrs = block["attrs"]
        if type == "checkListItem" || (type == "list" && attrs?["checked"]?.boolValue != nil) {
            return attrs?["checked"]?.boolValue == true ? "☑" : "☐"
        }
        if type == "numberedListItem" || (type == "list" && attrs?["ordered"]?.boolValue == true) { return "–" }
        if type == "bulletListItem" || type == "list" { return "•" }
        return nil
    }
}

extension NSTextView {
    /// The height of all the text laid out at `width` (TextKit 2). Measured from the
    /// layout fragments: `usageBoundsForTextContainer` only grows, so it can't be used
    /// after the width changed.
    func fittingTextHeight(width: CGFloat) -> CGFloat {
        var changed = false
        if abs(frame.width - width) > 0.5 {
            setFrameSize(NSSize(width: width, height: max(frame.height, 10)))
            changed = true
        }
        guard let manager = textLayoutManager, let container = manager.textContainer else { return 0 }
        let inset = textContainerInset
        let containerWidth = max(1, width - 2 * inset.width)
        if abs(container.size.width - containerWidth) > 0.5 {
            container.size = NSSize(width: containerWidth, height: CGFloat.greatestFiniteMagnitude)
            changed = true
        }
        // The container follows the view's width, but laid-out fragments keep the old
        // width until they are invalidated.
        if changed { manager.invalidateLayout(for: manager.documentRange) }
        var bottom: CGFloat = 0
        manager.enumerateTextLayoutFragments(from: manager.documentRange.location, options: [.ensuresLayout]) { fragment in
            bottom = max(bottom, fragment.layoutFragmentFrame.maxY)
            return true
        }
        return ceil(bottom)
    }
}
