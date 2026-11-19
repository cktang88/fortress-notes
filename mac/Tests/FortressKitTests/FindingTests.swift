import Foundation
import Testing

@testable import FortressKit

// MARK: Highlights (UTF-16 offsets from the backend)

@Suite struct HighlightTests {
    private func highlighted(_ text: String, _ ranges: [[Int]]) -> [String] {
        PassageHighlights.ranges(in: text, utf16: ranges).map { String(text[$0]) }
    }

    @Test func plainASCII() {
        #expect(highlighted("We booked a ryokan in Gion.", [[12, 18]]) == ["ryokan"])
    }

    @Test func emojiBeforeTheMatchCountsAsTwo() {
        // "🌿" is 2 UTF-16 units: "ryokan" starts at UTF-16 offset 6, character offset 5.
        let text = "🌿 Our ryokan"
        #expect(highlighted(text, [[7, 13]]) == ["ryokan"])
    }

    @Test func precomposedAndDecomposedAccents() {
        #expect(highlighted("Un café à Kyoto", [[3, 7]]) == ["café"])
        // "cafe\u{301}": the backend widens the range over the combining accent.
        let decomposed = "Un cafe\u{301} ici"
        #expect(highlighted(decomposed, [[3, 8]]) == ["cafe\u{301}"])
        // Even if it didn't, we never cut a character in half.
        #expect(highlighted(decomposed, [[3, 7]]) == ["cafe\u{301}"])
    }

    @Test func rangeInsideASurrogatePairWidensToTheWholeEmoji() {
        let text = "a😀b"
        #expect(highlighted(text, [[2, 3]]) == ["😀"])
    }

    @Test func invalidOverlappingAndUnsortedRanges() {
        let text = "ryokan and onsen"
        #expect(highlighted(text, [[11, 16], [0, 6], [3, 2], [40, 50], [1], [2, 4]]) == ["ryokan", "onsen"])
        #expect(highlighted(text, [[-3, 2], [14, 99]]) == ["ry", "en"])
    }

    @Test func segmentsCoverTheWholePassage() {
        let text = "…the ryokan was quiet"
        let segments = PassageHighlights.segments(of: text, utf16: [[5, 11]])
        #expect(segments.map(\.text).joined() == text)
        #expect(segments.filter(\.isMatch).map { String($0.text) } == ["ryokan"])
    }
}

// MARK: Palette ranking

@Suite struct PaletteRankingTests {
    let notes = [
        PaletteNote(id: "1", title: "Kyoto trip", folder: "Travel"),
        PaletteNote(id: "2", title: "Packing list", folder: nil),
        PaletteNote(id: "3", title: "Travel ideas: ship", folder: nil),
        PaletteNote(id: "4", title: "Café list", folder: nil),
    ]

    private func titles(_ query: String) -> [String] {
        PaletteCatalog.sections(for: query, notes: notes, recent: []).first?.items.map(\.title) ?? []
    }

    @Test func commandContainingTheQueryBeatsScatteredLetters() {
        // "trash": the "Show Trash" command contains it; "Travel ideas: ship"
        // only has the letters scattered. The command must come first.
        let result = titles("trash")
        #expect(result.first == "Show Trash")
        #expect(result.contains("Travel ideas: ship"))
    }

    @Test func prefixBeatsWordStartBeatsInside() {
        let prefix = FuzzyMatch.match("kyo", in: "Kyoto trip")!.score
        let wordStart = FuzzyMatch.match("tri", in: "Kyoto trip")!.score
        let inside = FuzzyMatch.match("oto", in: "Kyoto trip")!.score
        let scattered = FuzzyMatch.match("kt", in: "Kyoto trip")!.score
        #expect(prefix > wordStart && wordStart > inside && inside > scattered)
    }

