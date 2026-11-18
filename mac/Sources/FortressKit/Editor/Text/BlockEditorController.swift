import AppKit
import ImageIO
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
    /// A column's editor: told after its layout changed (a picture loaded), and when the
    /// caret leaves its first or last line (true = forward).
    var onRelayout: (() -> Void)?
    var onLeave: ((Bool) -> Void)?
    /// A column's editor inside a columns block, not the note's own text.
    let isEmbedded: Bool

    private var isNormalizing = false
    private var isLoading = false
    private var lastReplacement: String?
    /// The pending change adds, removes or retypes paragraphs (list numbers may move).
    private var structuralEdit = false
    private var focusedBlockId: String?
    private lazy var completion = LinkCompletion(controller: self)
    private lazy var blockHover = BlockHover(controller: self)

    init(baseURL: URL, embedded: Bool = false) {
        textView = FortressTextView.make()
        scrollView = NSScrollView()
        context = AttachmentContext(baseURL: baseURL)
        isEmbedded = embedded
        super.init()
        if embedded {
            textView.isEmbedded = true
            textView.isContinuousSpellCheckingEnabled = false
            textView.isVerticallyResizable = false
            textView.autoresizingMask = []
        } else {
            scrollView.documentView = textView
        }
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = true
        scrollView.backgroundColor = .themeBackground
        scrollView.contentView.postsBoundsChangedNotifications = false
        textView.controller = self
        textView.delegate = self
        textView.textStorage?.delegate = self
        textView.textLayoutManager?.delegate = fragments
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        context.relayout = { [weak self] in self?.relayoutAttachments() }
        context.imageDidLoad = { [weak self] url in self?.imageDidLoad(url) }
        context.openURL = { [weak self] url in self?.open(url) }
        context.openNote = { [weak self] id in self?.onOpenNote?(id, nil) }
        context.contentDidChange = { [weak self] in
            guard let self, !self.isLoading else { return }
            self.onChange?()
        }
        context.attachmentDidResize = { [weak self] attachment in self?.relayout(attachment) }
        context.leaveAttachment = { [weak self] attachment, forward in self?.leave(attachment, forward: forward) }
        context.configureNested = { [weak self] child in self?.configure(nested: child) }
    }

    /// A column's editor forwards links, uploads and focus to this editor.
    private func configure(nested child: BlockEditorController) {
        child.theme = theme
        child.onOpenNote = { [weak self] id, block in self?.onOpenNote?(id, block) }
        child.onFocusBlock = { [weak self] id in self?.onFocusBlock?(id) }
        child.searchNotes = { [weak self] query in
            guard let search = self?.searchNotes else { return [] }
            return await search(query)
        }
        child.uploadFile = { [weak self] data, name, type in
            guard let upload = self?.uploadFile else { throw URLError(.cancelled) }
            return try await upload(data, name, type)
        }
        child.context.resolveReference = { [weak self] id in
            guard let resolve = self?.context.resolveReference else { return nil }
            return await resolve(id)
        }
        child.context.referenceText = context.referenceText
        child.context.documentTitles = context.documentTitles
        child.context.session = context.session
    }

    // MARK: Tables and columns

    private func range(of attachment: NSTextAttachment) -> NSRange? {
        var found: NSRange?
        storage.enumerateAttribute(.attachment, in: NSRange(location: 0, length: storage.length), options: []) { value, range, stop in
            if (value as AnyObject?) === attachment {
                found = range
                stop.pointee = true
            }
        }
        return found
    }

    /// A table or columns block changed height: lay it (and what follows) out again.
    private func relayout(_ attachment: BlockAttachment) {
        guard let manager = textView.textLayoutManager, let range = range(of: attachment),
              let textRange = textRange(for: NSRange(location: range.location, length: string.length - range.location))
        else { relayoutAttachments(); return }
        manager.invalidateLayout(for: textRange)
        manager.textViewportLayoutController.layoutViewport()
        textView.needsDisplay = true
        onRelayout?()
    }

    /// Esc, Tab or an arrow at the edge of a table or columns: the caret goes to the
    /// text after (or before) the block, making an empty paragraph if there is none.
    private func leave(_ attachment: BlockAttachment, forward: Bool) {
        guard let range = range(of: attachment) else { return }
        let paragraph = paragraph(at: range.location)
        textView.window?.makeFirstResponder(textView)
        if forward {
            if NSMaxRange(paragraph) >= string.length {
                let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
                replace(NSRange(location: NSMaxRange(paragraph), length: 0), with: attributed("\n", info: BlockInfo(depth: info.depth)),
                        selecting: NSRange(location: NSMaxRange(paragraph), length: 0))
            } else {
                textView.setSelectedRange(NSRange(location: NSMaxRange(paragraph), length: 0))
            }
        } else if paragraph.location > 0 {
            let previous = self.paragraph(at: paragraph.location - 1)
            textView.setSelectedRange(NSRange(location: NSMaxRange(contentRange(of: previous)), length: 0))
        } else if isEmbedded {
            onLeave?(false)
        } else {
            replace(NSRange(location: 0, length: 0), with: attributed("\n", info: BlockInfo()),
                    selecting: NSRange(location: 0, length: 0))
        }
        textView.scrollRangeToVisible(textView.selectedRange())
    }

    /// Inserts an empty 3 × 3 table (with a header row) after the current block.
    func insertTable() {
        let at = NSMaxRange(paragraph(at: textView.selectedRange().location))
        let depth = info(at: textView.selectedRange().location).depth
        let table = DocumentText.paragraph(EditorBlock(kind: .table, depth: depth, table: .blank()), context: context)
        replace(NSRange(location: min(at, string.length), length: 0), with: table,
                selecting: NSRange(location: min(at, string.length), length: 0))
        if let attachment = storage.attribute(.attachment, at: min(at, storage.length - 1), effectiveRange: nil) as? BlockAttachment {
            Self.later { [weak attachment] in attachment?.live?.focus(atEnd: false) }
        }
    }

    /// After the current layout pass (a run loop turn).
    static func later(_ body: @escaping @MainActor () -> Void) {
        RunLoop.main.perform(inModes: [.common]) { MainActor.assumeIsolated { body() } }
    }

    /// The height of a column's text at `width` (embedded editors).
    func contentHeight(forWidth width: CGFloat) -> CGFloat {
        ceil(max(26 * theme.scale, textView.fittingTextHeight(width: width)))
    }

    func focusEnd() {
        textView.window?.makeFirstResponder(textView)
        textView.setSelectedRange(NSRange(location: max(0, string.length - 1), length: 0))
    }

    var storage: NSTextStorage { textView.textStorage! }
    private var string: NSString { storage.string as NSString }

    // MARK: Loading and reading

    /// Shows `blocks`. A long note shows its first screens at once and the rest in
    /// chunks over the next run loop turns (`appendPendingBlocks`), so opening it costs
    /// what its first screens cost; until the rest is in, `currentBlocks()` reports it
    /// as it will read back, so nothing is lost or saved twice. Anything that needs the
    /// whole text (finding a block, select all, find) finishes loading first.
    func load(_ blocks: [EditorBlock]) {
        let interval = Signposts.begin("Load text")
        defer { Signposts.end("Load text", interval) }
        loadGeneration += 1
        let all = blocks.isEmpty ? [EditorBlock()] : blocks
        let headCount = isEmbedded ? all.count : Self.headCount(all)
        let head = Array(all[..<headCount])
        isLoading = true
        defer { isLoading = false }
        let text = DocumentText.build(head, theme: theme, context: context)
        blockCache = [:]
        storage.setAttributedString(text)
        blockCache = cachedBlocks(head, from: 0)
        lastRead = []
        dirtyBlocks.removeAll()
        dirtyInfos.removeAll()
        pendingBlocks = headCount < all.count ? all[headCount...].map { Self.readBack($0, context: context) } : []
        textView.undoManager?.removeAllActions()
        textView.setSelectedRange(NSRange(location: 0, length: 0))
        syncTypingAttributes()
        if !pendingBlocks.isEmpty {
            let generation = loadGeneration
            Self.later { [weak self] in self?.appendPendingBlocks(generation: generation) }
        }
    }

    /// A text block just shown is what reading it back gives (unless building the text
    /// rewrote it): keep those, so the baseline right after opening and the first save
    /// don't read every block back. Tables, columns and embeds are read from the text.
    private func cachedBlocks(_ blocks: [EditorBlock], from start: Int) -> [String: CachedBlock] {
        var byId: [String: EditorBlock] = [:]
        byId.reserveCapacity(blocks.count)
        for block in blocks where Self.readsBackAsShown(block) { byId[block.id] = block }
        var cache = blockCache
        cache.reserveCapacity(cache.count + blocks.count)
        storage.enumerateAttribute(.fnBlock, in: NSRange(location: start, length: storage.length - start), options: []) { value, range, _ in
            guard let info = value as? BlockInfo, let block = byId[info.id], cache[info.id]?.info !== info else { return }
            cache[info.id] = CachedBlock(info: info, length: range.length, block: block)
        }
        return cache
    }

    /// Blocks not in the text yet (a long note still loading), in order.
    private(set) var pendingBlocks: [EditorBlock] = []
    private var loadGeneration = 0
    /// Told when a long note's last blocks are in the text.
    var onLoadComplete: (() -> Void)?

    /// How many blocks fill the first screens (a little more than a tall window shows).
    private static func headCount(_ blocks: [EditorBlock]) -> Int {
        guard blocks.count > 150 else { return blocks.count }
        var height: CGFloat = 0
        for (index, block) in blocks.enumerated() {
            height += estimatedHeight(block)
            if height > 4000 || index >= 120 { return index + 1 }
        }
        return blocks.count
    }

    /// A rough height for choosing chunks (never used for layout).
    private static func estimatedHeight(_ block: EditorBlock) -> CGFloat {
        switch block.kind {
        case .table: return CGFloat(block.table?.rows.count ?? 3) * 34
        case .columns: return 240
        case .image: return 320
        case .divider, .file, .math, .embed: return 50
        default:
            let characters = block.runs.reduce(0) { $0 + $1.text.utf16.count }
            return 26 * CGFloat(1 + characters / 70) + 10
        }
    }

    /// `block` as reading it back from the text will give it (what `currentBlocks()`
    /// reports for it while it waits to be shown).
    private static func readBack(_ block: EditorBlock, context: AttachmentContext) -> EditorBlock {
        guard block.kind.hasInlineText, !readsBackAsShown(block) else { return block }
        var copy = block
        let text = DocumentText.inline(block.runs, info: BlockInfo(block), context: context)
        copy.runs = DocumentText.runs(in: text, range: NSRange(location: 0, length: text.length))
        return copy
    }

    /// Puts the next chunk of a loading note into the text (about a screen's worth of
    /// work per run loop turn, so typing and scrolling go on meanwhile).
    private func appendPendingBlocks(generation: Int) {
        guard generation == loadGeneration, !pendingBlocks.isEmpty else { return }
        if textView.hasMarkedText() {
            // Not while an input method is composing.
            Self.later { [weak self] in self?.appendPendingBlocks(generation: generation) }
            return
        }
        var height: CGFloat = 0
        var count = 0
        for block in pendingBlocks {
            height += Self.estimatedHeight(block)
            count += 1
            if height > 3000 || count >= 80 { break }
        }
        append(Array(pendingBlocks.prefix(count)))
        pendingBlocks.removeFirst(count)
        if pendingBlocks.isEmpty {
            onLoadComplete?()
        } else {
            Self.later { [weak self] in self?.appendPendingBlocks(generation: generation) }
        }
    }

    /// Shows the rest of a loading note now.
    func finishLoading() {
        guard !pendingBlocks.isEmpty else { return }
        let rest = pendingBlocks
        pendingBlocks = []
        append(rest)
        onLoadComplete?()
    }

    /// Appends loaded blocks after the text (not an edit: no undo, no save).
    private func append(_ blocks: [EditorBlock]) {
        let interval = Signposts.begin("Append blocks")
        defer { Signposts.end("Append blocks", interval) }
        isLoading = true
        defer { isLoading = false }
        let start = storage.length
        let text = DocumentText.build(blocks, theme: theme, context: context)
        storage.append(text)
        blockCache = cachedBlocks(blocks, from: start)
        extendLastRead(from: start)
    }

    /// The paragraphs just appended join the last read (so the first save after a long
    /// note loads doesn't walk all of it): read from the cache, or from the text.
    private func extendLastRead(from start: Int) {
        guard !lastRead.isEmpty, lastRead.reduce(0, { $0 + $1.length }) == start else { lastRead = []; return }
        var added: [ReadBlock] = []
        var valid = true
        storage.enumerateAttribute(.fnBlock, in: NSRange(location: start, length: storage.length - start), options: []) { value, range, stop in
            guard let info = value as? BlockInfo, string.character(at: NSMaxRange(range) - 1) == 0x0A,
                  string.range(of: "\n", options: .literal, range: NSRange(location: range.location, length: range.length - 1)).location == NSNotFound
            else { valid = false; stop.pointee = true; return }
            let block: EditorBlock
            if let cached = blockCache[info.id], cached.info === info, cached.length == range.length {
                block = cached.block
            } else {
                block = DocumentText.block(in: storage, paragraph: range, info: info)
            }
            added.append(ReadBlock(info: info, length: range.length, block: block))
        }
        if valid { lastRead += added } else { lastRead = [] }
    }

    /// The blocks as they are now (fixing duplicate ids from pastes as a side effect).
    ///
    /// Reading a block back (its runs and marks) is the expensive part, so blocks are kept
    /// from the last read and only paragraphs edited since (`dirtyBlocks`, or a new
    /// `BlockInfo`) are read again: a save after a keystroke reads one paragraph, not the
    /// note. Tables and columns hold their edits in the attachment and are always asked.
    func currentBlocks() -> [EditorBlock] {
        if let blocks = readEditedBlocks() { return pendingBlocks.isEmpty ? blocks : blocks + pendingBlocks }
        let string = self.string
        var blocks: [EditorBlock] = []
        var read: [ReadBlock] = []
        read.reserveCapacity(lastRead.count)
        var renamed: [(NSRange, BlockInfo)] = []
        var seen = Set<String>()
        var cache: [String: CachedBlock] = [:]
        cache.reserveCapacity(blockCache.count)
        var location = 0
        while location < string.length {
            let paragraph = string.paragraphRange(at: location)
            location = NSMaxRange(paragraph)
            let stored = storage.blockInfo(at: paragraph.location)
            var info = stored ?? BlockInfo()
            if seen.contains(info.id) || stored == nil {
                info = info.renamed()
                renamed.append((paragraph, info))
            }
            seen.insert(info.id)
            var block: EditorBlock
            if let cached = blockCache[info.id], cached.info === info, cached.length == paragraph.length,
               !dirtyBlocks.contains(info.id), !dirtyInfos.contains(ObjectIdentifier(info)) {
                block = cached.block
            } else {
                block = DocumentText.block(in: storage, paragraph: paragraph, info: info)
            }
            cache[info.id] = CachedBlock(info: info, length: paragraph.length, block: block)
            read.append(ReadBlock(info: info, length: paragraph.length, block: block))
            if !info.kind.hasInlineText,
               let attachment = storage.attribute(.attachment, at: paragraph.location, effectiveRange: nil) as? BlockAttachment,
               let live = attachment.live {
                live.write(into: &block)
            }
            blocks.append(block)
        }
        blockCache = cache
        lastRead = renamed.isEmpty ? read : []
        dirtyBlocks.removeAll(keepingCapacity: true)
        dirtyInfos.removeAll(keepingCapacity: true)
        if !renamed.isEmpty {
            isNormalizing = true
            storage.beginEditing()
            for (range, info) in renamed { storage.addAttribute(.fnBlock, value: info, range: range) }
            storage.endEditing()
            isNormalizing = false
        }
        return pendingBlocks.isEmpty ? blocks : blocks + pendingBlocks
    }

    /// The blocks when the paragraphs are the ones last read, in the same order (only
    /// their text changed): each kept block is checked where it should be (its block
    /// info at its start, its "\n" at its end) and only edited ones are read again, with
    /// no lookups by id. Nil when paragraphs were added, removed, split or joined since
    /// (then everything is walked again).
    private func readEditedBlocks() -> [EditorBlock]? {
        guard !lastRead.isEmpty else { return nil }
        let string = self.string
        let length = string.length
        let checkDirty = !dirtyInfos.isEmpty
        var blocks: [EditorBlock] = []
        blocks.reserveCapacity(lastRead.count)
        var location = 0
        for index in lastRead.indices {
            guard location < length, let info = storage.blockInfo(at: location) else { return nil }
            let entry = lastRead[index]
            var block: EditorBlock
            var paragraphLength = entry.length
            if info === entry.info, !checkDirty || !dirtyInfos.contains(ObjectIdentifier(info)) {
                let end = location + entry.length
                guard end <= length, string.character(at: end - 1) == 0x0A else { return nil }
                block = entry.block
            } else {
                // Edited (or given a new kind): the same block, read again. (A failed
                // check below leaves `lastRead` half updated: the full walk replaces it.)
                guard info.id == entry.info.id else { return nil }
                let paragraph = string.paragraphRange(at: location)
                guard paragraph.location == location else { return nil }
                block = DocumentText.block(in: storage, paragraph: paragraph, info: info)
                paragraphLength = paragraph.length
                lastRead[index] = ReadBlock(info: info, length: paragraph.length, block: block)
                blockCache[info.id] = CachedBlock(info: info, length: paragraph.length, block: block)
            }
            if !info.kind.hasInlineText,
               let attachment = storage.attribute(.attachment, at: location, effectiveRange: nil) as? BlockAttachment,
               let live = attachment.live {
                live.write(into: &block)
            }
            blocks.append(block)
            location += paragraphLength
        }
        guard location == length else { return nil }
        dirtyBlocks.removeAll(keepingCapacity: true)
        dirtyInfos.removeAll(keepingCapacity: true)
        return blocks
    }

    private struct ReadBlock {
        let info: BlockInfo
        let length: Int
        let block: EditorBlock
    }

    /// Every paragraph as last read, in order (empty: walk the text again).
    private var lastRead: [ReadBlock] = []
    /// Block infos of paragraphs edited since the last read (by identity: cheap to ask).
    private var dirtyInfos: Set<ObjectIdentifier> = []

    /// Whether reading `block`'s paragraph back gives `block` itself: inline text only, with
    /// runs already normalized and no separators or stray attachment characters (building
    /// the text turns those into soft breaks or drops them).
    private static func readsBackAsShown(_ block: EditorBlock) -> Bool {
        guard block.kind.hasInlineText, block.table == nil, block.columns == nil else { return false }
        for run in block.runs where run.node == nil {
            if run.text.isEmpty { return false }
            for unit in run.text.utf16 where unit == 0x0A || unit == 0x0D || unit == 0x2029 || unit == 0xFFFC {
                return false
            }
        }
        return BlockCodec.normalized(block.runs) == block.runs
    }

    /// Forgets the blocks kept from loading and earlier reads, so the next
    /// `currentBlocks()` reads every paragraph back from the text (tests of the round trip).
    func discardReadCache() {
        blockCache = [:]
        lastRead = []
    }

    private struct CachedBlock {
        let info: BlockInfo
        let length: Int
        let block: EditorBlock
    }

    /// Blocks as last read back, by id (see `currentBlocks`).
    private var blockCache: [String: CachedBlock] = [:]
    /// Blocks whose paragraphs were edited since they were last read.
    private var dirtyBlocks: Set<String> = []

    /// Text of the first non-empty blocks, for the live auto title.
    func leadingTexts(limit: Int = 3) -> [String] {
        var output: [String] = []
        var location = 0
        while location < string.length, output.count < limit {
            let paragraph = string.paragraphRange(at: location)
            location = NSMaxRange(paragraph)
            guard let info = storage.blockInfo(at: paragraph.location), info.kind.hasInlineText else { continue }
            // A title is one line of at most 80 characters: never read a whole long paragraph.
            var content = contentRange(of: paragraph)
            if content.length > 400 { content = (string as NSString).rangeOfComposedCharacterSequences(for: NSRange(location: content.location, length: 400)) }
            let text = DocumentText.runs(in: storage, range: content)
            let plain = BlockCodec.plainText(text)
            if !plain.trimmingCharacters(in: .whitespaces).isEmpty { output.append(plain) }
        }
        return output
    }

    func range(ofBlock id: String) -> NSRange? {
        if !pendingBlocks.isEmpty {
            if let found = range(ofLoadedBlock: id) { return found }
            finishLoading()
        }
        return range(ofLoadedBlock: id)
    }

    private func range(ofLoadedBlock id: String) -> NSRange? {
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

    /// Where the caret is, as a block and an offset in it (survives reloads and edits elsewhere).
    func selectionAnchor() -> (blockId: String, offset: Int)? {
        let location = textView.selectedRange().location
        guard location < string.length, let id = storage.blockInfo(at: paragraph(at: location).location)?.id else { return nil }
        return (id, location - paragraph(at: location).location)
    }

    /// Puts the caret back where it was when the note was last open, and scrolls to it.
    @discardableResult
    func restoreSelection(_ anchor: (blockId: String, offset: Int)) -> Bool {
        guard let range = range(ofBlock: anchor.blockId) else { return false }
        let content = contentRange(of: range)
        let location = (string as NSString).rangeOfComposedCharacterSequence(at: min(content.location + anchor.offset, NSMaxRange(content))).location
        textView.window?.makeFirstResponder(textView)
        textView.setSelectedRange(NSRange(location: min(max(content.location, location), NSMaxRange(content)), length: 0))
        textView.scrollRangeToVisible(textView.selectedRange())
        return true
    }

    func focusStart() {
        textView.window?.makeFirstResponder(textView)
        textView.setSelectedRange(NSRange(location: 0, length: 0))
    }

    private func scrollToCenter(_ range: NSRange) {
        // Scroll there first (TextKit 2 places it from estimates), lay out just the
        // viewport, then center on the block's real frame. Laying out everything above the
        // block (ensureLayout from the start) made opening a link to the end of a long note
        // lay out the whole note.
        textView.layoutSubtreeIfNeeded()
        textView.scrollRangeToVisible(range)
        guard let manager = textView.textLayoutManager, let textRange = textRange(for: range) else { return }
        manager.textViewportLayoutController.layoutViewport()
        guard let fragment = manager.textLayoutFragment(for: textRange.location) else { return }
        let frame = fragment.layoutFragmentFrame.offsetBy(dx: textView.textContainerOrigin.x, dy: textView.textContainerOrigin.y)
        let visible = scrollView.contentView.bounds.height
        // The view may not have grown to the laid-out text yet.
        if textView.frame.height < frame.maxY + visible / 2 {
            textView.setFrameSize(NSSize(width: textView.frame.width, height: frame.maxY + visible / 2))
        }
        let y = max(0, frame.midY - visible / 2)
        scrollView.contentView.scroll(to: NSPoint(x: 0, y: y))
        scrollView.reflectScrolledClipView(scrollView.contentView)
        manager.textViewportLayoutController.layoutViewport()
    }

    private func flash(_ range: NSRange) {
        guard let manager = textView.textLayoutManager, let textRange = textRange(for: range) else { return }
        let color = NSColor.themeAccent.withAlphaComponent(0.12)
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

    /// Lays everything out again, once per run loop turn however many ask (a burst of
    /// resolved block references used to relayout the whole note once each).
    private func relayoutAttachments() {
        guard !relayoutScheduled else { return }
        relayoutScheduled = true
        Self.later { [weak self] in
            guard let self else { return }
            self.relayoutScheduled = false
            guard let manager = self.textView.textLayoutManager else { return }
            manager.invalidateLayout(for: manager.documentRange)
            self.textView.needsDisplay = true
            self.onRelayout?()
        }
    }

    private var relayoutScheduled = false
    private var loadedImages: Set<URL> = []

    /// A picture arrived. Pictures with a stored size already have their space (a
    /// placeholder of the right size), so nothing moves; only ones without lay out again,
    /// just their own paragraphs, batched per run loop turn.
    private func imageDidLoad(_ url: URL) {
        let first = loadedImages.isEmpty
        loadedImages.insert(url)
        guard first else { return }
        Self.later { [weak self] in self?.relayoutLoadedImages() }
    }

    private func relayoutLoadedImages() {
        let urls = loadedImages
        loadedImages = []
        guard let manager = textView.textLayoutManager else { return }
        var changed = false
        storage.enumerateAttribute(.attachment, in: NSRange(location: 0, length: storage.length), options: []) { value, range, _ in
            guard let attachment = value as? BlockAttachment, attachment.info.kind == .image,
                  let attrs = attachment.info.origin?.origin.attrs, !ImageLayout.hasStoredSize(attrs),
                  let href = attrs["url"]?.stringValue, let url = context.url(for: href), urls.contains(url),
                  let textRange = textRange(for: paragraph(at: range.location)) else { return }
            manager.invalidateLayout(for: textRange)
            changed = true
        }
        guard changed else { return }
        manager.textViewportLayoutController.layoutViewport()
        textView.needsDisplay = true
        onRelayout?()
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
            // A loaded note is built well-formed and styled (`DocumentText.build`).
            guard !isNormalizing, !isLoading else { return }
            isNormalizing = true
            defer { isNormalizing = false }
            let start = EditorMetrics.now()
            defer { EditorMetrics.normalize += EditorMetrics.since(start) }
            normalize(storage, editedRange)
            if editedMask.contains(.editedCharacters) {
                let context = context
                InlineNodes.separateCopies(in: storage, range: editedRange) { DocumentText.label(of: $0, context: context) }
            }
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
        // In place, so pasted rich text keeps its formatting (replacing the whole range
        // with the first character's attributes dropped every bold word and link).
        var edited = NSIntersectionRange(editedRange, NSRange(location: 0, length: storage.length))
        if edited.length > 0 {
            let mutable = storage.mutableString
            for (separator, shrinks) in [("\r\n", 1), ("\r", 0), ("\u{2029}", 0)]
            where mutable.range(of: separator, options: .literal, range: edited).location != NSNotFound {
                let count = mutable.replaceOccurrences(of: separator, with: "\n", options: .literal, range: edited)
                edited.length -= count * shrinks
            }
            if mutable.range(of: "\n", options: .literal, range: edited).location != NSNotFound { structuralEdit = true }
        }
        let text = storage.string as NSString
        let start = text.paragraphRange(at: min(editedRange.location, text.length - 1)).location
        let end = min(text.length, NSMaxRange(editedRange) + 1)
        var location = start
        var previousId: String? = start > 0 ? storage.blockInfo(at: text.paragraphRange(at: start - 1).location)?.id : nil
        var touched: [NSRange] = []
        var touchedInfo: BlockInfo?
        var uniform = true
        while location < text.length {
            let paragraph = text.paragraphRange(at: location)
            var info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
            if info.id == previousId {
                info = info.renamed()
            }
            if !info.kind.hasInlineText {
                let hasAttachment = paragraph.length > 1 && storage.attribute(.attachment, at: paragraph.location, effectiveRange: nil) is BlockAttachment
                if !hasAttachment {
                    info = BlockInfo(kind: .paragraph, depth: info.depth)
                }
            }
            // One lookup: the info on the paragraph's first character, and how far it runs.
            var tagged = NSRange()
            let first = storage.attribute(.fnBlock, at: paragraph.location, longestEffectiveRange: &tagged, in: paragraph) as? BlockInfo
            if first !== info || tagged != paragraph { storage.addAttribute(.fnBlock, value: info, range: paragraph) }
            dirtyBlocks.insert(info.id)
            dirtyInfos.insert(ObjectIdentifier(info))
            touched.append(paragraph)
            if let firstInfo = touchedInfo {
                if firstInfo.kind != info.kind || firstInfo.depth != info.depth || firstInfo.checked != info.checked { uniform = false }
            } else {
                touchedInfo = info
            }
            previousId = info.id
            location = NSMaxRange(paragraph)
            if location >= end { break }
        }
        let restyleStart = EditorMetrics.now()
        defer { EditorMetrics.restyle += EditorMetrics.since(restyleStart) }
        guard let first = touched.first, let last = touched.last else { return }
        let whole = NSUnionRange(first, last)
        if touched.count >= 16, uniform, let info = touchedInfo, Self.isPlainAndStyled(storage, whole),
           Self.has(theme.cachedAttributes(info: info, marks: InlineMarks()), throughout: whole, in: storage) {
            // Many paragraphs of one kind with no marks (pasted lines) already styled as
            // that kind (they came with the typing attributes): nothing to restyle.
            // Styling them paragraph by paragraph took a second for 25,000 lines.
        } else {
            for paragraph in touched { theme.restyle(storage, range: paragraph) }
        }
    }

    /// Whether every presentation attribute in `range` is the one in `attributes` (and
    /// none it lacks is set), checked a key at a time over the whole range.
    private static func has(_ attributes: [NSAttributedString.Key: Any], throughout range: NSRange, in storage: NSTextStorage) -> Bool {
        for key in EditorTheme.presentationKeys {
            var effective = NSRange()
            let value = storage.attribute(key, at: range.location, longestEffectiveRange: &effective, in: range)
            guard effective == range else { return false }
            switch (value, attributes[key]) {
            case (nil, nil): continue
            case (let found?, let expected?): if !(found as AnyObject).isEqual(expected) { return false }
            default: return false
            }
        }
        return true
    }

    /// No marks, nodes or attachments anywhere in `range`, and all of it styled by the
    /// editor (not pasted rich text still to be read for its marks).
    private static func isPlainAndStyled(_ storage: NSTextStorage, _ range: NSRange) -> Bool {
        for key in [NSAttributedString.Key.fnMarks, .fnNode, .attachment] {
            var effective = NSRange()
            if storage.attribute(key, at: range.location, longestEffectiveRange: &effective, in: range) != nil || effective != range {
                return false
            }
        }
        var effective = NSRange()
        return storage.attribute(.fnStyled, at: range.location, longestEffectiveRange: &effective, in: range) != nil && effective == range
    }

    // MARK: NSTextViewDelegate

    func textView(_ textView: NSTextView, willChangeSelectionFromCharacterRange oldSelectedCharRange: NSRange,
                  toCharacterRange newSelectedCharRange: NSRange) -> NSRange {
        // The final "\n" belongs to the last block; the caret never goes past it.
        let limit = max(0, string.length - 1)
        var range = newSelectedCharRange
        if range.location > limit { range.location = limit }
        if NSMaxRange(range) > limit { range.length = max(0, limit - range.location) }
        // Note links and block references are stepped over and selected whole.
        return InlineNodes.snappedSelection(range, from: oldSelectedCharRange, in: storage)
    }

    func textView(_ textView: NSTextView, shouldChangeTextIn affectedCharRange: NSRange, replacementString: String?) -> Bool {
        lastReplacement = replacementString
        if !structuralEdit {
            structuralEdit = replacementString.map { $0.utf16.contains(0x0A) } ?? true
                || (affectedCharRange.length > 0 && string.range(of: "\n", options: .literal, range: affectedCharRange).location != NSNotFound)
        }
        if !isApplyingEdit, let replacementString, !textView.hasMarkedText() {
            // A note link or block reference changes whole (Delete removes it), and
            // typing inside its label goes after it.
            let whole = InlineNodes.wholeNodes(affectedCharRange, in: storage)
            if whole != affectedCharRange {
                let typed = NSAttributedString(string: replacementString, attributes: textView.typingAttributes)
                replace(whole, with: typed, selecting: NSRange(location: whole.location + typed.length, length: 0))
                return false
            }
        }
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
        // Typing inside a paragraph can't renumber a list; only structural edits relayout
        // the items below (a long numbered list would otherwise relayout per keystroke).
        if structuralEdit { invalidateNumbering(around: textView.selectedRange().location) }
        structuralEdit = false
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
        if let id = NoteLinks.documentId(from: link) {
            onOpenNote?(id, nil)
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
        case #selector(NSResponder.moveUp(_:)), #selector(NSResponder.moveDown(_:)):
            return moveIntoBlock(forward: commandSelector == #selector(NSResponder.moveDown(_:)))
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
        format.addItem(withTitle: "Superscript", action: #selector(FortressTextView.fortressToggleSuperscript(_:)), keyEquivalent: "")
        format.addItem(withTitle: "Subscript", action: #selector(FortressTextView.fortressToggleSubscript(_:)), keyEquivalent: "")
        format.addItem(.separator())
        format.addItem(withTitle: "Insert Table", action: #selector(FortressTextView.fortressInsertTable(_:)), keyEquivalent: "")
        let item = NSMenuItem(title: "Paragraph Style", action: nil, keyEquivalent: "")
        item.submenu = format
        menu.insertItem(item, at: 0)
        menu.insertItem(.separator(), at: 1)
        return menu
    }

    /// ↑/↓ next to a table or columns block goes into it; in a column, ↑ on the first line
    /// or ↓ on the last leaves the column.
    private func moveIntoBlock(forward: Bool) -> Bool {
        let selection = textView.selectedRange()
        guard selection.length == 0 else { return false }
        let current = paragraph(at: selection.location)
        let onEdgeLine: Bool = {
            let caret = textView.firstRect(forCharacterRange: NSRange(location: selection.location, length: 0), actualRange: nil)
            let edgeLocation = forward ? NSMaxRange(contentRange(of: current)) : current.location
            let edge = textView.firstRect(forCharacterRange: NSRange(location: edgeLocation, length: 0), actualRange: nil)
            return abs(caret.midY - edge.midY) < 2
        }()
        guard onEdgeLine else { return false }
        if let attachment = storage.attribute(.attachment, at: current.location, effectiveRange: nil) as? BlockAttachment,
           let live = attachment.live {
            // The caret sits on a table or columns block itself.
            live.focus(atEnd: !forward)
            return true
        }
        let neighbour = forward ? NSMaxRange(current) : current.location - 1
        guard neighbour >= 0, neighbour < string.length else {
            if isEmbedded { onLeave?(forward); return true }
            return false
        }
        let next = paragraph(at: neighbour)
        guard let attachment = storage.attribute(.attachment, at: next.location, effectiveRange: nil) as? BlockAttachment,
              let live = attachment.live else { return false }
        live.focus(atEnd: !forward)
        return true
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
            $0.kind.holdsOwnChildren ? $0.depth - 1 : $0.depth
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
        case "####" where current == .paragraph: return (.heading4, false)
        case "#####" where current == .paragraph: return (.heading5, false)
        case "######" where current == .paragraph: return (.heading6, false)
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
                    let (temporary, _) = try await MediaCache.shared.session.download(from: url)
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
        // A `docLink` node: its label is the note's current title, never stored.
        context.documentTitles[suggestion.documentId] = suggestion.title
        let node = NoteLinks.documentNode(id: suggestion.documentId)
        let replacement = NSMutableAttributedString(attributedString: DocumentText.inline([.atomic(node)], info: info, context: context, theme: theme))
        replacement.append(attributed(" ", info: info))
        textView.breakUndoCoalescing()
        replace(range, with: replacement, selecting: NSRange(location: range.location + replacement.length, length: 0))
    }

    // MARK: Keys and clicks

    func handleKeyEquivalent(_ event: NSEvent) -> Bool {
        // During IME composition (Japanese, Chinese…) the input method owns the keys;
        // formatting the uncommitted text would break the composition.
        guard !textView.hasMarkedText() else { return false }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask).subtracting([.capsLock, .numericPad, .function])
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        switch (flags, key) {
        case ([.command], "b"): toggleMark(\.bold)
        case ([.command], "i"): toggleMark(\.italic)
        case ([.command], "u"): toggleMark(\.underline)
        case ([.command, .option], "e"): toggleMark(\.code)  // ⌘E stays Use Selection for Find.
        case ([.command, .shift], "x"): toggleMark(\.strike)
        case ([.command, .shift], "h"): toggleHighlight("yellow")
        case ([.command, .control], "="), ([.command, .control], "+"): toggleMark(\.sup)
        case ([.command, .control], "-"): toggleMark(\.sub)
        case ([.command], "k"): editLink()  // As in Notes, Pages, Mail and TextEdit.
        case ([.command, .option], "0"): setKind(.paragraph)
        case ([.command, .option], "1"): setKind(.heading1)
        case ([.command, .option], "2"): setKind(.heading2)
        case ([.command, .option], "3"): setKind(.heading3)
        case ([.command, .shift], "7"), ([.command, .shift], "&"): setKind(.bullet)
        case ([.command, .shift], "8"), ([.command, .shift], "*"): setKind(.numbered)
        case ([.command, .shift], "l"): setKind(.check)
        case ([.command, .option], "'"): setKind(.quote)
        case ([.command, .option, .control], "c"): setKind(.code)  // ⌥⌘C is Copy Style.
        case ([.command, .shift], "u"): toggleChecked()  // Notes' Mark as Checked.
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

    func hover(at point: NSPoint?) {
        guard !isEmbedded else { return }
        blockHover.hover(at: point)
    }

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

    /// Images and files from the pasteboard upload to the server and become blocks, in
    /// order, after the block at `location` (the drop point, or the caret). Files are read
    /// off the main thread; the blocks follow their anchor block even if the text is edited
    /// while uploads run.
    func pasteFiles(from pasteboard: NSPasteboard, at location: Int? = nil) -> Bool {
        guard let uploadFile else { return false }
        var sources: [(url: URL?, data: Data?, name: String, type: String)] = []
        if let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
            for url in urls {
                let type = UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
                sources.append((url, nil, url.lastPathComponent, type))
            }
        } else if pasteboard.string(forType: .string) == nil,
                  let data = pasteboard.data(forType: .png) ?? pasteboard.data(forType: .tiff).flatMap({ NSBitmapImageRep(data: $0)?.representation(using: .png, properties: [:]) }) {
            sources.append((nil, data, "Pasted image.png", "image/png"))
        }
        guard !sources.isEmpty else { return false }
        let at = min(max(0, location ?? textView.selectedRange().location), max(0, string.length - 1))
        var anchorId = storage.blockInfo(at: paragraph(at: at).location)?.id
        let depth = info(at: at).depth
        Task {
            for source in sources {
                do {
                    let data: Data
                    if let file = source.url {
                        data = try await Task.detached(priority: .userInitiated) { try Data(contentsOf: file, options: .mappedIfSafe) }.value
                    } else {
                        data = source.data ?? Data()
                    }
                    let uploaded = try await uploadFile(data, source.name, source.type)
                    let kind: BlockKind = source.type.hasPrefix("image/") ? .image : .file
                    var attrs: [String: JSONValue] = ["url": .string(uploaded.url), "name": .string(uploaded.name)]
                    if kind == .image, let size = await Self.pixelSize(of: data) {
                        // Stored size: the picture's space is right before it loads (no jump).
                        attrs["width"] = .number(Double(size.width))
                        attrs["height"] = .number(Double(size.height))
                    } else if kind == .file {
                        attrs["size"] = .number(Double(data.count))
                    }
                    let origin = BlockOrigin(type: kind.canonicalType, attrs: attrs, text: uploaded.name, kind: kind)
                    let block = EditorBlock(kind: kind, depth: depth, origin: origin)
                    let paragraph = DocumentText.paragraph(block, context: context)
                    let insertAt = min(anchorId.flatMap { range(ofBlock: $0) }.map(NSMaxRange) ?? string.length, string.length)
                    replace(NSRange(location: insertAt, length: 0), with: paragraph,
                            selecting: NSRange(location: insertAt + paragraph.length, length: 0))
                    anchorId = block.id
                } catch {
                    NSSound.beep()
                }
            }
        }
        return true
    }

    /// An image's pixel size from its header (no decoding).
    nonisolated static func pixelSize(of data: Data) async -> CGSize? {
        await Task.detached(priority: .utility) { () -> CGSize? in
            guard let source = CGImageSourceCreateWithData(data as CFData, nil),
                  let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
                  let width = properties[kCGImagePropertyPixelWidth] as? Int,
                  let height = properties[kCGImagePropertyPixelHeight] as? Int, width > 0, height > 0 else { return nil }
            let orientation = properties[kCGImagePropertyOrientation] as? Int ?? 1
            return orientation >= 5 ? CGSize(width: height, height: width) : CGSize(width: width, height: height)
        }.value
    }

    // MARK: Copying blocks within and between notes

    func writeBlocks(to pasteboard: NSPasteboard) -> Bool {
        let selection = textView.selectedRange()
        guard selection.length > 0 else { return false }
        let substring = storage.attributedSubstring(from: selection)
        let (blocks, _) = DocumentText.blocks(from: substring)
        let payload = blocks.map { block -> [String: JSONValue] in
            let fields = BlockCodec.encode(block)
            var item: [String: JSONValue] = [
                "type": .string(fields.type), "kind": .string(block.kind.rawValue), "attrs": .object(fields.attrs),
                "content": fields.content, "depth": .number(Double(block.depth)),
                "partial": .bool(!substring.string.hasSuffix("\n") && block == blocks.last),
            ]
            if let payload = fields.payload { item["payload"] = payload }
            // A table travels with its cells' text (pasted with new ids).
            if let table = block.table {
                item["rows"] = .array(table.rows.map { row in
                    .array(row.cells.map { cell in .object(["attrs": .object(cell.attrs), "content": BlockCodec.inlineContent(cell.runs)]) })
                })
            }
            return item
        }
        guard let data = try? JSONEncoder().encode(payload) else { return false }
        return pasteboard.setData(data, forType: Self.blocksPasteboardType)
    }

    /// The selection as Markdown (alongside rich text, plain text and the blocks).
    func writeMarkdown(to pasteboard: NSPasteboard) -> Bool {
        let selection = textView.selectedRange()
        guard selection.length > 0 else { return false }
        let (blocks, _) = DocumentText.blocks(from: storage.attributedSubstring(from: selection))
        return pasteboard.setString(MarkdownCopy.markdown(blocks), forType: Self.markdownPasteboardType)
    }

    static let markdownPasteboardType = NSPasteboard.PasteboardType(MarkdownCopy.typeIdentifier)

    /// Pastes blocks copied from a note, keeping kinds and marks (with new ids).
    func pasteBlocks(from pasteboard: NSPasteboard) -> Bool {
        guard let data = pasteboard.data(forType: Self.blocksPasteboardType),
              let payload = try? JSONDecoder().decode([[String: JSONValue]].self, from: data), !payload.isEmpty else { return false }
        let selection = textView.selectedRange()
        let current = info(at: selection.location)
        var blocks = payload.map { item -> EditorBlock in
            var rows: [BlockNode] = []
            if case .array(let items)? = item["rows"] {
                rows = items.map { row in
                    guard case .array(let cells) = row else { return BlockNode(id: EditorBlock.newId(), type: "table_row") }
                    return BlockNode(id: EditorBlock.newId(), type: "table_row", children: cells.map { cell in
                        var attrs: [String: JSONValue] = [:]
                        if case .object(let found)? = cell["attrs"] { attrs = found }
                        return BlockNode(id: EditorBlock.newId(), type: "table_cell", attrs: attrs, children: [
                            BlockNode(id: EditorBlock.newId(), type: "paragraph", content: cell["content"] ?? .array([])),
                        ])
                    })
                }
            }
            let node = BlockNode(
                id: EditorBlock.newId(), type: item["type"]?.stringValue ?? "paragraph",
                attrs: { if case .object(let attrs)? = item["attrs"] { return attrs }; return [:] }(),
                content: item["content"] ?? .null, payload: item["payload"], children: rows)
            var block = BlockCodec.decode(node, depth: (item["depth"]?.intValue ?? 0) + current.depth)
            if item["kind"]?.stringValue == BlockKind.quote.rawValue { block.kind = .quote }
            if block.kind.hasInlineText { block.origin = nil }
            if var table = block.table {
                // Pasted cells are new blocks: inserted, not updated.
                for row in table.rows.indices {
                    for cell in table.rows[row].cells.indices { table.rows[row].cells[cell].hasParagraph = false }
                }
                block.table = table
            }
            return block
        }
        // Columns travel without their blocks on this pasteboard; they aren't pasted.
        blocks.removeAll { $0.kind == .columns }
        guard !blocks.isEmpty else { return false }
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

