import Foundation
import Synchronization

/// What an `APIClient` shares between its copies: requests in flight (so concurrent
/// GETs for the same resource make one request), the document cache, HTTP validators
/// (ETag → body, so a 304 can be answered from memory) and request counters.
///
/// Thread-safe; every method is synchronous and cheap (a lock around dictionaries).
public final class APIStore: Sendable {
    /// Behaviour switches, mainly so tests and benchmarks can compare with everything off.
    public struct Options: Sendable, Hashable {
        /// Concurrent identical GETs share one request.
        public var coalescesRequests = true
        /// Documents are served from memory while fresh.
        public var cachesDocuments = true
        /// How long a cached document is served without asking the server again. After
        /// that it is revalidated (a cheap 304 when the server sends ETags).
        public var documentMaxAge: TimeInterval = 30
        /// Documents kept in memory.
        public var documentCapacity = 64
        /// Bytes of validated GET bodies kept for 304 answers.
        public var validatorCapacity = 16 << 20

        public init() {}

        /// Every cache and coalescing turned off: how the client behaved before them.
        public static var uncached: Options {
            var options = Options()
            options.coalescesRequests = false
            options.cachesDocuments = false
            options.validatorCapacity = 0
            return options
        }
    }

    /// Counters for tests, benchmarks and the debug log.
    public struct Stats: Sendable, Hashable {
        /// Requests that went to the network.
        public var requests = 0
        /// Calls that joined a request already in flight.
        public var coalesced = 0
        /// Documents served from memory.
        public var cacheHits = 0
        /// 304 answers served from a stored body.
        public var notModified = 0
        /// Documents loaded ahead of time.
        public var prefetched = 0
    }

    struct CachedDocument: Sendable {
        var document: BlockDocument
        var etag: String?
        var storedAt: Date
    }

    struct Validated: Sendable {
        var etag: String
        var body: Data
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
        var validated: [String: Validated] = [:]
        var validatedOrder: [String] = []
        var validatedBytes = 0
        /// Bumped by every mutation: a GET started before a change never answers one
        /// asked for after it.
        var epoch = 0
        var batchReadsUnsupported = false
        var batchMovesUnsupported = false
        var stats = Stats()
    }

    public let options: Options
    private let state = Mutex(State())

    public init(options: Options = Options()) {
        self.options = options
    }

    public var stats: Stats { state.withLock { $0.stats } }

    public func resetStats() { state.withLock { $0.stats = Stats() } }

    func count(_ update: (inout Stats) -> Void) { state.withLock { update(&$0.stats) } }

    // MARK: Coalescing

    /// The current mutation epoch, part of every coalescing key.
    var epoch: Int { state.withLock { $0.epoch } }

    func mutationStarted() { state.withLock { $0.epoch += 1 } }