    @Test func matchesIgnoreCaseAndAccentsAndReportIndices() {
        let match = FuzzyMatch.match("cafe", in: "Café list")
        #expect(match?.indices == [0, 1, 2, 3])
        #expect(FuzzyMatch.match("zzz", in: "Kyoto trip") == nil)
        #expect(FuzzyMatch.match("trip kyo", in: "Kyoto trip")?.indices == [0, 1, 2, 6, 7, 8, 9])
    }

    @Test func keywordsFindCommands() {
        #expect(titles("deleted").first == "Show Trash")
    }

    @Test func fallbacksAndPrefixes() {
        let sections = PaletteCatalog.sections(for: "onsen", notes: notes, recent: [])
        let actions = sections.flatMap(\.items).map(\.action)
        #expect(actions == [.newNote(title: "onsen"), .search("onsen"), .ask("onsen")])
        #expect(PaletteCatalog.sections(for: "? where did we stay", notes: notes, recent: [])
            .flatMap(\.items).map(\.action) == [.ask("where did we stay")])
        let commandsOnly = PaletteCatalog.sections(for: ">list", notes: notes, recent: []).flatMap(\.items)
        #expect(commandsOnly.allSatisfy { if case .command = $0.action { true } else { false } })
    }

    @Test func emptyQueryShowsRecentThenSuggestions() {
        let sections = PaletteCatalog.sections(for: "", notes: notes, recent: Array(notes.prefix(2)))
        #expect(sections.map(\.title) == ["Recent", "Suggestions"])
        #expect(sections[0].items.map(\.title) == ["Kyoto trip", "Packing list"])
    }

    @Test func flattensTheSidebarTree() throws {
        let navigation = try JSONDecoder().decode(NavigationResponse.self, from: try findingFixture("navigation.json"))
        let notes = PaletteCatalog.notes(in: navigation)
        #expect(notes.contains(PaletteNote(id: "01M7NZXNDZHSR801AP2K15KMAQ", title: "Kyoto trip", folder: "Travel")))
        #expect(notes.contains { $0.title == "Packing list" && $0.folder == nil })
    }
}

// MARK: Search debounce and stale results

@MainActor
@Suite struct SearchControllerTests {
    private func settle(_ milliseconds: Int = 300) async {
        try? await Task.sleep(for: .milliseconds(milliseconds))
    }

    /// Waits (up to 60 s: snapshot tests may hold the main thread meanwhile) for a condition.
    private func wait(until condition: () -> Bool) async {
        let deadline = Date.now.addingTimeInterval(180)  // A hang guard (see `eventually`).
        while !condition(), Date.now < deadline {
            try? await Task.sleep(for: .milliseconds(10))
        }
    }

    @Test func debouncesTyping() async {
        let calls = Calls()
        let controller = SearchController(debounce: .milliseconds(80))
        for query in ["r", "ry", "ryo", "ryok"] {
            controller.update(query: query) { query in await calls.add(query); return [] }
        }
        await wait { controller.resultsQuery == "ryok" }
        await settle(100)
        #expect(await calls.queries == ["ryok"])
    }

    @Test func aSlowOlderAnswerNeverReplacesANewerOne() async {
        let controller = SearchController(debounce: .zero)
        let first = searchMatch("old")
        let second = searchMatch("new")
        controller.update(query: "ryo") { _ in
            try? await Task.sleep(for: .milliseconds(150))  // Not cancellable, like a slow server.
            return [first]
        }
        await settle(20)
        controller.update(query: "ryokan") { _ in [second] }
        await wait { controller.resultsQuery == "ryokan" }
        await settle(250)
        #expect(controller.results == [second])
        #expect(controller.resultsQuery == "ryokan")
        #expect(!controller.isStale)
    }

