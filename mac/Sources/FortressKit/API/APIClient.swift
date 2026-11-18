import Foundation

/// An error from the backend, carrying its `detail` message when it sends one.
public struct APIError: LocalizedError, Sendable {
    public var status: Int
    public var message: String
    public var errorDescription: String? { message }

    public init(status: Int, message: String) {
        self.status = status
        self.message = message
    }

    /// From an error response: its `detail` when it has one, else a calm sentence.
    public init(status: Int, body: Data?) {
        self.status = status
        if let body, let detail = try? JSONDecoder().decode([String: JSONValue].self, from: body)["detail"]?.stringValue {
            message = detail
        } else if (500..<600).contains(status) {
            message = "Fortress's server had a problem (\(status)). Try again in a moment."
        } else {
            let text = HTTPURLResponse.localizedString(forStatusCode: status)
            message = text.prefix(1).uppercased() + text.dropFirst()
        }
    }

    /// A reply that isn't the JSON expected (status 0: no HTTP error).
    public static let malformed = APIError(status: 0, message: "Fortress's server sent a reply the app couldn't read.")
}

/// Talks to the Fortress Notes backend (FastAPI, by default on localhost:8000).
///
/// Copies share one `APIStore`: concurrent identical GETs make one request, documents are
/// cached (and kept current from every mutation's answer), and ETags are honoured.
public struct APIClient: Sendable {
    public var baseURL: URL
    /// Internal so feature-area extensions (e.g. Finding) send through the same session.
    let session: URLSession
    /// In-flight requests, caches and counters, shared by every copy of this client.
    public let store: APIStore

    public init(
        baseURL: URL = URL(string: "http://127.0.0.1:8000")!, session: URLSession = APIClient.sharedSession,
        store: APIStore = APIStore()
    ) {
        self.baseURL = baseURL
        self.session = session
        self.store = store
    }

    // MARK: Navigation and folders

    public func navigation(recentLimit: Int = 10) async throws -> NavigationResponse {
        try await get("api/navigation", query: ["recent_limit": "\(recentLimit)"])
    }

    public func createFolder(name: String, parentId: String? = nil) async throws -> Folder {
        try await send("POST", "api/folders", body: ["name": .string(name), "parent_id": parentId.map(JSONValue.string) ?? .null])
    }

    public func renameFolder(id: String, name: String) async throws -> Folder {
        try await send("PATCH", "api/folders/\(id)", body: ["name": .string(name)])
    }

    public func deleteFolder(id: String) async throws {
        try await sendNoContent("DELETE", "api/folders/\(id)")
    }

    // MARK: Documents

    /// The note. Freshness is the server's ETag: a cached copy is revalidated with
    /// `If-None-Match` (a 304 answers from memory), except within a moment of being
    /// loaded or saved (`documentMaxAge`, so the editor and inspector opening together
    /// make one request). Concurrent reads of one note share one request.
    public func document(id: String) async throws -> BlockDocument {
        if let cached = store.freshDocument(id) { return cached }
        do {
            return try await store.coalesced("GET document \(id) @\(store.epoch)") { [self] in
                // A read that finished between the check above and here filled the cache.
                if let cached = store.freshDocument(id) { return cached }
                return try await fetchDocument(id: id)
            }
        } catch is PrefetchMiss {
            return try await fetchDocument(id: id)
        }
    }

    /// The note from the server, never from memory (e.g. after a conflict).
    public func reloadDocument(id: String) async throws -> BlockDocument {
        store.invalidateDocument(id)
        return try await fetchDocument(id: id)
    }

    func fetchDocument(id: String) async throws -> BlockDocument {
        let token = store.documentToken(id)
        var request = try makeRequest("GET", "api/block-documents/\(id)", kind: .document)
        let cached = store.cachedDocument(id)
        if let etag = cached?.etag { request.setValue(etag, forHTTPHeaderField: "If-None-Match") }
        let (data, response) = try await load(request)
        if response.statusCode == 304, let cached {
            store.refresh(id)
            store.count { $0.notModified += 1 }
            return cached.document
        }
        do {
            try check(response, body: data)
        } catch let error as APIError where error.status == 404 {
            store.invalidateDocument(id)
            throw error
        }
        let document = try Self.decode(BlockDocument.self, from: data)
        store.store(document, etag: response.value(forHTTPHeaderField: "ETag"), token: token)
        return document
    }

    public func createDocument(title: String = "", folderId: String? = nil) async throws -> BlockDocument {
        let request = try makeRequest(
            "POST", "api/block-documents",
            body: try JSONEncoder().encode(["title": .string(title), "folder_id": folderId.map(JSONValue.string) ?? .null] as [String: JSONValue]))
        let (document, etag) = try await sendDocument(request)
        store.store(document, etag: etag)
        return document
    }

