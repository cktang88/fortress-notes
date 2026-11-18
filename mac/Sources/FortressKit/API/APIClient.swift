import Foundation

/// An error from the backend, carrying its `detail` message when it sends one.
public struct APIError: LocalizedError, Sendable {
    public var status: Int
    public var message: String
    public var errorDescription: String? { message }
}

/// Talks to the Fortress Notes backend (FastAPI, by default on localhost:8000).
public struct APIClient: Sendable {
    public var baseURL: URL
    /// Internal so feature-area extensions (e.g. Finding) send through the same session.
    let session: URLSession

    public init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
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

    public func document(id: String) async throws -> BlockDocument {
        try await get("api/block-documents/\(id)")
    }

    public func createDocument(title: String = "", folderId: String? = nil) async throws -> BlockDocument {
        try await send(
            "POST", "api/block-documents",
            body: ["title": .string(title), "folder_id": folderId.map(JSONValue.string) ?? .null])
    }

    public func updateDocument(id: String, title: String? = nil, status: NoteStatus? = nil) async throws -> BlockDocument {
        var body: [String: JSONValue] = [:]
        if let title { body["title"] = .string(title) }
        if let status { body["status"] = .string(status.rawValue) }
        return try await send("PATCH", "api/block-documents/\(id)", body: body)
    }

    public func trashDocument(id: String) async throws {
        try await sendNoContent("DELETE", "api/block-documents/\(id)")
    }

    public func moveDocument(id: String, folderId: String?) async throws {
        let _: JSONValue = try await send(
            "POST", "api/block-documents/\(id)/move",
            body: ["folder_id": folderId.map(JSONValue.string) ?? .null])
    }

    /// Applies block edits; the server answers with the updated document.
    public func apply(_ transaction: BlockTransaction, to documentId: String) async throws -> BlockDocument {
        try await send("POST", "api/block-documents/\(documentId)/transactions", encodable: transaction)
    }

    public func undo(documentId: String, revision: Int) async throws -> BlockDocument {
        try await send("POST", "api/block-documents/\(documentId)/undo", body: ["base_revision": .number(Double(revision))])
    }

    public func redo(documentId: String, revision: Int) async throws -> BlockDocument {
        try await send("POST", "api/block-documents/\(documentId)/redo", body: ["base_revision": .number(Double(revision))])
    }

    public func backlinks(documentId: String) async throws -> [Backlink] {
        try await get("api/block-documents/\(documentId)/backlinks")
    }

    public func similar(documentId: String) async throws -> [SimilarDocument] {
        try await get("api/block-documents/\(documentId)/similar")
    }

    public func related(documentId: String, blockId: String? = nil, count: Int = 5) async throws -> [RelatedResult] {
        var query = ["k": "\(count)"]
        if let blockId { query["block_id"] = blockId }
        return try await get("api/notes/\(documentId)/related", query: query)
    }

    public func linkTargets(query: String, limit: Int = 20) async throws -> [BlockLinkTarget] {
        try await get("api/block-link-targets", query: ["q": query, "limit": "\(limit)"])
    }

    public func markdownURL(documentId: String) -> URL {
        baseURL.appending(path: "api/block-documents/\(documentId)/markdown")
    }

    // MARK: Finding

    public func search(_ query: String, limit: Int = 30) async throws -> [SearchMatch] {
        try await get("api/unified-search", query: ["q": query, "limit": "\(limit)"])
    }

    /// Streams an answer: checked sentences as they complete, extra searches, then `done`.
    public func ask(_ question: String) -> AsyncThrowingStream<AskEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try makeRequest("POST", "api/ask", body: try JSONEncoder().encode(["question": question]))
                    request.setValue("application/x-ndjson", forHTTPHeaderField: "Accept")
                    let (bytes, response) = try await session.bytes(for: request)
                    try await check(response, body: nil)
                    for try await line in bytes.lines where !line.isEmpty {
                        if let event = try Self.askEvent(from: Data(line.utf8)) {
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
        let _: JSONValue = try await send("POST", "api/trash/\(documentId)/restore", body: [:])
    }

    public func deleteForever(documentId: String) async throws {
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
        let _: JSONValue = try await send("POST", "api/backups/\(name)/restore", body: [:])
    }

    public func health() async throws -> JSONValue {
        try await get("api/health")
    }

    // MARK: Plumbing

    private func makeRequest(_ method: String, _ path: String, query: [String: String] = [:], body: Data? = nil) throws -> URLRequest {
        var components = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.timeoutInterval = 30
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try await perform(try makeRequest("GET", path, query: query))
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: [String: JSONValue]) async throws -> T {
        try await perform(try makeRequest(method, path, body: try JSONEncoder().encode(body)))
    }

    private func send<T: Decodable, B: Encodable>(_ method: String, _ path: String, encodable: B) async throws -> T {
        try await perform(try makeRequest(method, path, body: try JSONEncoder().encode(encodable)))
    }

    private func sendNoContent(_ method: String, _ path: String) async throws {
        let (data, response) = try await session.data(for: try makeRequest(method, path))
        try await check(response, body: data)
    }

    private func perform<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await session.data(for: request)
        try await check(response, body: data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func check(_ response: URLResponse, body: Data?) async throws {
        guard let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) else { return }
        var message = HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
        if let body, let detail = try? JSONDecoder().decode([String: JSONValue].self, from: body)["detail"]?.stringValue {
            message = detail
        }
        throw APIError(status: http.statusCode, message: message)
    }
}
