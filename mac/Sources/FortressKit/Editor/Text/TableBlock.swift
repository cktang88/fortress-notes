import AppKit
import CoreText
import SwiftUI

/// An editable table inside the note. Cells are drawn by the grid from their styled
/// text (no view per cell); a cell gets a text view only while it is being edited, made
/// when it is clicked or reached with the keyboard. A 50 × 8 table is one view that
/// draws 400 strings, not 400 text views to lay out when it scrolls into sight.
///
/// Tab and ⇧Tab move between cells (Tab in the last cell adds a row), ↑/↓ at a cell's
/// edge move to the cell above or below, Esc leaves the table. Rows and columns are
/// added and removed from the context menu, as one undo step each. Edits of a cell's
/// text are undone on the table too (a step restores the table as it was, see
/// `registerCellUndo`), so they stay on the note's stack in order with everything else
/// however often the cell's editor comes and goes. Nested cell blocks
/// (OneNote lists and pictures in cells) show read-only under the cell's text and are
/// saved untouched. Accessibility sees rows of cells (`TableGridView`).
@MainActor
final class TableBlockController: NSObject, LiveBlock, NSTextViewDelegate, NSTextStorageDelegate {
    private(set) var table: TableContent
    let attrs: [String: JSONValue]
    weak var context: AttachmentContext?
    weak var attachment: BlockAttachment?
    let grid = TableGridView()
    var view: NSView { grid }

    /// Each cell's text, styled as its editor shows it (and as the grid draws it). Made
    /// the first time the table is laid out or entered, not when the note opens.
    private var texts: [[NSAttributedString]] = []
    /// What the grid draws for each cell: its text with links in the link colour.
    private var drawn: [[NSAttributedString]] = []
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
    /// The cell holding keyboard focus (its editor is the first responder).
    private(set) var focusedCell: CellTextView?
    /// The one cell editor in the grid, if a cell is being edited.
    private(set) var editor: CellTextView?

    struct CellPosition: Hashable {
        var row: Int
        var column: Int
    }

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
    var borders: Bool { table.extra["borders"]?.boolValue ?? attrs["borders"]?.boolValue ?? true }
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

    /// A cell's styled text (what its editor starts from).
    func text(row: Int, column: Int) -> NSAttributedString {
        buildIfNeeded()
        guard texts.indices.contains(row), texts[row].indices.contains(column) else { return NSAttributedString() }
        return texts[row][column]
    }

    /// Makes every cell's text for the current table (on first layout or a shape change).
    private func rebuild() {
        let focus = focusedCell.map { ($0.row, $0.column) }
        dropEditors()
        for row in nested { for view in row { view?.removeFromSuperview() } }
        texts = []
        drawn = []
        nested = []
        for (r, row) in table.rows.enumerated() {
            var rowTexts: [NSAttributedString] = []
            var rowNested: [NSView?] = []
            for (c, cell) in row.cells.enumerated() {
                rowTexts.append(styledText(cell.runs, theme: cellTheme(row: r, column: c)))
                if cell.nested.isEmpty {
                    rowNested.append(nil)
                } else {
                    let hosting = NSHostingView(rootView: NestedCellBlocks(blocks: cell.nested, context: context, scale: theme.scale))
                    hosting.sizingOptions = []
                    grid.addSubview(hosting)
                    rowNested.append(hosting)
                }
            }
            texts.append(rowTexts)
            drawn.append(rowTexts.map(Self.drawable))
            nested.append(rowNested)
        }
        measuredWidth = -1
        grid.cellsDidChange()
        if let focus, !texts.isEmpty { self.focus(row: min(focus.0, texts.count - 1), column: focus.1, atEnd: true) }
    }

    private func styledText(_ runs: [InlineRun], theme: EditorTheme) -> NSAttributedString {
        guard let context else { return NSAttributedString() }
        let text = NSMutableAttributedString(attributedString: DocumentText.inline(runs, info: Self.cellInfo, context: context))
        if text.length > 0 { theme.restyle(text, range: NSRange(location: 0, length: text.length)) }
        return text
    }

    /// The text as the grid draws it: links in the link colour (a text view does that
    /// with its link attributes; string drawing doesn't).
    private static func drawable(_ text: NSAttributedString) -> NSAttributedString {
        var hasLinks = false
        text.enumerateAttribute(.link, in: NSRange(location: 0, length: text.length), options: []) { value, _, stop in
            if value != nil { hasLinks = true; stop.pointee = true }
        }
        guard hasLinks else { return text }
        let copy = NSMutableAttributedString(attributedString: text)
        text.enumerateAttribute(.link, in: NSRange(location: 0, length: text.length), options: []) { value, range, _ in
            if value != nil { copy.addAttribute(.foregroundColor, value: NSColor.themeLink, range: range) }
        }
        return copy
    }

    private func typingAttributes(for view: CellTextView, marks: InlineMarks) -> [NSAttributedString.Key: Any] {
        var attributes = view.cellTheme.attributes(info: Self.cellInfo, marks: marks,
                                                   paragraphStyle: view.cellTheme.paragraphStyle(for: Self.cellInfo))
        attributes[.fnBlock] = Self.cellInfo
        if !marks.isPlain { attributes[.fnMarks] = MarksBox(marks) }
        attributes[.link] = nil
        return attributes
    }