    /// Renames a note or changes its status. The server answers with the note's
    /// metadata only (its content didn't change): it is merged into the cached copy,
    /// and the merged note is returned (the metadata alone when nothing is cached).
    public func updateDocument(id: String, title: String? = nil, status: NoteStatus? = nil) async throws -> BlockDocument {
        var body: [String: JSONValue] = [:]
        if let title { body["title"] = .string(title) }
        if let status { body["status"] = .string(status.rawValue) }
        let request = try makeRequest("PATCH", "api/block-documents/\(id)", body: try JSONEncoder().encode(body))
        do {
            let (metadata, _) = try await sendDocument(request)
            return store.mergeMetadata(metadata)
        } catch let error as APIError where error.status != 422 {
            store.invalidateDocument(id)
            throw error
        }
    }

    /// Records that the person opened the note ("last opened" in the inspector).
    /// Fire-and-forget: reads never write on the server, so opening is its own request.
    public func markOpened(id: String) async {
        guard let request = try? makeRequest("POST", "api/block-documents/\(id)/opened") else { return }
        _ = try? await load(request)
    }

    public func trashDocument(id: String) async throws {
        defer { store.invalidateDocument(id) }
        try await sendNoContent("DELETE", "api/block-documents/\(id)")
    }

    public func moveDocument(id: String, folderId: String?) async throws {
        let _: JSONValue = try await send(
            "POST", "api/block-documents/\(id)/move",
            body: ["folder_id": folderId.map(JSONValue.string) ?? .null])
    }

    /// Applies block edits; the server answers with the whole updated document
    /// (`?response=document`; without it the server answers metadata only).
    public func apply(_ transaction: BlockTransaction, to documentId: String) async throws -> BlockDocument {
        try await documentMutation(documentId, try makeRequest(
            "POST", "api/block-documents/\(documentId)/transactions", query: ["response": "document"],
            body: try JSONEncoder().encode(transaction)))
    }

    public func undo(documentId: String, revision: Int) async throws -> BlockDocument {
        try await documentMutation(documentId, try makeRequest(
            "POST", "api/block-documents/\(documentId)/undo",
            body: try JSONEncoder().encode(["base_revision": JSONValue.number(Double(revision))])))
    }

    public func redo(documentId: String, revision: Int) async throws -> BlockDocument {
        try await documentMutation(documentId, try makeRequest(
            "POST", "api/block-documents/\(documentId)/redo",
            body: try JSONEncoder().encode(["base_revision": JSONValue.number(Double(revision))])))
    }

    /// Runs a change to one note: its answer becomes the cached copy, kept under the
    /// `X-Document-ETag` it came with (so the next read is a 304); a rejection (409
    /// conflict, 404 gone) drops the cached copy so the next read asks the server.
    private func documentMutation(_ id: String, _ request: URLRequest) async throws -> BlockDocument {
        do {
            let (document, etag) = try await sendDocument(request)
            store.store(document, etag: etag)
            return document
        } catch let error as APIError where error.status != 422 {
            store.invalidateDocument(id)
            throw error
        }
    }

    /// A change answered with a note (or its metadata) and the ETag that note has now.
    private func sendDocument(_ request: URLRequest) async throws -> (BlockDocument, String?) {
        try await asMutation {
            let (data, response) = try await load(request)
            try check(response, body: data)
            return (try Self.decode(BlockDocument.self, from: data), response.value(forHTTPHeaderField: "X-Document-ETag"))
        }
    }

    public func backlinks(documentId: String) async throws -> [Backlink] {
        try await get("api/block-documents/\(documentId)/backlinks")
    }

    public func similar(documentId: String) async throws -> [SimilarDocument] {
        try await get("api/block-documents/\(documentId)/similar", kind: .search)
    }

    public func related(documentId: String, blockId: String? = nil, count: Int = 5) async throws -> [RelatedResult] {
        var query = ["k": "\(count)"]
        if let blockId { query["block_id"] = blockId }
        return try await get("api/block-documents/\(documentId)/related", query: query, kind: .search)
    }

    public func linkTargets(query: String, limit: Int = 20) async throws -> [BlockLinkTarget] {
        try await get("api/block-link-targets", query: ["q": query, "limit": "\(limit)"], kind: .search)
    }

    public func markdownURL(documentId: String) -> URL {
        baseURL.appending(path: "api/block-documents/\(documentId)/markdown")
    }

    // MARK: Finding

    public func search(_ query: String, limit: Int = 30) async throws -> [SearchMatch] {
        try await get("api/unified-search", query: ["q": query, "limit": "\(limit)"], kind: .search)
    }

