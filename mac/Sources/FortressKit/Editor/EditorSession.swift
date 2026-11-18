import AppKit
import Foundation
import Observation

/// One open note: loads it, and saves edits as block transactions.
///
/// Saving: a change schedules a flush 400 ms later. A flush diffs the text against what
/// the server holds (`base`) into one transaction at the current revision, so everything
/// typed in a debounce window (any number of edits to any blocks) is one transaction and
/// each changed block one operation. One is in flight at a time; edits made meanwhile go
/// in the next. The returned revision is adopted. A 409 means the note changed elsewhere:
/// sending stops and a banner offers to load the latest version. A network failure or a
/// 5xx keeps the edits (they are still in the text) and retries the same transaction with
/// backoff, so a request that did reach the server is not applied twice (the server
/// deduplicates by transaction id).
///
/// Durability: before a transaction is sent, the edits not yet confirmed are written to
/// disk (`PendingEdits`), and again when the app quits; they are removed once the server
/// has everything. Opening a note with such edits replays them onto it and saves them.
@MainActor
@Observable
public final class EditorSession {
    public enum SaveState: Equatable, Sendable {
        case saved, saving, offline, conflict
    }

    public let documentId: String
    @ObservationIgnored let api: APIClient

    public private(set) var document: BlockDocument?
    public private(set) var loadError: String?
    public private(set) var saveState: SaveState = .saved
    public var title = ""
    public private(set) var titleAuto = false
    public private(set) var status: NoteStatus = .rough
    public private(set) var updatedAt: Date?

    /// Supplies the editor's current blocks (set by the text view controller).
    @ObservationIgnored var currentBlocks: (() -> [EditorBlock]?)?
    /// Told about the note after each save, so the list, titles and Recent stay current.
    @ObservationIgnored public var onDocumentChange: ((BlockDocument) -> Void)?
    /// Replaces the editor's text (load, load latest).
    @ObservationIgnored var showBlocks: (([EditorBlock]) -> Void)?
    /// Told when a save can't reach the server (the app notices lost connections).
    @ObservationIgnored var onSaveError: ((Error) -> Void)?

    /// What the server holds, as the last diff left it. Built when first needed (the
    /// baseline of a note just opened is made after the note is on screen).
    var base: SyncedTree {
        get {
            if let baseTree { return baseTree }
            let interval = Signposts.begin("Baseline")
            let tree = SyncedTree(baseBlocks ?? [], reusing: nil, lazily: true)
            Signposts.end("Baseline", interval)
            baseTree = tree
            baseBlocks = nil
            return tree
        }
        set {
            baseTree = newValue
            baseBlocks = nil
        }
    }
    @ObservationIgnored private var baseTree: SyncedTree? = SyncedTree()
    /// The blocks the baseline is to be made from (until it is).
    @ObservationIgnored private var baseBlocks: [EditorBlock]?
    @ObservationIgnored private(set) var revision = 0
    @ObservationIgnored private var flushTask: Task<Void, Never>?
    @ObservationIgnored private var titleTask: Task<Void, Never>?
    @ObservationIgnored private var inFlight = false
    @ObservationIgnored private var inFlightWaiters: [CheckedContinuation<Void, Never>] = []
    @ObservationIgnored private var dirty = false
    @ObservationIgnored private var unsent: (transaction: BlockTransaction, result: SyncedTree)?
    @ObservationIgnored private var retryDelay: Double = 0
    /// Edits from an earlier visit replayed onto the loaded note (shown, not yet saved).
    @ObservationIgnored private var recovered: [EditorBlock]?
    /// The blocks of the transaction being sent, and of the last one the server took.
    /// Saves answer with metadata only, so these (not `document.children`) are the
    /// note's content as the server holds it.
    @ObservationIgnored private var sendingBlocks: [EditorBlock]?
    @ObservationIgnored private var savedBlocks: [EditorBlock]?
    /// The note's blocks as loaded.
    @ObservationIgnored private var loadedBlocks: [EditorBlock] = []
    /// Debounce for edits.
    @ObservationIgnored var saveDelay: Duration = .milliseconds(400)
    /// Whether unconfirmed edits are written to disk (off for previews and most tests).
    @ObservationIgnored var persistsPendingEdits = true

    public init(documentId: String, api: APIClient) {
        self.documentId = documentId
        self.api = api
        Self.register(self)
    }

