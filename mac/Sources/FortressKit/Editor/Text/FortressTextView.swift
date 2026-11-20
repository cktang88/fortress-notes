import AppKit

/// The note body: a TextKit 2 text view with a centered column and a header (title and
/// meta line) hosted above the text. Editing behavior lives in `BlockEditorController`.
final class FortressTextView: NSTextView {
    weak var controller: BlockEditorController?
    /// Title and meta line, laid out above the text.
    var header: NSView? {
        didSet {
            oldValue?.removeFromSuperview()
            if let header { addSubview(header) }
            layoutColumn()
        }
    }
    var theme = EditorTheme() { didSet { layoutColumn() } }
    /// Measure in characters (DESIGN a.3: Narrow 60 / Comfortable 68 / Wide 80).
    var measureCharacters: CGFloat = 68 { didSet { layoutColumn() } }
    /// A column's text inside a columns block: no header, no gutters.
    var isEmbedded = false { didSet { layoutColumn() } }
    private var headerHeight: CGFloat = 0
    /// The note's own undo stack. Text undo stays apart from the window's (where the
    /// list's Move to Trash, moves and renames are undone), so ⌘Z in the note undoes
    /// typing and ⌘Z in the list undoes list actions. One stack per note: the note's
    /// text, its columns' text and its table cells all register here (UNDO_AUDIT.md).
    let noteUndo = UndoManager()

    /// The note's stack: the editor's (shared with the columns and tables inside it),
    /// else the one of the note text view this view sits in, else its own. Not found by
    /// walking up the views alone: TextKit takes a column's text view out of the window
    /// and back as it lays out, and a view out of the window found no stack.
    override var undoManager: UndoManager? {
        controller?.context.undoManager ?? noteUndoManager ?? noteUndo
    }

    // Edit ▸ Undo and Redo (⌘Z, ⇧⌘Z) go up the responder chain to the window, which
    // undoes on *its* undo manager (the list's Move to Trash and moves), not on the
    // text view's: the note's stack was never reached from the menu. The note's text
    // views (this, a column's, a table cell's) answer them first, with the note's stack.
    @objc(undo:) func undoNoteEdit(_ sender: Any?) { NoteUndoActions.undo(undoManager, from: self) }
    @objc(redo:) func redoNoteEdit(_ sender: Any?) { NoteUndoActions.redo(undoManager, from: self) }