    // MARK: The cell editor

    /// The text view editing a cell, made (or brought back) for it now. Only one cell
    /// has one at a time; the others are drawn.
    @discardableResult
    func editor(row: Int, column: Int) -> CellTextView? {
        buildIfNeeded()
        guard texts.indices.contains(row), texts[row].indices.contains(column) else { return nil }
        if let editor, editor.row == row, editor.column == column { return editor }
        retireEditor()
        let position = CellPosition(row: row, column: column)
        let view = makeEditor(row: position.row, column: position.column)
        editor = view
        view.isHidden = false
        if view.superview !== grid { grid.addSubview(view) }
        placeEditor()
        grid.setNeedsDisplay(cellFrame(row: row, column: column))
        return view
    }

    private func makeEditor(row: Int, column: Int) -> CellTextView {
        let view = CellTextView.make()
        view.table = self
        view.row = row
        view.column = column
        view.cellTheme = cellTheme(row: row, column: column)
        view.delegate = self
        view.setAccessibilityLabel(String(localized: "Row \(row + 1), column \(column + 1)"))
        isLoadingCell = true
        view.textStorage?.setAttributedString(texts[row][column])
        isLoadingCell = false
        view.textStorage?.delegate = self
        view.typingAttributes = typingAttributes(for: view, marks: InlineMarks())
        return view
    }

    /// Takes the editor out of the grid (the cell is drawn again). Its edits are undone
    /// on the table, so nothing on the undo stack refers to it.
    private func retireEditor() {
        guard let view = editor else { return }
        editor = nil
        typingRun = nil
        if focusedCell === view { focusedCell = nil }
        view.removeFromSuperview()
        grid.setNeedsDisplay(cellFrame(row: view.row, column: view.column))
    }

    /// Forgets the editor (the table's shape changed: its position is stale).
    private func dropEditors() {
        retireEditor()
        focusedCell = nil
    }

    /// The editor lost keyboard focus: once focus has settled elsewhere, the cell goes
    /// back to being drawn.
    func editorDidResign(_ view: CellTextView) {
        BlockEditorController.later { [weak self, weak view] in
            guard let self, let view, self.editor === view, view.window?.firstResponder !== view else { return }
            self.retireEditor()
        }
    }

    private func placeEditor() {
        guard let view = editor, textHeights.indices.contains(view.row), textHeights[view.row].indices.contains(view.column) else { return }
        let frame = textFrame(row: view.row, column: view.column)
        if view.frame != frame { view.frame = frame }
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
        for (r, row) in texts.enumerated() {
            var heights: [CGFloat] = []
            var nesteds: [CGFloat] = []
            for (c, text) in row.enumerated() {
                let inner = innerWidth(row: r, column: c)
                heights.append(textHeight(text, theme: cellTheme(row: r, column: c), width: inner))
                nesteds.append(nested[r][c] == nil ? 0 : nestedHeight(row: r, column: c, width: inner))
            }
            textHeights.append(heights)
            nestedHeights.append(nesteds)
        }
        updateRowEdges(from: 0)
        placeViews(fromRow: 0)
        grid.cellsDidChange()
    }

    /// Re-measures after a shape change; tells the note when the table's height changed.
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
        guard measuredWidth > 0, textHeights.indices.contains(row), textHeights[row].indices.contains(column) else { remeasure(); return }
        let before = measuredHeight
        let rowBefore = rowEdges[row + 1] - rowEdges[row]
        textHeights[row][column] = textHeight(texts[row][column], theme: cellTheme(row: row, column: column),
                                              width: innerWidth(row: row, column: column))
        if abs(rowHeight(row) - rowBefore) > 0.5 {
            updateRowEdges(from: row)
            placeViews(fromRow: row)
            grid.cellsDidChange()
        } else {
            placeViews(row: row)
            grid.setNeedsDisplay(CGRect(x: 0, y: rowEdges[row], width: tableWidth, height: rowEdges[row + 1] - rowEdges[row]))
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
        guard let cell = table.cell(row: row, column: column), !cell.nested.isEmpty else { return 0 }
        let controller = NSHostingController(rootView: NestedCellBlocks(blocks: cell.nested, context: context, scale: theme.scale))
        return ceil(controller.sizeThatFits(in: CGSize(width: width, height: 10_000)).height)
    }

