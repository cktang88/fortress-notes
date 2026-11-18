import AppKit
import Foundation
import SwiftUI
import Testing

@testable import FortressKit

// Request counts and timings for Finding, against `FindingStub` (a fake backend with
// latency). Each test prints "PERF <name>: <value>" lines, collected from the CI log.

enum PerfData {
    static let words = ["Kyoto", "trip", "packing", "list", "ryokan", "onsen", "café", "budget", "ideas", "meeting",
                        "notes", "garden", "recipe", "book", "東京", "旅館", "plan", "draft", "weekly", "review"]

    /// A sidebar with `count` notes in 50 folders (titles of 2–5 words, some accented or CJK).
    static func navigation(notes count: Int) -> NavigationResponse {
        var generator = SplitMix(seed: 42)
        var folders: [NavigationFolder] = (0..<50).map { index in
            NavigationFolder(id: "f\(index)", parentId: nil, name: "Folder \(index)", position: index, children: [])
        }
        var recent: [NavigationDocument] = []
        for index in 0..<count {
            let length = 2 + Int(generator.next() % 4)
            let title = (0..<length).map { _ in words[Int(generator.next() % UInt64(words.count))] }.joined(separator: " ")
            let document = NavigationDocument(
                id: String(format: "doc%05d", index), title: "\(title) \(index)", status: .rough,
                updatedAt: "2026-11-01T10:00:00Z", createdAt: "2026-10-01T10:00:00Z", snippet: "",
                folderId: "f\(index % 50)", position: index)
            folders[index % 50].children.append(.document(document))
            if index < 20 { recent.append(document) }
        }
        return NavigationResponse(items: folders.map(NavigationNode.folder), recent: recent)
    }

    static func passages(_ count: Int) -> [SearchMatch] {
        (0..<count).map { index in
            let text = "…stayed two nights at a small ryokan near Arashiyama 🌿 — the kaiseki dinner (\(index)) was the best meal of the trip, and the 旅館 onsen opened at six. Café au lait in the morning."
            return SearchMatch(
                blockId: "b\(index)", documentId: "d\(index % 40)", documentTitle: "Japan, spring \(index)", blockType: "paragraph",
                text: text, highlights: FindingFixtures.ranges(of: "ryokan", in: text) + FindingFixtures.ranges(of: "café", in: text),
                matched: ["words", "meaning"], score: 0.5)
        }
    }

    static func related(_ prefix: String, count: Int = 3) -> [RelatedResult] {
        (0..<count).map { index in
            RelatedResult(
                note: NoteSummary(id: "\(prefix)-n\(index)", title: "Related \(index)", status: .rough, updatedAt: "2026-11-01T10:00:00Z", snippet: "s"),
                score: 0.8, matchedBlockId: "\(prefix)-m\(index)", matchedBlockText: "Something like this \(index)")
        }
    }

    static func ndjson(_ object: [String: Any]) -> Data {
        var data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data()
        data.append(0x0A)
        return data
    }

    static func sentenceLine(_ index: Int) -> Data {
        ndjson(["type": "sentence", "sentence": [
            "text": "Sentence number \(index) says something about the ryokan in Gion and the onsen.",
            "citations": [
                ["block_id": "blk\(index % 37)", "document_id": "doc\(index % 11)", "document_title": "Kyoto \(index % 11)", "quote": "ryokan"],
                ["block_id": "blk\((index + 5) % 37)", "document_id": "doc\(index % 7)", "document_title": "Trip \(index % 7)", "quote": "onsen"],
            ],
        ]])
    }

    static func doneLine(answered: Bool = true) -> Data {
        ndjson(["type": "done", "result": ["status": answered ? "answered" : "not_found", "answer": [], "sources": [], "steps": [], "elapsed_ms": 900]])
    }
}

/// Deterministic random numbers for fixtures.
struct SplitMix {
    var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
}

/// Timing and request-count measurements. They need the machine to themselves (other
/// suites hold the main thread for seconds while they render), so they run only when
/// FINDING_PERF=1, one at a time, in their own CI step:
/// `FINDING_PERF=1 swift test --filter FindingPerf`.
@Suite(.serialized, .enabled(if: ProcessInfo.processInfo.environment["FINDING_PERF"] == "1"))
enum FindingPerf {}

