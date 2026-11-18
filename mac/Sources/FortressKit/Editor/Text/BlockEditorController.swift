import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Owns the note body's text view: keeps paragraphs and blocks in step, handles
/// structural editing (Return, Tab, Markdown shortcuts, list exits, merges), inline
/// marks, links and `[[` completion, and reports changes to the session for saving.
@MainActor
final class BlockEditorController: NSObject, NSTextViewDelegate, NSTextStorageDelegate {
    static let blocksPasteboardType = NSPasteboard.PasteboardType("app.fortress.notes.blocks")

    let textView: FortressTextView
    let scrollView: NSScrollView
    let context: AttachmentContext
    private let fragments = BlockFragmentFactory()
    var theme = EditorTheme() {
        didSet {
            fragments.theme = theme
            context.theme = theme
            textView.theme = theme
        }
    }

    /// Called after every change of the text (not for loading).
    var onChange: (() -> Void)?
    /// The block holding the caret.
    var onFocusBlock: ((String?) -> Void)?
    /// A note link was clicked.
    var onOpenNote: ((String, String?) -> Void)?
    /// Note search for `[[` completion.
    var searchNotes: ((String) async -> [LinkSuggestion])?
    var uploadFile: ((Data, String, String) async throws -> (url: String, name: String))?

    private var isNormalizing = false
    private var isLoading = false
    private var lastReplacement: String?
    private var focusedBlockId: String?
    private lazy var completion = LinkCompletion(controller: self)
    private lazy var blockHover = BlockHover(controller: self)

    init(baseURL: URL) {
        textView = FortressTextView.make()
        scrollView = NSScrollView()
        context = AttachmentContext(baseURL: baseURL)
        super.init()
        scrollView.documentView = textView
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = true
        scrollView.backgroundColor = .textBackgroundColor
        scrollView.contentView.postsBoundsChangedNotifications = false
        textView.controller = self
        textView.delegate = self
        textView.textStorage?.delegate = self
        textView.textLayoutManager?.delegate = fragments
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        context.relayout = { [weak self] in self?.relayoutAttachments() }
        context.openURL = { [weak self] url in self?.open(url) }
    }

    var storage: NSTextStorage { textView.textStorage! }
    private var string: NSString { storage.string as NSString }

    // MARK: Loading and reading

    func load(_ blocks: [EditorBlock]) {
        isLoading = true
        defer { isLoading = false }
        let text = DocumentText.build(blocks, theme: theme, context: context)
        storage.setAttributedString(text)
        textView.undoManager?.removeAllActions()
        textView.setSelectedRange(NSRange(location: 0, length: 0))
        syncTypingAttributes()
    }

    /// The blocks as they are now (fixing duplicate ids from pastes as a side effect).
    func currentBlocks() -> [EditorBlock] {
        let (blocks, renamed) = DocumentText.blocks(from: storage)
        if !renamed.isEmpty {
            isNormalizing = true
            storage.beginEditing()
            for (range, info) in renamed { storage.addAttribute(.fnBlock, value: info, range: range) }
            storage.endEditing()
            isNormalizing = false
        }
        return blocks
    }

    /// Text of the first non-empty blocks, for the live auto title.
    func leadingTexts(limit: Int = 3) -> [String] {
        var output: [String] = []
        var location = 0
        while location < string.length, output.count < limit {
            let paragraph = string.paragraphRange(at: location)
            location = NSMaxRange(paragraph)
            guard let info = storage.blockInfo(at: paragraph.location), info.kind.hasInlineText else { continue }
            let text = DocumentText.runs(in: storage, range: contentRange(of: paragraph))
            let plain = BlockCodec.plainText(text)
            if !plain.trimmingCharacters(in: .whitespaces).isEmpty { output.append(plain) }
        }
        return output
    }

    func range(ofBlock id: String) -> NSRange? {
        var location = 0
        while location < string.length {
            let paragraph = string.paragraphRange(at: location)
            if storage.blockInfo(at: paragraph.location)?.id == id { return paragraph }
            location = NSMaxRange(paragraph)
        }
        return nil
    }

    /// Puts the caret at the start of a block, scrolls it to the middle and flashes it.
    func focus(blockId: String, attempts: Int = 20) {
        guard let range = range(ofBlock: blockId) else { return }
        // Wait until the view is in a sized window, or the scroll position is lost.
        if attempts > 0, textView.window == nil || scrollView.contentView.bounds.height < 100 {
            RunLoop.main.perform(inModes: [.common]) { [weak self] in
                MainActor.assumeIsolated { self?.focus(blockId: blockId, attempts: attempts - 1) }
            }
            return
        }
        textView.window?.makeFirstResponder(textView)
        textView.setSelectedRange(NSRange(location: range.location, length: 0))
        scrollToCenter(range)
        flash(range)
    }

    /// Takes keyboard focus, keeping the caret where it was.
    func focusCaret() {
        textView.window?.makeFirstResponder(textView)
        textView.scrollRangeToVisible(textView.selectedRange())
    }

    func focusStart() {
        textView.window?.makeFirstResponder(textView)
        textView.setSelectedRange(NSRange(location: 0, length: 0))
    }

    private func scrollToCenter(_ range: NSRange) {
        textView.layoutSubtreeIfNeeded()
        guard let manager = textView.textLayoutManager, let contentManager = manager.textContentManager,
              let start = contentManager.location(contentManager.documentRange.location, offsetBy: range.location)
        else { textView.scrollRangeToVisible(range); return }
        let end = contentManager.location(start, offsetBy: range.length) ?? start
        manager.ensureLayout(for: NSTextRange(location: contentManager.documentRange.location, end: end) ?? NSTextRange(location: start))
        textView.layoutSubtreeIfNeeded()
        guard let fragment = manager.textLayoutFragment(for: start) else { textView.scrollRangeToVisible(range); return }
        let frame = fragment.layoutFragmentFrame.offsetBy(dx: textView.textContainerOrigin.x, dy: textView.textContainerOrigin.y)
        let visible = scrollView.contentView.bounds.height
        // The view may not have grown to the laid-out text yet.
        textView.sizeToFit()
        if textView.frame.height < frame.maxY + visible / 2 {
            textView.setFrameSize(NSSize(width: textView.frame.width, height: frame.maxY + visible / 2))
        }
        textView.scrollRangeToVisible(range)
        let y = max(0, frame.midY - visible / 2)
        scrollView.contentView.scroll(to: NSPoint(x: 0, y: y))
        scrollView.reflectScrolledClipView(scrollView.contentView)
    }