    override func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        NoteUndoActions.validate(menuItem, undoManager) ?? super.validateMenuItem(menuItem)
    }

    static func make() -> FortressTextView {
        let textView = FortressTextView(usingTextLayoutManager: true)
        textView.isRichText = true
        textView.importsGraphics = false
        textView.allowsUndo = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.drawsBackground = true
        textView.backgroundColor = .themeBackground
        textView.insertionPointColor = .themeAccent
        textView.selectedTextAttributes = [.backgroundColor: NSColor.themeSelection]
        textView.usesFindBar = true
        textView.isIncrementalSearchingEnabled = true
        // Smart quotes and dashes follow System Settings ▸ Keyboard ▸ Text Input (the app
        // has no setting of its own); link detection is off because links are marks.
        textView.isAutomaticQuoteSubstitutionEnabled = NSSpellChecker.isAutomaticQuoteSubstitutionEnabled
        textView.isAutomaticDashSubstitutionEnabled = NSSpellChecker.isAutomaticDashSubstitutionEnabled
        textView.isAutomaticLinkDetectionEnabled = false
        textView.isContinuousSpellCheckingEnabled = true
        textView.smartInsertDeleteEnabled = true
        textView.displaysLinkToolTips = true
        textView.linkTextAttributes = [
            .foregroundColor: NSColor.themeLink,
            .cursor: NSCursor.pointingHand,
        ]
        textView.writingToolsBehavior = .complete
        textView.textContainer?.lineFragmentPadding = 0
        return textView
    }

    /// The column: `min(available − 2×48, 68 characters)`, centered.
    var columnWidth: CGFloat {
        if isEmbedded { return bounds.width }
        let font = theme.bodyFont()
        let sample = "abcdefghijklmnopqrstuvwxyz " as NSString
        let average = sample.size(withAttributes: [.font: font]).width / CGFloat(sample.length)
        let measure = (measureCharacters * average).rounded()
        return max(200, min(bounds.width - 96, measure))
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        layoutColumn()
        controller?.layoutBlockChrome()
    }

    func layoutColumn() {
        if isEmbedded {
            if textContainerInset != .zero { textContainerInset = .zero }
            return
        }
        let width = columnWidth
        let side = max(48, ((bounds.width - width) / 2).rounded(.down))
        if let header {
            header.frame = CGRect(x: side, y: 36, width: width, height: max(reportedHeaderHeight, 40))
            headerHeight = max(reportedHeaderHeight, 40)
        }
        let top = 36 + headerHeight + 12
        let inset = NSSize(width: side, height: top)
        if textContainerInset != inset { textContainerInset = inset }
    }

    /// The header's height as SwiftUI laid it out (its title wrapped, say).
    private var reportedHeaderHeight: CGFloat = 80
    func headerDidChange(height: CGFloat) {
        guard abs(height - reportedHeaderHeight) > 0.5 else { return }
        reportedHeaderHeight = height
        layoutColumn()
    }

    // A column's text is spell-checked while it is being edited (a note with many
    // columns scrolled into view would otherwise check them all at once).
    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted, isEmbedded { isContinuousSpellCheckingEnabled = true }
        return accepted
    }

    override func resignFirstResponder() -> Bool {
        let accepted = super.resignFirstResponder()
        if accepted, isEmbedded { isContinuousSpellCheckingEnabled = false }
        // Focus moving to a table cell, a column or the title ends this typing step, so
        // what is typed there isn't merged into it.
        if accepted { breakUndoCoalescing() }
        return accepted
    }

    // MARK: Keys

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if window?.firstResponder === self, let controller, controller.handleKeyEquivalent(event) { return true }
        return super.performKeyEquivalent(with: event)
    }

    override func keyDown(with event: NSEvent) {
        // One interval per key in Instruments (Points of Interest): the whole edit, from
        // the key to the text, normalizing and the change callbacks (not the redraw).
        let interval = Signposts.begin("Keystroke")
        defer { Signposts.end("Keystroke", interval) }
        if let controller, controller.handleKeyDown(event) { return }
        super.keyDown(with: event)
    }

    // MARK: The whole text (a long note may still be loading its last blocks)

    override func selectAll(_ sender: Any?) {
        controller?.finishLoading()
        super.selectAll(sender)
    }

    // A column's Find is the note's (NotePrinting.swift): its own find bar would search
    // the column alone.
    override func performTextFinderAction(_ sender: Any?) {
        if isEmbedded, forwardFindToNote(sender, panel: false) { return }
        controller?.finishLoading()
        super.performTextFinderAction(sender)
    }

    override func performFindPanelAction(_ sender: Any?) {
        if isEmbedded, forwardFindToNote(sender, panel: true) { return }
        controller?.finishLoading()
        super.performFindPanelAction(sender)
    }

    /// File ▸ Print…: the whole note, from the note's text or a column's.
    override func printView(_ sender: Any?) {
        guard let controller else { return super.printView(sender) }
        controller.printNote()
    }

    override func moveToEndOfDocument(_ sender: Any?) {
        controller?.finishLoading()
        super.moveToEndOfDocument(sender)
    }

    override func moveToEndOfDocumentAndModifySelection(_ sender: Any?) {
        controller?.finishLoading()
        super.moveToEndOfDocumentAndModifySelection(sender)
    }

    override func scrollToEndOfDocument(_ sender: Any?) {
        controller?.finishLoading()
        super.scrollToEndOfDocument(sender)
    }

    // MARK: Mouse

    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        // A click puts the caret back (blocks selected with Esc are let go).
        controller?.endBlockSelection(caret: nil)
        if let controller, controller.handleClick(at: point) { return }
        super.mouseDown(with: event)
    }

    /// Copy of selected blocks: the blocks whole (blocks, Markdown, RTF, plain text).
    override func copy(_ sender: Any?) {
        guard let controller, controller.blockSelection != nil else { super.copy(sender); return }
        controller.withBlocksSelectedAsText { super.copy(sender) }
    }

    /// Cut of selected blocks: copied, then deleted as blocks (one "Cut" step).
    override func cut(_ sender: Any?) {
        if let controller, controller.blockSelection != nil {
            copy(sender)
            controller.deleteBlocks(name: UndoNames.cut)
            return
        }
        super.cut(sender)
    }

    override func paste(_ sender: Any?) {
        if let controller, controller.pasteFiles(from: .general) { return }
        let before = controller?.blockIds() ?? []
        let start = selectedRange().location
        if controller?.pasteConvertingSeparators(from: .general, plain: false) != true { super.paste(sender) }
        controller?.markPasted(from: start, to: selectedRange().location, existing: before,
                               sourceURL: Self.sourceURL(in: .general))
    }

    /// Paste and Match Style.
    override func pasteAsPlainText(_ sender: Any?) {
        if controller?.pasteConvertingSeparators(from: .general, plain: true) == true { return }
        super.pasteAsPlainText(sender)
    }

    /// The page web content was copied from, when the browser says.
    static func sourceURL(in pasteboard: NSPasteboard) -> String? {
        if let url = pasteboard.string(forType: NSPasteboard.PasteboardType("org.chromium.source-url")), !url.isEmpty {
            return url
        }
        if let data = pasteboard.data(forType: NSPasteboard.PasteboardType("com.apple.webarchive")),
           let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let main = plist["WebMainResource"] as? [String: Any], let url = main["WebResourceURL"] as? String,
           url.hasPrefix("http") {
            return url
        }
        return nil
    }

    // MARK: Hover (block info in the left margin)

    private var hoverArea: NSTrackingArea?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let hoverArea { removeTrackingArea(hoverArea) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseMoved, .mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect], owner: self)
        addTrackingArea(area)
        hoverArea = area
    }

    override func mouseMoved(with event: NSEvent) {
        super.mouseMoved(with: event)
        controller?.hover(at: convert(event.locationInWindow, from: nil))
    }

    override func mouseExited(with event: NSEvent) {
        super.mouseExited(with: event)
        controller?.hover(at: nil)
    }

    override func readSelection(from pboard: NSPasteboard, type: NSPasteboard.PasteboardType) -> Bool {
        if type == BlockEditorController.blocksPasteboardType, let controller, controller.pasteBlocks(from: pboard) { return true }
        if type == BlockEditorController.blockReferencePasteboardType, let controller, controller.pasteBlockReference(from: pboard) { return true }
        return super.readSelection(from: pboard, type: type)
    }

    override var readablePasteboardTypes: [NSPasteboard.PasteboardType] {
        [BlockEditorController.blocksPasteboardType, BlockEditorController.blockReferencePasteboardType] + super.readablePasteboardTypes
    }

    /// Copy puts the note's own blocks, Markdown, and the text system's rich and plain
    /// text on the pasteboard, so a copy pastes well into Mail, Notes, Slack and VS Code.
    override var writablePasteboardTypes: [NSPasteboard.PasteboardType] {
        [BlockEditorController.blocksPasteboardType, BlockEditorController.markdownPasteboardType]
            + super.writablePasteboardTypes
    }

    override func writeSelection(to pboard: NSPasteboard, type: NSPasteboard.PasteboardType) -> Bool {
        if type == BlockEditorController.blocksPasteboardType {
            return controller?.writeBlocks(to: pboard) ?? false
        }
        if type == BlockEditorController.markdownPasteboardType {
            return controller?.writeMarkdown(to: pboard) ?? false
        }
        return super.writeSelection(to: pboard, type: type)
    }

    /// Soft separators (`SoftBreaks`) go out as the spaces they stand for. (The text
    /// system writes its plain and rich text itself, not through `writeSelection(to:type:)`:
    /// they are written again here when the selection has a separator.)
    override func writeSelection(to pboard: NSPasteboard, types: [NSPasteboard.PasteboardType]) -> Bool {
        guard super.writeSelection(to: pboard, types: types) else { return false }
        guard let storage = textStorage else { return true }
        let ranges = selectedRanges.map(\.rangeValue).filter { $0.length > 0 && NSMaxRange($0) <= storage.length }
        guard ranges.contains(where: { storage.mutableString.range(of: SoftBreaks.separator, options: .literal, range: $0).location != NSNotFound })
        else { return true }
        let text = NSMutableAttributedString()
        for (index, range) in ranges.enumerated() {
            if index > 0 { text.append(NSAttributedString(string: "\n")) }
            text.append(SoftBreaks.plain(storage.attributedSubstring(from: range)))
        }
        let whole = NSRange(location: 0, length: text.length)
        let written = Set(pboard.types ?? [])
        if written.contains(.string) { pboard.setString(text.string, forType: .string) }
        if written.contains(.rtf), let data = text.rtf(from: whole, documentAttributes: [:]) { pboard.setData(data, forType: .rtf) }
        if written.contains(.rtfd), let data = text.rtfd(from: whole, documentAttributes: [:]) { pboard.setData(data, forType: .rtfd) }
        return true
    }

    // MARK: Dragging blocks by their handle (the note's editor places them, for columns too)

    override var acceptableDragTypes: [NSPasteboard.PasteboardType] {
        [BlockDrag.type] + super.acceptableDragTypes
    }

    override func draggingEntered(_ sender: any NSDraggingInfo) -> NSDragOperation {
        if BlockDrag.isBlockDrag(sender), let root = controller?.rootEditor { return root.blockDragUpdated(sender) }
        return super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: any NSDraggingInfo) -> NSDragOperation {
        if BlockDrag.isBlockDrag(sender), let root = controller?.rootEditor { return root.blockDragUpdated(sender) }
        return super.draggingUpdated(sender)
    }

    override func draggingExited(_ sender: (any NSDraggingInfo)?) {
        if let sender, BlockDrag.isBlockDrag(sender), let root = controller?.rootEditor { root.blockDragExited(); return }
        super.draggingExited(sender)
    }

    override func prepareForDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        if BlockDrag.isBlockDrag(sender) { return true }
        return super.prepareForDragOperation(sender)
    }

    override func concludeDragOperation(_ sender: (any NSDraggingInfo)?) {
        if let sender, BlockDrag.isBlockDrag(sender) { return }
        super.concludeDragOperation(sender)
    }

    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        if BlockDrag.isBlockDrag(sender), let root = controller?.rootEditor { return root.performBlockDrop(sender) }
        let point = convert(sender.draggingLocation, from: nil)
        if let controller, controller.pasteFiles(from: sender.draggingPasteboard, at: characterIndexForInsertion(at: point)) {
            return true
        }
        return super.performDragOperation(sender)
    }

    // MARK: Format actions (for menus)

    @objc func fortressToggleBold(_ sender: Any?) { controller?.toggleMark(\.bold) }
    @objc func fortressToggleItalic(_ sender: Any?) { controller?.toggleMark(\.italic) }
    @objc func fortressToggleUnderline(_ sender: Any?) { controller?.toggleMark(\.underline) }
    @objc func fortressToggleStrikethrough(_ sender: Any?) { controller?.toggleMark(\.strike) }
    @objc func fortressToggleInlineCode(_ sender: Any?) { controller?.toggleMark(\.code) }
    @objc func fortressToggleSuperscript(_ sender: Any?) { controller?.toggleMark(\.sup) }
    @objc func fortressToggleSubscript(_ sender: Any?) { controller?.toggleMark(\.sub) }
    @objc func fortressInsertTable(_ sender: Any?) { controller?.insertTable() }
    @objc func fortressHighlight(_ sender: Any?) { controller?.toggleHighlight("yellow") }
    @objc func fortressAddLink(_ sender: Any?) { controller?.editLink() }
    @objc func fortressBody(_ sender: Any?) { controller?.setKind(.paragraph) }
    @objc func fortressHeading1(_ sender: Any?) { controller?.setKind(.heading1) }
    @objc func fortressHeading2(_ sender: Any?) { controller?.setKind(.heading2) }
    @objc func fortressHeading3(_ sender: Any?) { controller?.setKind(.heading3) }
    @objc func fortressBulletedList(_ sender: Any?) { controller?.setKind(.bullet) }
    @objc func fortressNumberedList(_ sender: Any?) { controller?.setKind(.numbered) }
    @objc func fortressChecklist(_ sender: Any?) { controller?.setKind(.check) }
    @objc func fortressQuote(_ sender: Any?) { controller?.setKind(.quote) }
    @objc func fortressCodeBlock(_ sender: Any?) { controller?.setKind(.code) }
    @objc func fortressToggleCheckbox(_ sender: Any?) { controller?.toggleChecked() }
    @objc func fortressIndent(_ sender: Any?) { controller?.indent(by: 1) }
    @objc func fortressOutdent(_ sender: Any?) { controller?.indent(by: -1) }
    @objc func fortressMoveBlockUp(_ sender: Any?) { controller?.moveBlocks(by: -1) }
    @objc func fortressMoveBlockDown(_ sender: Any?) { controller?.moveBlocks(by: 1) }
    // Blocks (Format ▸ Block, the handle's menu)
    @objc func fortressDuplicateBlock(_ sender: Any?) { controller?.duplicateBlocks() }
    @objc func fortressDeleteBlock(_ sender: Any?) { controller?.deleteBlocks() }
    @objc func fortressCopyBlockLink(_ sender: Any?) { controller?.copyLinkToBlock() }
    @objc func fortressSelectBlock(_ sender: Any?) { controller?.selectBlocksAtCaret() }
    @objc func fortressInsertBlock(_ sender: Any?) { controller?.insertBlockAtCaret() }
    @objc func fortressTurnIntoTwoColumns(_ sender: Any?) { controller?.makeColumns(2) }
    @objc func fortressTurnIntoThreeColumns(_ sender: Any?) { controller?.makeColumns(3) }
    @objc func fortressAddColumn(_ sender: Any?) { controller?.addColumn() }
    @objc func fortressRemoveColumn(_ sender: Any?) { controller?.removeColumn() }
    @objc func fortressUnwrapColumns(_ sender: Any?) { controller?.unwrapColumns() }
    @objc func fortressBlockInfo(_ sender: Any?) { controller?.showBlockInfo() }

    // Standard Format ▸ Font items route here too.
    @objc override func underline(_ sender: Any?) { controller?.toggleMark(\.underline) }
}