func perf(_ name: String, _ value: Double, unit: String = "ms") {
    print("PERF \(name): \(String(format: "%.3f", value)) \(unit)")
}

func perf(_ name: String, count: Int) {
    print("PERF \(name): \(count)")
}

/// Hosts a view in an off-screen window so `.task`, `.onChange` and layout run as in the app.
@MainActor
final class Host<V: View> {
    let window: NSWindow
    let hosting: NSHostingView<V>

    init(_ view: V, size: CGSize = CGSize(width: 320, height: 700)) {
        hosting = NSHostingView(rootView: view)
        window = NSWindow(contentRect: CGRect(origin: CGPoint(x: -4000, y: -4000), size: size),
                          styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = hosting
        window.orderFrontRegardless()
        hosting.layoutSubtreeIfNeeded()
    }

    /// Lets SwiftUI and the main actor run for a while.
    func run(_ seconds: TimeInterval) async {
        let deadline = Date.now.addingTimeInterval(seconds)
        while Date.now < deadline {
            pumpRunLoop(0.005)
            try? await Task.sleep(for: .milliseconds(5))
        }
    }

    func close() { window.close() }
}

extension FindingPerf {
// MARK: Search typing

@MainActor
@Suite struct SearchRequestTests {
    /// Types like a fast typist (60 ms a key, faster than the debounce), with a trailing
    /// space and a backspace over a typo; the server takes 120 ms an answer.
    @Test func typingSendsOneRequestPerSettledQuery() async {
        let stub = FindingStub { _ in .json(PerfData.passages(5), delay: 0.12) }
        let controller = SearchController()
        let api = stub.api
        let keystrokes = ["r", "ry", "ryo", "ryok", "ryoka", "ryokan", "ryokan ", "ryokan g", "ryokan gi", "ryokan gix", "ryokan gi", "ryokan gio", "ryokan gion"]
        var last = ContinuousClock.now
        var longGaps = 0  // Pauses longer than the debounce (the test's sleeps can overshoot).
        for query in keystrokes {
            let now = ContinuousClock.now
            if now - last > .milliseconds(150) { longGaps += 1 }
            last = now
            controller.update(query: query) { try await api.search($0) }
            try? await Task.sleep(for: .milliseconds(60))
        }
        await eventually { controller.resultsQuery == "ryokan gion" }
        try? await Task.sleep(for: .milliseconds(300))
        let sent = stub.started.compactMap { URLComponents(url: $0, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "q" }?.value }
        print("PERF search.fastTyping.sent: \(sent) longGaps=\(longGaps)")
        perf("search.requests.fastTyping(13 keys)", count: stub.count("/api/unified-search"))
        perf("search.cancelled.fastTyping", count: stub.cancelledCount("/api/unified-search"))
        perf("search.store.requests.fastTyping", count: api.store.stats.requests)
        #expect(controller.results.count == 5)
        // "r" at once, then "ryokan gion" (plus one per pause longer than the debounce).
        #expect(stub.count("/api/unified-search") <= 2 + longGaps)
    }

    /// Slow typing (200 ms a key: every query settles) with a trailing space, a typo and
    /// backspaces back over it.
    @Test func slowTypingAndBackspacing() async {
        let stub = FindingStub { _ in .json(PerfData.passages(5), delay: 0.25) }
        let controller = SearchController()
        let api = stub.api
        let keystrokes = ["k", "ky", "kyo", "kyo ", "kyot", "kyoto", "kyotox", "kyoto", "kyot", "kyoto"]
        for query in keystrokes {
            controller.update(query: query) { try await api.search($0) }
            try? await Task.sleep(for: .milliseconds(200))
        }
        await eventually { controller.resultsQuery == "kyoto" && !controller.isSearching }
        try? await Task.sleep(for: .milliseconds(400))
        let sent = stub.started.compactMap { URLComponents(url: $0, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "q" }?.value }
        print("PERF search.slowTyping.sent: \(sent)")
        perf("search.requests.slowTyping(10 keys)", count: stub.count("/api/unified-search"))
        perf("search.cancelled.slowTyping", count: stub.cancelledCount("/api/unified-search"))
        #expect(controller.resultsQuery == "kyoto")
    }