    private func flash(_ range: NSRange) {
        guard let manager = textView.textLayoutManager, let textRange = textRange(for: range) else { return }
        let color = NSColor.controlAccentColor.withAlphaComponent(0.12)
        manager.addRenderingAttribute(.backgroundColor, value: color, for: textRange)
        Task { @MainActor [weak manager] in
            try? await Task.sleep(for: .milliseconds(1200))
            manager?.removeRenderingAttribute(.backgroundColor, for: textRange)
        }
    }

    func textRange(for range: NSRange) -> NSTextRange? {
        guard let contentManager = textView.textLayoutManager?.textContentManager,
              let start = contentManager.location(contentManager.documentRange.location, offsetBy: range.location),
              let end = contentManager.location(start, offsetBy: range.length) else { return nil }
        return NSTextRange(location: start, end: end)
    }

    private func relayoutAttachments() {
        guard let manager = textView.textLayoutManager else { return }
        manager.invalidateLayout(for: manager.documentRange)
        textView.needsDisplay = true
    }

    // MARK: Paragraph helpers

    private func paragraph(at index: Int) -> NSRange { string.paragraphRange(at: index) }

    /// A paragraph without its "\n".
    private func contentRange(of paragraph: NSRange) -> NSRange {
        var content = paragraph
        if content.length > 0, string.character(at: NSMaxRange(content) - 1) == 0x0A { content.length -= 1 }
        return content
    }

    private func info(at index: Int) -> BlockInfo {
        storage.blockInfo(at: paragraph(at: index).location) ?? BlockInfo()
    }

    /// Paragraphs touched by a range.
    private func paragraphs(in range: NSRange) -> [NSRange] {
        var output: [NSRange] = []
        var location = paragraph(at: range.location).location
        let end = max(NSMaxRange(range), range.location + 1)
        while location < string.length {
            let paragraph = paragraph(at: location)
            output.append(paragraph)
            location = NSMaxRange(paragraph)
            if location >= end { break }
        }
        return output
    }

    // MARK: Undoable edits

    private var isApplyingEdit = false

    /// Replaces text through the text view so it is one undo step and the session sees it.
    func replace(_ range: NSRange, with replacement: NSAttributedString, selecting selection: NSRange? = nil) {
        isApplyingEdit = true
        defer { isApplyingEdit = false }
        guard textView.shouldChangeText(in: range, replacementString: replacement.string) else { return }
        storage.replaceCharacters(in: range, with: replacement)
        textView.didChangeText()
        if let selection { textView.setSelectedRange(selection) }
    }

    /// Changes attributes undoably.
    private func changeAttributes(in range: NSRange, _ body: (NSTextStorage) -> Void) {
        isApplyingEdit = true
        defer { isApplyingEdit = false }
        guard textView.shouldChangeText(in: range, replacementString: nil) else { return }
        storage.beginEditing()
        body(storage)
        storage.endEditing()
        textView.didChangeText()
    }

    /// Gives paragraphs new block infos (kind, depth, checked) as one undo step.
    private func updateBlocks(_ paragraphs: [NSRange], _ transform: (BlockInfo) -> BlockInfo) {
        guard let first = paragraphs.first, let last = paragraphs.last else { return }
        let selection = textView.selectedRange()
        changeAttributes(in: NSUnionRange(first, last)) { storage in
            for paragraph in paragraphs {
                guard let info = storage.blockInfo(at: paragraph.location) else { continue }
                storage.addAttribute(.fnBlock, value: transform(info), range: paragraph)
            }
        }
        textView.setSelectedRange(selection)
        syncTypingAttributes()
    }

    private func attributed(_ string: String, info: BlockInfo, marks: InlineMarks = InlineMarks()) -> NSAttributedString {
        var attributes: [NSAttributedString.Key: Any] = [.fnBlock: info]
        if !marks.isPlain { attributes[.fnMarks] = MarksBox(marks) }
        return NSAttributedString(string: string, attributes: attributes)
    }

    /// `range`'s text with every character given `info`.
    private func retagged(_ range: NSRange, info: BlockInfo) -> NSMutableAttributedString {
        let copy = NSMutableAttributedString(attributedString: storage.attributedSubstring(from: range))
        if copy.length > 0 { copy.addAttribute(.fnBlock, value: info, range: NSRange(location: 0, length: copy.length)) }
        return copy
    }

    // MARK: Normalizing (NSTextStorageDelegate)

    nonisolated func textStorage(
        _ textStorage: NSTextStorage, willProcessEditing editedMask: NSTextStorageEditActions,
        range editedRange: NSRange, changeInLength delta: Int
    ) {
        nonisolated(unsafe) let storage = textStorage
        MainActor.assumeIsolated {
            guard !isNormalizing else { return }
            isNormalizing = true
            defer { isNormalizing = false }
            normalize(storage, editedRange)
        }
    }

    /// One block per paragraph, unique ids between neighbours, atomic blocks keep their
    /// attachment, the text ends with "\n", and presentation follows the semantics.
    private func normalize(_ storage: NSTextStorage, _ editedRange: NSRange) {
        let string = storage.string as NSString
        if string.length == 0 || string.character(at: string.length - 1) != 0x0A {
            let info = string.length > 0 ? (storage.blockInfo(at: string.length - 1) ?? BlockInfo()) : BlockInfo()
            storage.append(NSAttributedString(string: "\n", attributes: [.fnBlock: info]))
        }
        // Foreign separators (pasted "\r\n", U+2029) become paragraph breaks.
        let edited = NSIntersectionRange(editedRange, NSRange(location: 0, length: storage.length))
        if edited.length > 0 {
            let piece = (storage.string as NSString).substring(with: edited)
            if piece.contains("\r") || piece.contains("\u{2029}") {
                let fixed = piece.replacingOccurrences(of: "\r\n", with: "\n")
                    .replacingOccurrences(of: "\r", with: "\n").replacingOccurrences(of: "\u{2029}", with: "\n")
                let attributes = storage.attributes(at: edited.location, effectiveRange: nil)
                storage.replaceCharacters(in: edited, with: NSAttributedString(string: fixed, attributes: attributes))
            }
        }
        let text = storage.string as NSString
        let start = text.paragraphRange(at: min(editedRange.location, text.length - 1)).location
        let end = min(text.length, NSMaxRange(editedRange) + 1)
        var location = start
        var previousId: String? = start > 0 ? storage.blockInfo(at: text.paragraphRange(at: start - 1).location)?.id : nil
        while location < text.length {
            let paragraph = text.paragraphRange(at: location)
            var info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
            if info.id == previousId {
                info = BlockInfo(kind: info.kind, depth: info.depth, checked: info.checked, language: info.language, origin: info.origin)
            }
            if !info.kind.hasInlineText {
                let hasAttachment = paragraph.length > 1 && storage.attribute(.attachment, at: paragraph.location, effectiveRange: nil) is BlockAttachment
                if !hasAttachment {
                    info = BlockInfo(kind: .paragraph, depth: info.depth)
                }
            }
            var needsTagging = false
            storage.enumerateAttribute(.fnBlock, in: paragraph, options: []) { value, _, stop in
                if (value as? BlockInfo) !== info { needsTagging = true; stop.pointee = true }
            }
            if needsTagging { storage.addAttribute(.fnBlock, value: info, range: paragraph) }
            theme.restyle(storage, range: paragraph)
            previousId = info.id
            location = NSMaxRange(paragraph)
            if location >= end { break }
        }
    }

