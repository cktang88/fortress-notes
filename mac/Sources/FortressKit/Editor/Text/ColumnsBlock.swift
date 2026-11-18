import AppKit

/// Side-by-side columns inside the note. Each column is its own small block editor (the
/// same `BlockEditorController` as the main text, without scrolling), so its blocks are
/// edited exactly like the rest of the note: headings, lists, marks, links, pictures.
///
/// Trade-offs of one text view per column (instead of one text flow): a selection can't
/// run from a column into the main text, ↑/↓ leave a column only at its first or last
/// line, and Find searches the column you're in. Saving is unaffected: the columns'
/// blocks are ordinary children of their `column` blocks and are diffed with the note.
@MainActor
final class ColumnsBlockController: NSObject, LiveBlock {
    private var columns: [EditorColumn]
    /// The column editors, made the first time the block is laid out or entered (a note
    /// opening doesn't pay for columns it never scrolls to).
    var editors: [BlockEditorController] {
        buildIfNeeded()
        return columnEditors
    }
    private var columnEditors: [BlockEditorController] = []
    private var isBuilt = false
    weak var context: AttachmentContext?
    weak var attachment: BlockAttachment?
    let container = ColumnsView()
    var view: NSView { container }
    private var measuredWidth: CGFloat = -1
    private var measuredHeight: CGFloat = 0
    private(set) var frames: [CGRect] = []

    init(columns: [EditorColumn], context: AttachmentContext, attachment: BlockAttachment) {
        self.columns = columns
        self.context = context
        self.attachment = attachment
        super.init()
        container.controller = self
    }

    private func buildIfNeeded() {
        guard !isBuilt, let context else { return }
        isBuilt = true
        for column in columns {
            let editor = BlockEditorController(baseURL: context.baseURL, embedded: true)
            editor.theme = context.theme
            context.configureNested?(editor)
            editor.load(column.blocks.isEmpty ? [EditorBlock()] : column.blocks)
            editor.onChange = { [weak self] in self?.columnDidChange() }
            editor.onRelayout = { [weak self] in self?.remeasure() }
            editor.onLeave = { [weak self, weak editor] forward in
                guard let editor else { return }
                self?.leave(from: editor, forward: forward)
            }
            container.addSubview(editor.textView)
            columnEditors.append(editor)
        }
    }

    var theme: EditorTheme { context?.theme ?? EditorTheme() }
    /// Space between columns (a hairline sits in the middle).
    var gutter: CGFloat { 32 * theme.scale }

    func write(into block: inout EditorBlock) {
        // Never shown: the columns as they came.
        guard isBuilt else { block.columns = columns; return }
        block.columns = zip(columns, columnEditors).map { column, editor in
            var column = column
            column.blocks = editor.currentBlocks()
            return column
        }
    }

    func focus(atEnd: Bool) {
        guard let editor = atEnd ? editors.last : editors.first else { return }
        if atEnd { editor.focusEnd() } else { editor.focusStart() }
    }

    // MARK: Layout

    func height(forWidth width: CGFloat) -> CGFloat {
        layout(width: width)
        return measuredHeight
    }

    func layout(width: CGFloat) {
        guard abs(width - measuredWidth) > 0.5 else { return }
        measuredWidth = width
        let fractions = EditorColumn.fractions(columns)
        let usable = max(40, width - gutter * CGFloat(max(0, editors.count - 1)))
        var x: CGFloat = 0
        var height: CGFloat = 0
        frames = []
        for (index, editor) in editors.enumerated() {
            let columnWidth = (usable * CGFloat(fractions[index])).rounded(.down)
            let contentHeight = editor.contentHeight(forWidth: columnWidth)
            let frame = CGRect(x: x, y: 0, width: columnWidth, height: contentHeight)
            frames.append(frame)
            editor.textView.frame = frame
            height = max(height, contentHeight)
            x += columnWidth + gutter
        }
        measuredHeight = max(26 * theme.scale, height)
        container.needsDisplay = true
    }

    private func remeasure() {
        let before = measuredHeight
        let width = measuredWidth < 0 ? container.bounds.width : measuredWidth
        measuredWidth = -1
        layout(width: width)
        if abs(before - measuredHeight) > 0.5, let attachment {
            context?.attachmentDidResize?(attachment)
        }
    }

    private func columnDidChange() {
        remeasure()
        context?.contentDidChange?()
    }

    private func leave(from editor: BlockEditorController, forward: Bool) {
        guard let index = editors.firstIndex(where: { $0 === editor }) else { return }
        let next = index + (forward ? 1 : -1)
        if editors.indices.contains(next) {
            if forward { editors[next].focusStart() } else { editors[next].focusEnd() }
        } else if let attachment {
            context?.leaveAttachment?(attachment, forward)
        }
    }
}

/// Holds the column editors and draws a hairline between columns.
final class ColumnsView: NSView {
    weak var controller: ColumnsBlockController?

    override var isFlipped: Bool { true }

    override func layout() {
        super.layout()
        controller?.layout(width: bounds.width)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let controller, controller.frames.count > 1 else { return }
        NSColor.quaternaryLabelColor.setFill()
        for frame in controller.frames.dropLast() {
            let x = (frame.maxX + controller.gutter / 2).rounded(.down)
            CGRect(x: x, y: 4, width: 1, height: max(0, bounds.height - 8)).fill()
        }
    }

    override func mouseDown(with event: NSEvent) {
        // A click below a short column puts the caret at its end.
        let point = convert(event.locationInWindow, from: nil)
        guard let controller else { return super.mouseDown(with: event) }
        for (index, frame) in controller.frames.enumerated()
        where point.x >= frame.minX - controller.gutter / 2 && point.x <= frame.maxX + controller.gutter / 2 {
            controller.editors[index].focusEnd()
            return
        }
        super.mouseDown(with: event)
    }

    override func isAccessibilityElement() -> Bool { false }
}