    /// The height of a cell's text at `width`, from the string (no text view). Lines have
    /// a fixed height (min = max), so this is what the cell's editor lays out. A cell
    /// whose text fits on one line (most of them) is measured as one line of glyphs.
    private func textHeight(_ text: NSAttributedString, theme: EditorTheme, width: CGFloat) -> CGFloat {
        let lineHeight = theme.lineHeight(for: .paragraph)
        guard text.length > 0 else { return lineHeight }
        let string = text.nsString
        let multiline = string.rangeOfCharacter(from: Self.lineSeparators).location != NSNotFound
        if !multiline {
            let line = CTLineCreateWithAttributedString(text as CFAttributedString)
            if CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil)) <= width { return lineHeight }
        }
        let bounds = text.boundingRect(with: CGSize(width: width, height: .greatestFiniteMagnitude),
                                       options: [.usesLineFragmentOrigin, .usesFontLeading])
        return max(lineHeight, ceil(bounds.height))
    }

    private static let lineSeparators = CharacterSet(charactersIn: "\n\r\u{2028}\u{2029}")

    /// The cell's rectangle (spans counted from the cells before it in the row).
    func cellFrame(row: Int, column: Int) -> CGRect {
        guard rowEdges.count > row + 1 else { return .zero }
        let span = horizontalSpan(row: row, column: column)
        return CGRect(x: span.x, y: rowEdges[row], width: span.width, height: rowEdges[row + 1] - rowEdges[row])
    }

    /// Where a cell's text sits (the editor's frame when it is edited).
    func textFrame(row: Int, column: Int) -> CGRect {
        guard textHeights.indices.contains(row), textHeights[row].indices.contains(column) else { return .zero }
        let inner = cellFrame(row: row, column: column).insetBy(dx: padding.width, dy: padding.height)
        return CGRect(x: inner.minX, y: inner.minY, width: max(20, inner.width), height: textHeights[row][column])
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
        guard start < textHeights.count else { return }
        for row in start..<textHeights.count { placeViews(row: row) }
    }

    /// Frames for one row's nested views and the editor if it is in that row (no measuring).
    private func placeViews(row r: Int) {
        guard textHeights.indices.contains(r) else { return }
        for c in textHeights[r].indices {
            guard nested.indices.contains(r), nested[r].indices.contains(c), let extra = nested[r][c] else { continue }
            let text = textFrame(row: r, column: c)
            extra.frame = CGRect(x: text.minX, y: text.maxY + 4 * theme.scale, width: text.width, height: nestedHeights[r][c])
        }
        if editor?.row == r { placeEditor() }
    }

    /// Rows whose rectangles meet `rect` (for drawing only what is dirty).
    func rows(in rect: CGRect) -> Range<Int> {
        guard rowEdges.count > 1 else { return 0..<0 }
        let rows = rowEdges.count - 1
        func firstRow(below y: CGFloat) -> Int {
            // The first row whose bottom edge is below y.
            var low = 0, high = rows
            while low < high {
                let mid = (low + high) / 2
                if rowEdges[mid + 1] <= y { low = mid + 1 } else { high = mid }
            }
            return low
        }
        let start = firstRow(below: rect.minY)
        let end = min(rows, firstRow(below: rect.maxY) + 1)
        return start..<max(start, end)
    }

    /// Draws the cells' text in `rect` (the edited cell's editor draws its own).
    func drawTexts(in rect: CGRect) {
        for r in rows(in: rect) where drawn.indices.contains(r) {
            for c in drawn[r].indices {
                if let editor, editor.row == r, editor.column == c { continue }
                let text = drawn[r][c]
                guard text.length > 0 else { continue }
                let frame = textFrame(row: r, column: c)
                guard frame.intersects(rect) else { continue }
                text.draw(with: frame, options: [.usesLineFragmentOrigin, .usesFontLeading], context: nil)
            }
        }
    }

    // MARK: Editing (NSTextStorageDelegate, NSTextViewDelegate)

    private var isLoadingCell = false

    /// The cell editor holding `storage`.
    private func cellView(for storage: NSTextStorage) -> CellTextView? {
        if let editor, editor.textStorage === storage { return editor }
        return nil
    }

    /// Every change of a cell's text (typing, paste, and undo, which doesn't always
    /// tell the delegate `textDidChange`) restyles it and writes it to the table.
    /// (Cell storages are only edited on the main thread: see the note on
    /// `BlockEditorController`'s storage delegate for the `assumeIsolated` pattern.)
    nonisolated func textStorage(_ textStorage: NSTextStorage, willProcessEditing editedMask: NSTextStorageEditActions,
                                 range editedRange: NSRange, changeInLength delta: Int) {
        nonisolated(unsafe) let storage = textStorage
        MainActor.assumeIsolated {
            guard !isLoadingCell, storage.length > 0,
                  let view = cellView(for: storage) else { return }
            // Pasted rich text becomes marks; presentation follows the marks.
            view.cellTheme.restyle(storage, range: NSRange(location: 0, length: storage.length))
        }
    }

    nonisolated func textStorage(_ textStorage: NSTextStorage, didProcessEditing editedMask: NSTextStorageEditActions,
                                 range editedRange: NSRange, changeInLength delta: Int) {
        nonisolated(unsafe) let storage = textStorage
        MainActor.assumeIsolated {
            guard !isLoadingCell, let view = cellView(for: storage) else { return }
            cellTextDidChange(view)
        }
    }

    private func cellTextDidChange(_ view: CellTextView) {
        guard let storage = view.textStorage else { return }
        let full = NSRange(location: 0, length: storage.length)
        table.setRuns(DocumentText.runs(in: storage, range: full), row: view.row, column: view.column)
        if texts.indices.contains(view.row), texts[view.row].indices.contains(view.column) {
            let copy = NSAttributedString(attributedString: storage)
            texts[view.row][view.column] = copy
            drawn[view.row][view.column] = Self.drawable(copy)
        }
        remeasure(row: view.row, column: view.column)
        context?.contentDidChange?()
    }

    func textViewDidChangeSelection(_ notification: Notification) {
        guard let view = notification.object as? CellTextView, let storage = view.textStorage else { return }
        // The caret moved by a click or an arrow key (not by an edit just made): the next
        // typing is a new step.
        if !isEditingCell { typingRun = nil }
        let location = view.selectedRange().location
        guard view.selectedRange().length == 0 else { return }
        var marks = location > 0 && location <= storage.length ? storage.marks(at: location - 1) : InlineMarks()
        if marks.link != nil, location >= storage.length || storage.marks(at: location).link != marks.link { marks.link = nil }
        view.typingAttributes = typingAttributes(for: view, marks: marks)
    }

    func textView(_ textView: NSTextView, willChangeSelectionFromCharacterRange old: NSRange, toCharacterRange new: NSRange) -> NSRange {
        guard let storage = textView.textStorage else { return new }
        return InlineNodes.snappedSelection(new, from: old, in: storage)
    }

    func textView(_ textView: NSTextView, shouldChangeTextIn range: NSRange, replacementString: String?) -> Bool {
        if let cell = textView as? CellTextView {
            if !isEditingCell {
                // The caret moves after the edit, within this run loop pass.
                isEditingCell = true
                BlockEditorController.later { [weak self] in self?.isEditingCell = false }
            }
            registerCellUndo(for: cell, replacing: replacementString)
        }
        guard let storage = textView.textStorage, let replacementString, !textView.hasMarkedText() else { return true }
        // A note link or block reference is changed whole, never inside its label.
        let whole = InlineNodes.wholeNodes(range, in: storage)
        guard whole != range else { return true }
        let replacement = NSAttributedString(string: replacementString, attributes: textView.typingAttributes)
        if textView.shouldChangeText(in: whole, replacementString: replacementString) {
            storage.replaceCharacters(in: whole, with: replacement)
            textView.didChangeText()
            textView.setSelectedRange(NSRange(location: whole.location + replacement.length, length: 0))
        }
        return false
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
        if let id = NoteLinks.documentId(from: link) {
            context?.openNote?(id)
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
        if atEnd, let row = table.rows.indices.last, let column = table.rows[row].cells.indices.last {
            focus(row: row, column: column, atEnd: true)
        } else {
            focus(row: 0, column: 0, atEnd: false)
        }
    }

    func focus(row: Int, column: Int, atEnd: Bool) {
        guard table.rows.indices.contains(row), !table.rows[row].cells.isEmpty,
              let cell = editor(row: row, column: min(max(0, column), table.rows[row].cells.count - 1)) else { return }
        cell.window?.makeFirstResponder(cell)
        let length = cell.textStorage?.length ?? 0
        cell.setSelectedRange(atEnd ? NSRange(location: length, length: 0) : NSRange(location: 0, length: length))
    }

    private var positions: [CellPosition] {
        table.rows.enumerated().flatMap { r, row in row.cells.indices.map { CellPosition(row: r, column: $0) } }
    }

    private func move(from cell: CellTextView, by step: Int) {
        let flat = positions
        guard let index = flat.firstIndex(of: CellPosition(row: cell.row, column: cell.column)) else { return }
        let target = index + step
        if target >= flat.count {
            // Tab in the last cell adds a row, like Numbers and Pages.
            perform("Add Row") { $0.insertRow(at: $0.rows.count); return true }
            focus(row: table.rows.count - 1, column: 0, atEnd: false)
        } else if target < 0 {
            leave(forward: false)
        } else {
            focus(row: flat[target].row, column: flat[target].column, atEnd: false)
        }
    }

    private func moveVertically(from cell: CellTextView, by step: Int) {
        let row = cell.row + step
        if row < 0 { leave(forward: false); return }
        if row >= table.rows.count { leave(forward: true); return }
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
    func cell(at point: CGPoint) -> CellPosition? {
        for r in rows(in: CGRect(x: point.x, y: point.y, width: 1, height: 1)) where table.rows.indices.contains(r) {
            for c in table.rows[r].cells.indices where cellFrame(row: r, column: c).contains(point) {
                return CellPosition(row: r, column: c)
            }
        }
        return nil
    }

    /// A click in a drawn cell: the cell gets its editor, which takes the click (caret
    /// placement, a drag selection, a link).
    func click(_ event: NSEvent, at point: CGPoint) -> Bool {
        guard let position = cell(at: point), let view = editor(row: position.row, column: position.column) else { return false }
        view.window?.makeFirstResponder(view)
        if view.frame.contains(point) {
            view.mouseDown(with: event)
        } else {
            // Below the text in a tall row: the caret goes to the end.
            let length = view.textStorage?.length ?? 0
            view.setSelectedRange(NSRange(location: length, length: 0))
        }
        return true
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
        let undoManager = self.undoManager
        typingRun = nil
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

    // MARK: Undo of cell edits

    /// The note's undo stack (the note's editor shares it with its tables).
    private var undoManager: UndoManager? { context?.undoManager ?? grid.noteUndoManager }

    /// Typing in a cell that the next keystrokes join, as NSTextView joins typing into one
    /// step: same cell, same direction (typing or deleting), caret not moved meanwhile,
    /// and nothing else registered on the stack since.
    private var typingRun: (cell: CellPosition, deleting: Bool)?
    private var isRegisteringCellUndo = false
    /// A cell's text is being changed (this run loop pass).
    private var isEditingCell = false
    private weak var observedUndoManager: UndoManager?

    /// Before a cell's text changes: one step restoring the table as it is now, unless the
    /// change continues the typing step in progress. The cell editors don't keep undo
    /// steps of their own (`allowsUndo` is off): an editor is made and dropped as cells
    /// are entered and left, and a shape change rebuilds them, which left their steps
    /// pointing at text no cell showed (⌘Z then did nothing, or lost the edit).
    private func registerCellUndo(for cell: CellTextView, replacing replacement: String?) {
        guard let undoManager, !undoManager.isUndoing, !undoManager.isRedoing else { return }
        observe(undoManager)
        let position = CellPosition(row: cell.row, column: cell.column)
        let deleting = replacement?.isEmpty ?? false
        let typing = cell.isTyping && cell.pendingActionName == nil
        if typing, let run = typingRun, run.cell == position, run.deleting == deleting { return }
        let name = cell.pendingActionName ?? UndoNames.typing
        let before = table
        let selection = cell.selectedRange()
        isRegisteringCellUndo = true
        undoManager.registerUndo(withTarget: self) { target in
            MainActor.assumeIsolated { target.restore(before, actionName: name, cell: position, selection: selection) }
        }
        undoManager.setActionName(name)
        isRegisteringCellUndo = false
        typingRun = typing ? (position, deleting) : nil
    }

    /// Anything else put on the stack (an edit in the note, in another table, a status
    /// change) ends the typing step.
    private func observe(_ undoManager: UndoManager) {
        guard observedUndoManager !== undoManager else { return }
        if let observedUndoManager {
            NotificationCenter.default.removeObserver(self, name: .NSUndoManagerDidOpenUndoGroup, object: observedUndoManager)
        }
        observedUndoManager = undoManager
        NotificationCenter.default.addObserver(self, selector: #selector(undoGroupDidOpen(_:)),
                                               name: .NSUndoManagerDidOpenUndoGroup, object: undoManager)
    }

    @objc private func undoGroupDidOpen(_ notification: Notification) {
        if !isRegisteringCellUndo { typingRun = nil }
    }

    /// Undo (or redo) of a cell edit: the table as it was, the caret back in that cell
    /// where it was, and the opposite step on the stack.
    private func restore(_ snapshot: TableContent, actionName: String, cell: CellPosition, selection: NSRange) {
        let current = table
        var currentSelection = NSRange(location: selection.location, length: 0)
        if let editor, editor.row == cell.row, editor.column == cell.column { currentSelection = editor.selectedRange() }
        undoManager?.registerUndo(withTarget: self) { target in
            MainActor.assumeIsolated { target.restore(current, actionName: actionName, cell: cell, selection: currentSelection) }
        }
        undoManager?.setActionName(actionName)
        typingRun = nil
        if !applyCellTexts(of: snapshot) {
            table = snapshot
            isBuilt = true
            rebuild()
            remeasureAfterShapeChange()
        }
        // Show the change, as undo in text does: the caret goes back into the cell.
        guard grid.window != nil, table.rows.indices.contains(cell.row),
              let view = editor(row: cell.row, column: min(cell.column, max(0, table.rows[cell.row].cells.count - 1))) else { return }
        if view.window?.firstResponder !== view { view.window?.makeFirstResponder(view) }
        let length = view.textStorage?.length ?? 0
        let location = min(selection.location, length)
        view.setSelectedRange(NSRange(location: location, length: min(selection.length, length - location)))
    }

    /// `snapshot` when it has the table's shape (the same rows and cells): only the cells
    /// whose content differs are set, in place, the one being edited included, so the
    /// editor and the caret stay. False for a different shape (then it is rebuilt).
    private func applyCellTexts(of snapshot: TableContent) -> Bool {
        guard isBuilt, texts.count == table.rows.count, snapshot.rows.count == table.rows.count,
              zip(snapshot.rows, table.rows).allSatisfy({ $0.id == $1.id && $0.cells.map(\.id) == $1.cells.map(\.id) }),
              zip(texts, table.rows).allSatisfy({ $0.count == $1.cells.count }) else { return false }
        let old = table
        table = snapshot
        for r in snapshot.rows.indices {
            for c in snapshot.rows[r].cells.indices where snapshot.rows[r].cells[c] != old.rows[r].cells[c] {
                let text = styledText(snapshot.rows[r].cells[c].runs, theme: cellTheme(row: r, column: c))
                texts[r][c] = text
                drawn[r][c] = Self.drawable(text)
                if let editor, editor.row == r, editor.column == c, let storage = editor.textStorage {
                    isLoadingCell = true
                    storage.setAttributedString(text)
                    isLoadingCell = false
                }
                remeasure(row: r, column: c)
            }
        }
        grid.cellsDidChange()
        context?.contentDidChange?()
        return true
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
        view.pendingActionName = UndoNames.mark(keyPath)
        defer { view.pendingActionName = nil }
        guard view.shouldChangeText(in: selection, replacementString: nil) else { return }
        storage.beginEditing()
        storage.enumerateAttribute(.fnMarks, in: selection, options: []) { value, run, _ in
            if storage.attribute(.fnNode, at: run.location, effectiveRange: nil) != nil { return }
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
        case ([.command, .option], "e"): toggleMark(\.code, in: view)
        case ([.command, .shift], "x"): toggleMark(\.strike, in: view)
        case ([.command, .control], "="), ([.command, .control], "+"): toggleMark(\.sup, in: view)
        case ([.command, .control], "-"): toggleMark(\.sub, in: view)
        default: return false
        }
        return true
    }

    // MARK: Accessibility

    /// Rows of cells for VoiceOver: drawn cells are elements with their text as value
    /// (pressing one edits it).
    func accessibilityRows() -> [NSAccessibilityElement] {
        buildIfNeeded()
        if let cached = accessibilityCache { return cached }
        var rows: [NSAccessibilityElement] = []
        for (r, row) in table.rows.enumerated() {
            let rowElement = TableAccessibilityElement(controller: self, row: r, column: nil)
            rowElement.setAccessibilityParent(grid)
            rowElement.setAccessibilityChildren(row.cells.indices.map { c in
                let cell = TableAccessibilityElement(controller: self, row: r, column: c)
                cell.setAccessibilityParent(rowElement)
                return cell
            })
            rows.append(rowElement)
        }
        accessibilityCache = rows
        return rows
    }

    /// Dropped when cells move or change.
    var accessibilityCache: [NSAccessibilityElement]?

    /// A row's or cell's rectangle on screen.
    func screenFrame(row: Int, column: Int?) -> CGRect {
        guard rowEdges.count > row + 1, let window = grid.window else { return .zero }
        let rect = column.map { cellFrame(row: row, column: $0) }
            ?? CGRect(x: 0, y: rowEdges[row], width: tableWidth, height: rowEdges[row + 1] - rowEdges[row])
        return window.convertToScreen(grid.convert(rect, to: nil))
    }

    /// What a drawn cell reads as.
    func accessibilityValue(row: Int, column: Int) -> String {
        if let editor, editor.row == row, editor.column == column { return editor.string }
        return text(row: row, column: column).string
    }
}

/// One cell's text while it is edited: a TextKit 2 text view without chrome; the table
/// controller is its delegate.
final class CellTextView: NSTextView {
    weak var table: TableBlockController?

    /// None for the text view itself: a cell's edits are put on the note's stack by the
    /// table (`registerCellUndo`). NSTextView registered its typing steps on whatever this
    /// returned even with `allowsUndo` off, and undoing one emptied the cell before the
    /// table's step could note what to redo, so redo lost the typing.
    override var undoManager: UndoManager? { nil }

    /// The note's undo stack (see `FortressTextView.noteUndo`): ⌘Z in a cell walks back
    /// through the note's edits.
    var noteStack: UndoManager? { table?.context?.undoManager ?? noteUndoManager }
    var row = 0
    var column = 0
    var cellTheme = EditorTheme()
    /// A key is being typed (or deleted): such edits join one undo step.
    private(set) var isTyping = false
    /// What the edit under way is called on the undo stack (Paste, Cut, Bold…).
    var pendingActionName: String?

    private func typing<T>(_ body: () -> T) -> T {
        let outer = isTyping
        isTyping = true
        defer { isTyping = outer }
        return body()
    }

    private func asAction(_ name: String, _ body: () -> Void) {
        let outer = pendingActionName
        pendingActionName = outer ?? name
        defer { pendingActionName = outer }
        body()
    }

    override func keyDown(with event: NSEvent) { typing { super.keyDown(with: event) } }
    override func insertText(_ string: Any, replacementRange: NSRange) {
        typing { super.insertText(string, replacementRange: replacementRange) }
    }
    override func doCommand(by selector: Selector) { typing { super.doCommand(by: selector) } }
    override func paste(_ sender: Any?) { asAction(UndoNames.paste) { super.paste(sender) } }
    override func pasteAsPlainText(_ sender: Any?) { asAction(UndoNames.paste) { super.pasteAsPlainText(sender) } }
    override func pasteAsRichText(_ sender: Any?) { asAction(UndoNames.paste) { super.pasteAsRichText(sender) } }
    override func cut(_ sender: Any?) { asAction("Cut") { super.cut(sender) } }
    override func delete(_ sender: Any?) { asAction("Delete") { super.delete(sender) } }

    // Edit ▸ Undo and Redo: the note's stack (see `FortressTextView.undoNoteEdit`).
    @objc(undo:) func undoNoteEdit(_ sender: Any?) { NoteUndoActions.undo(noteStack, from: self) }
    @objc(redo:) func redoNoteEdit(_ sender: Any?) { NoteUndoActions.redo(noteStack, from: self) }

    override func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if let undo = NoteUndoActions.validate(menuItem, noteStack) { return undo }
        // Find and Print are the note's (NotePrinting.swift).
        if [#selector(performTextFinderAction(_:)), #selector(performFindPanelAction(_:)), #selector(NSView.printView(_:))]
            .contains(menuItem.action), enclosingNoteTextView != nil {
            return true
        }
        return super.validateMenuItem(menuItem)
    }

    // Find in a cell searches the note, in the note's find bar (not the cell alone).
    override func performTextFinderAction(_ sender: Any?) {
        if !forwardFindToNote(sender, panel: false) { super.performTextFinderAction(sender) }
    }

    override func performFindPanelAction(_ sender: Any?) {
        if !forwardFindToNote(sender, panel: true) { super.performFindPanelAction(sender) }
    }

    /// File ▸ Print…: the whole note, not this cell.
    override func printView(_ sender: Any?) {
        guard let note = enclosingNoteTextView?.controller else { return super.printView(sender) }
        note.printNote()
    }

    static func make() -> CellTextView {
        let view = CellTextView(usingTextLayoutManager: true)
        view.isRichText = true
        view.importsGraphics = false
        // Undone on the table (`TableBlockController.registerCellUndo`), not by the view.
        view.allowsUndo = false
        view.drawsBackground = false
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        view.textContainerInset = .zero
        view.textContainer?.lineFragmentPadding = 0
        view.textContainer?.widthTracksTextView = true
        // As the note's text view: the person's System Settings.
        view.isAutomaticQuoteSubstitutionEnabled = NSSpellChecker.isAutomaticQuoteSubstitutionEnabled
        view.isAutomaticDashSubstitutionEnabled = NSSpellChecker.isAutomaticDashSubstitutionEnabled
        view.isAutomaticLinkDetectionEnabled = false
        // Spell-checked while it is the first responder (see becomeFirstResponder).
        view.isContinuousSpellCheckingEnabled = false
        view.focusRingType = .none
        view.displaysLinkToolTips = true
        view.linkTextAttributes = [.foregroundColor: NSColor.themeLink, .cursor: NSCursor.pointingHand]
        view.insertionPointColor = .themeAccent
        view.selectedTextAttributes = [.backgroundColor: NSColor.themeSelection]
        return view
    }

    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted {
            isContinuousSpellCheckingEnabled = true
            table?.cellDidFocus(self)
        }
        return accepted
    }

    override func resignFirstResponder() -> Bool {
        let accepted = super.resignFirstResponder()
        if accepted { isContinuousSpellCheckingEnabled = false }
        if accepted, table?.focusedCell === self { table?.cellDidFocus(nil) }
        if accepted { table?.editorDidResign(self) }
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

/// Draws the table: cell backgrounds, the header tint, every cell's text but the one
/// being edited, and borders (when the table has them; borderless tables show faint
/// guides only while a cell is being edited).
final class TableGridView: NSView {
    weak var controller: TableBlockController?

    override var isFlipped: Bool { true }

    override func layout() {
        super.layout()
        controller?.layout(width: bounds.width)
    }

    /// Cells moved or changed: redraw, and accessibility asks again.
    func cellsDidChange() {
        controller?.accessibilityCache = nil
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let controller, controller.tableWidth > 0 else { return }
        let table = controller.table
        let rows = controller.rows(in: dirtyRect)
        let outline = CGRect(x: 0.5, y: 0.5, width: controller.tableWidth - 1, height: (controller.rowEdges.last ?? 0) - 1)
        let radius: CGFloat = 6
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(roundedRect: outline, xRadius: radius, yRadius: radius).addClip()
        for r in rows where table.rows.indices.contains(r) {
            for (c, cell) in table.rows[r].cells.enumerated() {
                let frame = controller.cellFrame(row: r, column: c)
                if let name = cell.backgroundColor, let color = EditorTheme.cellBackground(named: name) {
                    color.setFill()
                    frame.fill()
                } else if controller.isHeader(row: r, column: c) {
                    NSColor.themeSurface.setFill()
                    frame.fill()
                }
            }
        }
        NSGraphicsContext.restoreGraphicsState()

        controller.drawTexts(in: dirtyRect)

        let editing = controller.focusedCell != nil
        guard controller.borders || editing else { return }
        let line = controller.borders ? NSColor.themeBorder : NSColor.themeBorder.withAlphaComponent(0.6)
        line.setStroke()
        let grid = NSBezierPath()
        grid.lineWidth = 1
        if !controller.borders { grid.setLineDash([3, 3], count: 2, phase: 0) }
        for r in rows where table.rows.indices.contains(r) {
            for c in table.rows[r].cells.indices where c > 0 {
                let frame = controller.cellFrame(row: r, column: c)
                grid.move(to: CGPoint(x: frame.minX + 0.5, y: frame.minY))
                grid.line(to: CGPoint(x: frame.minX + 0.5, y: frame.maxY))
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
        if let controller, controller.click(event, at: point) { return }
        super.mouseDown(with: event)
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .iBeam)
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
    override func accessibilityChildren() -> [Any]? { controller?.accessibilityRows() }
    override func accessibilityRows() -> [Any]? { controller?.accessibilityRows() }
    override func accessibilityRowCount() -> Int { controller?.table.rows.count ?? 0 }
    override func accessibilityColumnCount() -> Int { controller?.table.columnCount ?? 0 }
}

/// A table row or cell for accessibility. A drawn cell reads its text; pressing or
/// focusing it edits it.
final class TableAccessibilityElement: NSAccessibilityElement {
    weak var controller: TableBlockController?
    let row: Int
    let column: Int?

    @MainActor init(controller: TableBlockController, row: Int, column: Int?) {
        self.controller = controller
        self.row = row
        self.column = column
        super.init()
        if let column {
            setAccessibilityRole(.cell)
            setAccessibilityLabel(String(localized: "Row \(row + 1), column \(column + 1)"))
            setAccessibilityRowIndexRange(NSRange(location: row, length: 1))
            setAccessibilityColumnIndexRange(NSRange(location: column, length: 1))
        } else {
            setAccessibilityRole(.row)
            setAccessibilityIndex(row)
        }
    }

    // Accessibility asks on the main thread; the overrides are nonisolated in the SDK, so
    // each asserts that with `MainActor.assumeIsolated` before touching the controller.
    override func accessibilityFrame() -> NSRect {
        let controller = controller
        let row = row, column = column
        return MainActor.assumeIsolated { controller?.screenFrame(row: row, column: column) ?? .zero }
    }

    override func accessibilityValue() -> Any? {
        let controller = controller
        guard let column else { return nil }
        let row = row
        let value: String? = MainActor.assumeIsolated { controller?.accessibilityValue(row: row, column: column) }
        return value
    }

    override func accessibilityPerformPress() -> Bool {
        let controller = controller
        guard let column else { return false }
        let row = row
        return MainActor.assumeIsolated {
            controller?.focus(row: row, column: column, atEnd: true)
            return controller != nil
        }
    }

    override func setAccessibilityFocused(_ focused: Bool) {
        let controller = controller
        guard focused, let column else { return }
        let row = row
        MainActor.assumeIsolated { controller?.focus(row: row, column: column, atEnd: true) }
    }
}

/// What a OneNote cell holds besides its first line: list items, more paragraphs,
/// pictures, nested tables, math. Read-only; never rewritten.
struct NestedCellBlocks: View {
    let blocks: [BlockNode]
    let context: AttachmentContext?
    let scale: CGFloat

    // Hosted in its own NSHostingView inside the text view: theme it here.
    var body: some View {
        themedBody.fortressTheme().foregroundStyle(.themeText)
    }

    @ViewBuilder private var themedBody: some View {
        VStack(alignment: .leading, spacing: 3 * scale) {
            ForEach(Array(flattened.enumerated()), id: \.offset) { _, item in
                row(item.block)
                    .padding(.leading, CGFloat(item.depth) * 14 * scale)
            }
        }
        .font(.system(size: 13 * scale))
        .frame(maxWidth: .infinity, alignment: .leading)
        .help("Shown as imported")
    }

    /// The blocks with their nested items (a table's rows are summarized instead).
    private var flattened: [(block: BlockNode, depth: Int)] {
        var output: [(BlockNode, Int)] = []
        func add(_ nodes: [BlockNode], _ depth: Int) {
            for node in nodes {
                output.append((node, depth))
                if node.type != "table" { add(node.children, depth + 1) }
            }
        }
        add(blocks, 0)
        return output
    }

    @ViewBuilder private func row(_ block: BlockNode) -> some View {
        switch block.type {
        case "image":
            if let url = block.attrs["url"]?.stringValue.flatMap({ context?.url(for: $0) }), let image = context?.image(for: url) {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fit)
                    .frame(maxWidth: 160 * scale, maxHeight: 120 * scale, alignment: .leading)
                    .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    .accessibilityLabel(block.attrs["alt"]?.stringValue ?? "Image")
            } else {
                Label(block.attrs["alt"]?.stringValue ?? "Image", systemImage: "photo")
                    .foregroundStyle(.themeSecondary).lineLimit(2)
            }
        case "table":
            Label("Table · \(block.children.count) rows", systemImage: "tablecells")
                .foregroundStyle(.themeSecondary)
                .help(TableContent.nestedText(block))
        case "math":
            Text(MathText.attributed(block.payload?["latex"]?.stringValue ?? block.text, size: 14 * scale))
        case "file":
            Label(block.attrs["name"]?.stringValue ?? "File", systemImage: "doc")
                .foregroundStyle(.themeSecondary)
        case "divider":
            Divider()
        default:
            HStack(alignment: .firstTextBaseline, spacing: 6 * scale) {
                if let marker = marker(for: block) {
                    Text(marker).foregroundStyle(.themeSecondary)
                }
                Text(block.text)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func marker(for block: BlockNode) -> String? {
        switch block.type {
        case "check_item": block.attrs["checked"]?.boolValue == true ? "☑" : "☐"
        case "numbered_item": "–"
        case "bullet_item": "•"
        default: nil
        }
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