    // MARK: NSTextViewDelegate

    func textView(_ textView: NSTextView, willChangeSelectionFromCharacterRange oldSelectedCharRange: NSRange,
                  toCharacterRange newSelectedCharRange: NSRange) -> NSRange {
        // The final "\n" belongs to the last block; the caret never goes past it.
        let limit = max(0, string.length - 1)
        var range = newSelectedCharRange
        if range.location > limit { range.location = limit }
        if NSMaxRange(range) > limit { range.length = max(0, limit - range.location) }
        return range
    }

    func textView(_ textView: NSTextView, shouldChangeTextIn affectedCharRange: NSRange, replacementString: String?) -> Bool {
        lastReplacement = replacementString
        guard !isApplyingEdit, let replacementString, !replacementString.isEmpty, !textView.hasMarkedText() else { return true }
        // Typing next to an image or divider starts a new paragraph instead.
        let paragraph = paragraph(at: affectedCharRange.location)
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        if !info.kind.hasInlineText, affectedCharRange.length == 0 {
            let typed = attributed(replacementString, info: BlockInfo(depth: info.depth))
            let fresh = NSMutableAttributedString(attributedString: typed)
            fresh.append(attributed("\n", info: fresh.blockInfo(at: 0)!))
            if affectedCharRange.location == paragraph.location {
                replace(NSRange(location: paragraph.location, length: 0), with: fresh,
                        selecting: NSRange(location: paragraph.location + typed.length, length: 0))
            } else {
                replace(NSRange(location: NSMaxRange(paragraph), length: 0), with: fresh,
                        selecting: NSRange(location: NSMaxRange(paragraph) + typed.length, length: 0))
            }
            return false
        }
        return true
    }

    func textDidChange(_ notification: Notification) {
        guard !isLoading else { return }
        let inserted = lastReplacement
        lastReplacement = nil
        if !textView.hasMarkedText(), !isApplyingEdit {
            if inserted == " " { scheduleMarkdownShortcut() }
            if inserted == "[" { maybeStartCompletion() }
        }
        completion.textDidChange()
        invalidateNumbering(around: textView.selectedRange().location)
        onChange?()
    }

