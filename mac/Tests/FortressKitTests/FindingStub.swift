import Foundation
import Synchronization

@testable import FortressKit

/// A fake backend for Finding's request-count and latency tests. Each test makes its own
/// `FindingStub` (its own host name), so suites can run in parallel without sharing state.
/// Every request is recorded; a handler decides the reply, how late it comes and whether
/// it streams in pieces. Cancelled requests are counted.
final class FindingStub: Sendable {
    struct Reply: Sendable {
        var status = 200
        /// Pieces of the body, each sent after its delay (seconds since the previous piece).
        var chunks: [(delay: TimeInterval, data: Data)]
        /// Fail with this error (after the delay of the first chunk) instead of finishing.
        var failure: URLError.Code?

        init(status: Int = 200, delay: TimeInterval = 0, body: Data) {
            self.status = status
            chunks = [(delay, body)]
        }

        init(status: Int = 200, chunks: [(delay: TimeInterval, data: Data)], failure: URLError.Code? = nil) {
            self.status = status
            self.chunks = chunks
            self.failure = failure
        }

        static func json<T: Encodable>(_ value: T, delay: TimeInterval = 0) -> Reply {
            Reply(delay: delay, body: (try? JSONEncoder().encode(value)) ?? Data("[]".utf8))
        }

        static func offline(delay: TimeInterval = 0) -> Reply {
            Reply(chunks: [(delay, Data())], failure: .notConnectedToInternet)
        }
    }

    struct Log: Sendable {
        var started: [URL] = []
        var cancelled: [URL] = []
        var finished: [URL] = []
    }

    let host: String
    let api: APIClient
    private let state: Mutex<Log> = Mutex(Log())
    private let handler: @Sendable (URLRequest) -> Reply

    init(_ handler: @escaping @Sendable (URLRequest) -> Reply) {
        host = "stub-\(UUID().uuidString.lowercased()).invalid"
        self.handler = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FindingStubProtocol.self]
        api = APIClient(baseURL: URL(string: "http://\(host)")!, session: URLSession(configuration: configuration))
        FindingStubProtocol.register(self)
    }

    deinit { FindingStubProtocol.unregister(host) }

    var log: Log { state.withLock { $0 } }
    var started: [URL] { log.started }

    /// Requests whose path ends with `suffix`.
    func count(_ suffix: String, where predicate: (URLComponents) -> Bool = { _ in true }) -> Int {
        log.started.filter { url in
            guard url.path().hasSuffix(suffix), let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
            return predicate(components)
        }.count
    }

    func cancelledCount(_ suffix: String) -> Int {
        log.cancelled.filter { $0.path().hasSuffix(suffix) }.count
    }

    fileprivate func reply(to request: URLRequest) -> Reply {
        state.withLock { $0.started.append(request.url!) }
        return handler(request)
    }

    fileprivate func didCancel(_ url: URL) { state.withLock { $0.cancelled.append(url) } }
    fileprivate func didFinish(_ url: URL) { state.withLock { $0.finished.append(url) } }
}

final class FindingStubProtocol: URLProtocol, @unchecked Sendable {
    private static let stubs = Mutex<[String: FindingStub]>([:])
    /// Whether loading stopped, and whether it had finished (or failed) by then.
    private let flags = Mutex((stopped: false, done: false))

    static func register(_ stub: FindingStub) { stubs.withLock { $0[stub.host] = stub } }
    static func unregister(_ host: String) { _ = stubs.withLock { $0.removeValue(forKey: host) } }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let request = self.request
        guard let host = request.url?.host(), let stub = Self.stubs.withLock({ $0[host] }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }
        let reply = stub.reply(to: request)
        let response = HTTPURLResponse(
            url: request.url!, statusCode: reply.status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"])!
        let queue = DispatchQueue(label: "finding-stub")
        var delay: TimeInterval = 0
        var sentResponse = false
        for (index, chunk) in reply.chunks.enumerated() {
            delay += chunk.delay
            let isLast = index == reply.chunks.count - 1
            // The response goes with the first piece, unless the request fails outright
            // (a lone failing piece with no data: no connection at all).
            let failsHere = isLast && reply.failure != nil
            let needsResponse = !sentResponse && !(failsHere && chunk.data.isEmpty)
            sentResponse = sentResponse || needsResponse
            queue.asyncAfter(deadline: .now() + delay) { [self] in
                guard !flags.withLock({ $0.stopped }) else { return }
                if isLast { flags.withLock { $0.done = true } }
                if let failure = reply.failure, isLast {
                    if needsResponse { client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed) }
                    if !chunk.data.isEmpty { client?.urlProtocol(self, didLoad: chunk.data) }
                    client?.urlProtocol(self, didFailWithError: URLError(failure))
                    return
                }
                if needsResponse { client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed) }
                if !chunk.data.isEmpty { client?.urlProtocol(self, didLoad: chunk.data) }
                if isLast {
                    stub.didFinish(request.url!)
                    client?.urlProtocolDidFinishLoading(self)
                }
            }
        }
    }

    override func stopLoading() {
        let cutShort = flags.withLock { value in
            defer { value.stopped = true }
            return !value.stopped && !value.done
        }
        guard cutShort, let url = request.url, let host = url.host(),
              let stub = Self.stubs.withLock({ $0[host] }) else { return }
        stub.didCancel(url)
    }
}

/// Waits (up to `timeout` seconds; snapshot suites can hold the main thread) for a condition.
@MainActor
func eventually(timeout: TimeInterval = 30, _ condition: () -> Bool) async {
    let deadline = Date.now.addingTimeInterval(timeout)
    while !condition(), Date.now < deadline {
        try? await Task.sleep(for: .milliseconds(5))
    }
}

/// Milliseconds a block takes, best of `repeats` runs.
@MainActor
func bestMilliseconds(repeats: Int = 5, _ body: () -> Void) -> Double {
    var best = Double.infinity
    for _ in 0..<repeats {
        let start = ContinuousClock.now
        body()
        let elapsed = ContinuousClock.now - start
        best = min(best, Double(elapsed.components.attoseconds) / 1e15 + Double(elapsed.components.seconds) * 1000)
    }
    return best
}
