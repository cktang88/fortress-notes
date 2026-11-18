import AppKit
import Foundation
final class Flag: Sendable {
    private let value = Mutex(false)
    var isOn: Bool {
        get { value.withLock { $0 } }
        set { value.withLock { $0 = newValue } }
    }
}
import Testing

@testable import FortressKit

// One test per bug fixed in Finding; each failed before its fix.

// MARK: Search

@MainActor
@Suite(.serialized) struct SearchFixTests {
    /// A trailing space (or any edit that trims to the same words) used to cancel the
    /// request in flight and send it again.
    @Test func sameWordsInFlightAreNotSentAgain() async {
        let stub = FindingStub { _ in .json(PerfData.passages(2), delay: 0.2) }
        let api = stub.api
        let controller = SearchController(debounce: .zero)
        controller.update(query: "ryokan") { try await api.search($0) }
        await eventually { stub.count("/api/unified-search") == 1 }
        controller.update(query: "ryokan ") { try await api.search($0) }
        controller.update(query: " ryokan") { try await api.search($0) }
        await eventually { controller.resultsQuery == "ryokan" }
        #expect(stub.count("/api/unified-search") == 1)
        #expect(stub.cancelledCount("/api/unified-search") == 0)
    }

    /// Backspacing to words answered a moment ago shows them without asking again.
    @Test func backspacingReusesRecentAnswers() async {
        let stub = FindingStub { _ in .json(PerfData.passages(2)) }
        let api = stub.api
        let controller = SearchController(debounce: .zero)
        controller.update(query: "kyoto") { try await api.search($0) }
        await eventually { controller.resultsQuery == "kyoto" }
        controller.update(query: "kyotox") { try await api.search($0) }
        await eventually { controller.resultsQuery == "kyotox" }
        controller.update(query: "kyoto") { try await api.search($0) }
        #expect(controller.resultsQuery == "kyoto")  // At once.
        #expect(stub.count("/api/unified-search") == 2)
    }

    /// A query that failed is sent again when typed again; clearing forgets the error.
    @Test func errorsAreRetriedAndClearedWithTheQuery() async {
        let stub = FindingStub { _ in .offline() }
        let api = stub.api
        let controller = SearchController(debounce: .zero)
        controller.update(query: "ryokan") { try await api.search($0) }
        await eventually { controller.errorMessage != nil }
        controller.update(query: "") { try await api.search($0) }
        #expect(controller.errorMessage == nil)
        controller.update(query: "ryokan") { try await api.search($0) }
        await eventually { stub.count("/api/unified-search") == 2 }
        #expect(stub.count("/api/unified-search") == 2)
    }
}

// MARK: Highlights and folding beyond Latin

@Suite struct InternationalTextTests {
    private func highlighted(_ text: String, _ ranges: [[Int]]) -> [String] {
        PassageHighlights.ranges(in: text, utf16: ranges).map { String(text[$0]) }
    }

    @Test func cjkAndEmojiSequences() {
        let cjk = "京都の旅館に泊まった"
        #expect(highlighted(cjk, [[3, 5]]) == ["旅館"])
        // A family emoji is 8 UTF-16 units; a range ending inside it widens to all of it.
        let family = "👨‍👩‍👧 trip to 旅館"
        #expect(highlighted(family, [[0, 3]]) == ["👨‍👩‍👧"])
        #expect(highlighted(family, [[17, 19]]) == ["旅館"])
        // A flag is two regional indicators (4 units); half a flag is the whole flag.
        let flag = "🇯🇵 Japan"
        #expect(highlighted(flag, [[2, 4]]) == ["🇯🇵"])
        #expect(highlighted(flag, [[5, 10]]) == ["Japan"])
    }

    @Test func segmentsNeverSplitCharacters() {
        let text = "e\u{301}té à 東京 👍🏽 fin"
        for start in 0..<text.utf16.count {
            for end in (start + 1)...text.utf16.count {
                let segments = PassageHighlights.segments(of: text, utf16: [[start, end]])
                #expect(segments.map(\.text).joined() == text)
            }
        }
    }

