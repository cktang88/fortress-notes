import AppKit
import Foundation
import SwiftUI
import Testing

@testable import FortressKit

// Finding scenes for the snapshot gallery. The views talk to a fake backend that serves
// the real captured fixtures plus a few richer responses of exactly the same shape.

enum FindingFixtures {
    static let kyoto = "01M7NZXNDZHSR801AP2K15KMAQ"
    static let kyotoRyokanBlock = "01M7NZXNE1DFCAQMW15HEBDN4H"
    static let kyotoCheckInBlock = "01M7NZXNE1DFCAQMW15HEBDN5A"
    static let packing = "01M7NZXNENK5XG14881ZB8R42Q"
    static let packingBlock = "01M7NZXNEQFS8JH461VADZS3WQ"
    static let japan = "01M7NZXNJAPAN2024SPRING0001"
    static let onsen = "01M7NZXNONSENETIQUETTE00001"
    static let cafes = "01M7NZXNCAFESKYOTO00000001"
    /// A note of scans and photos whose text was read (Text in pictures).
    static let scans = "01M7NZXNSCANSRECEIPTS00001"

    /// UTF-16 ranges of every case-insensitive occurrence of `word`, like the backend sends.
    static func ranges(of word: String, in text: String) -> [[Int]] {
        let ns = text as NSString
        var result: [[Int]] = []
        var search = NSRange(location: 0, length: ns.length)
        while true {
            let found = ns.range(of: word, options: [.caseInsensitive, .diacriticInsensitive], range: search)
            if found.location == NSNotFound { break }
            result.append([found.location, found.location + found.length])
            search = NSRange(location: found.upperBound, length: ns.length - found.upperBound)
        }
        return result
    }

    static func match(_ document: String, _ title: String, block: String, _ text: String, _ matched: [String], score: Double) -> SearchMatch {
        SearchMatch(
            blockId: block, documentId: document, documentTitle: title, blockType: "paragraph", text: text,
            highlights: ranges(of: "ryokan", in: text), matched: matched, score: score)
    }

    static let search: [SearchMatch] = [
        match(kyoto, "Kyoto trip", block: kyotoRyokanBlock, "We booked a ryokan in Gion.", ["words", "meaning"], score: 0.09),
        match(japan, "Japan, spring 2024", block: "01M7NZXNJAPANB1",
              "…stayed two nights at a small ryokan near Arashiyama 🌿 — the kaiseki dinner was the best meal of the trip, and the onsen opened at six.",
              ["words", "meaning"], score: 0.08),
        match(kyoto, "Kyoto trip", block: kyotoCheckInBlock,
              "Check-in at the ryokan is from 3 pm; dinner is served in the room at 6:30.", ["words"], score: 0.07),
        match(packing, "Packing list", block: packingBlock, "Pack the ryokan booking confirmation.", ["words", "meaning"], score: 0.07),
        match(onsen, "Onsen etiquette", block: "01M7NZXNONSENB1",
              "Wash before you get in; towels stay out of the water. Most inns lend you a yukata for dinner.",
              ["meaning"], score: 0.05),
        match(cafes, "Cafés in Kyoto", block: "01M7NZXNCAFEB1",
              "Café Bibliotic Hello! is a short walk from the Ryokan — good for a slow breakfast.", ["words", "title"], score: 0.04),
    ]

    static let question = "Where are we staying in Kyoto?"

    static let kyotoCitation = AskCitation(blockId: kyotoRyokanBlock, documentId: kyoto, documentTitle: "Kyoto trip", quote: "We booked a ryokan in Gion.")
    static let checkInCitation = AskCitation(blockId: kyotoCheckInBlock, documentId: kyoto, documentTitle: "Kyoto trip", quote: "Check-in at the ryokan is from 3 pm; dinner is served in the room at 6:30.")
    static let packingCitation = AskCitation(blockId: packingBlock, documentId: packing, documentTitle: "Packing list", quote: "Pack the ryokan booking confirmation.")
    static let japanCitation = AskCitation(blockId: "01M7NZXNJAPANB1", documentId: japan, documentTitle: "Japan, spring 2024", quote: "stayed two nights at a small ryokan near Arashiyama")

    static let sentences = [
        AskSentence(text: "You’re staying at a ryokan in Gion.", citations: [kyotoCitation]),
        AskSentence(text: "Check-in opens at 3 pm, and dinner is served in your room at 6:30.", citations: [checkInCitation]),
        AskSentence(text: "Bring the booking confirmation — it’s on your packing list.", citations: [packingCitation]),
        AskSentence(text: "Last spring you stayed at a different ryokan, near Arashiyama.", citations: [japanCitation]),
    ]

