import Foundation

/// What a request is for, which sets how long it may stay silent before it fails.
/// `URLRequest.timeoutInterval` is the idle time between packets, not the total.
public enum RequestKind: Sendable, Hashable {
    /// "Is the server there?": fail fast so reconnecting stays snappy.
    case probe
    /// Lists, trees and small lookups.
    case metadata
    /// A whole note.
    case document
    /// Search, related notes and other work that may wait on the embedding model.
    case search
    /// A change (save, move, create).
    case mutation
    /// A streamed answer (Ask): the model may think for a while between lines.
    case stream
    /// Uploads (images, files, OneNote exports).
    case upload

    public var timeout: TimeInterval {
        switch self {
        case .probe: 3
        case .metadata: 15
        case .document: 30
        case .search: 60
        case .mutation: 30
        case .stream: 300
        case .upload: 600
        }
    }
}

extension APIClient {
    /// The one session every client uses by default: keep-alive connections to the
    /// backend are reused across requests (a session per request would reconnect each
    /// time). Nothing is stored in URLCache: the client keeps its own caches and
    /// mutation responses must never be cached.
    public static let sharedSession: URLSession = URLSession(configuration: sessionConfiguration())

    /// The configuration of `sharedSession` (tests add protocol classes to it).
    public static func sessionConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.default
        // The backend is on this Mac (or the LAN): when it's not there, say so at once
        // rather than waiting for "connectivity" that won't come.
        #if os(macOS)
        configuration.waitsForConnectivity = false
        #endif
        configuration.timeoutIntervalForRequest = RequestKind.document.timeout
        // A whole upload or Ask answer may take long; per-request idle timeouts guard the rest.
        configuration.timeoutIntervalForResource = 60 * 60
        configuration.httpMaximumConnectionsPerHost = 6
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        // The backend wants this launch's token on every request (BackendAuth).
        configuration.httpAdditionalHeaders = BackendAuth.headers
        return configuration
    }

    // MARK: Batches

    /// What `POST /api/batch/block-documents` answers: each note with the ETag it has
    /// (the one `GET /api/block-documents/{id}` would send), notes whose ETag the client
    /// already holds, and notes that are gone.
    struct DocumentBatch: Decodable, Sendable {
        var documents: [BlockDocument]
        var etags: [String: String]?
        var unchanged: [String]
        var missing: [String]
    }

    /// Loads notes ahead of time (the next and previous in the list, the one under the
    /// pointer) in one request, so opening them is instant. Notes cached within
    /// `documentMaxAge` are skipped; other cached notes are sent with their ETags and come
    /// back as `unchanged` when they still are. A reader that asks for a note while its
    /// prefetch is in flight joins it instead of sending its own request.
    public func prefetchDocuments(ids: [String]) async {
        guard store.options.cachesDocuments else { return }
        let epoch = store.epoch
        var seen = Set<String>()
        let wanted = ids.filter { id in
            seen.insert(id).inserted && store.freshDocument(id, counting: false) == nil
                && !store.isInFlight("GET document \(id) @\(epoch)")
        }
        guard !wanted.isEmpty, !Task.isCancelled else { return }
        var known: [String: JSONValue] = [:]
        for id in wanted {
            if let etag = store.cachedDocument(id)?.etag { known[id] = .string(etag) }
        }
        let tokens = Dictionary(uniqueKeysWithValues: wanted.map { ($0, store.documentToken($0)) })
        let body: [String: JSONValue] = ["ids": .array(wanted.map(JSONValue.string)), "known": .object(known)]
        let client = self
        let batch = Task<[String: BlockDocument], any Error>.detached {
            var request = try client.makeRequest(
                "POST", "api/batch/block-documents", body: try JSONEncoder().encode(body), kind: .metadata)
            request.setValue("1", forHTTPHeaderField: "X-Fortress-Prefetch")
            let (data, response) = try await client.load(request)
            try client.check(response, body: data)
            let result = try APIClient.decode(DocumentBatch.self, from: data)
            var found: [String: BlockDocument] = [:]
            for document in result.documents {
                client.store.store(document, etag: result.etags?[document.id], token: tokens[document.id])
                found[document.id] = document
            }
            for id in result.unchanged {
                client.store.refresh(id)
                if let cached = client.store.cachedDocument(id) { found[id] = cached.document }
            }
            client.store.count { $0.prefetched += found.count }
            return found
        }
        for id in wanted {
            let one = Task<BlockDocument, any Error>.detached {
                // Any failure sends the reader who joined this to the network itself.
                guard let document = try? await batch.value[id] else { throw PrefetchMiss() }
                return document
            }
            store.register("GET document \(id) @\(epoch)", one)
        }
        _ = try? await batch.value
    }

    /// Moves notes and folders in one request (`POST /api/batch/move`). Folders move first.
    public func move(documents: [(id: String, folderId: String?)], folders: [(id: String, parentId: String?)]) async throws {
        guard !documents.isEmpty || !folders.isEmpty else { return }
        let body: [String: JSONValue] = [
            "documents": .array(documents.map { .object(["id": .string($0.id), "folder_id": $0.folderId.map(JSONValue.string) ?? .null]) }),
            "folders": .array(folders.map { .object(["id": .string($0.id), "parent_id": $0.parentId.map(JSONValue.string) ?? .null]) }),
        ]
        let request = try makeRequest("POST", "api/batch/move", body: try JSONEncoder().encode(body))
        try await asMutation {
            let (data, response) = try await load(request)
            try check(response, body: data)
        }
    }
}
