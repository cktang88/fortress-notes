import Foundation
import Observation

/// One open note: loads it, and saves edits as block transactions.
///
/// Saving: a change schedules a flush 400 ms later. A flush diffs the text against what
/// the server holds (`base`) into one transaction at the current revision. One is in
/// flight at a time; edits made meanwhile go in the next. The returned revision is
/// adopted. A 409 means the note changed elsewhere: sending stops and a banner offers
/// to load the latest version. A network failure keeps the edits (they are still in
/// the text) and retries the same transaction with backoff, so a request that did
/// reach the server is not applied twice (the server deduplicates by transaction id).
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
    /// Replaces the editor's text (load, load latest).
    @ObservationIgnored var showBlocks: (([EditorBlock]) -> Void)?

    @ObservationIgnored private(set) var base = SyncedTree()
    @ObservationIgnored private(set) var revision = 0
    @ObservationIgnored private var flushTask: Task<Void, Never>?
    @ObservationIgnored private var titleTask: Task<Void, Never>?
    @ObservationIgnored private var inFlight = false
    @ObservationIgnored private var dirty = false
    @ObservationIgnored private var unsent: (transaction: BlockTransaction, result: SyncedTree)?
    @ObservationIgnored private var retryDelay: Double = 0
    /// Debounce for edits.
    @ObservationIgnored var saveDelay: Duration = .milliseconds(400)

    public init(documentId: String, api: APIClient) {
        self.documentId = documentId
        self.api = api
    }

    /// For previews and tests: a session showing a document without a server.
    init(document: BlockDocument, api: APIClient = APIClient()) {
        self.documentId = document.id
        self.api = api
        adopt(document, replaceText: false)
    }

    public var hasUnsavedChanges: Bool { dirty || inFlight || unsent != nil }

    // MARK: Loading

    func load() async {
        do {
            let document = try await api.document(id: documentId)
            loadError = nil
            adopt(document, replaceText: true)
        } catch {
            loadError = error.localizedDescription
        }
    }

    /// Drops local edits and shows the server's version (after a conflict).
    func loadLatest() async {
        flushTask?.cancel()
        unsent = nil
        dirty = false
        await load()
        if loadError == nil { saveState = .saved }
    }

    private func adopt(_ document: BlockDocument, replaceText: Bool) {
        self.document = document
        revision = document.revision
        titleAuto = document.titleAuto ?? false
        title = document.title
        status = document.status
        updatedAt = Timestamp.date(document.updatedAt)
        let blocks = BlockCodec.flatten(document.children)
        if replaceText, let showBlocks {
            showBlocks(blocks)
            // The baseline is what the editor reads back, so an untouched note never saves.
            base = SyncedTree((currentBlocks?() ?? nil) ?? blocks)
        } else {
            base = SyncedTree(blocks)
        }
    }

    /// The blocks to show when the editor appears.
    var initialBlocks: [EditorBlock] {
        document.map { BlockCodec.flatten($0.children) } ?? []
    }

    func resetBase(to blocks: [EditorBlock]) {
        base = SyncedTree(blocks)
    }

    /// Lets go of the editor once the note is closed and saved.
    func detach() {
        currentBlocks = nil
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

    /// Sends pending edits now (also used when the note closes).
    public func flush() async {
        guard !inFlight, saveState != .conflict else { return }
        let pending: (transaction: BlockTransaction, result: SyncedTree)
        if let unsent {
            pending = unsent
        } else {
            guard dirty, let blocks = currentBlocks?() ?? nil else {
                if !dirty && saveState == .saving { saveState = .saved }
                return
            }
            dirty = false
            let (operations, result) = BlockDiff.operations(from: base, to: blocks)
            guard !operations.isEmpty else {
                base = result
                if saveState == .saving { saveState = .saved }
                return
            }
            pending = (BlockTransaction(baseRevision: revision, operations: operations), result)
        }
        inFlight = true
        saveState = .saving
        defer { inFlight = false }
        do {
            let document = try await api.apply(pending.transaction, to: documentId)
            unsent = nil
            retryDelay = 0
            base = pending.result
            revision = document.revision
            self.document = document
            updatedAt = Timestamp.date(document.updatedAt)
            if document.titleAuto ?? false, titleTask == nil {
                titleAuto = true
                title = document.title
            }
            saveState = dirty ? .saving : .saved
            if dirty { scheduleFlush(after: .milliseconds(50)) }
        } catch let error as APIError where (400..<500).contains(error.status) && error.status != 408 && error.status != 429 {
            unsent = nil
            saveState = .conflict
        } catch {
            // Offline or a server hiccup: keep the edits, try the same request again.
            unsent = pending
            saveState = .offline
            retryDelay = min(30, max(2, retryDelay * 2))
            scheduleFlush(after: .seconds(retryDelay))
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
        } catch {
            saveState = .offline
        }
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
        } catch {
            status = previous
        }
    }
}