    func textViewDidChangeSelection(_ notification: Notification) {
        guard !isLoading else { return }
        syncTypingAttributes()
        completion.selectionDidChange()
        let id = storage.blockInfo(at: paragraph(at: textView.selectedRange().location).location)?.id
        if id != focusedBlockId {
            focusedBlockId = id
            onFocusBlock?(id)
        }
    }

    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        let href = (link as? URL)?.absoluteString ?? (link as? String) ?? ""
        if let target = InlineMarkdown.documentLinkTarget(href) {
            onOpenNote?(target.id, nil)
            return true
        }
        if let url = context.url(for: href) {
            open(url)
            return true
        }
        return false
    }

    func textView(_ textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        if completion.isActive, completion.handle(commandSelector) { return true }
        switch commandSelector {
        case #selector(NSResponder.insertNewline(_:)): return insertNewline()
        case #selector(NSResponder.insertTab(_:)): return insertTab(outdent: false)
        case #selector(NSResponder.insertBacktab(_:)): return insertTab(outdent: true)
        case #selector(NSResponder.deleteBackward(_:)): return deleteBackward()
        case #selector(NSResponder.deleteForward(_:)): return deleteForward()
        default: return false
        }
    }

    func textView(_ view: NSTextView, menu: NSMenu, for event: NSEvent, at charIndex: Int) -> NSMenu? {
        let format = NSMenu(title: "Format")
        let items: [(String, Selector)] = [
            ("Body", #selector(FortressTextView.fortressBody(_:))),
            ("Heading 1", #selector(FortressTextView.fortressHeading1(_:))),
            ("Heading 2", #selector(FortressTextView.fortressHeading2(_:))),
            ("Heading 3", #selector(FortressTextView.fortressHeading3(_:))),
            ("Bulleted List", #selector(FortressTextView.fortressBulletedList(_:))),
            ("Numbered List", #selector(FortressTextView.fortressNumberedList(_:))),
            ("Checklist", #selector(FortressTextView.fortressChecklist(_:))),
            ("Quote", #selector(FortressTextView.fortressQuote(_:))),
            ("Code Block", #selector(FortressTextView.fortressCodeBlock(_:))),
        ]
        for (title, action) in items { format.addItem(withTitle: title, action: action, keyEquivalent: "") }
        format.addItem(.separator())
        format.addItem(withTitle: "Highlight", action: #selector(FortressTextView.fortressHighlight(_:)), keyEquivalent: "")
        format.addItem(withTitle: "Add Link…", action: #selector(FortressTextView.fortressAddLink(_:)), keyEquivalent: "")
        let item = NSMenuItem(title: "Paragraph Style", action: nil, keyEquivalent: "")
        item.submenu = format
        menu.insertItem(item, at: 0)
        menu.insertItem(.separator(), at: 1)
        return menu
    }

    // MARK: Typing attributes

    /// New text takes the paragraph's block, the marks before the caret, and never
    /// extends a link past its end.
    func syncTypingAttributes() {
        let location = textView.selectedRange().location
        let paragraph = paragraph(at: location)
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        var marks = textView.typingAttributes[.fnMarks].flatMap { ($0 as? MarksBox)?.marks } ?? InlineMarks()
        if textView.selectedRange().length == 0 {
            let content = contentRange(of: paragraph)
            if content.length == 0 {
                marks = (textView.typingAttributes[.fnBlock] as? BlockInfo)?.id == info.id ? marks : InlineMarks()
            } else if location > content.location {
                marks = storage.marks(at: location - 1)
                if marks.link != nil, location >= NSMaxRange(content) || storage.marks(at: location).link != marks.link {
                    marks.link = nil
                }
            } else {
                marks = storage.marks(at: location)
                marks.link = nil
            }
        }
        var attributes = theme.attributes(info: info, marks: marks, paragraphStyle: theme.paragraphStyle(for: info))
        attributes[.fnBlock] = info
        if !marks.isPlain { attributes[.fnMarks] = MarksBox(marks) }
        attributes[.link] = nil
        textView.typingAttributes = attributes
    }

    // MARK: Return, Tab, Delete

    private func insertNewline() -> Bool {
        var selection = textView.selectedRange()
        if selection.length > 0 {
            replace(selection, with: NSAttributedString(), selecting: NSRange(location: selection.location, length: 0))
            selection = textView.selectedRange()
        }
        let caret = selection.location
        let paragraph = paragraph(at: caret)
        let content = contentRange(of: paragraph)
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        let text = string.substring(with: content)

        if !info.kind.hasInlineText {
            // Return on an image/divider: a new paragraph below (or above, before it).
            let fresh = BlockInfo(depth: info.depth)
            if caret == paragraph.location && caret < NSMaxRange(content) {
                replace(NSRange(location: paragraph.location, length: 0), with: attributed("\n", info: fresh),
                        selecting: NSRange(location: paragraph.location + 1, length: 0))
            } else {
                replace(NSRange(location: NSMaxRange(paragraph), length: 0), with: attributed("\n", info: fresh),
                        selecting: NSRange(location: NSMaxRange(paragraph), length: 0))
            }
            return true
        }
        if info.kind == .code {
            if caret == NSMaxRange(content), text.hasSuffix(softBreak) {
                // Return on an empty last line leaves the code block.
                let fresh = BlockInfo(depth: info.depth)
                let lineBreak = NSRange(location: NSMaxRange(content) - 1, length: 1)
                let replacement = NSMutableAttributedString(attributedString: attributed("\n", info: info))
                replacement.append(attributed("\n", info: fresh))
                replace(NSRange(location: lineBreak.location, length: 2), with: replacement,
                        selecting: NSRange(location: lineBreak.location + 1, length: 0))
            } else {
                replace(NSRange(location: caret, length: 0), with: attributed(softBreak, info: info),
                        selecting: NSRange(location: caret + 1, length: 0))
            }
            return true
        }
        if content.length == 0, info.kind != .paragraph {
            // Return on an empty list item, quote or heading ends it.
            if info.kind.isList && info.depth > 0 {
                indent(paragraphs: [paragraph], by: -1)
            } else {
                updateBlocks([paragraph]) { $0.with(kind: .paragraph, checked: false) }
            }
            return true
        }
        let fence = text.trimmingCharacters(in: .whitespaces)
        if caret == NSMaxRange(content), fence.hasPrefix("```"), !fence.dropFirst(3).contains("`"), !fence.contains(" ") {
            let language = String(fence.dropFirst(3))
            let code = BlockInfo(id: info.id, kind: .code, depth: info.depth, language: language.isEmpty ? nil : language, origin: info.origin)
            textView.breakUndoCoalescing()
            replace(paragraph, with: attributed("\n", info: code), selecting: NSRange(location: paragraph.location, length: 0))
            return true
        }
        if caret == NSMaxRange(content), ["---", "***", "___"].contains(fence), info.kind == .paragraph {
            textView.breakUndoCoalescing()
            let divider = BlockInfo(kind: .divider, depth: info.depth)
            let replacement = NSMutableAttributedString(attributedString: NSAttributedString(
                string: objectReplacement, attributes: [.fnBlock: divider, .attachment: BlockAttachment(info: divider, context: context)]))
            replacement.append(attributed("\n", info: divider))
            let after = BlockInfo(depth: info.depth)
            replacement.append(attributed("\n", info: after))
            replace(paragraph, with: replacement, selecting: NSRange(location: paragraph.location + 2, length: 0))
            return true
        }

        let continued: BlockKind = info.kind.isList || info.kind == .quote ? info.kind : .paragraph
        if caret == content.location, content.length > 0 {
            // Return at the start: an empty block above, the text keeps its block.
            let above = info.successor(kind: info.kind.headingLevel != nil ? .paragraph : continued)
            replace(NSRange(location: paragraph.location, length: 0), with: attributed("\n", info: above),
                    selecting: NSRange(location: paragraph.location + 1, length: 0))
            return true
        }
        let tail = NSRange(location: caret, length: NSMaxRange(content) - caret)
        let nextKind: BlockKind = info.kind.headingLevel != nil && tail.length > 0 ? info.kind : continued
        let next = info.successor(kind: nextKind)
        let replacement = NSMutableAttributedString(attributedString: attributed("\n", info: info))
        replacement.append(retagged(tail, info: next))
        replacement.append(attributed("\n", info: next))
        replace(NSRange(location: caret, length: NSMaxRange(paragraph) - caret), with: replacement,
                selecting: NSRange(location: caret + 1, length: 0))
        return true
    }

    private func insertTab(outdent: Bool) -> Bool {
        let selection = textView.selectedRange()
        let paragraphs = paragraphs(in: selection)
        if !outdent, paragraphs.count == 1, info(at: selection.location).kind == .code {
            replace(selection, with: attributed("    ", info: info(at: selection.location)),
                    selecting: NSRange(location: selection.location + 4, length: 0))
            return true
        }
        indent(paragraphs: paragraphs, by: outdent ? -1 : 1)
        return true
    }

    func indent(by delta: Int) {
        indent(paragraphs: paragraphs(in: textView.selectedRange()), by: delta)
    }

    /// Indents paragraphs and the children that follow them, keeping depths valid.
    private func indent(paragraphs: [NSRange], by delta: Int) {
        guard let first = paragraphs.first, var last = paragraphs.last else { return }
        let firstInfo = storage.blockInfo(at: first.location) ?? BlockInfo()
        // Take the children along.
        let baseDepth = paragraphs.compactMap { storage.blockInfo(at: $0.location)?.depth }.min() ?? 0
        var cursor = NSMaxRange(last)
        var all = paragraphs
        while cursor < string.length {
            let next = paragraph(at: cursor)
            guard let info = storage.blockInfo(at: next.location), info.depth > baseDepth else { break }
            all.append(next)
            last = next
            cursor = NSMaxRange(next)
        }
        let previousDepth: Int? = first.location > 0 ? storage.blockInfo(at: paragraph(at: first.location - 1).location).map {
            $0.kind == .embed ? $0.depth - 1 : $0.depth
        } : nil
        if delta > 0 {
            guard let previousDepth, firstInfo.depth + delta <= previousDepth + 1 else { NSSound.beep(); return }
        } else if paragraphs.allSatisfy({ (storage.blockInfo(at: $0.location)?.depth ?? 0) == 0 }) {
            return
        }
        updateBlocks(all) { $0.with(depth: max(0, $0.depth + delta)) }
    }

    private func deleteBackward() -> Bool {
        let selection = textView.selectedRange()
        guard selection.length == 0 else { return false }
        let paragraph = paragraph(at: selection.location)
        guard selection.location == paragraph.location else { return false }
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        let content = contentRange(of: paragraph)

        if info.kind.hasInlineText, info.kind != .paragraph {
            // Backspace at the start of a list item, quote, heading or code: plain text first.
            updateBlocks([paragraph]) { $0.with(kind: .paragraph, checked: false) }
            return true
        }
        if info.kind == .paragraph, info.depth > 0 {
            indent(paragraphs: [paragraph], by: -1)
            return true
        }
        guard paragraph.location > 0 else { return true }
        let previous = self.paragraph(at: paragraph.location - 1)
        let previousInfo = storage.blockInfo(at: previous.location) ?? BlockInfo()
        let previousContent = contentRange(of: previous)

        if !info.kind.hasInlineText {
            // At the start of an image: remove an empty line above, else step into it.
            if previousContent.length == 0 {
                replace(previous, with: NSAttributedString(), selecting: NSRange(location: previous.location, length: 0))
            } else {
                textView.setSelectedRange(NSRange(location: NSMaxRange(previousContent), length: 0))
            }
            return true
        }
        if !previousInfo.kind.hasInlineText {
            // Backspace below an image or divider removes it (or this empty line).
            if content.length == 0 {
                replace(paragraph, with: NSAttributedString(), selecting: NSRange(location: previous.location, length: 0))
            } else {
                replace(previous, with: NSAttributedString(), selecting: NSRange(location: previous.location, length: 0))
            }
            return true
        }
        // Merge into the block above, which keeps its kind and id.
        let merged = retagged(content, info: previousInfo)
        merged.append(attributed("\n", info: previousInfo))
        let joinAt = NSMaxRange(previousContent)
        replace(NSRange(location: joinAt, length: NSMaxRange(paragraph) - joinAt), with: merged,
                selecting: NSRange(location: joinAt, length: 0))
        return true
    }

    private func deleteForward() -> Bool {
        let selection = textView.selectedRange()
        guard selection.length == 0 else { return false }
        let paragraph = paragraph(at: selection.location)
        let content = contentRange(of: paragraph)
        guard selection.location == NSMaxRange(content), NSMaxRange(paragraph) < string.length - 0 else { return false }
        guard NSMaxRange(paragraph) < string.length else { return true }
        let next = self.paragraph(at: NSMaxRange(paragraph))
        let nextInfo = storage.blockInfo(at: next.location) ?? BlockInfo()
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        if !nextInfo.kind.hasInlineText || !info.kind.hasInlineText {
            if content.length == 0 {
                replace(paragraph, with: NSAttributedString(), selecting: NSRange(location: paragraph.location, length: 0))
            } else if !nextInfo.kind.hasInlineText {
                replace(next, with: NSAttributedString(), selecting: selection)
            }
            return true
        }
        let merged = retagged(contentRange(of: next), info: info)
        merged.append(attributed("\n", info: info))
        replace(NSRange(location: NSMaxRange(content), length: NSMaxRange(next) - NSMaxRange(content)), with: merged,
                selecting: selection)
        return true
    }

    // MARK: Markdown shortcuts

    /// `# `, `## `, `### `, `- `, `* `, `1. `, `[ ] `, `[x] `, `> ` at the start of a
    /// paragraph. Runs after the space is typed, as its own undo step, so ⌘Z brings
    /// back the characters. Never during IME composition.
    private func scheduleMarkdownShortcut() {
        let caret = textView.selectedRange().location
        let paragraph = paragraph(at: caret)
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        guard info.kind == .paragraph || info.kind == .bullet, caret > paragraph.location else { return }
        let prefix = string.substring(with: NSRange(location: paragraph.location, length: caret - paragraph.location))
        guard let kind = Self.shortcutKind(prefix, current: info.kind) else { return }
        // A run loop pass later (not a GCD hop, which can't run while the main queue is
        // busy), so the transform is its own undo group.
        RunLoop.main.perform(inModes: [.common]) { [weak self] in
            MainActor.assumeIsolated { self?.applyMarkdownShortcut(paragraph: paragraph, prefix: prefix, info: info, kind: kind) }
        }
    }

    private func applyMarkdownShortcut(paragraph: NSRange, prefix: String, info: BlockInfo, kind: (kind: BlockKind, checked: Bool)) {
        do {
            guard !self.textView.hasMarkedText() else { return }
            let now = self.paragraph(at: paragraph.location)
            guard now.location == paragraph.location, NSMaxRange(now) >= paragraph.location + prefix.utf16.count,
                  self.string.substring(with: NSRange(location: now.location, length: prefix.utf16.count)) == prefix
            else { return }
            self.textView.breakUndoCoalescing()
            let current = self.storage.blockInfo(at: now.location) ?? info
            let updated = current.with(kind: kind.kind, checked: kind.checked)
            let rest = NSRange(location: now.location + prefix.utf16.count, length: NSMaxRange(now) - now.location - prefix.utf16.count)
            let replacement = self.retagged(rest, info: updated)
            let caretOffset = max(0, self.textView.selectedRange().location - now.location - prefix.utf16.count)
            self.replace(now, with: replacement, selecting: NSRange(location: now.location + caretOffset, length: 0))
            self.textView.breakUndoCoalescing()
        }
    }

    static func shortcutKind(_ prefix: String, current: BlockKind) -> (kind: BlockKind, checked: Bool)? {
        let trimmed = prefix.hasSuffix(" ") ? String(prefix.dropLast()) : prefix
        switch trimmed {
        case "#" where current == .paragraph: return (.heading1, false)
        case "##" where current == .paragraph: return (.heading2, false)
        case "###" where current == .paragraph: return (.heading3, false)
        case "-", "*", "+": return current == .paragraph ? (.bullet, false) : nil
        case ">" where current == .paragraph: return (.quote, false)
        case "[]", "[ ]", "- [ ]", "- []": return (.check, false)
        case "[x]", "[X]", "- [x]": return (.check, true)
        default:
            if current == .paragraph, trimmed.range(of: #"^\d{1,3}[.)]$"#, options: .regularExpression) != nil {
                return (.numbered, false)
            }
            return nil
        }
    }

    /// Numbers of following items change when one is inserted or removed.
    private func invalidateNumbering(around location: Int) {
        guard let manager = textView.textLayoutManager, string.length > 0 else { return }
        var start = paragraph(at: location).location
        if start > 0 { start = paragraph(at: start - 1).location }
        var end = start
        var cursor = start
        var sawNumbered = false
        while cursor < string.length {
            let paragraph = paragraph(at: cursor)
            let info = storage.blockInfo(at: paragraph.location)
            if info?.kind == .numbered { sawNumbered = true } else if sawNumbered && (info?.depth ?? 0) == 0 { break }
            end = NSMaxRange(paragraph)
            cursor = end
            if !sawNumbered && cursor > NSMaxRange(self.paragraph(at: location)) { break }
        }
        guard sawNumbered, let range = textRange(for: NSRange(location: start, length: end - start)) else { return }
        manager.invalidateLayout(for: range)
    }

    // MARK: Inline marks

    func toggleMark(_ keyPath: WritableKeyPath<InlineMarks, Bool>) {
        let selection = textView.selectedRange()
        if selection.length == 0 {
            var attributes = textView.typingAttributes
            var marks = (attributes[.fnMarks] as? MarksBox)?.marks ?? InlineMarks()
            marks[keyPath: keyPath].toggle()
            let info = info(at: selection.location)
            attributes = theme.attributes(info: info, marks: marks, paragraphStyle: theme.paragraphStyle(for: info))
            attributes[.fnBlock] = info
            if !marks.isPlain { attributes[.fnMarks] = MarksBox(marks) }
            textView.typingAttributes = attributes
            return
        }
        var allOn = true
        storage.enumerateAttribute(.fnMarks, in: selection, options: []) { value, range, stop in
            if string.substring(with: range).allSatisfy({ $0 == "\n" }) { return }
            if (value as? MarksBox)?.marks[keyPath: keyPath] != true { allOn = false; stop.pointee = true }
        }
        updateMarks(in: selection) { $0[keyPath: keyPath] = !allOn }
    }

    func toggleHighlight(_ color: String) {
        let selection = textView.selectedRange()
        guard selection.length > 0 else { return }
        let on = storage.marks(at: selection.location).highlight == color
        updateMarks(in: selection) { $0.highlight = on ? nil : color }
    }

    func setTextColor(_ color: String?) {
        let selection = textView.selectedRange()
        guard selection.length > 0 else { return }
        updateMarks(in: selection) { $0.textColor = color }
    }

    private func updateMarks(in range: NSRange, _ change: @escaping (inout InlineMarks) -> Void) {
        changeAttributes(in: range) { storage in
            storage.enumerateAttribute(.fnMarks, in: range, options: []) { value, run, _ in
                if storage.attribute(.fnNode, at: run.location, effectiveRange: nil) != nil { return }
                var marks = (value as? MarksBox)?.marks ?? InlineMarks()
                change(&marks)
                if marks.isPlain {
                    storage.removeAttribute(.fnMarks, range: run)
                } else {
                    storage.addAttribute(.fnMarks, value: MarksBox(marks), range: run)
                }
            }
        }
        textView.setSelectedRange(range)
    }

    func setKind(_ kind: BlockKind) {
        let paragraphs = paragraphs(in: textView.selectedRange())
        let textual = paragraphs.filter { storage.blockInfo(at: $0.location)?.kind.hasInlineText ?? false }
        let allAlready = textual.allSatisfy { storage.blockInfo(at: $0.location)?.kind == kind }
        let target: BlockKind = allAlready && kind != .paragraph ? .paragraph : kind
        updateBlocks(textual) { $0.with(kind: target, checked: target == .check ? $0.checked : false) }
    }

    func toggleChecked() {
        let paragraphs = paragraphs(in: textView.selectedRange()).filter { storage.blockInfo(at: $0.location)?.kind == .check }
        guard !paragraphs.isEmpty else { setKind(.check); return }
        let allChecked = paragraphs.allSatisfy { storage.blockInfo(at: $0.location)?.checked == true }
        updateBlocks(paragraphs) { $0.with(checked: !allChecked) }
    }

    /// Moves the selected blocks (with their children) above the previous or below the
    /// next block.
    func moveBlocks(by direction: Int) {
        let selection = textView.selectedRange()
        let selected = paragraphs(in: selection)
        guard let first = selected.first, let last = selected.last else { return }
        let block = NSUnionRange(first, last)
        if direction < 0 {
            guard block.location > 0 else { return }
            let previous = paragraph(at: block.location - 1)
            let moving = storage.attributedSubstring(from: block)
            let replacement = NSMutableAttributedString(attributedString: moving)
            replacement.append(storage.attributedSubstring(from: previous))
            replace(NSUnionRange(previous, block), with: replacement,
                    selecting: NSRange(location: selection.location - previous.length, length: selection.length))
        } else {
            guard NSMaxRange(block) < string.length else { return }
            let next = paragraph(at: NSMaxRange(block))
            let replacement = NSMutableAttributedString(attributedString: storage.attributedSubstring(from: next))
            replacement.append(storage.attributedSubstring(from: block))
            replace(NSUnionRange(block, next), with: replacement,
                    selecting: NSRange(location: selection.location + next.length, length: selection.length))
        }
    }

    // MARK: Links

    func editLink() {
        let selection = textView.selectedRange()
        let existing = storage.marks(at: selection.location).link
        var initial = existing ?? ""
        if existing == nil, selection.length > 0 {
            let selected = string.substring(with: selection)
            if selected.hasPrefix("http://") || selected.hasPrefix("https://") { initial = selected }
        }
        let rect = caretRect()
        LinkEditor.show(relativeTo: rect, of: textView, initial: initial, canRemove: existing != nil) { [weak self] result in
            guard let self else { return }
            self.textView.window?.makeFirstResponder(self.textView)
            switch result {
            case .cancel: self.textView.setSelectedRange(selection)
            case .remove:
                let range = selection.length > 0 ? selection : self.linkRange(at: selection.location) ?? selection
                self.updateMarks(in: range) { $0.link = nil }
            case .apply(let raw):
                let href = Self.normalizedLink(raw)
                if selection.length > 0 {
                    self.updateMarks(in: selection) { $0.link = href }
                } else if let range = self.linkRange(at: selection.location) {
                    self.updateMarks(in: range) { $0.link = href }
                } else {
                    let info = self.info(at: selection.location)
                    self.replace(selection, with: self.attributed(raw, info: info, marks: InlineMarks(link: href)),
                                 selecting: NSRange(location: selection.location + (raw as NSString).length, length: 0))
                }
            }
        }
    }

    static func normalizedLink(_ raw: String) -> String {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.contains("://") || text.hasPrefix("/") || text.hasPrefix("mailto:") || text.hasPrefix("[[") || text.hasPrefix("((") {
            return text
        }
        if text.contains("@") && !text.contains(" ") { return "mailto:" + text }
        return "https://" + text
    }

    private func linkRange(at location: Int) -> NSRange? {
        guard let link = storage.marks(at: location).link else { return nil }
        var start = location
        var end = location
        while start > 0, storage.marks(at: start - 1).link == link { start -= 1 }
        while end < string.length, storage.marks(at: end).link == link { end += 1 }
        return NSRange(location: start, length: end - start)
    }

    func caretRect() -> NSRect {
        let range = textView.selectedRange()
        let screen = textView.firstRect(forCharacterRange: range, actualRange: nil)
        guard let window = textView.window else { return .zero }
        let inWindow = window.convertFromScreen(screen)
        return textView.convert(inWindow, from: nil)
    }

    func open(_ url: URL) {
        guard url.isFileURL || url.host() != context.baseURL.host() || !url.path().hasPrefix("/media/") else {
            // Media from the local server: download to a cache folder and open with its app.
            Task {
                do {
                    let (temporary, _) = try await URLSession.shared.download(from: url)
                    let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                        .appending(path: "FortressNotes/Files", directoryHint: .isDirectory)
                    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                    let destination = folder.appending(path: url.lastPathComponent)
                    try? FileManager.default.removeItem(at: destination)
                    try FileManager.default.moveItem(at: temporary, to: destination)
                    NSWorkspace.shared.open(destination)
                } catch {
                    NSWorkspace.shared.open(url)
                }
            }
            return
        }
        NSWorkspace.shared.open(url)
    }

    // MARK: `[[` note links

    private func maybeStartCompletion() {
        let caret = textView.selectedRange().location
        guard caret >= 2, string.substring(with: NSRange(location: caret - 2, length: 2)) == "[[",
              info(at: caret).kind != .code else { return }
        completion.start(anchor: caret - 2)
    }

    /// Replaces `[[query` with a link to the chosen note.
    func insertNoteLink(_ suggestion: LinkSuggestion, replacing range: NSRange) {
        let info = info(at: range.location)
        let href = InlineMarkdown.documentLinkHref(id: suggestion.documentId, title: suggestion.title)
        let label = suggestion.title.isEmpty ? "Untitled" : suggestion.title
        let replacement = NSMutableAttributedString(attributedString: attributed(label, info: info, marks: InlineMarks(link: href)))
        replacement.append(attributed(" ", info: info))
        textView.breakUndoCoalescing()
        replace(range, with: replacement, selecting: NSRange(location: range.location + replacement.length, length: 0))
    }

    // MARK: Keys and clicks

    func handleKeyEquivalent(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask).subtracting([.capsLock, .numericPad, .function])
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        switch (flags, key) {
        case ([.command], "b"): toggleMark(\.bold)
        case ([.command], "i"): toggleMark(\.italic)
        case ([.command], "u"): toggleMark(\.underline)
        case ([.command], "e"): toggleMark(\.code)
        case ([.command, .shift], "x"): toggleMark(\.strike)
        case ([.command, .shift], "h"): toggleHighlight("yellow")
        case ([.command, .shift], "k"), ([.command], "l"): editLink()
        case ([.command, .option], "0"): setKind(.paragraph)
        case ([.command, .option], "1"): setKind(.heading1)
        case ([.command, .option], "2"): setKind(.heading2)
        case ([.command, .option], "3"): setKind(.heading3)
        case ([.command, .shift], "7"), ([.command, .shift], "&"): setKind(.bullet)
        case ([.command, .shift], "8"), ([.command, .shift], "*"): setKind(.numbered)
        case ([.command, .shift], "l"): setKind(.check)
        case ([.command, .option], "'"): setKind(.quote)
        case ([.command, .option], "c"): setKind(.code)
        case ([.command], "\r"): toggleChecked()
        case ([.command], "]"): indent(by: 1)
        case ([.command], "["): indent(by: -1)
        default:
            if flags == [.command, .control], event.keyCode == 126 { moveBlocks(by: -1); return true }
            if flags == [.command, .control], event.keyCode == 125 { moveBlocks(by: 1); return true }
            return false
        }
        return true
    }

    func handleKeyDown(_ event: NSEvent) -> Bool {
        false
    }

    /// Clicking a checkbox toggles it.
    func handleClick(at point: NSPoint) -> Bool {
        guard let manager = textView.textLayoutManager else { return false }
        let origin = textView.textContainerOrigin
        let local = CGPoint(x: point.x - origin.x, y: point.y - origin.y)
        guard let fragment = manager.textLayoutFragment(for: local) as? BlockLayoutFragment,
              let box = fragment.checkboxRect()?.insetBy(dx: -4, dy: -4),
              let contentManager = manager.textContentManager else { return false }
        let frame = fragment.layoutFragmentFrame
        let inFragment = CGPoint(x: local.x - frame.minX, y: local.y - frame.minY)
        guard box.contains(inFragment) else { return false }
        let offset = contentManager.offset(from: contentManager.documentRange.location, to: fragment.rangeInElement.location)
        let paragraph = paragraph(at: offset)
        updateBlocks([paragraph]) { $0.with(checked: !$0.checked) }
        return true
    }

    // MARK: Block info and provenance

    func hover(at point: NSPoint?) { blockHover.hover(at: point) }

    /// The paragraph under a point (text view coordinates) and its line area.
    func blockFrame(at point: CGPoint) -> (NSRange, CGRect)? {
        guard let manager = textView.textLayoutManager, let contentManager = manager.textContentManager else { return nil }
        let origin = textView.textContainerOrigin
        let local = CGPoint(x: max(0, point.x - origin.x), y: point.y - origin.y)
        guard let fragment = manager.textLayoutFragment(for: local) else { return nil }
        let offset = contentManager.offset(from: contentManager.documentRange.location, to: fragment.rangeInElement.location)
        guard offset < string.length else { return nil }
        var frame = fragment.layoutFragmentFrame
        if let first = fragment.textLineFragments.first {
            frame = CGRect(x: frame.minX, y: frame.minY + first.typographicBounds.minY, width: frame.width, height: first.typographicBounds.height)
        }
        let info = storage.blockInfo(at: offset)
        let indent = info.map { theme.textIndent(kind: $0.kind, depth: $0.depth) - ($0.kind.isList ? theme.markerWidth : 0) } ?? 0
        frame = frame.offsetBy(dx: origin.x + indent, dy: origin.y)
        return (paragraph(at: offset), frame)
    }

    func blockIds() -> Set<String> {
        var ids = Set<String>()
        var location = 0
        while location < string.length {
            let paragraph = paragraph(at: location)
            if let id = storage.blockInfo(at: paragraph.location)?.id { ids.insert(id) }
            location = NSMaxRange(paragraph)
        }
        return ids
    }

    /// Blocks a paste created remember where they came from (a web page's address).
    func markPasted(from start: Int, to end: Int, existing: Set<String>, sourceURL: String?) {
        guard let sourceURL, end > start else { return }
        let provenance: JSONValue = .object(["source": .string("paste"), "source_url": .string(sourceURL)])
        _ = currentBlocks()  // settle duplicate ids first
        isNormalizing = true
        defer { isNormalizing = false }
        for paragraph in paragraphs(in: NSRange(location: start, length: end - start)) {
            guard let info = storage.blockInfo(at: paragraph.location), !existing.contains(info.id) else { continue }
            storage.addAttribute(.fnBlock, value: info.with(newProvenance: provenance), range: paragraph)
        }
    }

    // MARK: Pasting and dropping files

    /// Images and files from the pasteboard upload to the server and become blocks.
    func pasteFiles(from pasteboard: NSPasteboard) -> Bool {
        guard let uploadFile else { return false }
        var items: [(Data, String, String)] = []
        if let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
            for url in urls {
                guard let data = try? Data(contentsOf: url) else { continue }
                let type = UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
                items.append((data, url.lastPathComponent, type))
            }
        } else if pasteboard.string(forType: .string) == nil,
                  let data = pasteboard.data(forType: .png) ?? pasteboard.data(forType: .tiff).flatMap({ NSBitmapImageRep(data: $0)?.representation(using: .png, properties: [:]) }) {
            items.append((data, "Pasted image.png", "image/png"))
        }
        guard !items.isEmpty else { return false }
        let at = textView.selectedRange()
        Task {
            var location = NSMaxRange(paragraph(at: at.location))
            for (data, name, type) in items {
                do {
                    let uploaded = try await uploadFile(data, name, type)
                    let kind: BlockKind = type.hasPrefix("image/") ? .image : .file
                    let markdown = kind == .image ? "![\(uploaded.name)](\(uploaded.url))" : "[\(uploaded.name)](\(uploaded.url))"
                    let origin = BlockOrigin(type: kind.canonicalType, attrs: ["url": .string(uploaded.url), "name": .string(uploaded.name)],
                                             content: .object(["markdown": .string(markdown)]), text: uploaded.name, kind: kind, native: true)
                    let paragraph = DocumentText.paragraph(EditorBlock(kind: kind, depth: info(at: at.location).depth, origin: origin), context: context)
                    location = min(location, string.length)
                    replace(NSRange(location: location, length: 0), with: paragraph,
                            selecting: NSRange(location: location + paragraph.length, length: 0))
                    location += paragraph.length
                } catch {
                    NSSound.beep()
                }
            }
        }
        return true
    }

    // MARK: Copying blocks within and between notes

    func writeBlocks(to pasteboard: NSPasteboard) -> Bool {
        let selection = textView.selectedRange()
        guard selection.length > 0 else { return false }
        let substring = storage.attributedSubstring(from: selection)
        let (blocks, _) = DocumentText.blocks(from: substring)
        let payload = blocks.map { block -> [String: JSONValue] in
            let fields = BlockCodec.encode(block)
            return [
                "type": .string(fields.type), "attrs": .object(fields.attrs), "content": fields.content,
                "text": .string(fields.text), "depth": .number(Double(block.depth)),
                "partial": .bool(!substring.string.hasSuffix("\n") && block == blocks.last),
            ]
        }
        guard let data = try? JSONEncoder().encode(payload) else { return false }
        return pasteboard.setData(data, forType: Self.blocksPasteboardType)
    }

    /// Pastes blocks copied from a note, keeping kinds and marks (with new ids).
    func pasteBlocks(from pasteboard: NSPasteboard) -> Bool {
        guard let data = pasteboard.data(forType: Self.blocksPasteboardType),
              let payload = try? JSONDecoder().decode([[String: JSONValue]].self, from: data), !payload.isEmpty else { return false }
        let selection = textView.selectedRange()
        let current = info(at: selection.location)
        var blocks = payload.map { item -> EditorBlock in
            let node = BlockNode(
                id: EditorBlock.newId(), documentId: "", parentId: nil, position: 0,
                type: item["type"]?.stringValue ?? "paragraph",
                attrs: { if case .object(let attrs)? = item["attrs"] { return attrs }; return [:] }(),
                userAttrs: [:], content: item["content"] ?? .object([:]), text: item["text"]?.stringValue ?? "",
                createdAt: "", updatedAt: "", children: [])
            var block = BlockCodec.decode(node, depth: (item["depth"]?.intValue ?? 0) + current.depth)
            block.origin = nil
            if block.kind.hasInlineText == false { block.origin = BlockCodec.decode(node).origin }
            return block
        }
        // One partial paragraph pastes inline, into the current paragraph.
        if blocks.count == 1, payload[0]["partial"]?.boolValue == true, blocks[0].kind.hasInlineText {
            let inline = DocumentText.inline(blocks[0].runs, info: current, context: context)
            replace(selection, with: inline, selecting: NSRange(location: selection.location + inline.length, length: 0))
            return true
        }
        let minDepth = blocks.map(\.depth).min() ?? 0
        for index in blocks.indices { blocks[index].depth = blocks[index].depth - minDepth + current.depth }
        let text = NSMutableAttributedString()
        for block in blocks { text.append(DocumentText.paragraph(block, context: context)) }
        let paragraph = paragraph(at: selection.location)
        let content = contentRange(of: paragraph)
        if content.length == 0 && selection.length == 0 {
            replace(paragraph, with: text, selecting: NSRange(location: paragraph.location + text.length - 1, length: 0))
        } else {
            replace(selection, with: text, selecting: NSRange(location: selection.location + text.length, length: 0))
        }
        return true
    }
}

