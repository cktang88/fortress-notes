import AppKit
import UniformTypeIdentifiers

/// What a block can be turned into from its menu, the Format menu or the `/` menu.
enum BlockTurnInto: Hashable {
    /// A text kind (paragraph, headings, lists, quote, code).
    case kind(BlockKind)
    /// A callout: quote lines in a `callout` container.
    case callout
    /// A divider (only an empty block: its text would be lost).
    case divider
    /// A table whose first cell holds the block's text.
    case table
    /// Side-by-side columns holding the blocks.
    case columns(Int)

    var undoName: String {
        switch self {
        case .kind(let kind): UndoNames.kind(kind)
        case .callout: UndoNames.callout
        case .divider: UndoNames.divider
        case .table: UndoNames.insertTable
        case .columns: UndoNames.columns
        }
    }
}

/// Block operations on the note's text: the block handle's menu, block selection (Esc),
/// the `/` menu, drag and drop of blocks and columns. Every operation is one named step
/// on the note's undo stack, made of ordinary text edits (`replace`), so saving turns it
/// into block operations (`BlockDiff`: insert, update, move, delete with after_id and
/// parent_id) like any other edit, and undo and redo save too.
extension BlockEditorController {
    // MARK: Blocks as ranges of the text

    /// The note's own editor (a column's editor answers its columns block's editor's).
    var rootEditor: BlockEditorController { parentEditor?.rootEditor ?? self }

    /// The paragraph at `location` and its children (the deeper paragraphs after it).
    func subtree(at location: Int) -> NSRange {
        let first = paragraph(at: location)
        guard let info = storage.blockInfo(at: first.location), !info.kind.takesNoChildren else { return first }
        var end = NSMaxRange(first)
        while end < string.length {
            let next = paragraph(at: end)
            guard let nextInfo = storage.blockInfo(at: next.location), nextInfo.depth > info.depth else { break }
            end = NSMaxRange(next)
        }
        return NSRange(location: first.location, length: end - first.location)
    }

    /// The paragraphs `range` touches, with the children of each.
    func blocksRange(for range: NSRange) -> NSRange {
        let touched = paragraphs(in: range)
        guard let first = touched.first else { return paragraph(at: range.location) }
        var end = NSMaxRange(first)
        for paragraph in touched { end = max(end, NSMaxRange(subtree(at: paragraph.location))) }
        return NSRange(location: first.location, length: end - first.location)
    }

    /// One paragraph read back as a block (tables and columns with their current state).
    func block(at paragraph: NSRange) -> EditorBlock {
        DocumentText.block(in: storage, paragraph: paragraph, info: storage.blockInfo(at: paragraph.location) ?? BlockInfo())
    }

    /// The blocks of a range of whole paragraphs, in order.
    func blocks(in range: NSRange) -> [EditorBlock] {
        var output: [EditorBlock] = []
        var location = range.location
        while location < NSMaxRange(range), location < string.length {
            let current = paragraph(at: location)
            output.append(block(at: current))
            location = NSMaxRange(current)
        }
        return output
    }

    /// Blocks as text to put in the note (styled when they go in).
    func text(for blocks: [EditorBlock]) -> NSAttributedString {
        let output = NSMutableAttributedString()
        for block in blocks { output.append(DocumentText.paragraph(block, context: context)) }
        return output
    }

    /// Text that takes `range` out of the note: the range itself, or for the whole text
    /// an empty paragraph (a note always has one).
    func removal(of range: NSRange) -> (NSRange, NSAttributedString) {
        if range.location == 0, NSMaxRange(range) >= string.length {
            return (NSRange(location: 0, length: string.length), attributed("\n", info: BlockInfo()))
        }
        return (range, NSAttributedString())
    }

    /// Several replacements as parts of the current step, from the last to the first so
    /// the earlier ranges stay where they are (at one location, the longer range first).
    func replaceAll(_ edits: [(NSRange, NSAttributedString)]) {
        let ordered = edits.sorted { left, right in
            left.0.location != right.0.location ? left.0.location > right.0.location : left.0.length > right.0.length
        }
        for (range, text) in ordered { replace(range, with: text) }
    }

    /// `text` with every paragraph's depth moved by `delta` (never below 0).
    func shifted(_ text: NSAttributedString, by delta: Int) -> NSAttributedString {
        guard delta != 0 else { return text }
        let copy = NSMutableAttributedString(attributedString: text)
        let string = copy.nsString
        var location = 0
        while location < string.length {
            let paragraph = string.paragraphRange(at: location)
            if let info = copy.blockInfo(at: paragraph.location) {
                copy.addAttribute(.fnBlock, value: info.with(depth: max(0, info.depth + delta)), range: paragraph)
            }
            location = NSMaxRange(paragraph)
        }
        return copy
    }

    /// Depths of `blocks` shifted so the shallowest is `depth`.
    static func rebased(_ blocks: [EditorBlock], to depth: Int) -> [EditorBlock] {
        let minimum = blocks.map(\.depth).min() ?? 0
        return blocks.map { block in
            var block = block
            block.depth = block.depth - minimum + depth
            return block
        }
    }

    // MARK: Block selection (Esc)

