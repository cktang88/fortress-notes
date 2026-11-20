import AppKit

/// Blocks being dragged by their handle: which editor they are in and the ids of their
/// first and last paragraphs (so the range is found again however the text moved).
@MainActor
final class BlockDrag {
    static let type = NSPasteboard.PasteboardType("app.fortress.notes.block-drag")
    /// The drag in progress (a block drag never leaves the app as blocks).
    static var current: BlockDrag?

    weak var source: BlockEditorController?
    let firstId: String
    let lastId: String
    /// Where the drag started (the note's text view): dragging right of it nests.
    let startX: CGFloat

    init(source: BlockEditorController, firstId: String, lastId: String, startX: CGFloat) {
        self.source = source
        self.firstId = firstId
        self.lastId = lastId
        self.startX = startX
    }

    /// The dragged paragraphs in their editor (nil once they are gone).
    var range: NSRange? {
        guard let source, let first = source.range(ofBlock: firstId), let last = source.range(ofBlock: lastId),
              NSMaxRange(last) > first.location else { return nil }
        return NSRange(location: first.location, length: NSMaxRange(last) - first.location)
    }

    static func isBlockDrag(_ info: any NSDraggingInfo) -> Bool {
        current != nil && info.draggingPasteboard.availableType(from: [type]) != nil
    }
}

/// Where dropped blocks go.
enum BlockDropTarget {
    /// Between blocks of `editor` (the note's or a column's), at a paragraph boundary,
    /// at a depth (nested under the block above when deeper).
    case flow(editor: BlockEditorController, location: Int, depth: Int)
    /// Beside a top-level block of the note: the two become columns.
    case beside(paragraph: Int)
    /// A new column of a columns block, at `index`.
    case newColumn(paragraph: Int, index: Int)
    /// Into a table cell (one block of text: its text joins the cell's).
    case cell(paragraph: Int, row: Int, column: Int)
}

extension BlockEditorController {
    // MARK: The handle's block

    /// The block the handle stands for at `point` (the note's text view): a block of a
    /// column when the pointer is over the column, else the note's block on that line.
    func handleTarget(at point: CGPoint) -> HandleTarget? {
        let origin = textView.textContainerOrigin
        let width = textView.textContainer?.size.width ?? textView.bounds.width
        guard point.x > origin.x - 72, point.x < origin.x + width + 24,
              let (paragraph, frame) = blockFrame(at: CGPoint(x: origin.x + 1, y: point.y)),
              let info = storage.blockInfo(at: paragraph.location) else { return nil }
        if point.x >= origin.x, info.kind == .columns,
           let columns = (storage.attribute(.attachment, at: paragraph.location, effectiveRange: nil) as? BlockAttachment)?.live as? ColumnsBlockController {
            let editors = columns.builtEditors
            for (index, editor) in editors.enumerated() {
                let columnFrame = editor.textView.convert(editor.textView.bounds, to: textView)
                let left = index == 0 ? columnFrame.minX - 8 : columnFrame.minX - columns.gutter / 2
                guard point.x >= left, point.x < columnFrame.maxX + columns.gutter / 2 else { continue }
                let local = editor.textView.convert(point, from: textView)
                guard let (inner, innerFrame) = editor.blockFrame(at: CGPoint(x: editor.textView.textContainerOrigin.x + 1, y: local.y)),
                      let id = editor.storage.blockInfo(at: inner.location)?.id else { return nil }
                return HandleTarget(editor: editor, blockId: id, frame: editor.textView.convert(innerFrame, to: textView), inColumn: true)
            }
        }
        return HandleTarget(editor: self, blockId: info.id, frame: frame, inColumn: false)
    }

    // MARK: Dropping (the note's editor works out where, for its own text and its columns)

