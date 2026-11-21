import Foundation
import Observation

/// Loads what the inspector shows for the open note, with as few requests as possible:
///
/// - **Info, related notes and backlinks** load together (in parallel) when a note opens,
///   are cancelled when another note opens first, and are cached per note *revision*
///   (the note's `updated_at`): going back to a note that hasn't changed asks nothing.
///   An edit refreshes Info only, after the edits pause.
/// - **Seen before** follows the caret, but only once it settles on a block (400 ms),
///   cancelling the request for a block the caret already left. Answers are cached per
///   (note, block, the block's saved text) in an LRU, so moving back over blocks
///   whose text didn't change asks nothing.
@MainActor
@Observable
final class InspectorModel {
    private(set) var noteId: String?
    private(set) var info = NoteInfo()
    private(set) var related: [RelatedResult] = []
    private(set) var backlinks: [Backlink] = []
    private(set) var seenBefore: [RelatedResult] = []
    /// Set when related notes or backlinks couldn't load (offline): the empty states
    /// mustn't claim there are none.
    private(set) var loadError: String?
    /// The workspace's background reading of pictures and PDFs (`GET /api/ocr`).
    private(set) var ocrStatus: OCRStatus?

    struct SeenKey: Hashable, Sendable {
        var noteId: String
        var blockId: String
        /// The block's saved text version (nil until the note is first saved).
        var version: Int?
    }

    struct NoteEntry: Sendable {
        var revision: String?
        var info: NoteInfo
        var infoRevision: String?
        var related: [RelatedResult]
        var backlinks: [Backlink]
    }

    @ObservationIgnored private let seenDebounce: Duration
    @ObservationIgnored private let infoDebounce: Duration
    @ObservationIgnored private var noteTask: Task<Void, Never>?
    @ObservationIgnored private var infoTask: Task<Void, Never>?
    @ObservationIgnored private var seenTask: Task<Void, Never>?
    @ObservationIgnored private var revision: String?
    @ObservationIgnored private var seenKey: SeenKey?
    @ObservationIgnored private(set) var notes = LRUCache<String, NoteEntry>(capacity: 48)
    @ObservationIgnored private(set) var seen = LRUCache<SeenKey, [RelatedResult]>(capacity: 256)

    init(seenDebounce: Duration = .milliseconds(400), infoDebounce: Duration = .milliseconds(800)) {
        self.seenDebounce = seenDebounce
        self.infoDebounce = infoDebounce
    }

    isolated deinit {
        noteTask?.cancel()
        infoTask?.cancel()
        seenTask?.cancel()
    }

    // MARK: The note

    /// The open note (and its revision) changed. `fallback` is what the sidebar knows,
    /// shown at once for a note seen for the first time.
    func show(noteId: String?, revision: String?, fallback: @autoclosure () -> NoteInfo, api: APIClient) {
        guard let noteId else {
            cancelAll()
            self.noteId = nil
            self.revision = nil
            return
        }
        if noteId == self.noteId {
            guard revision != self.revision else { return }
            // Edited: refresh Info once the edits pause; related notes and backlinks keep.
            self.revision = revision
            refreshInfo(noteId: noteId, revision: revision, api: api, after: infoDebounce)
            return
        }

        cancelAll()
        self.noteId = noteId
        self.revision = revision
        seenKey = nil
        seenBefore = []
        loadError = nil
        if let entry = notes.value(for: noteId) {
            info = entry.info
            related = entry.related
            backlinks = entry.backlinks
            if entry.revision == revision { return }  // Unchanged since last time: nothing to ask.
        } else {
            info = fallback()
            related = []
            backlinks = []
        }
        noteTask = Task { [weak self] in
            await self?.loadNote(noteId: noteId, revision: revision, api: api)
        }
    }

    private func loadNote(noteId: String, revision: String?, api: APIClient) async {
        // All three at once; each runs off the main actor (decoding included).
        async let infoResult = Self.loadInfo(noteId: noteId, api: api)
        async let relatedResult = Self.loadRelated(noteId: noteId, api: api)
        async let linksResult = Self.loadBacklinks(noteId: noteId, api: api)
        let (loadedInfo, loadedRelated, loadedLinks) = await (infoResult, relatedResult, linksResult)
        guard !Task.isCancelled, self.noteId == noteId else { return }

        if let loadedInfo { info = merged(loadedInfo) }
        if case .success(let value) = loadedRelated { related = value }
        if case .success(let value) = loadedLinks { backlinks = value }
        if info.backlinksIn == nil, case .success(let links) = loadedLinks {
            info.backlinksIn = Set(links.map(\.sourceDocumentId)).count
        }
        let failed = [loadedRelated.failure, loadedLinks.failure].compactMap { $0 }.first
        loadError = failed.map { AskTurn.message(for: $0) }
        // Only complete answers are cached, so a failure is asked again next time.
        if failed == nil, loadedInfo != nil {
            notes.insert(NoteEntry(revision: revision, info: info, infoRevision: revision, related: related, backlinks: backlinks),
                         for: noteId)
        }
    }