    @Test func fuzzyMatchingFoldsAccentsAndKeepsCJK() {
        #expect(FuzzyMatch.match("ete", in: "Été à Kyoto")?.indices == [0, 1, 2])
        #expect(FuzzyMatch.match("旅館", in: "京都の旅館")?.indices == [3, 4])
        #expect(FuzzyMatch.match("ETE", in: "e\u{301}te\u{301}")?.indices == [0, 1, 2])
        #expect(FuzzyMatch.match("👍", in: "Ideas 👍🏽 later") == nil || FuzzyMatch.match("👍", in: "Ideas 👍🏽 later")?.indices == [6])
        let folded = FoldedText("Café 東京")
        #expect(folded.count == "Café 東京".count)
    }
}

// MARK: Ask

@MainActor
@Suite(.serialized) struct AskFixTests {
    /// Long answers: numbering is kept as sentences arrive and paragraphs close as they fill.
    @Test func paragraphsCloseAndCitationsNumberIncrementally() {
        var state = AskState()
        for index in 0..<60 {
            if let event = try? APIClient.askEvent(from: PerfData.sentenceLine(index)) { state.apply(event) }
        }
        #expect(state.paragraphs.count > 3)
        #expect(state.paragraphs.map(\.sentences.count).reduce(0, +) == 60)
        #expect(state.paragraphs.first?.sentences.lowerBound == 0)
        // Paragraph ranges tile the sentences.
        for (previous, next) in zip(state.paragraphs, state.paragraphs.dropFirst()) {
            #expect(previous.sentences.upperBound == next.sentences.lowerBound)
        }
        let numbers = state.citations.map { state.number(of: $0) }
        #expect(numbers == Array(1...state.citations.count).map(Optional.some))
        let closed = state.paragraphs.dropLast()
        state.apply(try! #require(APIClient.askEvent(from: PerfData.sentenceLine(60))))
        #expect(Array(state.paragraphs.prefix(closed.count)) == Array(closed))  // Append-only.
    }

    /// Asking something else (a follow-up, or leaving the page) stops the stream on the wire.
    @Test func stoppingCancelsTheRequest() async {
        let stub = FindingStub { _ in
            FindingStub.Reply(chunks: [(0.02, PerfData.sentenceLine(0)), (5, PerfData.doneLine())])
        }
        let turn = AskTurn(question: "q")
        turn.start(stub.api.ask("q"))
        await eventually { !turn.state.sentences.isEmpty }
        turn.stop()
        await eventually { stub.cancelledCount("/api/ask") == 1 }
        #expect(stub.cancelledCount("/api/ask") == 1)
        #expect(turn.state.phase == .stopped)
        #expect(turn.timeToFirstSentence != nil)
    }

    /// A stream that goes quiet ends with a message instead of spinning forever.
    @Test func aStalledStreamTimesOut() async {
        let stub = FindingStub { _ in
            FindingStub.Reply(chunks: [(0.02, PerfData.sentenceLine(0)), (20, PerfData.doneLine())])
        }
        let turn = AskTurn(question: "q")
        turn.stallTimeout = .milliseconds(400)
        turn.start(stub.api.ask("q"))
        await eventually(timeout: 10) { !turn.state.isStreaming }
        #expect(turn.state.phase == .failed("The answer stopped coming in."))
        #expect(turn.state.sentences.count == 1)
        await eventually { stub.cancelledCount("/api/ask") == 1 }
        #expect(stub.cancelledCount("/api/ask") == 1)
    }

    @Test func disconnectSaysTheAnswerWasCutOff() async {
        let stub = FindingStub { _ in
            FindingStub.Reply(chunks: [(0.02, PerfData.sentenceLine(0)), (0.05, Data())], failure: .networkConnectionLost)
        }
        let turn = AskTurn(question: "q")
        turn.start(stub.api.ask("q"))
        await eventually { !turn.state.isStreaming }
        #expect(turn.state.phase == .failed("The connection dropped before the answer finished."))
        #expect(turn.state.sentences.count == 1)
    }
}

// MARK: Inspector

@MainActor
@Suite(.serialized) struct InspectorModelTests {
    static let note = FindingFixtures.kyoto

