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
    /// time). Nothing is stored in URLCache: the client keeps its own note cache and
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

    // MARK: Prefetch and batch moves

    /// Loads notes ahead of time (the next and previous in the list, the one under the
    /// pointer) with plain GETs, so opening one within `documentMaxAge` is instant; a
    /// reader that asks while its prefetch is in flight joins it.
    public func prefetchDocuments(ids: [String]) async {
        await withTaskGroup(of: Void.self) { group in
            for id in Set(ids) { group.addTask { _ = try? await document(id: id) } }
        }
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