    static let steps = [
        AskStep(action: "grep", detail: "ryokan|hotel|stay"),
        AskStep(action: "read", detail: "Kyoto trip"),
    ]

    /// The streamed answer as the backend sends it (NDJSON).
    static var askStream: Data {
        var lines: [[String: Any]] = steps.map { ["type": "step", "step": ["action": $0.action, "detail": $0.detail]] }
        func json(_ c: AskCitation) -> [String: Any] {
            ["block_id": c.blockId, "document_id": c.documentId, "document_title": c.documentTitle, "quote": c.quote]
        }
        let answer = sentences.map { ["text": $0.text, "citations": $0.citations.map(json)] as [String: Any] }
        lines += answer.map { ["type": "sentence", "sentence": $0] as [String: Any] }
        lines.append(["type": "done", "result": [
            "status": "answered", "answer": answer, "sources": [],
            "steps": steps.map { ["action": $0.action, "detail": $0.detail] }, "elapsed_ms": 1840,
        ] as [String: Any]])
        let text = lines.map { String(decoding: try! JSONSerialization.data(withJSONObject: $0), as: UTF8.self) }.joined(separator: "\n")
        return Data((text + "\n").utf8)
    }

    static func note(_ id: String, _ title: String, _ snippet: String) -> NoteSummary {
        NoteSummary(id: id, title: title, status: .rough, updatedAt: "2026-11-17T09:12:00+00:00", snippet: snippet)
    }

    static let seenBefore: [RelatedResult] = [
        RelatedResult(note: note(japan, "Japan, spring 2024", ""), score: 0.71, matchedBlockId: "01M7NZXNJAPANB1",
                      matchedBlockText: "Stayed two nights at a small ryokan near Arashiyama — the kaiseki dinner was the best meal of the trip."),
        RelatedResult(note: note(onsen, "Onsen etiquette", ""), score: 0.52, matchedBlockId: "01M7NZXNONSENB1",
                      matchedBlockText: "Most inns lend you a yukata for dinner; wear it left over right."),
    ]

    static let related: [RelatedResult] = [
        RelatedResult(note: note(japan, "Japan, spring 2024", "Two weeks from Tokyo to Hiroshima."), score: 0.64, matchedBlockId: "01M7NZXNJAPANB1",
                      matchedBlockText: "Stayed two nights at a small ryokan near Arashiyama."),
        RelatedResult(note: note(packing, "Packing list", "Passport, charger, rain jacket."), score: 0.58, matchedBlockId: packingBlock,
                      matchedBlockText: "Pack the ryokan booking confirmation."),
        RelatedResult(note: note(cafes, "Cafés in Kyoto", "Where to get breakfast."), score: 0.41, matchedBlockId: nil, matchedBlockText: ""),
        RelatedResult(note: note(onsen, "Onsen etiquette", "Wash first, towels out of the water."), score: 0.33, matchedBlockId: "01M7NZXNONSENB1",
                      matchedBlockText: "Wash before you get in; towels stay out of the water."),
    ]

    static let backlinks: [Backlink] = [
        Backlink(sourceBlockId: "01M7NZXNPACKB2", sourceDocumentId: packing, sourceDocumentTitle: "Packing list",
                 sourceText: "Everything for Kyoto trip goes in the small suitcase.", label: "Kyoto trip", targetBlockId: nil),
        Backlink(sourceBlockId: "01M7NZXNJAPANB7", sourceDocumentId: japan, sourceDocumentTitle: "Japan, spring 2024",
                 sourceText: "Next time: see Kyoto trip for the Gion ryokan.", label: "Kyoto trip", targetBlockId: nil),
    ]

    /// The real `/info` answer for an imported note, placed in Travel with links in and out.
    static var info: Data {
        guard let data = try? findingFixture("info-imported.json"),
              var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return Data("{}".utf8) }
        object["folder_path"] = [["id": "01M7NZXND9X670BWK4XMRNMYCP", "name": "Travel"]]
        object["last_opened_at"] = "2026-11-17T21:40:00+00:00"
        var counts = object["counts"] as? [String: Any] ?? [:]
        counts["words"] = 186
        counts["blocks"] = 14
        counts["outgoing_links"] = 3
        counts["backlinks"] = 2
        counts["edits"] = 9
        object["counts"] = counts
        object["blocks_by_source"] = ["onenote_import": 12, "manual": 2]
        return (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
    }

