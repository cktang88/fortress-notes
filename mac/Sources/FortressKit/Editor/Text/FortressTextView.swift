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
        textView.backgroundColor = .textBackgroundColor
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
            .foregroundColor: NSColor.controlAccentColor,
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

    override func performTextFinderAction(_ sender: Any?) {
        controller?.finishLoading()
        super.performTextFinderAction(sender)
    }

    override func performFindPanelAction(_ sender: Any?) {
        controller?.finishLoading()
        super.performFindPanelAction(sender)
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
        if let controller, controller.handleClick(at: point) { return }
        super.mouseDown(with: event)
    }

    override func paste(_ sender: Any?) {
        if let controller, controller.pasteFiles(from: .general) { return }
        let before = controller?.blockIds() ?? []
        let start = selectedRange().location
        super.paste(sender)
        controller?.markPasted(from: start, to: selectedRange().location, existing: before,
                               sourceURL: Self.sourceURL(in: .general))
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
        return super.readSelection(from: pboard, type: type)
    }

    override var readablePasteboardTypes: [NSPasteboard.PasteboardType] {
        [BlockEditorController.blocksPasteboardType] + super.readablePasteboardTypes
    }

    override var writablePasteboardTypes: [NSPasteboard.PasteboardType] {
        [BlockEditorController.blocksPasteboardType] + super.writablePasteboardTypes
    }

    override func writeSelection(to pboard: NSPasteboard, type: NSPasteboard.PasteboardType) -> Bool {
        if type == BlockEditorController.blocksPasteboardType {
            return controller?.writeBlocks(to: pboard) ?? false
        }
        return super.writeSelection(to: pboard, type: type)
    }

    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        let point = convert(sender.draggingLocation, from: nil)
        if let controller, controller.pasteFiles(from: sender.draggingPasteboard, at: characterIndexForInsertion(at: point)) {
            return true
        }
        return super.performDragOperation(sender)
    }

    // MARK: Format actions (for menus and the command palette)

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

    // Standard Format ▸ Font items route here too.
    @objc override func underline(_ sender: Any?) { controller?.toggleMark(\.underline) }
}