    /// The results view goes away (search cleared) while a request is in flight.
    @Test func clearingCancelsTheRequestInFlight() async {
        let stub = FindingStub { _ in .json(PerfData.passages(5), delay: 0.5) }
        var controller: SearchController? = SearchController()
        let api = stub.api
        controller?.update(query: "ryokan") { try await api.search($0) }
        await eventually { stub.count("/api/unified-search") == 1 }
        controller?.update(query: "") { try await api.search($0) }
        controller = nil
        try? await Task.sleep(for: .milliseconds(700))
        perf("search.cancelledOnClear", count: stub.cancelledCount("/api/unified-search"))
        #expect(stub.cancelledCount("/api/unified-search") == 1)
    }
}

// MARK: Inspector: Seen before, related, backlinks, info

@MainActor
@Suite struct InspectorRequestTests {
    static func stub(latency: TimeInterval = 0.08) -> FindingStub {
        FindingStub { request in
            let url = request.url!
            let path = url.path()
            let block = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "block_id" }?.value
            // The note "other" never answers in time: it is always left before it does.
            let latency = path.contains("/other/") ? 30 : latency
            if path.hasSuffix("/related") { return .json(PerfData.related(block ?? "note"), delay: latency) }
            if path.hasSuffix("/backlinks") { return .json([Backlink](), delay: latency) }
            if path.hasSuffix("/info") { return Reply(delay: latency, body: FindingFixtures.info) }
            return .json([String]())
        }
    }

    typealias Reply = FindingStub.Reply

    static func seenRequests(_ stub: FindingStub) -> Int {
        stub.count("/related") { $0.queryItems?.contains { $0.name == "block_id" } == true }
    }

    @Test func caretMovingThroughBlocks() async {
        let stub = Self.stub()
        let model = AppModel(api: stub.api)
        model.connectsOnAppear = false
        model.navigation = FindingFixtures.navigation()
        model.route = .note(id: FindingFixtures.kyoto, focusBlockId: nil)
        let host = Host(InspectorView().environment(model))
        await host.run(0.6)
        let opening = stub.started.count

        // Arrow-key through 12 blocks, 50 ms a block, then rest.
        for index in 0..<12 {
            model.focusedBlockId = "block-\(index)"
            await host.run(0.05)
        }
        await host.run(0.8)
        let afterSweep = Self.seenRequests(stub)

        // Back and forth between two blocks that were each looked at (rests 600 ms).
        for id in ["block-3", "block-11", "block-3", "block-11"] {
            model.focusedBlockId = id
            await host.run(0.6)
        }
        let afterRevisits = Self.seenRequests(stub)

        // Switch notes and back (rests 700 ms).
        model.route = .note(id: FindingFixtures.packing, focusBlockId: nil)
        model.focusedBlockId = nil
        await host.run(0.7)
        let beforeReturn = stub.started.count
        model.route = .note(id: FindingFixtures.kyoto, focusBlockId: nil)
        await host.run(0.7)
        let onReturn = stub.started.count - beforeReturn
        host.close()

        perf("inspector.requests.opening", count: opening)
        perf("inspector.seenBefore.requests.sweep12", count: afterSweep)
        perf("inspector.seenBefore.requests.revisit4", count: afterRevisits - afterSweep)
        perf("inspector.requests.returnToNote", count: onReturn)
        perf("inspector.cancelled", count: stub.log.cancelled.count)
        #expect(afterSweep <= 1)
    }
}

// MARK: Ask streaming

@MainActor
@Suite struct AskStreamTests {
    /// The server finds sources (300 ms), then streams sentences 40 ms apart.
    @Test func timeToFirstSentence() async {
        let stub = FindingStub { _ in
            var chunks: [(delay: TimeInterval, data: Data)] = [
                (0.05, PerfData.ndjson(["type": "step", "step": ["action": "grep", "detail": "ryokan"]])),
                (0.25, PerfData.sentenceLine(0)),
            ]
            for index in 1..<6 { chunks.append((0.04, PerfData.sentenceLine(index))) }
            chunks.append((0.02, PerfData.doneLine()))
            return FindingStub.Reply(chunks: chunks)
        }
        var samples: [Double] = []
        for _ in 0..<3 {
            let turn = AskTurn(question: "Where are we staying?")
            let start = ContinuousClock.now
            turn.start(stub.api.ask(turn.question))
            await eventually { !turn.state.sentences.isEmpty }
            let elapsed = ContinuousClock.now - start
            samples.append(Double(elapsed.components.attoseconds) / 1e15 + Double(elapsed.components.seconds) * 1000)
            await eventually { !turn.state.isStreaming }
            #expect(turn.state.phase == .finished(.answered))
        }
        let best = samples.min() ?? 0
        perf("ask.timeToFirstSentence(server 300ms)", best)
        perf("ask.clientOverheadToFirstSentence", best - 300)
        #expect(best < 600)
    }