extension NSView {
    /// The undo stack of the note this view is in: the outermost note text view's (a
    /// column's text, a table cell and the note itself share one).
    var noteUndoManager: UndoManager? {
        var view: NSView? = self
        var outermost: FortressTextView?
        while let current = view {
            if let text = current as? FortressTextView { outermost = text }
            view = current.superview
        }
        if let outermost { return outermost.controller?.context.undoManager ?? outermost.noteUndo }
        return nil
    }
}

/// Edit ▸ Undo and Redo for the note's text views (see `FortressTextView.undoNoteEdit`).
@MainActor
enum NoteUndoActions {
    static func undo(_ undoManager: UndoManager?, from view: NSView) {
        guard let undoManager, undoManager.canUndo else { NSSound.beep(); return }
        keepingFocus(in: view) { undoManager.undo() }
    }

    static func redo(_ undoManager: UndoManager?, from view: NSView) {
        guard let undoManager, undoManager.canRedo else { NSSound.beep(); return }
        keepingFocus(in: view) { undoManager.redo() }
    }

    /// Undoing the insertion of a table while typing in one of its cells takes the cell
    /// out of the window, and the window itself would answer the next ⌘Z (with the
    /// list's stack). Focus goes back to the note's text then.
    private static func keepingFocus(in view: NSView, _ body: () -> Void) {
        let window = view.window
        var note: FortressTextView?
        var current: NSView? = view
        while let found = current {
            if let text = found as? FortressTextView, !text.isEmbedded { note = text }
            current = found.superview
        }
        body()
        guard let window, let note, note.window === window else { return }
        // Attachment views (the table) leave the window as the text is laid out.
        note.textLayoutManager?.textViewportLayoutController.layoutViewport()
        let responder = window.firstResponder
        if responder === window || responder == nil || (responder as? NSView)?.window !== window {
            window.makeFirstResponder(note)
        }
    }

    /// Enables Undo and Redo and names them ("Undo Paste", "Redo Move"); nil for other items.
    static func validate(_ item: NSMenuItem, _ undoManager: UndoManager?) -> Bool? {
        if item.action == undoAction {
            item.title = undoManager?.undoMenuItemTitle ?? "Undo"
            return undoManager?.canUndo ?? false
        }
        if item.action == redoAction {
            item.title = undoManager?.redoMenuItemTitle ?? "Redo"
            return undoManager?.canRedo ?? false
        }
        return nil
    }

    /// The Edit menu's actions (AppKit declares no Swift name for them).
    static let undoAction = NSSelectorFromString("undo:")
    static let redoAction = NSSelectorFromString("redo:")
}
