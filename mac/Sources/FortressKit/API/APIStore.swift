import Foundation
import Synchronization

/// What an `APIClient` shares between its copies: requests in flight (so concurrent
/// GETs for the same resource make one request), a small cache of notes kept with their
/// ETags, and a request counter.
///
/// Thread-safe; every method is synchronous and cheap (a lock around dictionaries).
public final class APIStore: Sendable {
    struct CachedDocument: Sendable {
        var document: BlockDocument
        var etag: String?
        var storedAt: Date
    }

    private struct InFlight: Sendable {
        var id: UInt64
        var task: Task<any Sendable, any Error>
        var waiters: Int
    }

    private struct State: Sendable {
        var nextId: UInt64 = 0
        var inFlight: [String: InFlight] = [:]
        var documents: [String: CachedDocument] = [:]
        /// Least recently used first.
        var documentOrder: [String] = []
        /// Bumped when a document is invalidated, so a read that started before can't
        /// put the old version back.
        var documentGenerations: [String: Int] = [:]
        var allGeneration = 0
        /// Bumped by every mutation: a GET started before a change never answers one
        /// asked for after it.
        var epoch = 0
        var requests = 0
    }

    /// How long a cached note is served without asking the server at all: only long
    /// enough for reads that arrive together (the editor and the inspector opening one
    /// note, a prefetched neighbour opened with ↓) to share it. After that its ETag
    /// decides (a 304 is cheap).
    let documentMaxAge: TimeInterval
    /// Notes kept in memory.
    let documentCapacity: Int
    private let state = Mutex(State())

    public init(documentMaxAge: TimeInterval = 2, documentCapacity: Int = 64) {
        self.documentMaxAge = documentMaxAge
        self.documentCapacity = documentCapacity
    }

    /// Requests that went to the network (for tests and benchmarks).
    public var requests: Int { state.withLock { $0.requests } }

    public func resetRequests() { state.withLock { $0.requests = 0 } }

    func countRequest() { state.withLock { $0.requests += 1 } }

    // MARK: Coalescing

    /// The current mutation epoch, part of every coalescing key.
    var epoch: Int { state.withLock { $0.epoch } }

    func mutationStarted() { state.withLock { $0.epoch += 1 } }

    /// Runs `operation`, or joins the one already running under `key`. A waiter that is
    /// cancelled stops waiting; the request itself is cancelled once nobody waits for it.
    func coalesced<T: Sendable>(
        _ key: String, _ operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        let (id, task) = state.withLock { state -> (UInt64, Task<any Sendable, any Error>) in
            if var entry = state.inFlight[key] {
                entry.waiters += 1
                state.inFlight[key] = entry
                return (entry.id, entry.task)
            }
            state.nextId += 1
            let task = Task<any Sendable, any Error>.detached { try await operation() }
            state.inFlight[key] = InFlight(id: state.nextId, task: task, waiters: 1)
            return (state.nextId, task)
        }
        return try await withTaskCancellationHandler {
            defer { finish(key, id: id) }
            let value = try await task.value
            try Task.checkCancellation()
            guard let typed = value as? T else { throw CancellationError() }
            return typed
        } onCancel: {
            leave(key, id: id)
        }
    }

    /// Readers waiting on the request in flight under `key` (for tests).
    func waiters(_ key: String) -> Int { state.withLock { $0.inFlight[key]?.waiters ?? 0 } }

    private func finish(_ key: String, id: UInt64) {
        state.withLock { state in
            if state.inFlight[key]?.id == id { state.inFlight[key] = nil }
        }
    }

    private func leave(_ key: String, id: UInt64) {
        let abandoned: Task<any Sendable, any Error>? = state.withLock { state in
            guard var entry = state.inFlight[key], entry.id == id else { return nil }
            entry.waiters -= 1
            if entry.waiters <= 0 {
                state.inFlight[key] = nil
                return entry.task
            }
            state.inFlight[key] = entry
            return nil
        }
        abandoned?.cancel()
    }

