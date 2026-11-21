import AppKit
import Foundation
import SwiftUI
import Testing

@testable import FortressKit

// The "Download from Microsoft" way of the OneNote import sheet: the backend's download
// job (app/onenote_graph.py) as it reports it, the importer's state machine against a
// stubbed backend, and the sheet's snapshots.

enum GraphFixtures {
    static let clientId = "1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901"

    static let queued = #"{"job_id": "dl-1", "kind": "download", "status": "queued"}"#

    static let signingInWithCode = #"""
    {"job_id": "dl-1", "kind": "download", "status": "signing_in",
     "progress": {"phase": "signin", "done": 0, "total": 0, "current": null},
     "device_code": {"user_code": "HX4K-9QPD", "verification_uri": "https://microsoft.com/devicelogin",
                     "expires_at": "2026-11-20T14:20:00Z"},
     "wait": null, "eta_seconds": null, "warning_count": 0, "import_job_id": null, "error": null}
    """#

    static let throttled = #"""
    {"job_id": "dl-1", "kind": "download", "status": "downloading",
     "progress": {"phase": "pages", "done": 212, "total": 640, "current": "Work › Meetings › Weekly sync 2024-03-11"},
     "device_code": null, "wait": {"seconds": 240.0, "reason": "throttled by Microsoft (429), backing off",
                                   "until": "2026-11-20T14:05:00Z"},
     "eta_seconds": 4100, "warning_count": 2, "import_job_id": null, "error": null}
    """#

    static let downloading = #"""
    {"job_id": "dl-1", "kind": "download", "status": "downloading",
     "progress": {"phase": "pages", "done": 212, "total": 640, "current": "Work › Meetings › Weekly sync 2024-03-11"},
     "wait": null, "eta_seconds": 1530}
    """#

    static let handedOver = #"""
    {"job_id": "dl-1", "kind": "download", "status": "done", "import_job_id": "job-7f3a",
     "progress": {"phase": "done", "done": 0, "total": 0, "current": null}}
    """#

    static let failed = #"""
    {"job_id": "dl-1", "kind": "download", "status": "failed", "resumable": false,
     "error": "Sign-in failed: AADSTS700016: Application with identifier '1b2c…' was not found in the directory."}
    """#

    static let cancelled = #"""
    {"job_id": "dl-1", "kind": "download", "status": "cancelled",
     "error": "Download stopped. Download and Import again continues where it stopped."}
    """#

    static let notebooksQueued = #"{"job_id": "nb-1", "kind": "notebooks", "status": "queued"}"#

    static let notebooksDone = #"""
    {"job_id": "nb-1", "kind": "notebooks", "status": "done",
     "notebooks": [{"name": "Work", "sections": 6}, {"name": "Recipes", "sections": 3}, {"name": "Old school", "sections": 12}]}
    """#

    static let available = #"{"available": true, "problem": null, "job": null}"#

    static let uvMissing = #"""
    {"available": false, "problem": "Downloading from Microsoft needs uv, which isn't installed. Install it with “brew install uv” (or see docs.astral.sh/uv), then try again.", "job": null}
    """#

    static func job(_ json: String) -> OneNoteGraphJob {
        try! JSONDecoder().decode(OneNoteGraphJob.self, from: Data(json.utf8))
    }

    @MainActor
    static func defaults() -> UserDefaults {
        let name = "onenote-graph-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defaults.removePersistentDomain(forName: name)
        return defaults
    }
}

/// Answers by method and path, so a cancel racing the poll loop still gets its answer.
final class GraphStub: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (URLRequest, Data?) -> (Int, String) = { _, _ in (500, "{}") }
    nonisolated(unsafe) static var requests: [(method: String, path: String, body: [String: Any]?)] = []
    private static let lock = NSLock()

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body = request.httpBody ?? request.httpBodyStream.map(Self.read)
        let (status, text): (Int, String) = Self.lock.withLock {
            let json = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            Self.requests.append((request.httpMethod ?? "GET", request.url?.path ?? "", json))
            return Self.handler(request, body)
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(text.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func read(_ stream: InputStream) -> Data {
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

    static func api() -> APIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GraphStub.self]
        return APIClient(baseURL: URL(string: "http://stub.local")!, session: URLSession(configuration: configuration))
    }

    /// Replies to `GET path` one after another (the last one repeats).
    static func sequence(_ replies: [String: [String]], other: @escaping (URLRequest) -> (Int, String) = { _ in (404, #"{"detail": "not found"}"#) }) {
        var remaining = replies
        handler = { request, _ in
            let key = "\(request.httpMethod ?? "GET") \(request.url?.path ?? "")"
            guard var queue = remaining[key], !queue.isEmpty else { return other(request) }
            let reply = queue.count > 1 ? queue.removeFirst() : queue[0]
            remaining[key] = queue
            let status = key.hasPrefix("POST") ? 202 : 200
            return (status, reply)
        }
    }
}

@Suite struct OneNoteGraphDecodingTests {
    @Test func decodesThrottlingAndEstimate() throws {
        let job = GraphFixtures.job(GraphFixtures.throttled)
        #expect(job.status == .downloading)
        #expect(job.progress.done == 212 && job.progress.total == 640)
        #expect(job.progress.fraction.map { abs($0 - 212.0 / 640.0) < 0.0001 } == true)
        #expect(job.wait?.seconds == 240)
        let until = try Date("2026-11-20T14:05:00Z", strategy: .iso8601)
        #expect(job.wait?.until == until)
        #expect(job.etaSeconds == 4100)
        #expect(OneNoteGraph.pageCount(job.progress) == "212 of 640 pages")
        #expect(OneNoteImport.step(for: job.status) == .download)
        let time = until.formatted(date: .omitted, time: .shortened)
        #expect(OneNoteGraph.waitText(job.wait!) == "Waiting for Microsoft's rate limit… resumes at \(time)")
    }

    @Test func decodesDeviceCode() {
        let job = GraphFixtures.job(GraphFixtures.signingInWithCode)
        #expect(job.status == .signingIn)
        #expect(job.deviceCode?.userCode == "HX4K-9QPD")
        #expect(job.deviceCode?.url == URL(string: "https://microsoft.com/devicelogin"))
        #expect(OneNoteImport.step(for: job.status) == .signIn)
        #expect(OneNoteGraph.caption(job) == "Waiting for you to sign in with the code…")
    }

    @Test func lenientDecoding() {
        let job = GraphFixtures.job(#"{"job_id": "x", "status": "reticulating"}"#)
        #expect(job.status == .downloading)
        #expect(!job.status.isFinished)
        #expect(job.kind == .download)
        #expect(GraphFixtures.job(GraphFixtures.notebooksDone).notebooks?.map(\.name) == ["Work", "Recipes", "Old school"])
        #expect(GraphFixtures.job(GraphFixtures.cancelled).status.isFinished)
    }

    @Test func clientIdsAreGUIDs() {
        #expect(OneNoteGraph.isValidClientId(GraphFixtures.clientId))
        #expect(OneNoteGraph.isValidClientId(" \(GraphFixtures.clientId)\n"))
        #expect(!OneNoteGraph.isValidClientId(""))
        #expect(!OneNoteGraph.isValidClientId("my-app"))
        #expect(!OneNoteGraph.isValidClientId("1b2c3d4e5f60718293a4b5c6d7e8f901"))
    }

    @Test func estimates() {
        #expect(OneNoteGraph.etaText(30) == "Less than a minute left")
        #expect(OneNoteGraph.etaText(1530).hasPrefix("About 26"))
        #expect(OneNoteGraph.etaText(4100).hasPrefix("About 1"))
    }

    @Test func registrationStepsCoverTheSettingsTheToolNeeds() {
        let text = OneNoteGraph.registrationSteps.joined(separator: " ")
        for needed in ["Multitenant and personal", "Public client/native", "http://localhost", "Allow public client flows: Yes",
                       "Delegated", "Notes.Read", "Notes.Read.All", "offline_access", "User.Read", "No client secret"] {
            #expect(text.contains(needed), "missing \(needed)")
        }
    }
}

@MainActor
@Suite(.serialized) struct OneNoteGraphImporterTests {
    private func importer(_ defaults: UserDefaults = GraphFixtures.defaults()) -> OneNoteImporter {
        let importer = OneNoteImporter(defaults: defaults)
        importer.pollInterval = .milliseconds(1)
        return importer
    }

    @Test func startsOnMicrosoftAndRemembersTheAppId() {
        let defaults = GraphFixtures.defaults()
        let first = importer(defaults)
        #expect(first.source == .microsoft)
        #expect(!first.canDownload)
        first.clientId = GraphFixtures.clientId
        first.account = .work
        #expect(first.canDownload)
        let again = importer(defaults)
        #expect(again.clientId == GraphFixtures.clientId)
        #expect(again.account == .work)
        // A dropped file switches to the file way.
        let file = OneNoteImporter(file: URL(fileURLWithPath: "/tmp/Work.zip"), defaults: defaults)
        #expect(file.source == .file)
    }

    @Test func missingToolIsSaidAndBlocksTheDownload() async {
        GraphStub.requests = []
        GraphStub.sequence(["GET /api/import/onenote-graph": [GraphFixtures.uvMissing]])
        let importer = importer()
        importer.clientId = GraphFixtures.clientId
        await importer.checkMicrosoft(api: GraphStub.api())
        #expect(importer.graphStatus?.available == false)
        #expect(importer.graphStatus?.problem?.contains("brew install uv") == true)
        #expect(!importer.canDownload)
    }

    @Test func downloadsThenImportsAndReports() async throws {
        GraphStub.requests = []
        GraphStub.sequence([
            "POST /api/import/onenote-graph": [GraphFixtures.queued],
            "GET /api/import/onenote-graph/dl-1": [GraphFixtures.signingInWithCode, GraphFixtures.throttled,
                                                   GraphFixtures.downloading, GraphFixtures.handedOver],
            "GET /api/import/onenote/job-7f3a": [OneNoteFixtures.importing, OneNoteFixtures.done],
        ])
        let importer = importer()
        importer.clientId = GraphFixtures.clientId
        importer.folderId = "f1"
        var finished: OneNoteImportJob?
        importer.onFinished = { finished = $0 }
        await importer.runDownload(kind: .download, api: GraphStub.api())

        guard case .finished(let job) = importer.phase else {
            Issue.record("phase is \(importer.phase)")
            return
        }
        #expect(job.result?.notebooks.count == 2)
        #expect(finished?.jobId == "job-7f3a")
        #expect(importer.origin == .microsoft)
        let start = try #require(GraphStub.requests.first)
        #expect(start.method == "POST" && start.path == "/api/import/onenote-graph")
        #expect(start.body?["client_id"] as? String == GraphFixtures.clientId)
        #expect(start.body?["account"] as? String == "personal")
        #expect(start.body?["folder_id"] as? String == "f1")
        #expect(start.body?["notebooks"] == nil)
        #expect(GraphStub.requests.contains { $0.path == "/api/import/onenote/job-7f3a" })
        #expect(GraphStub.requests.filter { $0.path == "/api/import/onenote-graph/dl-1" }.count == 4)
    }

    @Test func listsNotebooksThenDownloadsTheChosenOnes() async throws {
        GraphStub.requests = []
        GraphStub.sequence([
            "POST /api/import/onenote-graph/notebooks": [GraphFixtures.notebooksQueued],
            "GET /api/import/onenote-graph/nb-1": [GraphFixtures.notebooksDone],
        ])
        let importer = importer()
        importer.clientId = GraphFixtures.clientId
        await importer.runDownload(kind: .notebooks, api: GraphStub.api())
        #expect(importer.phase == .choosing)
        #expect(importer.notebookChoices?.count == 3)
        #expect(importer.selectedNotebooks == ["Work", "Recipes", "Old school"])
        #expect(importer.requestedNotebooks == nil)  // all of them: no filter
        importer.setNotebook("Old school", selected: false)
        #expect(importer.requestedNotebooks == ["Work", "Recipes"])
        for name in ["Work", "Recipes"] { importer.setNotebook(name, selected: false) }
        #expect(!importer.canDownload)
        importer.setNotebook("Work", selected: true)

        GraphStub.requests = []
        GraphStub.sequence([
            "POST /api/import/onenote-graph": [GraphFixtures.queued],
            "GET /api/import/onenote-graph/dl-1": [GraphFixtures.failed],
        ])
        await importer.runDownload(kind: .download, api: GraphStub.api())
        let start = try #require(GraphStub.requests.first)
        #expect(start.body?["notebooks"] as? [String] == ["Work"])

        // Another account has other notebooks.
        importer.account = .work
        #expect(importer.notebookChoices == nil)
    }

    @Test func failureShowsTheToolsMessage() async {
        GraphStub.sequence([
            "POST /api/import/onenote-graph": [GraphFixtures.queued],
            "GET /api/import/onenote-graph/dl-1": [GraphFixtures.failed],
        ])
        let importer = importer()
        importer.clientId = GraphFixtures.clientId
        await importer.runDownload(kind: .download, api: GraphStub.api())
        guard case .failed(let message, let converterMissing) = importer.phase else {
            Issue.record("phase is \(importer.phase)")
            return
        }
        #expect(message.contains("AADSTS700016"))
        #expect(!converterMissing)
        importer.reset(keepFile: true)
        #expect(importer.phase == .choosing)
        #expect(importer.clientId == GraphFixtures.clientId)
    }

    @Test func refusedStartIsShown() async {
        GraphStub.sequence([:]) { _ in (503, #"{"detail": "Downloading from Microsoft needs uv, which isn't installed."}"#) }
        let importer = importer()
        importer.clientId = GraphFixtures.clientId
        await importer.runDownload(kind: .download, api: GraphStub.api())
        #expect(importer.phase == .failed(message: "Downloading from Microsoft needs uv, which isn't installed.", converterMissing: false))
    }

    @Test func cancelStopsTheDownloadAndGoesBack() async throws {
        GraphStub.requests = []
        final class Flag: @unchecked Sendable { var value = false }
        let cancelled = Flag()
        GraphStub.handler = { request, _ in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/import/onenote-graph"): return (202, GraphFixtures.queued)
            case ("POST", "/api/import/onenote-graph/dl-1/cancel"):
                cancelled.value = true
                return (202, GraphFixtures.downloading)
            case ("GET", "/api/import/onenote-graph/dl-1"):
                return (200, cancelled.value ? GraphFixtures.cancelled : GraphFixtures.downloading)
            default: return (404, "{}")
            }
        }
        let importer = importer()
        importer.clientId = GraphFixtures.clientId
        importer.pollInterval = .milliseconds(20)
        let run = Task { await importer.runDownload(kind: .download, api: GraphStub.api()) }
        while true {
            if case .downloading(let job) = importer.phase, job.status == .downloading { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        importer.cancel(api: GraphStub.api())
        #expect(importer.isCancelling)
        await run.value
        #expect(importer.phase == .choosing)
        #expect(importer.notice?.contains("continues where it stopped") == true)
        #expect(!importer.isCancelling)
        #expect(GraphStub.requests.contains { $0.method == "POST" && $0.path == "/api/import/onenote-graph/dl-1/cancel" })
    }

    @Test func picksUpARunningDownload() async throws {
        GraphStub.sequence([
            "GET /api/import/onenote-graph": [#"{"available": true, "job": \#(GraphFixtures.downloading)}"#],
            "GET /api/import/onenote-graph/dl-1": [GraphFixtures.cancelled],
        ])
        let importer = OneNoteImporter(source: .file, defaults: GraphFixtures.defaults())
        importer.pollInterval = .milliseconds(1)
        await importer.checkMicrosoft(api: GraphStub.api())
        #expect(importer.source == .microsoft)
        guard case .downloading = importer.phase else {
            Issue.record("phase is \(importer.phase)")
            return
        }
        for _ in 0..<200 where importer.phase != .choosing { try await Task.sleep(for: .milliseconds(5)) }
        #expect(importer.phase == .choosing)
    }
}

// MARK: Gallery

@MainActor
@Suite(.serialized) struct OneNoteGraphGallerySnapshotTests {
    private func both<V: View>(_ name: String, height: CGFloat, _ view: () -> V) throws {
        let size = CGSize(width: 560, height: height)
        try snapshot(view(), named: "onenote-\(name)-light", size: size)
        try snapshot(view(), named: "onenote-\(name)-dark", size: size, appearance: .darkAqua)
    }

    @Test func microsoft() throws {
        let model = ShellFixtures.model()

        // First time: no app id yet, so the how-to is open.
        try both("microsoft-first", height: 820) {
            OneNoteImportSheet(importer: OneNoteImporter(defaults: GraphFixtures.defaults())).environment(model)
        }

        // Returning: the remembered app id, notebooks listed and one left out.
        let defaults = GraphFixtures.defaults()
        let ready = OneNoteImporter(defaults: defaults)
        ready.clientId = GraphFixtures.clientId
        ready.offerNotebooks(GraphFixtures.job(GraphFixtures.notebooksDone).notebooks ?? [])
        ready.setNotebook("Old school", selected: false)
        try both("microsoft-ready", height: 520) { OneNoteImportSheet(importer: ready).environment(model) }

        let code = OneNoteImporter(phase: .downloading(GraphFixtures.job(GraphFixtures.signingInWithCode)),
                                   source: .microsoft, defaults: defaults)
        try both("microsoft-device-code", height: 470) { OneNoteImportSheet(importer: code).environment(model) }

        let throttled = OneNoteImporter(phase: .downloading(GraphFixtures.job(GraphFixtures.throttled)),
                                        source: .microsoft, defaults: defaults)
        try both("microsoft-throttled", height: 400) { OneNoteImportSheet(importer: throttled).environment(model) }

        let importing = OneNoteImporter(phase: .running(try OneNoteFixtures.job(OneNoteFixtures.importing)),
                                        source: .microsoft, defaults: defaults)
        try snapshot(OneNoteImportSheet(importer: importing).environment(model), named: "onenote-microsoft-importing-light",
                     size: CGSize(width: 560, height: 400))

        let failed = OneNoteImporter(phase: .failed(message: "Microsoft stopped answering for now (HTTP 429 throttled). Nothing is lost: Download and Import again continues where it stopped.", converterMissing: false),
                                     source: .microsoft, defaults: defaults)
        try snapshot(OneNoteImportSheet(importer: failed).environment(model), named: "onenote-microsoft-failed-light",
                     size: CGSize(width: 560, height: 260))
    }
}