    /// Where blocks dragged with `drag` would go if dropped at `point` (the note's text view).
    func blockDropTarget(at point: CGPoint, drag: BlockDrag) -> BlockDropTarget? {
        guard let source = drag.source, let moving = drag.range else { return nil }
        let moved = source.blocks(in: moving)
        let hasColumns = moved.contains { $0.kind == .columns }
        let oneText = moved.count == 1 && moved[0].kind.hasInlineText && moved[0].kind != .code
        let origin = textView.textContainerOrigin
        let width = textView.textContainer?.size.width ?? textView.bounds.width
        if let (paragraph, _) = blockFrame(at: CGPoint(x: origin.x + 1, y: point.y)),
           !(source === self && NSIntersectionRange(paragraph, moving).length > 0),
           let info = storage.blockInfo(at: paragraph.location) {
            let live = (storage.attribute(.attachment, at: paragraph.location, effectiveRange: nil) as? BlockAttachment)?.live
            if let columns = live as? ColumnsBlockController, point.x >= origin.x - 8 {
                let editors = columns.editors
                let frames = editors.map { $0.textView.convert($0.textView.bounds, to: textView) }
                if let last = frames.last, point.x > last.maxX + 6 {
                    return hasColumns ? nil : .newColumn(paragraph: paragraph.location, index: editors.count)
                }
                for (index, frame) in frames.enumerated() {
                    if index > 0, point.x < frame.minX - 6, point.x > frames[index - 1].maxX + 6 {
                        return hasColumns ? nil : .newColumn(paragraph: paragraph.location, index: index)
                    }
                    guard point.x >= frame.minX - 6, point.x <= frame.maxX + 6 else { continue }
                    guard !hasColumns else { return nil }
                    let editor = editors[index]
                    guard let flow = editor.flowTarget(at: editor.textView.convert(point, from: textView), drag: drag) else { return nil }
                    return .flow(editor: editor, location: flow.location, depth: flow.depth)
                }
            }
            if let table = live as? TableBlockController, oneText, point.x >= origin.x,
               let cell = table.cell(at: table.grid.convert(point, from: textView)) {
                return .cell(paragraph: paragraph.location, row: cell.row, column: cell.column)
            }
            if !isEmbedded, info.depth == 0, info.kind != .columns, !hasColumns, point.x > origin.x + width - 48,
               !(source === self && NSIntersectionRange(subtree(at: paragraph.location), moving).length > 0) {
                return .beside(paragraph: paragraph.location)
            }
        }
        guard let flow = flowTarget(at: point, drag: drag) else { return nil }
        return .flow(editor: self, location: flow.location, depth: flow.depth)
    }

    /// A place between this editor's blocks for `point` (its own text view): above or
    /// below the block under it, at a depth the blocks around allow, deeper the further
    /// right of where the drag started. Nil when the drop would change nothing.
    func flowTarget(at point: CGPoint, drag: BlockDrag) -> (location: Int, depth: Int)? {
        guard let source = drag.source, let moving = drag.range else { return nil }
        let own: NSRange? = source === self ? moving : nil
        let origin = textView.textContainerOrigin
        var location: Int
        if let (paragraph, _) = blockFrame(at: CGPoint(x: origin.x + 1, y: point.y)) {
            location = point.y < selectionFrame(paragraph).midY ? paragraph.location : NSMaxRange(paragraph)
        } else {
            location = point.y < origin.y + 8 ? 0 : string.length
        }
        if let own, location > own.location, location < NSMaxRange(own) {
            location = point.y < selectionFrame(own).midY ? own.location : NSMaxRange(own)
        }
        // The blocks around the place, the dragged ones left out.
        var before = location
        if let own, before == NSMaxRange(own) { before = own.location }
        var after = location
        if let own, after == own.location { after = NSMaxRange(own) }
        let previous = before > 0 ? storage.blockInfo(at: paragraph(at: before - 1).location) : nil
        let next = after < string.length ? storage.blockInfo(at: after) : nil
        let maxDepth = previous.map { $0.kind.takesNoChildren ? $0.depth : $0.depth + 1 } ?? 0
        let minDepth = min(next?.depth ?? 0, maxDepth)
        let movedDepth = source.storage.blockInfo(at: moving.location)?.depth ?? 0
        let wanted: Int
        if own != nil {
            let start = textView.convert(CGPoint(x: drag.startX, y: 0), from: rootEditor.textView).x
            wanted = movedDepth + Int(((point.x - start) / theme.indentStep).rounded())
        } else {
            wanted = Int(((point.x - origin.x) / theme.indentStep).rounded(.down))
        }
        let depth = max(minDepth, min(maxDepth, wanted))
        if let own, location == own.location || location == NSMaxRange(own), depth == movedDepth { return nil }
        return (location, depth)
    }