    /// The paragraphs of the selected blocks with their children.
    func blockSelectionRange() -> NSRange? {
        guard let selection = blockSelection, let anchor = range(ofBlock: selection.anchor),
              let head = range(ofBlock: selection.head) else { return nil }
        let start = min(anchor.location, head.location)
        let end = max(NSMaxRange(subtree(at: anchor.location)), NSMaxRange(subtree(at: head.location)))
        return NSRange(location: start, length: end - start)
    }

    /// The paragraphs chosen (from the first to the last selected, not the last one's
    /// children): what Turn Into changes.
    func selectedParagraphs() -> [NSRange] {
        if let selection = blockSelection, let anchor = range(ofBlock: selection.anchor),
           let head = range(ofBlock: selection.head) {
            let first = min(anchor.location, head.location)
            let last = max(anchor.location, head.location)
            return paragraphs(in: NSRange(location: first, length: last - first))
        }
        return paragraphs(in: textView.selectedRange())
    }

    /// What block commands act on: the selected blocks, else the blocks the text
    /// selection touches; with their children.
    func targetBlocksRange() -> NSRange {
        blockSelectionRange() ?? blocksRange(for: textView.selectedRange())
    }

    /// Esc: the blocks the caret or text selection is in become selected as blocks.
    @discardableResult
    func selectBlocksAtCaret() -> Bool {
        let selection = textView.selectedRange()
        let touched = paragraphs(in: selection)
        guard let first = touched.first, let last = touched.last,
              let anchor = storage.blockInfo(at: first.location)?.id,
              let head = storage.blockInfo(at: last.location)?.id else { return false }
        selectBlocks(anchor: anchor, head: head)
        return true
    }

    /// Selects blocks as blocks (a highlight around them, no caret).
    func selectBlocks(anchor: String, head: String) {
        if blockSelection == nil {
            savedSelectedTextAttributes = textView.selectedTextAttributes
            textView.selectedTextAttributes = [.backgroundColor: NSColor.clear]
        }
        blockSelection = (anchor, head)
        showBlockSelection()
    }

    /// Puts the text selection over the selected blocks (Copy, Cut and the menus use it)
    /// and draws the highlight.
    func showBlockSelection() {
        guard let range = blockSelectionRange() else { endBlockSelection(caret: nil); return }
        isSettingBlockSelection = true
        defer { isSettingBlockSelection = false }
        let limit = max(0, string.length - 1)
        let start = min(range.location, limit)
        textView.window?.makeFirstResponder(textView)
        textView.setSelectedRange(NSRange(location: start, length: max(0, min(NSMaxRange(range), limit) - start)))
        textView.scrollRangeToVisible(NSRange(location: start, length: 0))
        let overlay = blockSelectionOverlay ?? BlockSelectionOverlay()
        if overlay.superview !== textView { textView.addSubview(overlay) }
        blockSelectionOverlay = overlay
        overlay.frame = selectionFrame(range).insetBy(dx: -6, dy: -3)
        overlay.needsDisplay = true
        overlay.setAccessibilityLabel(String(localized: "Selected blocks"))
        NSAccessibility.post(element: textView, notification: .selectedTextChanged)
    }

    /// Back to the caret (at `caret`, or where the text selection is).
    func endBlockSelection(caret: Int?) {
        guard blockSelection != nil else { return }
        blockSelection = nil
        blockSelectionOverlay?.removeFromSuperview()
        if let saved = savedSelectedTextAttributes { textView.selectedTextAttributes = saved }
        savedSelectedTextAttributes = nil
        if let caret {
            isSettingBlockSelection = true
            textView.setSelectedRange(NSRange(location: min(max(0, caret), max(0, string.length - 1)), length: 0))
            isSettingBlockSelection = false
        }
    }

    /// Keeps the selected blocks' highlight on them after the view's size changed.
    func layoutBlockChrome() {
        guard let range = blockSelectionRange(), let overlay = blockSelectionOverlay else { return }
        overlay.frame = selectionFrame(range).insetBy(dx: -6, dy: -3)
    }

    /// Info for the block with the caret (or the first selected): where it came from.
    func showBlockInfo() {
        let root = rootEditor
        let target = selectedParagraphs().first ?? paragraph(at: textView.selectedRange().location)
        guard let id = storage.blockInfo(at: target.location)?.id else { return }
        let frame = selectionFrame(target)
        root.blockHandle.show(HandleTarget(editor: self, blockId: id, frame: root.textView.convert(frame, from: textView), inColumn: isEmbedded))
        root.blockHandle.showInfo()
    }

    /// The area of a range of paragraphs (text view coordinates), full measure wide.
    func selectionFrame(_ range: NSRange) -> CGRect {
        guard let manager = textView.textLayoutManager, let textRange = textRange(for: range) else { return .zero }
        manager.ensureLayout(for: textRange)
        var frame = CGRect.null
        _ = manager.enumerateTextLayoutFragments(from: textRange.location, options: [.ensuresLayout]) { fragment in
            guard let contentManager = manager.textContentManager else { return false }
            let offset = contentManager.offset(from: contentManager.documentRange.location, to: fragment.rangeInElement.location)
            guard offset < NSMaxRange(range) else { return false }
            frame = frame.union(fragment.layoutFragmentFrame)
            return true
        }
        guard !frame.isNull else { return .zero }
        let origin = textView.textContainerOrigin
        let depth = storage.blockInfo(at: range.location)?.depth ?? 0
        let left = CGFloat(depth) * theme.indentStep
        let width = (textView.textContainer?.size.width ?? frame.width) - left
        return CGRect(x: origin.x + left, y: origin.y + frame.minY, width: max(20, width), height: frame.height)
    }