    @Test func enterNeverOpensStaleResults() async {
        let controller = SearchController(debounce: .milliseconds(40))
        controller.update(query: "kyo") { _ in [searchMatch("a", document: "old-doc")] }
        await wait { controller.resultsQuery == "kyo" }
        #expect(controller.selection == .result("a"))

        controller.update(query: "kyoto") { _ in
            try? await Task.sleep(for: .milliseconds(60))
            return [searchMatch("b", document: "new-doc")]
        }
        #expect(controller.isStale)
        let performed = Performed()
        controller.activate { performed.actions.append($0) }
        #expect(performed.actions.isEmpty)  // Waits instead of opening "kyo"'s result.
        await wait { !performed.actions.isEmpty }
        #expect(performed.actions == [.open(documentId: "new-doc", blockId: "b")])
    }

    @Test func askRowAndMovement() async {
        let controller = SearchController(debounce: .zero)
        controller.update(query: "  ryokan ") { _ in [searchMatch("a"), searchMatch("b")] }
        await wait { controller.resultsQuery == "ryokan" }
        #expect(controller.rows == [.ask, .result("a"), .result("b")])
        controller.move(by: 1)
        controller.move(by: 5)
        #expect(controller.selection == .result("b"))
        controller.move(by: -9)
        #expect(controller.selection == .ask)
        let performed = Performed()
        controller.activate { performed.actions.append($0) }
        #expect(performed.actions == [.ask(question: "ryokan")])
    }

    @Test func clearingTheQueryClearsResults() async {
        let controller = SearchController(debounce: .zero)
        controller.update(query: "ryokan") { _ in [searchMatch("a")] }
        await wait { controller.resultsQuery == "ryokan" }
        controller.update(query: "") { _ in [searchMatch("x")] }
        #expect(controller.results.isEmpty)
        #expect(controller.selection == .ask)
    }
}

private func searchMatch(_ id: String, document: String = "doc") -> SearchMatch {
    SearchMatch(blockId: id, documentId: document, documentTitle: "T", blockType: "paragraph",
                text: id, highlights: [], matched: ["words"], score: 1)
}

@MainActor
private final class Performed {
    var actions: [SearchAction] = []
}

private actor Calls {
    var queries: [String] = []
    func add(_ query: String) { queries.append(query) }
}

// MARK: Ask stream reduction

@Suite struct AskStateTests {
    private func events(_ name: String) throws -> [AskEvent] {
        let text = String(decoding: try findingFixture(name), as: UTF8.self)
        return try text.split(separator: "\n").compactMap { try APIClient.askEvent(from: Data($0.utf8)) }
    }

    @Test func answeredStreamFromTheRealBackend() throws {
        var state = AskState()
        let events = try events("ask-answered.ndjson")
        state.apply(events[0])
        #expect(state.steps.map(\.detail) == ["ryokan|hotel"])
        #expect(state.trace == "Searched for “ryokan|hotel”")
        state.apply(events[1])
        #expect(state.sentences.map(\.text) == ["You're staying at a ryokan in Gion."])
        #expect(state.isStreaming)
        state.apply(events[2])
        #expect(state.phase == .finished(.answered))
        #expect(state.elapsedMs == 812)
        #expect(state.sentences.count == 1)
        #expect(state.citations.map(\.documentTitle) == ["Kyoto trip"])
        #expect(state.number(of: state.citations[0]) == 1)
    }

    @Test func notConfiguredKeepsTheClosestPassages() throws {
        var state = AskState()
        for event in try events("ask.ndjson") { state.apply(event) }
        #expect(state.phase == .finished(.notConfigured))
        #expect(state.sources.map(\.text) == ["We booked a ryokan in Gion."])
        #expect(state.sentences.isEmpty)
    }

    @Test func citationsAreNumberedOncePerBlock() {
        let kyoto = AskCitation(blockId: "b1", documentId: "d1", documentTitle: "Kyoto trip", quote: "ryokan")
        let packing = AskCitation(blockId: "b2", documentId: "d2", documentTitle: "Packing list", quote: "booking")
        var state = AskState()
        state.apply(.sentence(AskSentence(text: "One.", citations: [kyoto])))
        state.apply(.sentence(AskSentence(text: "Two.", citations: [packing, kyoto])))
        #expect(state.citations.map(\.blockId) == ["b1", "b2"])
        #expect(state.number(of: packing) == 2)
        #expect(state.number(of: kyoto) == 1)
    }