    /// Where the drop line or box is drawn for `target` (the note's text view), and
    /// whether it is a box.
    func indicatorFrame(for target: BlockDropTarget) -> (frame: CGRect, box: Bool)? {
        switch target {
        case .flow(let editor, let location, let depth):
            let origin = editor.textView.textContainerOrigin
            let y: CGFloat
            if location < editor.string.length {
                y = editor.selectionFrame(editor.paragraph(at: location)).minY
            } else if editor.string.length > 0 {
                y = editor.selectionFrame(editor.paragraph(at: editor.string.length - 1)).maxY
            } else {
                y = origin.y
            }
            let indent = CGFloat(depth) * theme.indentStep
            let width = (editor.textView.textContainer?.size.width ?? editor.textView.bounds.width) - indent
            let line = CGRect(x: origin.x + indent, y: y - 1, width: max(24, width), height: 2)
            return (textView.convert(line, from: editor.textView), false)
        case .beside(let location):
            let area = selectionFrame(subtree(at: location))
            return (CGRect(x: area.maxX + 6, y: area.minY, width: 2, height: max(18, area.height)), false)
        case .newColumn(let location, let index):
            guard let columns = live(at: location) as? ColumnsBlockController else { return nil }
            let frames = columns.editors.map { $0.textView.convert($0.textView.bounds, to: textView) }
            let area = selectionFrame(paragraph(at: location))
            let x: CGFloat
            if index >= frames.count { x = (frames.last?.maxX ?? area.maxX) + 6 } else { x = frames[index].minX - columns.gutter / 2 }
            return (CGRect(x: x - 1, y: area.minY, width: 2, height: max(18, area.height)), false)
        case .cell(let location, let row, let column):
            guard let table = live(at: location) as? TableBlockController else { return nil }
            return (textView.convert(table.cellFrame(row: row, column: column), from: table.grid), true)
        }
    }

    private func live(at location: Int) -> (any LiveBlock)? {
        (storage.attribute(.attachment, at: paragraph(at: location).location, effectiveRange: nil) as? BlockAttachment)?.live
    }

    /// A drag moved over the note: shows where the blocks would go.
    func blockDragUpdated(_ info: any NSDraggingInfo) -> NSDragOperation {
        let indicator = blockHandle.indicator
        guard let drag = BlockDrag.current else { indicator.isHidden = true; return [] }
        let point = textView.convert(info.draggingLocation, from: nil)
        autoscrollForDrag(at: point)
        guard let target = blockDropTarget(at: point, drag: drag), let shown = indicatorFrame(for: target) else {
            indicator.isHidden = true
            return []
        }
        if indicator.superview !== textView { textView.addSubview(indicator) }
        indicator.isBox = shown.box
        indicator.frame = shown.frame
        indicator.isHidden = false
        return .move
    }

    func blockDragExited() {
        blockHandle.indicator.isHidden = true
    }

    func performBlockDrop(_ info: any NSDraggingInfo) -> Bool {
        blockHandle.indicator.isHidden = true
        guard let drag = BlockDrag.current else { return false }
        return dropBlocks(drag, at: textView.convert(info.draggingLocation, from: nil))
    }

    /// Drops the dragged blocks at `point` (the note's text view): one "Move" step.
    @discardableResult
    func dropBlocks(_ drag: BlockDrag, at point: CGPoint) -> Bool {
        guard let target = blockDropTarget(at: point, drag: drag) else { return false }
        return drop(drag, on: target)
    }

    /// Scrolls while a drag is near the top or bottom of the visible note.
    private func autoscrollForDrag(at point: CGPoint) {
        guard !isEmbedded else { return }
        let visible = scrollView.contentView.bounds
        var y = visible.minY
        if point.y < visible.minY + 36 { y -= 14 } else if point.y > visible.maxY - 36 { y += 14 } else { return }
        y = max(0, min(y, textView.frame.height - visible.height))
        scrollView.contentView.scroll(to: NSPoint(x: visible.minX, y: y))
        scrollView.reflectScrolledClipView(scrollView.contentView)
    }