    /// For previews and tests: a session showing a document without a server.
    init(document: BlockDocument, api: APIClient = APIClient()) {
        self.documentId = document.id
        self.api = api
        persistsPendingEdits = false
        adopt(document, decoded: BlockCodec.flatten(document.children, resolved: document.resolvedLinks), replaceText: false)
        Self.register(self)
    }

    /// For tests: a session showing a document already decoded (as `load` decodes it,
    /// off the main thread).
    init(document: BlockDocument, decoded: [EditorBlock], api: APIClient = APIClient()) {
        self.documentId = document.id
        self.api = api
        persistsPendingEdits = false
        adopt(document, decoded: decoded, replaceText: false)
        Self.register(self)
    }

    /// The note's blocks, decoded off the main thread (a long note's tree takes a while).
    nonisolated static func decode(_ document: BlockDocument) async -> [EditorBlock] {
        await Task.detached(priority: .userInitiated) {
            let interval = Signposts.begin("Decode note")
            defer { Signposts.end("Decode note", interval) }
            return BlockCodec.flatten(document.children, resolved: document.resolvedLinks)
        }.value
    }

    public var hasUnsavedChanges: Bool { dirty || inFlight || isDiffing || unsent != nil }

    /// Notes with more blocks than this are diffed off the main thread.
    static let diffOffMainAbove = 5000
    @ObservationIgnored private var isDiffing = false
    /// Changes whenever the baseline is replaced from outside a save (a load, a reset).
    @ObservationIgnored private var baseGeneration = 0

    // MARK: Loading

    /// Loads the note. With `debounce`, a note that isn't already in memory waits a beat
    /// first, so skimming the list (a selection every few milliseconds, each view
    /// cancelled by the next) fetches only the note the person stops on.
    func load(debounce: Duration? = nil, latest: Bool = false) async {
        if let debounce, api.store.freshDocument(documentId, counting: false) == nil {
            try? await Task.sleep(for: debounce)
            if Task.isCancelled { return }
        }
        let interval = Signposts.begin("Note open")
        defer { Signposts.end("Note open", interval) }
        do {
            let document = latest ? try await api.reloadDocument(id: documentId) : try await api.document(id: documentId)
            let decoded = await Self.decode(document)
            loadError = nil
            let pending = persistsPendingEdits ? PendingEdits.load(documentId: documentId) : nil
            adopt(document, decoded: decoded, replaceText: true, pending: pending)
        } catch {
            loadError = error.localizedDescription
        }
    }

    /// Drops local edits and shows the server's version (after a conflict).
    func loadLatest() async {
        flushTask?.cancel()
        unsent = nil
        dirty = false
        recovered = nil
        savedBlocks = nil
        if persistsPendingEdits { PendingEdits.remove(documentId: documentId) }
        // From the server: the cached copy is the one that conflicted.
        await load(latest: true)
        if loadError == nil { saveState = .saved }
    }

    private func adopt(_ document: BlockDocument, decoded blocks: [EditorBlock], replaceText: Bool, pending: PendingEdits.Record? = nil) {
        self.document = document
        revision = document.revision
        titleAuto = document.titleAuto ?? false
        title = document.title
        status = document.status
        updatedAt = Timestamp.date(document.updatedAt)
        loadedBlocks = blocks
        var shown = blocks
        savedBlocks = nil
        recovered = nil
        if let pending, !pending.operations.isEmpty {
            // Edits from last time that never reached the server: show them on top of
            // the note and save them (the diff against the server's version sends only
            // what is still missing, so ones that did arrive aren't repeated).
            shown = BlockCodec.flatten(PendingEdits.apply(pending.operations, to: document.children), resolved: document.resolvedLinks)
            recovered = shown
        }
        if replaceText, let showBlocks {
            showBlocks(shown)
            // The baseline is what the editor reads back, so an untouched note never saves.
            setBaseLater(recovered == nil ? (currentBlocks?() ?? nil) ?? blocks : blocks)
        } else {
            setBaseLater(blocks)
        }
        if recovered != nil {
            dirty = true
            saveState = .saving
            scheduleFlush(after: saveDelay)
        }
    }

    /// The blocks to show when the editor appears.
    var initialBlocks: [EditorBlock] {
        if let recovered { return recovered }
        if let savedBlocks { return savedBlocks }
        // Decoded once when the note arrived (decoding a long note is not free).
        return loadedBlocks
    }

    func resetBase(to blocks: [EditorBlock]) {
        // Recovered edits stay pending against the server's version.
        guard recovered == nil else { return }
        // The baseline's fields are encoded only for blocks that change.
        setBaseLater(blocks)
    }