    @Test func eventsAfterTheEndAreIgnoredAndBrokenStreamsSaySo() {
        var state = AskState()
        state.streamEnded()
        #expect(state.phase == .failed("The answer stopped before it finished."))
        state.apply(.sentence(AskSentence(text: "Late.", citations: [])))
        #expect(state.sentences.isEmpty)

        var partial = AskState()
        partial.apply(.sentence(AskSentence(text: "Kept.", citations: [])))
        partial.stop()
        partial.apply(.done(AskResponse(status: .answered, answer: [], steps: [], elapsedMs: 5)))
        #expect(partial.phase == .stopped)
        #expect(partial.sentences.map(\.text) == ["Kept."])
    }

    @Test func doneWithEmptyAnswerKeepsStreamedSentences() {
        var state = AskState()
        state.apply(.sentence(AskSentence(text: "Shown.", citations: [])))
        state.apply(.done(AskResponse(status: .answered, answer: [], steps: nil, elapsedMs: 900)))
        #expect(state.sentences.map(\.text) == ["Shown."])
    }

    @Test func citationLinksRoundTrip() throws {
        let url = try #require(URL(string: "fortress-cite:01M7NZXNDZHSR801AP2K15KMAQ/01M7NZXNE1DFCAQMW15HEBDN4H"))
        let target = try #require(AskTurnView.citation(from: url))
        #expect(target.documentId == "01M7NZXNDZHSR801AP2K15KMAQ")
        #expect(target.blockId == "01M7NZXNE1DFCAQMW15HEBDN4H")
    }
}

// MARK: Note info

@Test func noteInfoCountsWordsBlocksAndLinks() throws {
    let navigation = try JSONDecoder().decode(NavigationResponse.self, from: try findingFixture("navigation.json"))
    var info = NoteInfo.from(navigation: navigation, documentId: "01M7NZXNDZHSR801AP2K15KMAQ")
    #expect(info.folderPath == ["Travel"])
    #expect(info.createdAt != nil)
    info.add(try JSONDecoder().decode(BlockDocument.self, from: try findingFixture("document.json")))
    #expect((info.blocks ?? 0) >= 4)
    #expect((info.words ?? 0) > 8)
    #expect(NoteInfo.linkCount(in: .array([
        .object(["type": .string("text"), "text": .string("See ")]),
        .object(["type": .string("docLink"), "id": .string("01M7")]),
        .object(["type": .string("link"), "href": .string("https://x.y"), "content": .array([.object(["type": .string("text"), "text": .string("map")])])]),
    ])) == 2)
}

@MainActor @Test func noteInfoFromTheRealInfoEndpoint() throws {
    let imported = NoteInfo(try JSONDecoder().decode(DocumentInfo.self, from: try findingFixture("info-imported.json")))
    let provenance = try #require(imported.provenance)
    #expect(provenance.label == "Imported from OneNote")
    #expect(provenance.path == "Travel notebook › Japan › Kyoto")
    #expect(provenance.importer == "onenote 1.0")
    #expect(provenance.isNotable)
    #expect(imported.words == 14 && imported.blocks == 2)
    #expect(InfoSection.sourceDates(provenance)?.hasPrefix("Written ") == true)
    #expect(imported.mixedSources == nil)

    let manual = NoteInfo(try JSONDecoder().decode(DocumentInfo.self, from: try findingFixture("info.json")))
    #expect(manual.folderPath == ["Travel"])
    #expect(manual.provenance?.isNotable == false)
    #expect(manual.backlinksIn == 0)

    var mixed = manual
    mixed.blocksBySource = ["onenote_import": 12, "manual": 3]
    #expect(mixed.mixedSources == "12 from OneNote · 3 written here")
}

func findingFixture(_ name: String) throws -> Data {
    let url = try #require(Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}
