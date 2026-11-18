import Foundation
import Testing

@testable import FortressKit

extension FindingPerf {
@MainActor
@Suite struct PalettePerfTests {
    static let queries = ["k", "ky", "kyo", "kyot", "kyoto", "kyoto ", "kyoto t", "kyoto tr", "trip kyo", "zq", "旅", "cafe"]

    /// The old per-keystroke path: walk the tree, fold every title, rank, sort everything.
    @Test func baselineRankingPerKeystroke() {
        let navigation = PerfData.navigation(notes: 5000)
        var worst = 0.0
        for query in Self.queries.prefix(4) {
            let ms = bestMilliseconds(repeats: 1) {
                let notes = PaletteCatalog.notes(in: navigation)
                _ = PaletteCatalog.sections(for: query, notes: notes, recent: [])
            }
            worst = max(worst, ms)
        }
        perf("palette.uncachedIndex.worst(5000 notes)", worst)
    }

    /// What the palette does per keystroke with 5,000 notes: rank against the cached index.
    @Test func rankingPerKeystrokeWith5000Notes() {
        let navigation = PerfData.navigation(notes: 5000)
        let cache = PaletteCache()
        let build = bestMilliseconds(repeats: 1) { _ = cache.sections(for: "", navigation: navigation) }
        perf("palette.indexBuild(5000 notes)", build)
        var worst = 0.0
        var total = 0.0
        for (index, query) in Self.queries.enumerated() {
            // Fresh queries each time (no cached sections): append a counter-free variant
            // by alternating the cache with an empty query.
            let ms = bestMilliseconds(repeats: 3) {
                _ = cache.sections(for: "", navigation: navigation)
                _ = cache.sections(for: query, navigation: navigation)
            }
            perf("palette.keystroke[\(index):\(query)]", ms)
            worst = max(worst, ms)
            total += ms
        }
        perf("palette.keystroke.worst(5000 notes)", worst)
        perf("palette.keystroke.mean(5000 notes)", total / Double(Self.queries.count))
        #expect(cache.indexBuilds == 1)
        #expect(worst < 5)
    }
}
}

@MainActor
@Suite struct PaletteIndexTests {
    static let queries = FindingPerf.PalettePerfTests.queries

    /// Hovering or moving the selection re-renders the palette: nothing is ranked again,
    /// and a save (dates change, titles don't) doesn't rebuild the index.
    @Test func rerendersAndSavesDoNotRerank() {
        var navigation = PerfData.navigation(notes: 500)
        let cache = PaletteCache()
        let first = cache.sections(for: "kyo", navigation: navigation)
        for _ in 0..<10 { #expect(cache.sections(for: "kyo", navigation: navigation) == first) }
        #expect(cache.rankings == 1)
        if case .folder(var folder) = navigation.items[0], case .document(var note) = folder.children[0] {
            note.updatedAt = "2026-11-18T10:00:00Z"
            folder.children[0] = .document(note)
            navigation.items[0] = .folder(folder)
        }
        _ = cache.sections(for: "kyo", navigation: navigation)
        #expect(cache.indexBuilds == 1)
        #expect(cache.rankings == 1)
        // A renamed note does rebuild it.
        if case .folder(var folder) = navigation.items[0], case .document(var note) = folder.children[0] {
            note.title = "Kyoto renamed"
            folder.children[0] = .document(note)
            navigation.items[0] = .folder(folder)
        }
        let renamed = cache.sections(for: "kyo", navigation: navigation)
        #expect(cache.indexBuilds == 2)
        #expect(renamed.flatMap(\.items).contains { $0.title == "Kyoto renamed" })
    }

    /// The indexed ranking gives exactly what ranking from scratch (sorting every match) gives.
    @Test func indexedRankingMatchesFullSort() {
        let navigation = PerfData.navigation(notes: 800)
        let notes = PaletteCatalog.notes(in: navigation)
        let index = PaletteIndex(notes: notes, recent: [])
        for query in Self.queries + ["Packing", "notes 1", "ÉTÉ", "ka"] {
            let trimmed = query.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty else { continue }
            let full = PaletteCatalog.ranked(notes.compactMap { note -> PaletteItem? in
                guard let match = FuzzyMatch.match(trimmed, in: note.title) else { return nil }
                return PaletteItem(id: "note:\(note.id)", title: note.title, symbol: "", action: .openNote(note.id),
                                   matched: match.indices, score: match.score)
            }).prefix(PaletteCatalog.maxMatches)
            let indexed = PaletteCatalog.sections(for: query, index: index).first?.items
                .filter { if case .openNote = $0.action { true } else { false } } ?? []
            // Commands can take some of the 12 places; the notes shown keep the full order.
            #expect(Array(full.map(\.id).prefix(indexed.count)) == indexed.map(\.id), "\(query)")
            #expect(Array(full.map(\.matched).prefix(indexed.count)) == indexed.map(\.matched), "\(query)")
        }
    }
}