    nonisolated private static func loadRelated(noteId: String, api: APIClient) async -> Result<[RelatedResult], Error> {
        do { return .success(try await api.related(documentId: noteId, count: 8)) } catch { return .failure(error) }
    }

    nonisolated private static func loadBacklinks(noteId: String, api: APIClient) async -> Result<[Backlink], Error> {
        do { return .success(try await api.backlinks(documentId: noteId)) } catch { return .failure(error) }
    }

    /// Fresh info, keeping what only the sidebar knew.
    private func merged(_ loaded: NoteInfo) -> NoteInfo {
        var merged = loaded
        if merged.backlinksIn == nil { merged.backlinksIn = info.backlinksIn }
        if merged.folderPath.isEmpty { merged.folderPath = info.folderPath }
        if merged.createdAt == nil { merged.createdAt = info.createdAt }
        if merged.updatedAt == nil { merged.updatedAt = info.updatedAt }
        return merged
    }

    /// `…/info` (nil when it fails: the sidebar's info stays).
    nonisolated private static func loadInfo(noteId: String, api: APIClient) async -> NoteInfo? {
        try? await NoteInfo(api.documentInfo(id: noteId))
    }

    private func refreshInfo(noteId: String, revision: String?, api: APIClient, after delay: Duration) {
        infoTask?.cancel()
        infoTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            let loaded = await Self.loadInfo(noteId: noteId, api: api)
            guard let self, !Task.isCancelled, self.noteId == noteId, let loaded else { return }
            let merged = self.merged(loaded)
            self.info = merged
            if var entry = self.notes.peek(noteId) {
                entry.info = merged
                entry.infoRevision = revision
                entry.revision = revision
                self.notes.insert(entry, for: noteId)
            }
        }
    }

    // MARK: Text in pictures

    /// Reloads Info now (a picture of this note was just read).
    func reloadInfo(api: APIClient) {
        guard let noteId else { return }
        refreshInfo(noteId: noteId, revision: revision, api: api, after: .zero)
    }

    /// While the inspector is shown: how far the workspace's picture reading is, and the
    /// open note's pictures while any is still to be read (their reading isn't an edit, so
    /// nothing else would reload them). Every `busy` while there's work, else every `idle`.
    func watchPictureReading(api: APIClient, busy: Duration = .seconds(5), idle: Duration = .seconds(30)) async {
        while !Task.isCancelled {
            if let status = try? await api.ocrStatus(), status != ocrStatus { ocrStatus = status }
            if Task.isCancelled { return }
            if info.isReadingPictures { reloadInfo(api: api) }
            let working = (ocrStatus?.remaining ?? 0) > 0 || info.isReadingPictures
            try? await Task.sleep(for: working ? busy : idle)
        }
    }

    // MARK: Seen before

    /// The caret moved to `blockId` (nil: no block). Cached answers show at once; others
    /// are asked for once the caret has rested `seenDebounce`.
    func focus(blockId: String?, version: Int?, api: APIClient) {
        guard let noteId, let blockId else {
            seenTask?.cancel()
            seenTask = nil
            seenKey = nil
            if !seenBefore.isEmpty { seenBefore = [] }
            return
        }
        let key = SeenKey(noteId: noteId, blockId: blockId, version: version)
        guard key != seenKey else { return }
        seenKey = key
        seenTask?.cancel()
        seenTask = nil
        if let cached = seen.value(for: key) {
            if cached != seenBefore { seenBefore = cached }
            return
        }
        let delay = seenDebounce
        seenTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            let results: [RelatedResult]
            do {
                results = try await api.related(documentId: noteId, blockId: blockId, count: 3)
            } catch {
                return  // Offline or cancelled: keep what's shown, ask again next time.
            }
            guard let self, !Task.isCancelled else { return }
            let matches = results.filter { $0.matchedBlockId != nil && !$0.matchedBlockText.isEmpty }
            self.seen.insert(matches, for: key)
            guard self.seenKey == key else { return }
            if matches != self.seenBefore { self.seenBefore = matches }
        }
    }

    private func cancelAll() {
        noteTask?.cancel()
        infoTask?.cancel()
        seenTask?.cancel()
        noteTask = nil
        infoTask = nil
        seenTask = nil
    }
}

private extension Result where Failure == any Error {
    var failure: Error? {
        if case .failure(let error) = self { return error }
        return nil
    }
}

extension AppModel {
    /// Called with each saved version of the open note: remembers each block's text
    /// version, so "Seen before" is cached per block text, not per keystroke.
    func recordBlockVersions(of document: BlockDocument) {
        var versions: [String: Int] = [:]
        func visit(_ nodes: [BlockNode]) {
            for node in nodes {
                var hasher = Hasher()
                hasher.combine(node.text)
                hasher.combine(node.revision)
                versions[node.id] = hasher.finalize()
                visit(node.children)
            }
        }
        visit(document.children)
        if savedBlockVersions != versions { savedBlockVersions = versions }
    }
}