    /// The baseline becomes `blocks`: made on the next quiet run loop turn (or when a
    /// save needs it first), not while the note is being put on screen.
    private func setBaseLater(_ blocks: [EditorBlock]) {
        baseGeneration += 1
        baseTree = nil
        baseBlocks = blocks
        RunLoop.main.perform(inModes: [.default]) { [weak self] in
            MainActor.assumeIsolated { _ = self?.base }
        }
    }

    /// Lets go of the editor once the note is closed. What it shows last stays readable,
    /// so a retry after the editor is gone still has the edits.
    func detach() {
        if let blocks = currentBlocks?() {
            currentBlocks = { blocks }
        } else {
            currentBlocks = nil
        }
        showBlocks = nil
    }

    // MARK: Saving the body

    func textDidChange() {
        dirty = true
        if saveState == .saved { saveState = .saving }
        scheduleFlush(after: saveDelay)
    }

    private func scheduleFlush(after delay: Duration) {
        flushTask?.cancel()
        flushTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            await self?.flush()
        }
    }

    /// Sends pending edits now. If a transaction is already in flight, the edits go
    /// after it (they are never dropped).
    public func flush() async {
        guard !inFlight, !isDiffing, saveState != .conflict else {
            if inFlight || isDiffing, dirty { scheduleFlush(after: .milliseconds(50)) }
            return
        }
        let pending: (transaction: BlockTransaction, result: SyncedTree)
        if let unsent {
            pending = unsent
        } else {
            guard dirty, let blocks = currentBlocks?() ?? nil else {
                if !dirty && saveState == .saving { saveState = .saved }
                return
            }
            dirty = false
            let interval = Signposts.begin("Save diff")
            let diffed: (operations: [BlockOperation], result: SyncedTree)
            if blocks.count > Self.diffOffMainAbove {
                // A huge note (a pasted log): the diff runs off the main thread, so typing
                // goes on while it is worked out. The baseline must still be the one it
                // started from when it is done, or it is worked out again.
                let base = self.base
                let generation = baseGeneration
                isDiffing = true
                diffed = await Task.detached(priority: .userInitiated) { BlockDiff.operations(from: base, to: blocks) }.value
                isDiffing = false
                guard generation == baseGeneration else {
                    Signposts.end("Save diff", interval)
                    dirty = true
                    scheduleFlush(after: .milliseconds(50))
                    return
                }
            } else {
                diffed = BlockDiff.operations(from: base, to: blocks)
            }
            Signposts.end("Save diff", interval)
            let (operations, result) = diffed
            guard !operations.isEmpty else {
                base = result
                recovered = nil
                if persistsPendingEdits { PendingEdits.remove(documentId: documentId) }
                if saveState == .saving || saveState == .offline { saveState = .saved }
                return
            }
            pending = (BlockTransaction(baseRevision: revision, operations: operations), result)
            sendingBlocks = blocks
        }
        // Durable before it is sent: quitting or losing the connection now loses nothing.
        if persistsPendingEdits {
            PendingEdits.save(PendingEdits.Record(documentId: documentId, baseRevision: pending.transaction.baseRevision,
                                                  operations: pending.transaction.operations, savedAt: Date()))
        }
        inFlight = true
        saveState = .saving
        defer {
            inFlight = false
            let waiters = inFlightWaiters
            inFlightWaiters = []
            for waiter in waiters { waiter.resume() }
        }
        do {
            // Metadata only: the editor already has the content.
            var document = try await api.applyReturningMeta(pending.transaction, to: documentId)
            unsent = nil
            retryDelay = 0
            base = pending.result
            recovered = nil
            if let sendingBlocks { savedBlocks = sendingBlocks }
            revision = document.revision
            // Keep the loaded tree; the list's snippet comes from the first saved block.
            let firstText = savedBlocks?.first { $0.kind.hasInlineText }?.plainText
            document.children = self.document?.children ?? []
            self.document = document
            if let firstText {
                document.children = [BlockNode(id: "", documentId: documentId, type: "paragraph", text: firstText)]
            }
            updatedAt = Timestamp.date(document.updatedAt)
            if document.titleAuto ?? false, titleTask == nil {
                titleAuto = true
                title = document.title
            }
            onDocumentChange?(document)
            if !dirty, persistsPendingEdits { PendingEdits.remove(documentId: documentId) }
            saveState = dirty ? .saving : .saved
            if dirty { scheduleFlush(after: .milliseconds(50)) }
        } catch let error as APIError where (400..<500).contains(error.status) && error.status != 408 && error.status != 429 {
            // The edits stay on disk: Load Latest discards them, reopening replays them.
            unsent = nil
            saveState = .conflict
        } catch {
            // Offline or a server hiccup: keep the edits, try the same request again.
            unsent = pending
            if saveState != .offline { onSaveError?(error) }
            saveState = .offline
            retryDelay = min(30, max(2, retryDelay * 2))
            scheduleFlush(after: .seconds(retryDelay))
        }
    }

    /// Waits for a transaction on the wire to come back.
    private func waitForInFlight() async {
        guard inFlight else { return }
        await withCheckedContinuation { inFlightWaiters.append($0) }
    }

    /// Closing the note (switching to another): sends everything typed, waiting for a
    /// transaction already in flight first, and keeps on disk whatever can't be sent now.
    public func close() async {
        flushTask?.cancel()
        detach()
        for _ in 0..<4 {
            await waitForInFlight()
            guard hasUnsavedChanges, saveState != .conflict else { break }
            await flush()
            if saveState == .offline { break }
        }
        if hasUnsavedChanges { persistUnsavedEdits(synchronously: false) }
        await commitTitle()
    }

    /// Writes the edits the server hasn't confirmed to disk (also on quit).
    func persistUnsavedEdits(synchronously: Bool) {
        guard persistsPendingEdits, hasUnsavedChanges || recovered != nil, let blocks = currentBlocks?() ?? nil else { return }
        let operations = BlockDiff.operations(from: base, to: blocks).operations
        guard !operations.isEmpty else { return }
        PendingEdits.save(PendingEdits.Record(documentId: documentId, baseRevision: revision, operations: operations,
                                              savedAt: Date()), synchronously: synchronously)
    }

    // MARK: Quitting

    private final class WeakSession {
        weak var session: EditorSession?
        init(_ session: EditorSession) { self.session = session }
    }

    private static var sessions: [WeakSession] = []
    private static var terminationObserver: NSObjectProtocol?

    private static func register(_ session: EditorSession) {
        sessions.removeAll { $0.session == nil }
        sessions.append(WeakSession(session))
        guard terminationObserver == nil else { return }
        terminationObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { _ in
            MainActor.assumeIsolated { EditorSession.applicationWillTerminate() }
        }
    }

    /// The app is quitting: there is no time for a request, so unsaved edits go to disk
    /// (synchronously) and are saved the next time their note opens.
    static func applicationWillTerminate() {
        for box in sessions {
            box.session?.persistUnsavedEdits(synchronously: true)
        }
    }

    // MARK: Title and status

    /// The title as the person types it. Empty hands the title back to the first line.
    func titleDidChange(_ newTitle: String) {
        guard newTitle != title else { return }
        title = newTitle
        titleAuto = false
        titleTask?.cancel()
        titleTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(700))
            guard !Task.isCancelled else { return }
            await self?.commitTitle()
        }
    }

    func commitTitle() async {
        titleTask?.cancel()
        titleTask = nil
        guard let document, title != document.title || (titleAuto != (document.titleAuto ?? false)) else { return }
        let chosen = title.trimmingCharacters(in: .whitespaces)
        do {
            let updated = try await api.updateDocument(id: documentId, title: chosen)
            self.document?.title = updated.title
            self.document?.titleAuto = updated.titleAuto
            titleAuto = updated.titleAuto ?? false
            if chosen.isEmpty || titleAuto { title = updated.title }
            onDocumentChange?(updated)
        } catch {
            saveState = .offline
        }
    }

    /// The editor's text changed: save it, and keep an automatic title on the first line.
    /// The first lines are read only while the title follows them (not per keystroke).
    func editorDidChange(leadingTexts: () -> [String]) {
        textDidChange()
        if titleAuto, titleTask == nil { updateAutoTitle(from: leadingTexts()) }
    }

    /// While the title follows the first line, show the first line as it is typed.
    func updateAutoTitle(from texts: [String]) {
        guard titleAuto, titleTask == nil else { return }
        let derived = BlockCodec.firstLineTitle(texts)
        if derived != title { title = derived }
    }

    func setStatus(_ newStatus: NoteStatus) async {
        guard newStatus != status else { return }
        let previous = status
        status = newStatus
        do {
            let updated = try await api.updateDocument(id: documentId, status: newStatus)
            status = updated.status
            self.document?.status = updated.status
            onDocumentChange?(updated)
        } catch {
            status = previous
        }
    }
}