    @Test func noteLoadsInParallelAndIsCachedPerRevision() async {
        let stub = InspectorRequestTests.stub(latency: 0.2)
        let inspector = InspectorModel(seenDebounce: .milliseconds(50), infoDebounce: .milliseconds(50))
        let start = ContinuousClock.now
        inspector.show(noteId: Self.note, revision: "r1", fallback: NoteInfo(), api: stub.api)
        await eventually { stub.log.finished.count >= 3 && !inspector.related.isEmpty }
        let elapsed = ContinuousClock.now - start
        perf("inspector.noteLoad(3 requests, 200 ms each)", Double(elapsed.components.attoseconds) / 1e15 + Double(elapsed.components.seconds) * 1000)
        #expect(elapsed < .milliseconds(550))  // In parallel: about one latency, not three.
        #expect(stub.started.count == 3)

        // Open another note, and come back before its answers arrive.
        inspector.show(noteId: "other", revision: "x", fallback: NoteInfo(), api: stub.api)
        #expect(inspector.related.isEmpty)
        await eventually { stub.started.count == 6 }
        inspector.show(noteId: Self.note, revision: "r1", fallback: NoteInfo(), api: stub.api)
        #expect(!inspector.related.isEmpty)  // From the cache, at once.
        try? await Task.sleep(for: .milliseconds(400))
        #expect(stub.started.count == 6)  // Nothing asked for Kyoto again…
        #expect(stub.log.cancelled.count == 3)  // …and "other"'s three requests were cancelled.
        #expect(inspector.related.first?.note.id == "note-n0")

        // An edit refreshes Info only.
        let before = stub.started.count
        inspector.show(noteId: Self.note, revision: "r2", fallback: NoteInfo(), api: stub.api)
        await eventually { stub.started.count > before }
        try? await Task.sleep(for: .milliseconds(300))
        #expect(stub.started.count == before + 1)
        #expect(stub.started.last?.path().hasSuffix("/info") == true)
    }

    @Test func seenBeforeDebouncesCancelsAndCachesPerBlockText() async {
        let stub = InspectorRequestTests.stub(latency: 0.05)
        let inspector = InspectorModel(seenDebounce: .milliseconds(150))
        inspector.show(noteId: Self.note, revision: "r1", fallback: NoteInfo(), api: stub.api)
        await eventually { !inspector.related.isEmpty }
        let seen = { InspectorRequestTests.seenRequests(stub) }

        for index in 0..<20 {  // The caret sweeps through 20 blocks, 20 ms each.
            inspector.focus(blockId: "b\(index)", version: 1, api: stub.api)
            try? await Task.sleep(for: .milliseconds(20))
        }
        await eventually { !inspector.seenBefore.isEmpty }
        #expect(seen() == 1)
        #expect(inspector.seenBefore.first?.note.id == "b19-n0")

        inspector.focus(blockId: "b3", version: 1, api: stub.api)
        try? await Task.sleep(for: .milliseconds(300))
        inspector.focus(blockId: "b19", version: 1, api: stub.api)  // Cached: shown at once.
        #expect(inspector.seenBefore.first?.note.id == "b19-n0")
        try? await Task.sleep(for: .milliseconds(300))
        #expect(seen() == 2)

        inspector.focus(blockId: "b19", version: 2, api: stub.api)  // Its text was saved again.
        try? await Task.sleep(for: .milliseconds(300))
        #expect(seen() == 3)
    }

    /// Caret moves cost the main thread almost nothing, even with requests in flight.
    @Test func caretMovesDoNotBlockTyping() async {
        let stub = InspectorRequestTests.stub(latency: 0.3)
        let inspector = InspectorModel()
        inspector.show(noteId: Self.note, revision: "r1", fallback: NoteInfo(), api: stub.api)
        let ms = bestMilliseconds(repeats: 1) {
            for index in 0..<2_000 { inspector.focus(blockId: "b\(index % 50)", version: index, api: stub.api) }
        }
        perf("inspector.focus.perCaretMove", ms / 2_000)
        #expect(ms / 2_000 < 0.5)
        inspector.focus(blockId: nil, version: nil, api: stub.api)
    }
}

// MARK: Quick Capture

@MainActor
@Suite(.serialized) struct QuickCaptureFixTests {
    static func tempFile() -> URL {
        FileManager.default.temporaryDirectory.appending(path: "outbox-\(UUID().uuidString).json")
    }