    /// Streams an answer: checked sentences as they complete, extra searches, then `done`.
    public func ask(_ question: String) -> AsyncThrowingStream<AskEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try makeRequest(
                        "POST", "api/ask", body: try JSONEncoder().encode(["question": question]), kind: .stream)
                    request.setValue("application/x-ndjson", forHTTPHeaderField: "Accept")
                    store.count { $0.requests += 1 }
                    let (bytes, response) = try await session.bytes(for: request)
                    if let http = response as? HTTPURLResponse { try check(http, body: nil) }
                    for try await line in bytes.lines where !line.isEmpty {
                        // Finding: a garbled line is skipped, not the end of the answer.
                        if let event = try? Self.askEvent(from: Data(line.utf8)) {
                            continuation.yield(event)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    static func askEvent(from line: Data) throws -> AskEvent? {
        struct Envelope: Decodable {
            var type: String
            var sentence: AskSentence?
            var step: AskStep?
            var result: AskResponse?
        }
        let envelope = try JSONDecoder().decode(Envelope.self, from: line)
        switch envelope.type {
        case "sentence": return envelope.sentence.map(AskEvent.sentence)
        case "step": return envelope.step.map(AskEvent.step)
        case "done": return envelope.result.map(AskEvent.done)
        default: return nil
        }
    }

    // MARK: Capture, trash, backups

    public func capture(_ text: String) async throws -> CaptureResult {
        try await send("POST", "api/capture", body: ["text": .string(text)])
    }

    public func trash() async throws -> [TrashedDocument] {
        try await get("api/trash")
    }

    public func restore(documentId: String) async throws {
        store.invalidateDocument(documentId)
        let _: JSONValue = try await send("POST", "api/trash/\(documentId)/restore", body: [:])
    }

    public func deleteForever(documentId: String) async throws {
        store.invalidateDocument(documentId)
        try await sendNoContent("DELETE", "api/trash/\(documentId)")
    }

    public func emptyTrash() async throws {
        try await sendNoContent("DELETE", "api/trash")
    }

    public func backups() async throws -> [Backup] {
        try await get("api/backups")
    }

    public func createBackup() async throws -> Backup {
        try await send("POST", "api/backups", body: [:])
    }

    public func restoreBackup(name: String) async throws {
        defer { store.invalidateAllDocuments() }
        let _: JSONValue = try await send("POST", "api/backups/\(name)/restore", body: [:])
    }

    public func health() async throws -> JSONValue {
        try await get("api/health", kind: .probe)
    }

    // MARK: Plumbing

    func makeRequest(
        _ method: String, _ path: String, query: [String: String] = [:], body: Data? = nil, kind: RequestKind? = nil
    ) throws -> URLRequest {
        var components = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        let kind = kind ?? (method == "GET" ? .metadata : .mutation)
        request.timeoutInterval = kind.timeout
        // The client keeps its own caches; nothing (least of all a mutation) is answered
        // from URLCache.
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    /// Coalesced, ETag-validated, counted GET. Internal so feature-area extensions
    /// (Finding's `documentInfo`) go through the store too.
    func get<T: Decodable & Sendable>(
        _ path: String, query: [String: String] = [:], kind: RequestKind = .metadata
    ) async throws -> T {
        let request = try makeRequest("GET", path, query: query, kind: kind)
        let key = "GET \(request.url!.absoluteString) \(T.self) @\(store.epoch)"
        return try await store.coalesced(key) { [self] in try await performGET(request) }
    }

    /// A GET that sends the stored ETag and answers a 304 from the stored body.
    private func performGET<T: Decodable>(_ request: URLRequest) async throws -> T {
        var request = request
        let key = request.url!.absoluteString
        let stored = store.validated(key)
        if let stored { request.setValue(stored.etag, forHTTPHeaderField: "If-None-Match") }
        let (data, response) = try await load(request)
        if response.statusCode == 304, let stored {
            store.count { $0.notModified += 1 }
            return try Self.decode(T.self, from: stored.body)
        }
        try check(response, body: data)
        if let etag = response.value(forHTTPHeaderField: "ETag") {
            store.keepValidated(key, etag: etag, body: data)
        }
        return try Self.decode(T.self, from: data)
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: [String: JSONValue]) async throws -> T {
        try await perform(try makeRequest(method, path, body: try JSONEncoder().encode(body)))
    }

    private func sendNoContent(_ method: String, _ path: String) async throws {
        try await asMutation {
            let (data, response) = try await load(try makeRequest(method, path))
            try check(response, body: data)
        }
    }

    private func perform<T: Decodable>(_ request: URLRequest) async throws -> T {
        try await asMutation(request.httpMethod != "GET") {
            let (data, response) = try await load(request)
            try check(response, body: data)
            return try Self.decode(T.self, from: data)
        }
    }

    /// Runs a change so no GET started before it is shared with a reader after it.
    func asMutation<T>(_ isMutation: Bool = true, _ operation: () async throws -> T) async throws -> T {
        if isMutation { store.mutationStarted() }
        defer { if isMutation { store.mutationStarted() } }
        return try await operation()
    }

    /// Sends a request through the session, counting it.
    func load(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        store.count { $0.requests += 1 }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http)
    }

    /// Decodes a body, reporting a malformed one as a calm, readable error.
    static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.malformed
        }
    }

    func check(_ response: HTTPURLResponse, body: Data?) throws {
        guard !(200..<300).contains(response.statusCode) else { return }
        throw APIError(status: response.statusCode, body: body)
    }
}

/// A prefetch that didn't bring the note a reader joined it for.
struct PrefetchMiss: Error {}