    /// The connection drops after two sentences.
    @Test func disconnectMidStreamKeepsWhatArrived() async {
        let stub = FindingStub { _ in
            FindingStub.Reply(chunks: [(0.02, PerfData.sentenceLine(0)), (0.02, PerfData.sentenceLine(1)), (0.05, Data())],
                              failure: .networkConnectionLost)
        }
        let turn = AskTurn(question: "q")
        turn.start(stub.api.ask("q"))
        await eventually { !turn.state.isStreaming }
        print("PERF ask.disconnect.phase: \(turn.state.phase) sentences=\(turn.state.sentences.count)")
        #expect(turn.state.sentences.count == 2)
        if case .failed = turn.state.phase {} else { Issue.record("expected a failure, got \(turn.state.phase)") }
    }

    /// One garbled line in the middle of the stream.
    @Test func aBadLineDoesNotEndTheAnswer() async {
        let stub = FindingStub { _ in
            FindingStub.Reply(chunks: [
                (0.01, PerfData.sentenceLine(0)), (0.01, Data("{\"type\": \"sentence\", \"sentence\": {\"te\n".utf8)),
                (0.01, PerfData.sentenceLine(1)), (0.01, PerfData.doneLine()),
            ])
        }
        let turn = AskTurn(question: "q")
        turn.start(stub.api.ask("q"))
        await eventually { !turn.state.isStreaming }
        print("PERF ask.badLine.phase: \(turn.state.phase) sentences=\(turn.state.sentences.count)")
        #expect(turn.state.phase == .finished(.answered))
    }

    /// A long answer (400 sentences, 2 citations each): the work the answer view does per
    /// streamed sentence (apply the event, then number every chip).
    @Test func longAnswerCostPerSentence() {
        let events: [AskEvent] = (0..<400).compactMap { try? APIClient.askEvent(from: PerfData.sentenceLine($0)) }
        var state = AskState()
        var worst = 0.0
        var total = 0.0
        for event in events {
            let ms = bestMilliseconds(repeats: 1) {
                state.apply(event)
                var sum = 0
                for sentence in state.sentences {
                    for citation in sentence.citations { sum &+= state.number(of: citation) ?? 0 }
                }
                _ = state.citations.count
                _ = sum
            }
            worst = max(worst, ms)
            total += ms
        }
        perf("ask.longAnswer.worstEvent(400 sentences)", worst)
        perf("ask.longAnswer.totalForStream", total)
    }
}

// MARK: Search results list

@MainActor
@Suite struct SearchListPerfTests {
    @Test func hundredPassagesRenderAndMove() async {
        let stub = FindingStub { _ in .json([SearchMatch]()) }
        let model = AppModel(api: stub.api)
        model.connectsOnAppear = false
        let controller = SearchController(debounce: .zero)
        let passages = PerfData.passages(100)
        controller.update(query: "ryokan") { _ in passages }
        await eventually { controller.resultsQuery == "ryokan" }
        let host = Host(SearchResultsView(controller: controller).environment(model), size: CGSize(width: 320, height: 2400))
        await host.run(0.3)

        let highlight = bestMilliseconds(repeats: 3) {
            for match in passages { _ = Text.highlighted(match.text, ranges: match.highlights) }
        }
        var moves: [Double] = []
        for _ in 0..<20 {
            moves.append(bestMilliseconds(repeats: 1) {
                controller.move(by: 1)
                RunLoop.main.run(until: .now)
                host.hosting.layoutSubtreeIfNeeded()
                host.window.displayIfNeeded()
            })
        }
        host.close()
        perf("searchList.highlightText(100 passages)", highlight)
        perf("searchList.selectionMove.median", moves.sorted()[moves.count / 2])
        perf("searchList.selectionMove.worst", moves.max() ?? 0)
    }
}
}