    static let pictures: [[String: Any]] = [
        ["block_id": "01M7NZXNSCANB1", "media_name": "01M7NZXNSCANIMG1.png", "content_hash": "a1", "mime_type": "image/png",
         "status": "done", "method": "ocr", "model": "apple-vision/26", "read_at": "2026-11-18T09:12:00+00:00",
         "text": "Ryokan Gion Hatanaka\nCheck-in 15:00 · Check-out 10:00\n2 guests · 3 nights\nTotal ¥186,000 (tax incl.)",
         "text_chars": 92],
        ["block_id": "01M7NZXNSCANB2", "media_name": "01M7NZXNSCANIMG2.jpg", "content_hash": "a2", "mime_type": "image/jpeg",
         "status": "done", "method": "ocr", "model": "apple-vision/26", "read_at": "2026-11-18T09:12:04+00:00",
         "text": "Привет из Санкт-Петербурга!\nУвидимся в пятницу — Маша", "text_chars": 52],
        ["block_id": "01M7NZXNSCANB3", "media_name": "01M7NZXNSCANPDF1.pdf", "content_hash": "a3", "mime_type": "application/pdf",
         "status": "done", "method": "pdf-text+ocr", "pages": 3, "model": "apple-vision/26", "read_at": "2026-11-18T09:13:00+00:00",
         "text": "JR Pass exchange order\nName: TANAKA MEI\nValid 14 days from first use\nExchange at any JR ticket office before 2027-02-01.",
         "text_chars": 1204],
        ["block_id": "01M7NZXNSCANB4", "media_name": "01M7NZXNSCANIMG3.png", "content_hash": "a4", "mime_type": "image/png",
         "status": "queued", "model": "rapidocr-ppocrv6-small/1", "text": "", "text_chars": 0],
    ]

    /// `/info` for the scans note: three pictures read, one waiting.
    static var scansInfo: Data {
        guard let data = try? findingFixture("info-imported.json"),
              var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return Data("{}".utf8) }
        object["id"] = scans
        object["title"] = "Receipts and tickets"
        object["provenance"] = ["source": "paste"]
        object["blocks_by_source"] = ["paste": 6]
        object["picture_text"] = pictures
        return (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
    }

    static func navigation() -> NavigationResponse {
        (try? JSONDecoder().decode(NavigationResponse.self, from: findingFixture("navigation.json")))
            ?? NavigationResponse(items: [], recent: [])
    }

    /// A model whose API client is answered by `FixtureURLProtocol`.
    @MainActor
    static func model() -> AppModel {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FixtureURLProtocol.self]
        let api = APIClient(baseURL: URL(string: "http://fixtures.invalid")!, session: URLSession(configuration: configuration))
        let model = AppModel(api: api)
        model.navigation = navigation()
        model.hasLoadedNavigation = true
        model.connectsOnAppear = false  // Don't start or look for a real backend.
        return model
    }
}

/// Serves fixture responses for the fake backend, by path.
final class FixtureURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let url = request.url!
        let body = Self.body(path: url.path(), query: URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? [])
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    static func body(path: String, query: [URLQueryItem]) -> Data {
        let encoder = JSONEncoder()
        func encoded<T: Encodable>(_ value: T) -> Data { (try? encoder.encode(value)) ?? Data("[]".utf8) }
        if path.hasSuffix("/api/navigation") { return (try? findingFixture("navigation.json")) ?? Data() }
        if path.hasSuffix("/api/unified-search") { return encoded(FindingFixtures.search) }
        if path.hasSuffix("/api/ask") { return FindingFixtures.askStream }
        if path.hasSuffix("/info") {
            return path.contains(FindingFixtures.scans) ? FindingFixtures.scansInfo : FindingFixtures.info
        }
        if path.hasSuffix("/backlinks") { return encoded(FindingFixtures.backlinks) }
        if path.hasSuffix("/related") {
            let hasBlock = query.contains { $0.name == "block_id" }
            return encoded(hasBlock ? FindingFixtures.seenBefore : FindingFixtures.related)
        }
        if path.contains("/api/block-documents/"), !path.hasSuffix("/similar") {
            return (try? findingFixture("document.json")) ?? Data()
        }
        return Data("[]".utf8)
    }
}