    // MARK: Documents

    /// The cached document while it is fresh enough to show without asking.
    func freshDocument(_ id: String, now: Date = Date()) -> BlockDocument? {
        state.withLock { state in
            guard let entry = state.documents[id], now.timeIntervalSince(entry.storedAt) < documentMaxAge
            else { return nil }
            Self.touch(id, in: &state)
            return entry.document
        }
    }

    /// The cached document however old (to revalidate it).
    func cachedDocument(_ id: String) -> CachedDocument? {
        state.withLock { $0.documents[id] }
    }

    /// A token taken before reading a document; `store` drops the result if the document
    /// was invalidated meanwhile.
    func documentToken(_ id: String) -> DocumentToken {
        state.withLock { DocumentToken(document: $0.documentGenerations[id] ?? 0, all: $0.allGeneration) }
    }

    struct DocumentToken: Sendable, Hashable {
        var document: Int
        var all: Int
    }

    /// Keeps a document the server sent. An older revision never replaces a newer one.
    func store(_ document: BlockDocument, etag: String? = nil, token: DocumentToken? = nil) {
        state.withLock { state in
            if let token,
               token != DocumentToken(document: state.documentGenerations[document.id] ?? 0, all: state.allGeneration) {
                return
            }
            if let existing = state.documents[document.id], existing.document.revision > document.revision { return }
            state.documents[document.id] = CachedDocument(document: document, etag: etag, storedAt: Date())
            Self.touch(document.id, in: &state)
            while state.documentOrder.count > max(1, documentCapacity) {
                let evicted = state.documentOrder.removeFirst()
                state.documents[evicted] = nil
            }
        }
    }

    /// Merges a metadata-only answer (a rename: `children` empty) into
    /// the cached note and returns the merged note. Only when the cached copy is at the
    /// same revision (its content is then still the server's); it keeps its old ETag,
    /// so the next read after `documentMaxAge` revalidates. Otherwise the cached copy is
    /// dropped and the metadata comes back as it is.
    func mergeMetadata(_ metadata: BlockDocument) -> BlockDocument {
        state.withLock { state in
            guard var entry = state.documents[metadata.id], entry.document.revision == metadata.revision else {
                Self.drop(metadata.id, in: &state)
                return metadata
            }
            var merged = metadata
            merged.children = entry.document.children
            merged.resolvedLinks = entry.document.resolvedLinks
            entry.document = merged
            state.documents[metadata.id] = entry
            return merged
        }
    }

    /// Marks a cached document as just checked (a 304).
    func refresh(_ id: String) {
        state.withLock { state in
            state.documents[id]?.storedAt = Date()
            Self.touch(id, in: &state)
        }
    }

    public func invalidateDocument(_ id: String) {
        state.withLock { Self.drop(id, in: &$0) }
    }

    public func invalidateAllDocuments() {
        state.withLock { state in
            state.documents = [:]
            state.documentOrder = []
            state.allGeneration += 1
        }
    }

    /// Drops cached documents the navigation tree says changed (or went away).
    public func reconcile(with navigation: NavigationResponse) {
        let updated = Dictionary(
            NoteListing.allDocuments(in: navigation.items).map { ($0.id, $0.updatedAt) }, uniquingKeysWith: { a, _ in a })
        state.withLock { state in
            for (id, entry) in state.documents where updated[id] != entry.document.updatedAt {
                Self.drop(id, in: &state)
            }
        }
    }

    public var cachedDocumentIds: Set<String> { state.withLock { Set($0.documents.keys) } }

    private static func drop(_ id: String, in state: inout State) {
        state.documents[id] = nil
        state.documentOrder.removeAll { $0 == id }
        state.documentGenerations[id, default: 0] += 1
    }

    private static func touch(_ id: String, in state: inout State) {
        if let index = state.documentOrder.lastIndex(of: id) { state.documentOrder.remove(at: index) }
        state.documentOrder.append(id)
    }
}
