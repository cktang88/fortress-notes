import Foundation
import Testing

@testable import FortressKit

/// Answers the editor's requests in tests: records transactions, replies per script.
final class StubProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var replies: [(status: Int, body: Data?)] = []
    nonisolated(unsafe) static var requests: [URLRequest] = []
    nonisolated(unsafe) static var bodies: [Data] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(request)
        if let stream = request.httpBodyStream {
            stream.open()
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
            Self.bodies.append(data)
        } else {
            Self.bodies.append(request.httpBody ?? Data())
        }
        let reply = Self.replies.isEmpty ? (status: 500, body: nil) : Self.replies.removeFirst()
        if reply.status == 0 {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: reply.body ?? Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@MainActor
@Suite(.serialized) struct EditorSessionTests {
    private func makeSession(_ document: BlockDocument) -> EditorSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubProtocol.self]
        let api = APIClient(baseURL: URL(string: "http://stub.local")!, session: URLSession(configuration: configuration))
        StubProtocol.replies = []
        StubProtocol.requests = []
        StubProtocol.bodies = []
        let session = EditorSession(document: document, api: api)
        session.saveDelay = .milliseconds(1)
        return session
    }

    private func document(revision: Int, text: String) -> BlockDocument {
        let node = BlockNode(id: "p1", documentId: "d", parentId: nil, position: 0, type: "paragraph", attrs: [:],
                             userAttrs: [:], content: .object(["markdown": .string(text)]), text: text,
                             createdAt: "2026-11-18T00:00:00+00:00", updatedAt: "2026-11-18T00:00:00+00:00", children: [])
        return BlockDocument(id: "d", revision: revision, titleAuto: true, canUndo: false, canRedo: false, title: text,
                             status: .rough, createdAt: "2026-11-18T00:00:00+00:00", updatedAt: "2026-11-18T00:00:00+00:00",
                             children: [node])
    }

    @Test func savesWithBaseRevisionAndAdoptsTheNewOne() async throws {
        let session = makeSession(document(revision: 3, text: "Hello"))
        var blocks = BlockCodec.flatten(session.document!.children)
        session.currentBlocks = { blocks }
        blocks[0].runs = [InlineRun("Hello there")]
        StubProtocol.replies = [(200, try JSONEncoder().encode(document(revision: 4, text: "Hello there")))]
        session.textDidChange()
        await session.flush()
        #expect(session.saveState == .saved)
        let sent = try JSONDecoder().decode(FortressKit.BlockTransaction.self, from: try #require(StubProtocol.bodies.first))
        #expect(sent.baseRevision == 3)
        #expect(sent.operations.count == 1)
        #expect(sent.operations[0].text == "Hello there")
        #expect(session.revision == 4)
        #expect(session.title == "Hello there")  // auto title follows the server

        // Nothing changed since: nothing is sent.
        session.textDidChange()
        await session.flush()
        #expect(StubProtocol.bodies.count == 1)
    }

    @Test func conflictStopsSavingAndShowsTheBanner() async throws {
        let session = makeSession(document(revision: 3, text: "Hello"))
        var blocks = BlockCodec.flatten(session.document!.children)
        session.currentBlocks = { blocks }
        blocks[0].runs = [InlineRun("Changed")]
        StubProtocol.replies = [(409, Data(#"{"detail":{"code":"document_revision_conflict"}}"#.utf8))]
        session.textDidChange()
        await session.flush()
        #expect(session.saveState == .conflict)
        blocks[0].runs = [InlineRun("Changed again")]
        session.textDidChange()
        await session.flush()
        #expect(StubProtocol.bodies.count == 1)
    }

    @Test func offlineKeepsTheTransactionAndRetriesItUnchanged() async throws {
        let session = makeSession(document(revision: 3, text: "Hello"))
        var blocks = BlockCodec.flatten(session.document!.children)
        session.currentBlocks = { blocks }
        blocks[0].runs = [InlineRun("Offline edit")]
        StubProtocol.replies = [(0, nil), (200, try JSONEncoder().encode(document(revision: 4, text: "Offline edit")))]
        session.textDidChange()
        await session.flush()
        #expect(session.saveState == .offline)
        #expect(session.hasUnsavedChanges)
        await session.flush()
        #expect(session.saveState == .saved)
        let first = try JSONDecoder().decode(FortressKit.BlockTransaction.self, from: StubProtocol.bodies[0])
        let second = try JSONDecoder().decode(FortressKit.BlockTransaction.self, from: StubProtocol.bodies[1])
        #expect(first == second)  // same transaction id: the server applies it once
        #expect(!session.hasUnsavedChanges)
    }
}