    /// Keys while blocks are selected: Return edits the first block, typing ends the
    /// block selection and types at the end of the last block.
    func handleBlockSelectionKey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask).subtracting([.capsLock, .numericPad, .function])
        guard flags.isDisjoint(with: [.command, .control]) else { return false }
        if event.keyCode == 36 || event.keyCode == 76 {  // Return, Enter
            let first = blockSelectionRange().map { contentRange(of: paragraph(at: $0.location)) }
            endBlockSelection(caret: first.map(NSMaxRange))
            return true
        }
        guard let characters = event.characters, let scalar = characters.unicodeScalars.first,
              !(0xF700...0xF8FF).contains(scalar.value), scalar.value >= 0x20, scalar.value != 0x7F else { return false }
        // A printable character: back to the caret, after the last selected block.
        if let range = blockSelectionRange() {
            let last = paragraph(at: max(range.location, NSMaxRange(range) - 1))
            endBlockSelection(caret: NSMaxRange(contentRange(of: last)))
        }
        return false
    }

    /// Commands while blocks are selected (nil: not ours, NSTextView does it).
    func handleBlockSelectionCommand(_ selector: Selector) -> Bool? {
        switch selector {
        case #selector(NSResponder.cancelOperation(_:)):
            let first = blockSelectionRange().map { contentRange(of: paragraph(at: $0.location)) }
            endBlockSelection(caret: first.map(NSMaxRange))
            return true
        case #selector(NSResponder.moveUp(_:)), #selector(NSResponder.moveLeft(_:)):
            stepBlockSelection(by: -1, extending: false)
            return true
        case #selector(NSResponder.moveDown(_:)), #selector(NSResponder.moveRight(_:)):
            stepBlockSelection(by: 1, extending: false)
            return true
        case #selector(NSResponder.moveUpAndModifySelection(_:)):
            stepBlockSelection(by: -1, extending: true)
            return true
        case #selector(NSResponder.moveDownAndModifySelection(_:)):
            stepBlockSelection(by: 1, extending: true)
            return true
        case #selector(NSResponder.moveParagraphBackwardAndModifySelection(_:)):
            moveSelectedBlocks(by: -1)
            return true
        case #selector(NSResponder.moveParagraphForwardAndModifySelection(_:)):
            moveSelectedBlocks(by: 1)
            return true
        case #selector(NSResponder.deleteBackward(_:)), #selector(NSResponder.deleteForward(_:)),
             #selector(NSResponder.deleteWordBackward(_:)), #selector(NSResponder.deleteWordForward(_:)):
            deleteBlocks()
            return true
        case #selector(NSResponder.insertTab(_:)):
            indentSelectedBlocks(by: 1)
            return true
        case #selector(NSResponder.insertBacktab(_:)):
            indentSelectedBlocks(by: -1)
            return true
        case #selector(NSResponder.insertNewline(_:)):
            let first = blockSelectionRange().map { contentRange(of: paragraph(at: $0.location)) }
            endBlockSelection(caret: first.map(NSMaxRange))
            return true
        default:
            return nil
        }
    }

    /// ↑/↓ choose the block above or below; with ⇧ the selection grows or shrinks by a block.
    func stepBlockSelection(by step: Int, extending: Bool) {
        guard let selection = blockSelection, let head = range(ofBlock: selection.head),
              let anchor = range(ofBlock: selection.anchor), let selected = blockSelectionRange() else { return }
        func id(at location: Int) -> String? { storage.blockInfo(at: paragraph(at: location).location)?.id }
        if !extending {
            let target: Int? = step < 0 ? (selected.location > 0 ? blockStart(before: selected.location) : nil)
                : (NSMaxRange(selected) < string.length ? NSMaxRange(selected) : nil)
            guard let target, let new = id(at: target) else { NSSound.beep(); return }
            selectBlocks(anchor: new, head: new)
            return
        }
        var newHead: Int?
        if step > 0 {
            if head.location < anchor.location {
                newHead = NSMaxRange(subtree(at: head.location))
            } else if NSMaxRange(selected) < string.length {
                newHead = NSMaxRange(selected)
            }
        } else {
            if head.location > anchor.location {
                newHead = blockStart(before: head.location)
            } else if selected.location > 0 {
                newHead = blockStart(before: selected.location)
            }
        }
        guard let location = newHead, location < string.length, let new = id(at: location) else { NSSound.beep(); return }
        selectBlocks(anchor: selection.anchor, head: new)
    }

    /// The start of the block before `location`: the nearest paragraph above at the depth
    /// of the paragraph at `location` or shallower (a sibling or the parent).
    func blockStart(before location: Int) -> Int {
        let depth = location < string.length ? (storage.blockInfo(at: location)?.depth ?? 0) : 0
        var cursor = paragraph(at: location - 1).location
        while cursor > 0 {
            guard let info = storage.blockInfo(at: cursor), info.depth > depth else { break }
            cursor = paragraph(at: cursor - 1).location
        }
        return cursor
    }

    func indentSelectedBlocks(by delta: Int) {
        let paragraphs = selectedParagraphs()
        keepingBlockSelection {
            undoStep(delta < 0 ? UndoNames.outdent : UndoNames.indent) { indent(paragraphs: paragraphs, by: delta) }
        }
    }

    /// Runs an edit of the selected blocks and selects them again after it (the edit
    /// moves the text selection, which would end the block selection).
    func keepingBlockSelection(_ body: () -> Void) {
        let kept = blockSelection
        body()
        if let kept, range(ofBlock: kept.anchor) != nil, range(ofBlock: kept.head) != nil {
            selectBlocks(anchor: kept.anchor, head: kept.head)
        }
    }

    // MARK: Move, delete, duplicate

    /// Moves the selected blocks (with their children) past the block above or below.
    /// At the edge of their parent they leave it (above the parent, or after it).
    func moveSelectedBlocks(by direction: Int) {
        let moving = targetBlocksRange()
        guard let info = storage.blockInfo(at: moving.location) else { return }
        keepingBlockSelection { undoStep(UndoNames.move) {
            if direction < 0 {
                guard moving.location > 0 else { NSSound.beep(); return }
                let target = blockStart(before: moving.location)
                let targetDepth = storage.blockInfo(at: target)?.depth ?? 0
                // Past a sibling, or out of the parent (above it, at its depth).
                move(moving, to: target, depthDelta: min(0, targetDepth - info.depth))
            } else {
                guard NSMaxRange(moving) < string.length else { NSSound.beep(); return }
                let next = NSMaxRange(moving)
                let nextDepth = storage.blockInfo(at: next)?.depth ?? 0
                let after = NSMaxRange(subtree(at: next))
                move(moving, to: after, depthDelta: min(0, nextDepth - info.depth))
            }
        } }
    }

    /// Moves whole paragraphs to the paragraph boundary `location` (outside them), their
    /// depths moved by `depthDelta`: one removal and one insertion in the current step.
    func move(_ moving: NSRange, to location: Int, depthDelta: Int) {
        guard location <= moving.location || location >= NSMaxRange(moving) else { return }
        let movedId = storage.blockInfo(at: moving.location)?.id
        if location == moving.location || location == NSMaxRange(moving) {
            guard depthDelta != 0 else { return }
            let text = shifted(storage.attributedSubstring(from: moving), by: depthDelta)
            replace(moving, with: text)
        } else {
            let text = shifted(storage.attributedSubstring(from: moving), by: depthDelta)
            replaceAll([(moving, NSAttributedString()), (NSRange(location: location, length: 0), text)])
        }
        invalidateNumbering(around: min(location, max(0, string.length - 1)))
        if blockSelection == nil, let movedId, let moved = range(ofBlock: movedId) {
            textView.setSelectedRange(NSRange(location: moved.location, length: 0))
        }
    }

    /// Deletes the selected blocks (or the blocks the selection touches) with their children.
    func deleteBlocks(name: String = UndoNames.delete) {
        let range = targetBlocksRange()
        endBlockSelection(caret: nil)
        undoStep(name) {
            let (removed, replacement) = removal(of: range)
            replace(removed, with: replacement)
            let caret = min(range.location, max(0, string.length - 1))
            textView.setSelectedRange(NSRange(location: caret, length: 0))
        }
        invalidateNumbering(around: min(range.location, max(0, string.length - 1)))
    }

    /// Puts copies of the selected blocks (new ids, children included) after them.
    func duplicateBlocks() {
        let range = targetBlocksRange()
        let originals = blocks(in: range)
        let reference = rootEditor.noteReference?()
        let copies = originals.map { block in
            var copy = Self.withFreshIds(block)
            var origin: [String: JSONValue] = ["operation": .string("duplicate"), "block_id": .string(block.id)]
            if let reference, !reference.id.isEmpty {
                origin["document_id"] = .string(reference.id)
                origin["document_title"] = .string(reference.title)
            }
            copy.newProvenance = .object(["source": .string("manual"), "origin": .object(origin)])
            return copy
        }
        guard let first = copies.first, let last = copies.last else { return }
        let wasSelecting = blockSelection != nil
        let caretOffset = textView.selectedRange().location - range.location
        undoStep(UndoNames.duplicate) {
            replace(NSRange(location: NSMaxRange(range), length: 0), with: text(for: copies))
        }
        if wasSelecting {
            // The copies are selected (as Finder selects what Duplicate made).
            let lastTop = copies.last { $0.depth == first.depth } ?? last
            selectBlocks(anchor: first.id, head: lastTop.id)
        } else if let copy = self.range(ofBlock: first.id) {
            textView.setSelectedRange(NSRange(location: min(copy.location + max(0, caretOffset), NSMaxRange(contentRange(of: copy))), length: 0))
        }
    }

    /// `block` with new ids for it and everything inside it (rows, cells, columns and
    /// their blocks): a copy that is inserted, never a move of the original.
    static func withFreshIds(_ block: EditorBlock) -> EditorBlock {
        var copy = block
        copy.id = EditorBlock.newId()
        if var table = copy.table {
            for row in table.rows.indices {
                table.rows[row].id = EditorBlock.newId()
                for cell in table.rows[row].cells.indices {
                    table.rows[row].cells[cell].id = EditorBlock.newId()
                    table.rows[row].cells[cell].paragraphId = EditorBlock.newId()
                    table.rows[row].cells[cell].hasParagraph = false
                    table.rows[row].cells[cell].nested = table.rows[row].cells[cell].nested.map(freshNode)
                }
            }
            copy.table = table
        }
        if let columns = copy.columns {
            copy.columns = columns.map { column in
                var column = column
                column.id = EditorBlock.newId()
                column.blocks = column.blocks.map(withFreshIds)
                return column
            }
        }
        return copy
    }

    private static func freshNode(_ node: BlockNode) -> BlockNode {
        var copy = node
        copy.id = EditorBlock.newId()
        copy.children = node.children.map(freshNode)
        return copy
    }

    // MARK: Turn Into

    /// Turns the selected blocks (or the one with the caret) into `target`.
    func turnInto(_ target: BlockTurnInto) {
        let paragraphs = selectedParagraphs()
        guard !paragraphs.isEmpty else { return }
        let selecting = blockSelection
        switch target {
        case .kind(let kind):
            let textual = paragraphs.filter { storage.blockInfo(at: $0.location)?.kind.hasInlineText ?? false }
            guard !textual.isEmpty else { NSSound.beep(); return }
            undoStep(UndoNames.kind(kind)) {
                if kind == .quote {
                    makeQuoteLines(textual, container: quoteContainer(around: textual, type: "quote"))
                } else {
                    updateBlocks(textual) { $0.with(kind: kind, checked: kind == .check ? $0.checked : false) }
                }
            }
        case .callout:
            let textual = paragraphs.filter { storage.blockInfo(at: $0.location)?.kind.hasInlineText ?? false }
            guard !textual.isEmpty else { NSSound.beep(); return }
            undoStep(UndoNames.callout) {
                makeQuoteLines(textual, container: quoteContainer(around: textual, type: "callout"))
            }
        case .divider:
            guard paragraphs.count == 1, let paragraph = paragraphs.first, let info = storage.blockInfo(at: paragraph.location),
                  info.kind.hasInlineText, contentRange(of: paragraph).length == 0 else { NSSound.beep(); return }
            undoStep(UndoNames.divider) {
                let divider = EditorBlock(id: info.id, kind: .divider, depth: info.depth)
                replace(paragraph, with: text(for: [divider]), selecting: NSRange(location: paragraph.location, length: 0))
            }
        case .table:
            guard let paragraph = paragraphs.first, let info = storage.blockInfo(at: paragraph.location), info.kind.hasInlineText else {
                NSSound.beep(); return
            }
            undoStep(UndoNames.insertTable) {
                var table = TableContent.blank()
                table.setRuns(block(at: paragraph).runs, row: 0, column: 0)
                let tableBlock = EditorBlock(kind: .table, depth: info.depth, table: table)
                replace(paragraph, with: text(for: [tableBlock]), selecting: NSRange(location: paragraph.location, length: 0))
            }
        case .columns(let count):
            makeColumns(count)
            return
        }
        if let selecting { selectBlocks(anchor: selecting.anchor, head: selecting.head) }
    }

    /// The container new quote lines join: the one of the lines right above or below
    /// when it is of the same type, else a new one.
    func quoteContainer(around paragraphs: [NSRange], type: String) -> QuoteContainer {
        guard let first = paragraphs.first, let last = paragraphs.last else { return QuoteContainer(id: EditorBlock.newId(), type: type) }
        if first.location > 0, let above = storage.blockInfo(at: paragraph(at: first.location - 1).location),
           above.kind == .quote, let container = above.container, container.type == type {
            return container
        }
        if NSMaxRange(last) < string.length, let below = storage.blockInfo(at: NSMaxRange(last)),
           below.kind == .quote, let container = below.container, container.type == type {
            return container
        }
        return QuoteContainer(id: EditorBlock.newId(), type: type)
    }

    /// Makes paragraphs quote lines of `container`. Lines of another container after them
    /// that would join the one before them again get a container of their own (a quote
    /// split by a callout stays two quotes).
    func makeQuoteLines(_ paragraphs: [NSRange], container: QuoteContainer) {
        guard let first = paragraphs.first, let last = paragraphs.last else { return }
        let above = first.location > 0 ? storage.blockInfo(at: paragraph(at: first.location - 1).location) : nil
        var following: [NSRange] = []
        if let aboveContainer = above?.container, above?.kind == .quote, aboveContainer.id != container.id {
            var cursor = NSMaxRange(last)
            while cursor < string.length {
                let next = paragraph(at: cursor)
                guard let info = storage.blockInfo(at: next.location), info.kind == .quote,
                      info.container?.id == aboveContainer.id || paragraphs.contains(next) else { break }
                if !paragraphs.contains(next) { following.append(next) }
                cursor = NSMaxRange(next)
            }
        }
        let split = following.first.flatMap { storage.blockInfo(at: $0.location)?.container }
            .map { QuoteContainer(id: EditorBlock.newId(), type: $0.type, attrs: $0.attrs) }
        let all = paragraphs + following
        let selection = textView.selectedRange()
        changeAttributes(in: NSUnionRange(first, all.last ?? last)) { storage in
            for paragraph in all {
                guard let info = storage.blockInfo(at: paragraph.location) else { continue }
                let target = paragraphs.contains(paragraph) ? container : (split ?? container)
                storage.addAttribute(.fnBlock, value: info.with(kind: .quote, container: target), range: paragraph)
            }
        }
        textView.setSelectedRange(selection)
        syncTypingAttributes()
    }

    // MARK: Columns

    /// The selected blocks (or the one with the caret) become `count` columns: as many
    /// blocks to a column as there are blocks to share, else the blocks in the first.
    func makeColumns(_ count: Int) {
        guard !isEmbedded else { NSSound.beep(); return }  // Columns don't nest.
        let range = targetBlocksRange()
        let selected = blocks(in: range)
        guard let first = selected.first, !selected.contains(where: { $0.kind == .columns }) else { NSSound.beep(); return }
        let groups = Self.topLevelGroups(selected)
        var columns: [EditorColumn] = []
        if groups.count >= count {
            let size = Int((Double(groups.count) / Double(count)).rounded(.up))
            var index = 0
            while index < groups.count {
                let chunk = groups[index..<min(groups.count, index + size)].flatMap { $0 }
                columns.append(EditorColumn(blocks: Self.rebased(chunk, to: 0)))
                index += size
            }
        } else {
            columns.append(EditorColumn(blocks: Self.rebased(selected, to: 0)))
        }
        while columns.count < count { columns.append(EditorColumn(blocks: [EditorBlock()])) }
        let columnsBlock = EditorBlock(kind: .columns, depth: first.depth, columns: columns)
        endBlockSelection(caret: nil)
        undoStep(UndoNames.columns) {
            replace(range, with: text(for: [columnsBlock]), selecting: NSRange(location: range.location, length: 0))
        }
        selectBlocks(anchor: columnsBlock.id, head: columnsBlock.id)
    }

    /// Blocks grouped as top-level blocks with their children.
    static func topLevelGroups(_ blocks: [EditorBlock]) -> [[EditorBlock]] {
        let base = blocks.map(\.depth).min() ?? 0
        var groups: [[EditorBlock]] = []
        for block in blocks {
            if block.depth <= base || groups.isEmpty { groups.append([block]) } else { groups[groups.count - 1].append(block) }
        }
        return groups
    }

    /// The columns block a command acts on: the one this column's editor is in, or the
    /// selected one. With its paragraph in the note's text and which column.
    func targetColumns() -> (editor: BlockEditorController, paragraph: NSRange, column: Int?)? {
        if let host = columnsHost, let parent = parentEditor, let attachment = host.attachment,
           let range = parent.range(of: attachment) {
            return (parent, parent.paragraph(at: range.location), host.builtEditors.firstIndex { $0 === self })
        }
        let current = paragraph(at: targetBlocksRange().location)
        guard storage.blockInfo(at: current.location)?.kind == .columns else { return nil }
        return (self, current, nil)
    }

    /// Adds an empty column (after the one with the caret, else at the end).
    func addColumn() {
        guard let target = targetColumns() else { NSSound.beep(); return }
        target.editor.rewriteColumns(at: target.paragraph, name: UndoNames.addColumn) { columns in
            let index = target.column.map { $0 + 1 } ?? columns.count
            columns.insert(EditorColumn(blocks: [EditorBlock()]), at: min(index, columns.count))
        }
    }

    /// Removes the column with the caret (or the last): its blocks join the column before
    /// it (or after it); one column left unwraps.
    func removeColumn() {
        guard let target = targetColumns() else { NSSound.beep(); return }
        target.editor.rewriteColumns(at: target.paragraph, name: UndoNames.removeColumn) { columns in
            let index = target.column ?? columns.count - 1
            guard columns.indices.contains(index) else { return }
            let removed = columns.remove(at: index)
            let kept = removed.blocks.filter { !($0.kind.hasInlineText && $0.runs.isEmpty) }
            guard !kept.isEmpty, !columns.isEmpty else { return }
            let into = max(0, index - 1)
            if index == 0 { columns[0].blocks = kept + columns[0].blocks } else { columns[into].blocks += kept }
        }
    }

    /// The columns' blocks back in the note, one column after another.
    func unwrapColumns() {
        guard let target = targetColumns() else { NSSound.beep(); return }
        target.editor.rewriteColumns(at: target.paragraph, name: UndoNames.unwrapColumns) { columns in
            columns = [EditorColumn(blocks: columns.flatMap(\.blocks))]
        }
    }

    /// Replaces a columns block with `change` applied to its columns, as one step. Empty
    /// columns go away; one column left is unwrapped into the note.
    func rewriteColumns(at paragraph: NSRange, name: String, _ change: (inout [EditorColumn]) -> Void) {
        let edit = columnsEdit(at: paragraph, change)
        endBlockSelection(caret: nil)
        undoStep(name) { replace(edit.0, with: edit.1, selecting: NSRange(location: paragraph.location, length: 0)) }
        if let columns = edit.2 {
            selectBlocks(anchor: columns, head: columns)
        } else {
            textView.window?.makeFirstResponder(textView)
        }
    }

    /// The edit that gives a columns block new columns (nil id: it was unwrapped).
    func columnsEdit(at paragraph: NSRange, _ change: (inout [EditorColumn]) -> Void) -> (NSRange, NSAttributedString, String?) {
        var columnsBlock = block(at: paragraph)
        var columns = columnsBlock.columns ?? []
        change(&columns)
        columns.removeAll { $0.blocks.isEmpty }
        if columns.count <= 1 {
            let blocks = Self.rebased(columns.first?.blocks ?? [EditorBlock()], to: columnsBlock.depth)
            return (paragraph, text(for: blocks.isEmpty ? [EditorBlock(depth: columnsBlock.depth)] : blocks), nil)
        }
        columnsBlock.columns = columns
        return (paragraph, text(for: [columnsBlock]), columnsBlock.id)
    }

    // MARK: Copy Link to Block

    static let blockReferencePasteboardType = NSPasteboard.PasteboardType("app.fortress.notes.block-reference")

    /// Puts a link to the block with the caret (or the first selected) on the pasteboard:
    /// a block reference for pasting into notes, and Obsidian's `[[Note#^id]]` as text.
    func copyLinkToBlock(_ pasteboard: NSPasteboard = .general) {
        let target = selectedParagraphs().first ?? paragraph(at: textView.selectedRange().location)
        guard let info = storage.blockInfo(at: target.location) else { return }
        let reference = rootEditor.noteReference?() ?? (id: "", title: "")
        let title = reference.title.isEmpty ? String(localized: "Untitled") : reference.title
        let label = BlockCodec.plainText(block(at: target).runs)
        BlockReferenceLabels.labels[info.id] = label.isEmpty ? nil : label
        pasteboard.clearContents()
        let payload: [String: String] = ["document_id": reference.id, "block_id": info.id, "title": title]
        if let data = try? JSONEncoder().encode(payload) {
            pasteboard.setData(data, forType: Self.blockReferencePasteboardType)
        }
        pasteboard.setString("[[\(Self.wikilinkName(title))#^\(info.id)]]", forType: .string)
    }

    /// A title as Obsidian's wikilink syntax can hold it (`[`, `]`, `|`, `#`, `^` can't be in one).
    static func wikilinkName(_ title: String) -> String {
        String(title.map { "[]|#^".contains($0) ? " " : $0 })
    }

    /// Pastes a copied block link as a block reference (its label is the block's text).
    func pasteBlockReference(from pasteboard: NSPasteboard) -> Bool {
        guard let data = pasteboard.data(forType: Self.blockReferencePasteboardType),
              let payload = try? JSONDecoder().decode([String: String].self, from: data),
              let id = payload["block_id"], !id.isEmpty else { return false }
        if let label = BlockReferenceLabels.labels[id] { context.referenceText[id] = label }
        let selection = textView.selectedRange()
        let current = info(at: selection.location)
        guard current.kind.hasInlineText, current.kind != .code else { return false }
        let node = DocumentText.inline([.atomic(NoteLinks.blockNode(id: id))], info: current, context: context, theme: theme)
        undoStep(UndoNames.paste) {
            replace(selection, with: node, selecting: NSRange(location: selection.location + node.length, length: 0))
        }
        return true
    }

    // MARK: Inserting blocks

    /// Inserts an empty paragraph after the block at `location` (with its children) and
    /// opens the `/` menu in it.
    func insertBlockBelow(at location: Int) {
        endBlockSelection(caret: nil)
        let range = subtree(at: location)
        let depth = storage.blockInfo(at: range.location)?.depth ?? 0
        let at = NSMaxRange(range)
        undoStep(UndoNames.insertBlock) {
            replace(NSRange(location: at, length: 0), with: attributed("/\n", info: BlockInfo(depth: depth)),
                    selecting: NSRange(location: at + 1, length: 0))
        }
        textView.window?.makeFirstResponder(textView)
        slashMenu.start(anchor: at)
    }

    /// Format ▸ Insert Block… : the `/` menu where the caret is (in a new block below
    /// when the caret's block has text).
    func insertBlockAtCaret() {
        endBlockSelection(caret: nil)
        let caret = textView.selectedRange().location
        let current = paragraph(at: caret)
        let info = storage.blockInfo(at: current.location) ?? BlockInfo()
        if info.kind.hasInlineText, info.kind != .code, contentRange(of: current).length == 0 {
            undoStep(UndoNames.insertBlock) {
                replace(NSRange(location: current.location, length: 0), with: attributed("/", info: info),
                        selecting: NSRange(location: current.location + 1, length: 0))
            }
            slashMenu.start(anchor: current.location)
        } else {
            insertBlockBelow(at: caret)
        }
    }

    /// Puts `blocks` in place of the empty paragraph at `paragraph`, or after its block
    /// when it has text; the caret goes into the first new place to type.
    func insertBlocks(_ blocks: [EditorBlock], at paragraph: NSRange) {
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        let empty = info.kind.hasInlineText && contentRange(of: paragraph).length == 0
        var placed = Self.rebased(blocks, to: info.depth)
        // A block that isn't text gets a paragraph after it when it would end the note.
        let target = empty ? paragraph : NSRange(location: NSMaxRange(subtree(at: paragraph.location)), length: 0)
        if let last = placed.last, !last.kind.hasInlineText, NSMaxRange(target) >= string.length {
            placed.append(EditorBlock(depth: info.depth))
        }
        replace(target, with: text(for: placed), selecting: NSRange(location: target.location, length: 0))
        let firstKind = placed.first?.kind ?? .paragraph
        if !firstKind.hasInlineText, let attachment = storage.attribute(.attachment, at: target.location, effectiveRange: nil) as? BlockAttachment {
            Self.later { [weak self, weak attachment] in
                self?.textView.textLayoutManager?.textViewportLayoutController.layoutViewport()
                if let live = attachment?.live {
                    live.focus(atEnd: false)
                } else if let self, NSMaxRange(target) < self.string.length {
                    let next = self.paragraph(at: target.location + 2)
                    self.textView.setSelectedRange(NSRange(location: next.location, length: 0))
                }
            }
        }
    }

    // MARK: Math

    /// Shows the LaTeX of a math block for editing (or of a new one, put in with `apply`).
    func editMath(at paragraph: NSRange?, initial: String, apply: @escaping (String) -> Void) {
        let rect: NSRect
        if let paragraph, let frame = blockFrame(at: CGPoint(x: textView.textContainerOrigin.x + 1, y: selectionFrame(paragraph).minY + 2))?.1 {
            rect = frame
        } else {
            rect = caretRect()
        }
        MathEditor.show(relativeTo: rect, of: textView, initial: initial) { [weak self] result in
            guard let self else { return }
            self.textView.window?.makeFirstResponder(self.textView)
            if let result { apply(result) }
        }
    }

    /// Edit Math… for the selected math block.
    func editSelectedMath() {
        let current = paragraph(at: targetBlocksRange().location)
        guard let info = storage.blockInfo(at: current.location), info.kind == .math else { NSSound.beep(); return }
        let latex = info.origin?.origin.payload?["latex"]?.stringValue ?? ""
        editMath(at: current, initial: latex) { [weak self] latex in
            guard let self, let range = self.range(ofBlock: info.id) else { return }
            var block = self.block(at: range)
            var origin = block.origin ?? BlockOrigin(type: "math", kind: .math)
            origin.payload = .object(["latex": .string(latex)])
            block.origin = origin
            self.endBlockSelection(caret: nil)
            self.undoStep(UndoNames.math) {
                self.replace(range, with: self.text(for: [block]), selecting: NSRange(location: range.location, length: 0))
            }
        }
    }

    static func mathBlock(_ latex: String, depth: Int = 0) -> EditorBlock {
        let origin = BlockOrigin(type: "math", payload: .object(["latex": .string(latex)]), text: latex, kind: .math)
        return EditorBlock(kind: .math, depth: depth, origin: origin)
    }

    // MARK: Files

    /// The open panel for Image… and File… in the `/` menu.
    static func openPanel(images: Bool, completion: @escaping @MainActor ([URL]) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        if images { panel.allowedContentTypes = [.image] }
        panel.begin { response in
            MainActor.assumeIsolated { completion(response == .OK ? panel.urls : []) }
        }
    }

    /// Picks files and puts them in as picture or file blocks after the block at `location`.
    func chooseAndInsertFiles(images: Bool, after location: Int) {
        let picker = chooseFiles ?? { images, completion in Self.openPanel(images: images, completion: completion) }
        let anchor = storage.blockInfo(at: paragraph(at: location).location)?.id
        picker(images) { [weak self] urls in
            guard let self, !urls.isEmpty else { return }
            let at = anchor.flatMap { self.range(ofBlock: $0) }.map(\.location) ?? location
            self.insertFiles(urls, after: at)
        }
    }

    /// Uploads files and puts them in as picture or file blocks after the block at `location`.
    func insertFiles(_ urls: [URL], after location: Int) {
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("fortress.insert.\(UUID().uuidString)"))
        pasteboard.clearContents()
        pasteboard.writeObjects(urls as [NSURL])
        _ = pasteFiles(from: pasteboard, at: location)
        pasteboard.releaseGlobally()
    }
}

/// The text of blocks whose links were copied in this session, so a pasted reference
/// shows it before the server resolves it.
@MainActor
enum BlockReferenceLabels {
    static var labels: [String: String] = [:]
}

/// The selected blocks' highlight: drawn over the text, never in the way of the mouse.
final class BlockSelectionOverlay: NSView {
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 6, yRadius: 6)
        NSColor.themeAccent.withAlphaComponent(0.12).setFill()
        path.fill()
        NSColor.themeAccent.withAlphaComponent(0.45).setStroke()
        path.lineWidth = 1
        path.stroke()
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func isAccessibilityElement() -> Bool { false }
}

/// View ▸ Show Blocks (off by default): faint outlines and handles on every block.
enum BlockOutlines {
    static let key = "showBlocks"
    static var isOn: Bool { UserDefaults.standard.bool(forKey: key) }
}

extension UserDefaults {
    /// `BlockOutlines.key`, observable (the name matches the key).
    @objc dynamic var showBlocks: Bool { bool(forKey: BlockOutlines.key) }
}
