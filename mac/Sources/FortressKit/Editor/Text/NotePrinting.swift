import AppKit

// File ▸ Print… and Edit ▸ Find from anywhere in a note: a table cell and a column have
// text views of their own, and the menu commands go to the focused one. Both are the
// note's: Print prints the whole note, Find uses the note's find bar.

extension NSView {
    /// The note's text view this view is in (the outermost editor text view).
    var enclosingNoteTextView: FortressTextView? {
        var view: NSView? = self
        var outermost: FortressTextView?
        while let current = view {
            if let text = current as? FortressTextView { outermost = text }
            view = current.superview
        }
        return outermost?.controller?.rootEditor.textView ?? outermost
    }
}

extension NSTextView {
    /// Find from a table cell or a column: the note's find bar, searching the note from
    /// where the cell or column is. Use Selection for Find (⌘E) stays here: it puts the
    /// selection on the find pasteboard, which the note's find bar shares.
    func forwardFindToNote(_ sender: Any?, panel: Bool) -> Bool {
        guard let note = enclosingNoteTextView, note !== self, let window else { return false }
        let tag = (sender as? NSValidatedUserInterfaceItem)?.tag ?? 0
        if tag == NSTextFinder.Action.setSearchString.rawValue {
            let selected = (string as NSString).substring(with: selectedRange())
            guard !selected.isEmpty else { return true }
            let pasteboard = NSPasteboard(name: .find)
            pasteboard.clearContents()
            pasteboard.setString(selected, forType: .string)
            return true
        }
        let here = note.convert(NSPoint(x: bounds.minX, y: bounds.minY), from: self)
        let index = note.characterIndexForInsertion(at: here)
        window.makeFirstResponder(note)
        if index != NSNotFound { note.setSelectedRange(NSRange(location: min(index, (note.string as NSString).length), length: 0)) }
        if panel { note.performFindPanelAction(sender) } else { note.performTextFinderAction(sender) }
        return true
    }
}

extension BlockEditorController {
    /// File ▸ Print…: the whole note (every block, tables and columns included), in light
    /// colours on paper, from any of the note's text views.
    func printNote() {
        let root = rootEditor
        root.finishLoading()
        let operation = root.notePrintOperation(NSPrintInfo.shared)
        if let run = root.runPrintOperation { run(operation); return }
        if let window = root.textView.window {
            operation.runModal(for: window, delegate: nil, didRun: nil, contextInfo: nil)
        } else {
            operation.run()
        }
    }

    /// A print of the note: a copy of it laid out at the page's width (the editor on
    /// screen keeps its caret, selection, hover handle and colours).
    func notePrintOperation(_ printInfo: NSPrintInfo) -> NSPrintOperation {
        finishLoading()
        let info = printInfo.copy() as? NSPrintInfo ?? NSPrintInfo()
        info.horizontalPagination = .fit
        info.verticalPagination = .automatic
        info.isHorizontallyCentered = false
        info.isVerticallyCentered = false
        let width = max(200, info.paperSize.width - info.leftMargin - info.rightMargin)

        let copy = BlockEditorController(baseURL: context.baseURL)
        copy.context.session = context.session
        copy.context.pipeline = context.pipeline
        copy.context.referenceText = context.referenceText
        copy.context.documentTitles = context.documentTitles
        copy.theme = theme
        let view = copy.textView
        let title = noteReference?().title ?? ""
        if !title.isEmpty {
            let label = NSTextField(wrappingLabelWithString: title)
            label.font = .systemFont(ofSize: 24, weight: .bold)
            label.textColor = .black
            view.header = label
            view.headerDidChange(height: label.intrinsicContentSize.height + 8)
        }
        view.appearance = NSAppearance(named: .aqua)
        view.drawsBackground = false
        view.measureCharacters = 10_000
        view.frame = NSRect(x: 0, y: 0, width: width, height: 100)
        copy.load(currentBlocks())
        copy.finishLoading()
        // Laid out whole (and its tables and columns in place) before the pages are cut.
        let paper = NSWindow(contentRect: NSRect(x: -20_000, y: -20_000, width: width, height: 800),
                             styleMask: [.borderless], backing: .buffered, defer: true)
        paper.isReleasedWhenClosed = false
        paper.appearance = view.appearance
        copy.scrollView.frame = NSRect(x: 0, y: 0, width: width, height: 800)
        paper.contentView = copy.scrollView
        copy.layoutForPrinting()

        let operation = NSPrintOperation(view: view, printInfo: info)
        operation.jobTitle = title
        operation.showsProgressPanel = true
        PrintCopies.keep(copy, window: paper, for: operation)
        return operation
    }

    /// Lays out every fragment and attachment: the window is as tall as the note, so
    /// the viewport (and the tables and columns it places) covers all of it. The window
    /// is never shown (it has no backing store).
    private func layoutForPrinting() {
        guard let manager = textView.textLayoutManager, let paper = scrollView.window else { return }
        for _ in 0..<3 {
            manager.ensureLayout(for: manager.documentRange)
            textView.sizeToFit()
            let height = max(textView.frame.height, 100)
            paper.setContentSize(NSSize(width: textView.frame.width, height: height))
            scrollView.frame = NSRect(x: 0, y: 0, width: textView.frame.width, height: height)
            scrollView.layoutSubtreeIfNeeded()
            manager.textViewportLayoutController.layoutViewport()
            relayoutAttachments()
        }
    }
}

/// The copies printed now, kept until their print operation is done with them.
@MainActor
enum PrintCopies {
    private static var copies: [ObjectIdentifier: (BlockEditorController, NSWindow)] = [:]
    private static var order: [ObjectIdentifier] = []

    static func keep(_ copy: BlockEditorController, window: NSWindow, for operation: NSPrintOperation) {
        let key = ObjectIdentifier(operation)
        copies[key] = (copy, window)
        order.append(key)
        // A few at most: an older print has finished (or been cancelled) by now.
        while order.count > 3 { copies[order.removeFirst()] = nil }
    }
}