    /// Runs `operation`, or joins the one already running under `key`. A waiter that is
    /// cancelled stops waiting; the request itself is cancelled once nobody waits for it.
    func coalesced<T: Sendable>(
        _ key: String, _ operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        guard options.coalescesRequests else { return try await operation() }
        let (id, task) = state.withLock { state -> (UInt64, Task<any Sendable, any Error>) in
            if var entry = state.inFlight[key] {
                entry.waiters += 1
                state.inFlight[key] = entry
                state.stats.coalesced += 1
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

    /// Puts a task others can join (a prefetch) under `key` without waiting for it.
    func register<T: Sendable>(_ key: String, _ task: Task<T, any Error>) {
        guard options.coalescesRequests else { return }
        state.withLock { state in
            guard state.inFlight[key] == nil else { return }
            state.nextId += 1
            let id = state.nextId
            let erased = Task<any Sendable, any Error>.detached { try await task.value }
            state.inFlight[key] = InFlight(id: id, task: erased, waiters: 0)
        }
        Task.detached { [weak self] in
            _ = try? await task.value
            self?.state.withLock { state in
                if let entry = state.inFlight[key], entry.waiters == 0 { state.inFlight[key] = nil }
            }
        }
    }

    func isInFlight(_ key: String) -> Bool { state.withLock { $0.inFlight[key] != nil } }

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
    func freshDocument(_ id: String, now: Date = Date(), counting: Bool = true) -> BlockDocument? {
        guard options.cachesDocuments else { return nil }
        let maxAge = options.documentMaxAge
        return state.withLock { state in
            guard let entry = state.documents[id], now.timeIntervalSince(entry.storedAt) < maxAge
            else { return nil }
            if counting {
                Self.touch(id, in: &state)
                state.stats.cacheHits += 1
            }
            return entry.document
        }
    }

    /// The cached document however old (to revalidate it).
    func cachedDocument(_ id: String) -> CachedDocument? {
        guard options.cachesDocuments else { return nil }
        return state.withLock { $0.documents[id] }
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
        guard options.cachesDocuments else { return }
        state.withLock { state in
            if let token,
               token != DocumentToken(document: state.documentGenerations[document.id] ?? 0, all: state.allGeneration) {
                return
            }
            if let existing = state.documents[document.id], existing.document.revision > document.revision { return }
            state.documents[document.id] = CachedDocument(document: document, etag: etag, storedAt: Date())
            Self.touch(document.id, in: &state)
            while state.documentOrder.count > max(1, options.documentCapacity) {
                let evicted = state.documentOrder.removeFirst()
                state.documents[evicted] = nil
            }
        }
    }

    /// Marks a cached document as just checked (a 304, or `unchanged` from a batch).
    func refresh(_ id: String) {
        state.withLock { state in
            state.documents[id]?.storedAt = Date()
            Self.touch(id, in: &state)
        }
    }

    public func invalidateDocument(_ id: String) {
        state.withLock { state in
            state.documents[id] = nil
            state.documentOrder.removeAll { $0 == id }
            state.documentGenerations[id, default: 0] += 1
        }
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
        var updated: [String: String] = [:]
        func walk(_ nodes: [NavigationNode]) {
            for node in nodes {
                switch node {
                case .document(let document): updated[document.id] = document.updatedAt
                case .folder(let folder): walk(folder.children)
                }
            }
        }
        walk(navigation.items)
        state.withLock { state in
            for (id, entry) in state.documents where updated[id] != entry.document.updatedAt {
                state.documents[id] = nil
                state.documentOrder.removeAll { $0 == id }
                state.documentGenerations[id, default: 0] += 1
            }
        }
    }

    public var cachedDocumentIds: Set<String> { state.withLock { Set($0.documents.keys) } }

    private static func touch(_ id: String, in state: inout State) {
        if let index = state.documentOrder.lastIndex(of: id) { state.documentOrder.remove(at: index) }
        state.documentOrder.append(id)
    }

    // MARK: HTTP validators

    func validated(_ key: String) -> Validated? {
        guard options.validatorCapacity > 0 else { return nil }
        return state.withLock { $0.validated[key] }
    }

    func keepValidated(_ key: String, etag: String, body: Data) {
        let capacity = options.validatorCapacity
        guard capacity > 0, body.count <= capacity / 4 else { return }
        state.withLock { state in
            if let old = state.validated[key] {
                state.validatedBytes -= old.body.count
                state.validatedOrder.removeAll { $0 == key }
            }
            state.validated[key] = Validated(etag: etag, body: body)
            state.validatedOrder.append(key)
            state.validatedBytes += body.count
            while state.validatedBytes > capacity, !state.validatedOrder.isEmpty {
                let evicted = state.validatedOrder.removeFirst()
                state.validatedBytes -= state.validated.removeValue(forKey: evicted)?.body.count ?? 0
            }
        }
    }

    // MARK: Batch support

    var batchReadsUnsupported: Bool {
        get { state.withLock { $0.batchReadsUnsupported } }
    }

    func markBatchReadsUnsupported() { state.withLock { $0.batchReadsUnsupported = true } }

    var batchMovesUnsupported: Bool {
        get { state.withLock { $0.batchMovesUnsupported } }
    }

    func markBatchMovesUnsupported() { state.withLock { $0.batchMovesUnsupported = true } }
}
