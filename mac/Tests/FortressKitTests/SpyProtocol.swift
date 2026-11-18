import Foundation
import Synchronization

@testable import FortressKit

/// A fake backend for data-layer tests: answers through a handler, records every request
/// (method, path, headers, body), and can delay or fail an answer. Requests cancelled
/// while delayed are recorded as cancelled.
final class SpyProtocol: URLProtocol, @unchecked Sendable {
    struct Reply: Sendable {
        var status = 200
        var body = Data("{}".utf8)
        var headers: [String: String] = [:]
        /// Seconds before answering.
        var delay: Double = 0
        /// Fail with this instead of answering.
        var failure: URLError.Code?

        static func json(_ body: Data, status: Int = 200, headers: [String: String] = [:], delay: Double = 0) -> Reply {
            Reply(status: status, body: body, headers: headers, delay: delay)
        }

        static func document(_ document: BlockDocument, etag: String? = nil, delay: Double = 0) -> Reply {
            .json(SpyJSON.encoded(document), headers: etag.map { ["ETag": $0] } ?? [:], delay: delay)
        }
    }

    struct Seen: Sendable {
        var method: String
        var path: String
        var query: String?
        var headers: [String: String]
        var body: Data
        var cancelled = false
    }

    private struct State {
        var handler: @Sendable (Seen) -> Reply = { _ in Reply() }
        var seen: [Seen] = []
    }

    private static let state = Mutex(State())

    static func reset(_ handler: @escaping @Sendable (Seen) -> Reply) {
        state.withLock { $0 = State(handler: handler, seen: []) }
    }

    static var seen: [Seen] { state.withLock { $0.seen } }

    static func count(_ method: String, _ pathSuffix: String) -> Int {
        seen.filter { $0.method == method && $0.path.hasSuffix(pathSuffix) && !$0.cancelled }.count
    }

    /// An API client whose session answers through this protocol.
    static func client(options: APIStore.Options = APIStore.Options()) -> APIClient {
        let configuration = APIClient.sessionConfiguration()
        configuration.protocolClasses = [SpyProtocol.self]
        return APIClient(
            baseURL: URL(string: "http://spy.local")!, session: URLSession(configuration: configuration),
            store: APIStore(options: options))
    }

    /// (answered, stopped by the client)
    private let flags = Mutex((delivered: false, stopped: false))
    private let index = Mutex(0)

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let url = request.url!
        let seen = Seen(
            method: request.httpMethod ?? "GET", path: url.path(), query: url.query(),
            headers: request.allHTTPHeaderFields ?? [:], body: Self.body(of: request))
        let (reply, index) = Self.state.withLock { state -> (Reply, Int) in
            state.seen.append(seen)
            return (state.handler(seen), state.seen.count - 1)
        }
        self.index.withLock { $0 = index }
        if reply.delay > 0 {
            nonisolated(unsafe) let loader = self
            DispatchQueue.global().asyncAfter(deadline: .now() + reply.delay) {
                loader.deliver(reply)
            }
        } else {
            deliver(reply)
        }
    }

    /// Records the request as cancelled when the client stopped it before the answer.
    override func stopLoading() {
        let unanswered = flags.withLock { flags -> Bool in
            flags.stopped = true
            return !flags.delivered
        }
        guard unanswered else { return }
        let index = index.withLock { $0 }
        Self.state.withLock { state in
            if state.seen.indices.contains(index) { state.seen[index].cancelled = true }
        }
    }

    private func deliver(_ reply: Reply) {
        let stopped = flags.withLock { flags -> Bool in
            if !flags.stopped { flags.delivered = true }
            return flags.stopped
        }
        guard !stopped else { return }
        if let failure = reply.failure {
            client?.urlProtocol(self, didFailWithError: URLError(failure))
            return
        }
        var headers = reply.headers
        headers["Content-Type"] = headers["Content-Type"] ?? "application/json"
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: reply.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    private static func body(of request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

extension BlockNode {
    /// A paragraph as the server sends it.
    static func paragraph(_ id: String, _ text: String, documentId: String = "d", createdAt: String = "2026-11-18T00:00:00+00:00",
                          updatedAt: String = "2026-11-18T00:00:00+00:00") -> BlockNode {
        BlockNode(id: id, documentId: documentId, type: "paragraph",
                  content: .array(text.isEmpty ? [] : [.object(["type": .string("text"), "text": .string(text)])]),
                  text: text, createdAt: createdAt, updatedAt: updatedAt)
    }
}

extension BlockOperation {
    /// The plain text of the inline content an insert or update sends.
    var plainText: String? { content.map { BlockCodec.plainText(BlockCodec.runs(fromContent: $0)) } }
}

/// JSON the fake backend answers with.
enum SpyJSON {
    static func document(_ id: String, revision: Int = 1, title: String = "Note", updatedAt: String = "2026-11-18T10:00:00.000000+00:00") -> BlockDocument {
        let block = BlockNode.paragraph("\(id)-b1", title, documentId: id, createdAt: "2026-11-01T00:00:00+00:00", updatedAt: updatedAt)
        return BlockDocument(
            id: id, revision: revision, titleAuto: false, canUndo: false, canRedo: false, title: title, status: .rough,
            createdAt: "2026-11-01T00:00:00+00:00", updatedAt: updatedAt, children: [block])
    }

    static func encoded<T: Encodable>(_ value: T) -> Data { try! JSONEncoder().encode(value) }

    static func note(_ id: String, title: String? = nil, updatedAt: String = "2026-11-18T09:00:00.000000+00:00", folder: String? = nil) -> NavigationDocument {
        NavigationDocument(
            id: id, title: title ?? "Note \(id)", status: .rough, updatedAt: updatedAt,
            createdAt: "2026-11-01T00:00:00+00:00", snippet: "", folderId: folder, position: 0)
    }

    /// `/api/navigation` for these top-level notes and folders (`kind` keys included).
    static func navigation(_ items: [NavigationNode], recent: [NavigationDocument]? = nil) -> Data {
        func object(_ node: NavigationNode) -> Any {
            switch node {
            case .document(let note):
                var dictionary = try! JSONSerialization.jsonObject(with: encoded(note)) as! [String: Any]
                dictionary["kind"] = "document"
                return dictionary
            case .folder(let folder):
                return [
                    "kind": "folder", "id": folder.id, "parent_id": folder.parentId.map { $0 as Any } ?? NSNull(), "name": folder.name,
                    "position": folder.position, "children": folder.children.map(object),
                ]
            }
        }
        let notes = NoteListing.allDocuments(in: items)
        let recentNotes = recent ?? notes.sorted { $0.updatedAt > $1.updatedAt }
        let root: [String: Any] = [
            "items": items.map(object),
            "recent": recentNotes.map { try! JSONSerialization.jsonObject(with: encoded($0)) },
        ]
        return try! JSONSerialization.data(withJSONObject: root)
    }
}