    /// Moves the dragged blocks to `target`. Within one editor it is a move of its
    /// paragraphs; across the note and its columns, the note's text is rewritten where
    /// the blocks leave and arrive (columns blocks are made again with their new columns).
    func drop(_ drag: BlockDrag, on target: BlockDropTarget) -> Bool {
        guard let source = drag.source, let moving = drag.range else { return false }
        let moved = source.blocks(in: moving)
        let movedIds = Set(moved.map(\.id))
        let baseDepth = moved.first?.depth ?? 0
        source.endBlockSelection(caret: nil)
        if case .flow(let editor, let location, let depth) = target, editor === source {
            source.undoStep(UndoNames.move) { source.move(moving, to: location, depthDelta: depth - baseDepth) }
            if let range = source.range(ofBlock: drag.firstId) {
                source.textView.window?.makeFirstResponder(source.textView)
                source.textView.setSelectedRange(NSRange(location: range.location, length: 0))
            }
            return true
        }
        guard !isEmbedded else { return false }
        var edits: [(NSRange, NSAttributedString)] = []
        /// Changes to columns blocks, by their paragraph's location: removals run first.
        var removals: [Int: [(inout [EditorColumn]) -> Void]] = [:]
        var insertions: [Int: [(inout [EditorColumn]) -> Void]] = [:]
        func column(of editor: BlockEditorController) -> (location: Int, index: Int)? {
            guard let host = editor.columnsHost, let attachment = host.attachment, let range = range(of: attachment),
                  let index = host.builtEditors.firstIndex(where: { $0 === editor }) else { return nil }
            return (paragraph(at: range.location).location, index)
        }
        // Where the blocks leave.
        if source === self {
            edits.append(removal(of: moving))
        } else if let place = column(of: source) {
            removals[place.location, default: []].append { columns in
                guard columns.indices.contains(place.index) else { return }
                columns[place.index].blocks.removeAll { movedIds.contains($0.id) }
            }
        } else {
            return false
        }
        // Where they arrive.
        switch target {
        case .flow(let editor, let location, let depth):
            if editor === self {
                edits.append((NSRange(location: location, length: 0), text(for: Self.rebased(moved, to: depth))))
            } else if let place = column(of: editor) {
                let index = editor.blockCount(before: location)
                let placed = Self.rebased(moved, to: depth)
                insertions[place.location, default: []].append { columns in
                    guard columns.indices.contains(place.index) else { return }
                    columns[place.index].blocks.insert(contentsOf: placed, at: min(index, columns[place.index].blocks.count))
                }
            } else {
                return false
            }
        case .beside(let location):
            let besideRange = subtree(at: location)
            let columns = [EditorColumn(blocks: Self.rebased(blocks(in: besideRange), to: 0)),
                           EditorColumn(blocks: Self.rebased(moved, to: 0))]
            edits.append((besideRange, text(for: [EditorBlock(kind: .columns, depth: 0, columns: columns)])))
        case .newColumn(let location, let index):
            let placed = Self.rebased(moved, to: 0)
            insertions[location, default: []].append { columns in
                columns.insert(EditorColumn(blocks: placed), at: min(index, columns.count))
            }
        case .cell(let location, let row, let columnIndex):
            let tableParagraph = paragraph(at: location)
            var tableBlock = block(at: tableParagraph)
            guard var table = tableBlock.table, let first = moved.first else { return false }
            let existing = table.cell(row: row, column: columnIndex)?.runs ?? []
            let runs = existing.isEmpty ? first.runs : existing + [InlineRun(softBreak)] + first.runs
            table.setRuns(BlockCodec.normalized(runs), row: row, column: columnIndex)
            tableBlock.table = table
            edits.append((tableParagraph, text(for: [tableBlock])))
        }
        for location in Set(removals.keys).union(insertions.keys) {
            let (range, text, _) = columnsEdit(at: paragraph(at: location)) { columns in
                for change in removals[location] ?? [] { change(&columns) }
                for change in insertions[location] ?? [] { change(&columns) }
            }
            edits.append((range, text))
        }
        undoStep(UndoNames.move) { replaceAll(edits) }
        textView.window?.makeFirstResponder(textView)
        if let range = range(ofBlock: drag.firstId) {
            textView.setSelectedRange(NSRange(location: range.location, length: 0))
        }
        return true
    }

    /// How many blocks (paragraphs) come before `location`.
    func blockCount(before location: Int) -> Int {
        var count = 0
        var cursor = 0
        while cursor < min(location, string.length) {
            count += 1
            cursor = NSMaxRange(paragraph(at: cursor))
        }
        return count
    }
}