/// A stand-in desktop behind floating panels, so the glass has something to refract.
struct DesktopBackdrop: View {
    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.36, green: 0.52, blue: 0.78), Color(red: 0.86, green: 0.62, blue: 0.46)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
            Circle().fill(Color(red: 0.95, green: 0.82, blue: 0.55)).frame(width: 260).offset(x: 220, y: -90).blur(radius: 6)
            Circle().fill(Color(red: 0.25, green: 0.36, blue: 0.62)).frame(width: 320).offset(x: -260, y: 120).blur(radius: 6)
        }
    }
}

/// Runs the main run loop briefly (AppKit layout and drawing).
@MainActor
func pumpRunLoop(_ seconds: TimeInterval) {
    RunLoop.main.run(until: Date().addingTimeInterval(seconds))
}

/// Like `snapshot(_:named:)`, but lets the window run longer first, so views that load
/// from the (fake) backend and stream answers have settled.
@MainActor
func settledSnapshot<V: View>(
    _ view: V, named name: String, size: CGSize, appearance: NSAppearance.Name, seconds: Double = 2.5
) async throws {
    guard let directory = ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] else { return }
    try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    app.appearance = NSAppearance(named: appearance)
    let window = NSWindow(
        contentRect: CGRect(origin: CGPoint(x: 80, y: 80), size: size),
        styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
        backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: appearance)
    window.titlebarAppearsTransparent = true
    window.isReleasedWhenClosed = false
    window.contentView = NSHostingView(rootView: view)
    window.setContentSize(size)
    window.makeKeyAndOrderFront(nil)
    window.orderFrontRegardless()
    app.activate()
    let deadline = Date().addingTimeInterval(seconds)
    // Alternate: the run loop draws, the await lets tasks on the main actor (loads, streams) resume.
    while Date() < deadline {
        pumpRunLoop(0.03)
        try? await Task.sleep(for: .milliseconds(30))
    }

    let url = URL(fileURLWithPath: directory).appendingPathComponent("\(name).png")
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-o", "-l", String(window.windowNumber), url.path]
    try capture.run()
    capture.waitUntilExit()
    window.close()
}

extension GallerySnapshotTests {
    private func both<V: View>(
        _ name: String, size: CGSize = CGSize(width: 1000, height: 660), seconds: Double = 2.5, _ make: () -> V
    ) async throws {
        try await settledSnapshot(make(), named: "\(name)-light", size: size, appearance: .aqua, seconds: seconds)
        try await settledSnapshot(make(), named: "\(name)-dark", size: size, appearance: .darkAqua, seconds: seconds)
    }

    @Test func findingSearchResults() async throws {
        // Other suites hold the main thread while they render, so allow time for the fetch.
        try await both("finding-search", seconds: 15) {
            let model = FindingFixtures.model()
            model.query = "ryokan"
            model.route = .note(id: FindingFixtures.kyoto, focusBlockId: FindingFixtures.kyotoRyokanBlock)
            return MainWindow().environment(model)
        }
    }

    @Test func findingAskAnswered() async throws {
        // The finished answer, deterministic.
        try await both("finding-ask-answered", size: CGSize(width: 760, height: 660)) {
            var state = AskState()
            FindingFixtures.steps.forEach { state.apply(.step($0)) }
            state.apply(.done(AskResponse(status: .answered, answer: FindingFixtures.sentences,
                                          steps: FindingFixtures.steps, elapsedMs: 1840)))
            let turn = AskTurn(question: FindingFixtures.question, state: state)
            return AskView(question: FindingFixtures.question, turns: [turn])
                .environment(FindingFixtures.model())
                .tint(.fortressEmber)
        }
    }

    @Test func findingAskStates() async throws {
        try await both("finding-ask-streaming", size: CGSize(width: 760, height: 560)) {
            var state = AskState()
            FindingFixtures.steps.prefix(1).forEach { state.apply(.step($0)) }
            FindingFixtures.sentences.prefix(2).forEach { state.apply(.sentence($0)) }
            let turn = AskTurn(question: FindingFixtures.question, state: state, started: .now.addingTimeInterval(-1.2))
            return AskView(question: FindingFixtures.question, turns: [turn]).environment(FindingFixtures.model())
                .tint(.fortressEmber)
        }
        try await both("finding-ask-states", size: CGSize(width: 760, height: 660)) {
            var notConfigured = AskState()
            notConfigured.apply(.done(AskResponse(
                status: .notConfigured, answer: [],
                sources: [AskSource(blockId: FindingFixtures.kyotoRyokanBlock, documentId: FindingFixtures.kyoto, documentTitle: "Kyoto trip", text: "We booked a ryokan in Gion.")],
                steps: [], elapsedMs: 11)))
            var notFound = AskState()
            notFound.apply(.step(AskStep(action: "grep", detail: "passport|visa")))
            notFound.apply(.done(AskResponse(status: .notFound, answer: [], steps: nil, elapsedMs: 1320)))
            var failed = AskState()
            failed.fail("The local server isn’t responding.")
            return AskView(question: "", turns: [
                AskTurn(question: "Which ryokan did we book?", state: notConfigured),
                AskTurn(question: "When does my passport expire?", state: notFound),
                AskTurn(question: "What did Mei recommend?", state: failed),
            ]).environment(FindingFixtures.model())
                .tint(.fortressEmber)
        }
    }