    static func drafts() -> CaptureDraftStore {
        CaptureDraftStore(defaults: UserDefaults(suiteName: "finding-tests-\(UUID().uuidString)")!)
    }

    static let saved = Data(#"{"document_id": "d1", "title": "Call the dentist", "folder": "Inbox", "folder_id": null}"#.utf8)

    @Test func offlineCapturesWaitOnDiskAndAreSentLater() async throws {
        let online = Flag()
        let stub = FindingStub { _ in online.isOn ? FindingStub.Reply(body: Self.saved) : .offline() }
        let file = Self.tempFile()
        let model = AppModel(api: stub.api)
        model.connectsOnAppear = false
        let capture = QuickCapture(outbox: CaptureOutbox(fileURL: file), drafts: Self.drafts())
        capture.outbox.backoff = [.milliseconds(100)]
        capture.attach(model)
        capture.state.text = "Call the dentist"
        await capture.save(andOpen: false)?.value
        #expect(capture.state.phase == .queued)
        #expect(capture.state.text.isEmpty)
        #expect(capture.outbox.items.map(\.text) == ["Call the dentist"])
        #expect(FileManager.default.fileExists(atPath: file.path()))
        // Survives a restart.
        #expect(CaptureOutbox(fileURL: file).items.map(\.text) == ["Call the dentist"])

        online.isOn = true
        await eventually { capture.outbox.items.isEmpty }
        #expect(capture.outbox.items.isEmpty)
        #expect(stub.count("/api/capture") >= 2)
        #expect(!FileManager.default.fileExists(atPath: file.path()))
    }

    @Test func escapeAndRefusalsKeepTheDraft() async {
        let stub = FindingStub { _ in FindingStub.Reply(status: 422, body: Data(#"{"detail": "Too long"}"#.utf8)) }
        let drafts = Self.drafts()
        let model = AppModel(api: stub.api)
        model.connectsOnAppear = false
        let capture = QuickCapture(outbox: CaptureOutbox(fileURL: nil), drafts: drafts)
        capture.attach(model)
        capture.state.text = "A thought"
        capture.hide()  // esc
        #expect(drafts.load() == "A thought")
        #expect(QuickCapture(outbox: CaptureOutbox(fileURL: nil), drafts: drafts).state.text == "A thought")
        await capture.save(andOpen: false)?.value
        #expect(capture.state.phase == .failed("Too long"))
        #expect(capture.state.text == "A thought")
        #expect(drafts.load() == "A thought")
    }

    @Test func shortcutConflictIsReportedAndRetriedAndSurvivesSleep() {
        let workspace = NotificationCenter()
        let app = NotificationCenter()
        var available = false
        var conflicts = 0
        let keeper = HotKeyKeeper(register: { available ? NSObject() : nil }, workspaceCenter: workspace, appCenter: app)
        keeper.onConflict = { conflicts += 1 }
        keeper.activate()
        #expect(keeper.status == .conflict)
        #expect(conflicts == 1)
        app.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        #expect(keeper.status == .conflict)
        #expect(conflicts == 1)  // Said once.

        available = true  // The other app quit.
        app.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        #expect(keeper.status == .registered)
        let registrations = keeper.registrations
        app.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        #expect(keeper.registrations == registrations)  // Already registered: nothing to do.

        workspace.post(name: NSWorkspace.didWakeNotification, object: nil)
        #expect(keeper.registrations == registrations + 1)
        #expect(keeper.status == .registered)
    }
}

final class Flag: Sendable {
    private let value = Mutex(false)
    var isOn: Bool {
        get { value.withLock { $0 } }
        set { value.withLock { $0 = newValue } }
    }
}
