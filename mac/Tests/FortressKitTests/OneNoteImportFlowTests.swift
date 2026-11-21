import AppKit
import Foundation
import SwiftUI
import Testing

@testable import FortressKit

/// Answers the importer's requests in tests (its own state, so it never races the
/// editor's stub).
final class ImportStub: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var replies: [(status: Int, body: String)] = []
    nonisolated(unsafe) static var requests: [URLRequest] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(request)
        let reply = Self.replies.isEmpty ? (status: 500, body: "{}") : Self.replies.removeFirst()
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: nil,
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(reply.body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    static func api() -> APIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ImportStub.self]
        return APIClient(baseURL: URL(string: "http://stub.local")!, session: URLSession(configuration: configuration))
    }
}

@MainActor
private func exportFile(_ name: String = "Work.zip") throws -> URL {
    let folder = FileManager.default.temporaryDirectory.appending(path: "onenote-tests-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let url = folder.appending(path: name)
    try Data("PK\u{3}\u{4} pretend zip".utf8).write(to: url)
    return url
}

@MainActor
@Suite(.serialized) struct OneNoteImportFlowTests {
    @Test func uploadsPollsAndReports() async throws {
        ImportStub.requests = []
        ImportStub.replies = [(202, OneNoteFixtures.queued), (200, OneNoteFixtures.importing), (200, OneNoteFixtures.done)]
        let importer = OneNoteImporter()
        importer.pollInterval = .milliseconds(1)
        importer.folderId = "01M7FOLDERWORK000000000000"
        let file = try exportFile()
        #expect(importer.choose(file))
        #expect(importer.fileSize ?? 0 > 0)
        var finished: OneNoteImportJob?
        importer.onFinished = { finished = $0 }
        await importer.run(file: file, api: ImportStub.api())

        guard case .finished(let job) = importer.phase else {
            Issue.record("phase is \(importer.phase)")
            return
        }
        #expect(job.result?.notebooks.count == 2)
        #expect(finished?.jobId == "job-7f3a")
        let requests = ImportStub.requests
        #expect(requests.map { $0.httpMethod ?? "GET" } == ["POST", "GET", "GET"])
        #expect(requests.first?.url?.path == "/api/import/onenote")
        #expect(requests.first?.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("multipart/form-data; boundary=") == true)
        #expect(requests.last?.url?.path == "/api/import/onenote/job-7f3a")
    }

    @Test func multipartBodyHasTheFileAndFolder() throws {
        let file = try exportFile("My \"Work\".onepkg")
        let body = try APIClient.oneNoteMultipartFile(file: file, folderId: "f1", boundary: "B")
        let text = String(decoding: try Data(contentsOf: body), as: UTF8.self)
        #expect(text.hasPrefix("--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"My 'Work'.onepkg\"\r\n"))
        #expect(text.contains("pretend zip\r\n--B\r\nContent-Disposition: form-data; name=\"folder_id\"\r\n\r\nf1\r\n--B--\r\n"))
        let bare = try APIClient.oneNoteMultipartFile(file: file, folderId: nil, boundary: "B")
        #expect(!String(decoding: try Data(contentsOf: bare), as: UTF8.self).contains("folder_id"))
    }

    @Test func missingConverterIsExplained() async throws {
        ImportStub.requests = []
        ImportStub.replies = [(503, #"{"detail": "The OneNote converter (fortress-onenote) is not installed. Set FORTRESS_ONENOTE_BIN."}"#)]
        let importer = OneNoteImporter()
        let file = try exportFile()
        importer.choose(file)
        await importer.run(file: file, api: ImportStub.api())
        guard case .failed(let message, let converterMissing) = importer.phase else {
            Issue.record("phase is \(importer.phase)")
            return
        }
        #expect(converterMissing)
        #expect(message.contains("fortress-onenote"))
        importer.reset(keepFile: true)
        #expect(importer.phase == .choosing)
        #expect(importer.file == file)
    }

    @Test func failedJobShowsItsError() async throws {
        ImportStub.requests = []
        ImportStub.replies = [(202, OneNoteFixtures.queued), (200, OneNoteFixtures.failed)]
        let importer = OneNoteImporter()
        importer.pollInterval = .milliseconds(1)
        let file = try exportFile()
        importer.choose(file)
        await importer.run(file: file, api: ImportStub.api())
        guard case .failed(_, let converterMissing) = importer.phase else {
            Issue.record("phase is \(importer.phase)")
            return
        }
        #expect(converterMissing)
    }

    @Test func rejectsOtherFiles() {
        let importer = OneNoteImporter()
        #expect(!importer.choose(URL(fileURLWithPath: "/tmp/notes.md")))
        #expect(importer.fileProblem != nil)
        #expect(importer.file == nil)
    }

    @Test func converterIsFoundInTheRepository() {
        let built = "/repo/tools/fortress-onenote/target/release/fortress-onenote"
        let files: Set<String> = ["/repo/backend/app/main.py", "/repo/backend/.venv/bin/uvicorn", built]
        let launch = BackendLaunch.make(folder: URL(fileURLWithPath: "/repo/backend"), port: 8000, home: "/h", fileExists: files.contains)
        #expect(launch?.onenoteConverter?.path == built)
        let fromRepo = BackendLaunch.make(folder: URL(fileURLWithPath: "/repo"), port: 8000, home: "/h", fileExists: files.contains)
        #expect(fromRepo?.onenoteConverter?.path == built)
        let none = BackendLaunch.make(folder: URL(fileURLWithPath: "/repo"), port: 8000, home: "/h",
                                      fileExists: { $0 == "/repo/backend/app/main.py" })
        #expect(none?.onenoteConverter == nil)
    }
}

// MARK: Gallery

@MainActor
@Suite(.serialized) struct OneNoteGallerySnapshotTests {
    private func both<V: View>(_ name: String, height: CGFloat, _ view: () -> V) throws {
        let size = CGSize(width: 560, height: height)
        try snapshot(view(), named: "onenote-\(name)-light", size: size)
        try snapshot(view(), named: "onenote-\(name)-dark", size: size, appearance: .darkAqua)
    }

    private func job(_ json: String) -> OneNoteImportJob {
        try! OneNoteFixtures.job(json)
    }

    @Test func steps() throws {
        let model = ShellFixtures.model()
        let work = URL(fileURLWithPath: "/tmp/Work.zip")

        try both("choose-file", height: 560) {
            OneNoteImportSheet(importer: OneNoteImporter(source: .file, defaults: GraphFixtures.defaults())).environment(model)
        }

        let chosen = OneNoteImporter(file: work, fileSize: 48_213_504)
        chosen.folderId = "work"
        try snapshot(OneNoteImportSheet(importer: chosen).environment(model), named: "onenote-chosen-light",
                     size: CGSize(width: 560, height: 460))

        try snapshot(OneNoteImportSheet(importer: OneNoteImporter(phase: .uploading(0.42), file: work)).environment(model),
                     named: "onenote-uploading-light", size: CGSize(width: 560, height: 340))

        let running = OneNoteImporter(phase: .running(job(OneNoteFixtures.importing)), file: work)
        try both("progress", height: 340) { OneNoteImportSheet(importer: running).environment(model) }

        let done = OneNoteImporter(phase: .finished(job(OneNoteFixtures.done)), file: work)
        try both("report", height: 660) { OneNoteImportSheet(importer: done).environment(model) }

        let missing = OneNoteImporter(phase: .failed(
            message: "The OneNote converter (fortress-onenote) is not installed. Set FORTRESS_ONENOTE_BIN.", converterMissing: true), file: work)
        try both("converter-missing", height: 300) { OneNoteImportSheet(importer: missing).environment(model) }
    }
}