    @Test func findingPalette() async throws {
        try await both("finding-palette") {
            let model = FindingFixtures.model()
            model.route = .note(id: FindingFixtures.kyoto, focusBlockId: nil)
            model.isPaletteShown = true
            return MainWindow().environment(model)
        }
        try await both("finding-palette-query") {
            let model = FindingFixtures.model()
            model.route = .note(id: FindingFixtures.kyoto, focusBlockId: nil)
            model.isPaletteShown = true
            model.paletteQuery = "tr"
            return MainWindow().environment(model)
        }
    }

    @Test func findingInspector() async throws {
        try await both("finding-inspector", size: CGSize(width: 300, height: 660), seconds: 8) {
            let model = FindingFixtures.model()
            model.route = .note(id: FindingFixtures.kyoto, focusBlockId: nil)
            model.focusedBlockId = FindingFixtures.kyotoRyokanBlock
            return InspectorView()
                .environment(model)
                .tint(.fortressEmber)
                .background(Color(nsColor: .windowBackgroundColor))
        }
    }

    @Test func findingInspectorPictures() async throws {
        try await both("finding-inspector-pictures", size: CGSize(width: 300, height: 760), seconds: 8) {
            let model = FindingFixtures.model()
            model.route = .note(id: FindingFixtures.scans, focusBlockId: nil)
            return InspectorView()
                .environment(model)
                .tint(.fortressEmber)
                .background(Color(nsColor: .windowBackgroundColor))
        }
    }

    @Test func findingPictureTextStates() async throws {
        try await both("finding-picture-text-states", size: CGSize(width: 300, height: 760)) {
            let pictures = [
                PictureText(blockId: "b1", mediaName: "a.png", contentHash: "h1", mimeType: "image/png", status: "queued",
                            model: "rapidocr-ppocrv6-small/1"),
                PictureText(blockId: "b2", mediaName: "b.png", contentHash: "h2", mimeType: "image/png", status: "queued",
                            model: "apple-vision/26", text: "Old reading of a whiteboard"),
                PictureText(blockId: "b3", mediaName: "c.pdf", contentHash: "h3", mimeType: "application/pdf", status: "failed",
                            error: "PdfiumError: Failed to load document (PDFium: Incorrect password error)."),
                PictureText(blockId: "b4", mediaName: "d.png", contentHash: "h4", mimeType: "image/png", status: "done",
                            method: "ocr", model: "apple-vision/26"),
                PictureText(blockId: "b5", mediaName: "e.png", contentHash: "h5", mimeType: "image/png", status: "done",
                            method: "ocr", model: "rapidocr-ppocrv6-small/1",
                            text: "Shopping list\nmilk\neggs\nbread\ncoffee beans\nbasil\nlemons\nolive oil\nparmesan", textChars: 2400),
            ]
            return ScrollView {
                PictureTextSection(pictures: pictures, reading: ["h2"], remaining: 1204)
                    .padding(16)
            }
            .environment(FindingFixtures.model())
            .tint(.fortressEmber)
            .background(Color(nsColor: .windowBackgroundColor))
        }
    }

    @Test func findingQuickCapture() async throws {
        try await both("finding-capture", size: CGSize(width: 720, height: 300)) {
            ZStack {
                DesktopBackdrop()
                QuickCaptureView(state: QuickCaptureState(text: "Call the dentist about the cleaning\nAsk about the Tuesday slot"))
                    .tint(.fortressEmber)
            }
        }
        try await both("finding-capture-saved", size: CGSize(width: 720, height: 300)) {
            ZStack {
                DesktopBackdrop()
                QuickCaptureView(state: QuickCaptureState(phase: .saved(title: "Call the dentist about the cleaning", folder: "Uncategorized")))
                    .tint(.fortressEmber)
            }
        }
    }
}
