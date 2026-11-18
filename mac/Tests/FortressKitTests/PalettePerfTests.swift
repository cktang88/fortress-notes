import Foundation
import Testing

@testable import FortressKit

extension FindingPerf {
@MainActor
@Suite struct PalettePerfTests {
    static let queries = ["k", "ky", "kyo", "kyot", "kyoto", "kyoto ", "kyoto t", "kyoto tr", "trip kyo", "zq", "旅", "cafe"]

    /// What the palette does per keystroke with 5,000 notes (the view's whole body path).
    @Test func rankingPerKeystrokeWith5000Notes() {
        let navigation = PerfData.navigation(notes: 5000)
        var worst = 0.0
        var total = 0.0
        for query in Self.queries {
            let ms = bestMilliseconds(repeats: 3) {
                let notes = PaletteCatalog.notes(in: navigation)
                let recent = navigation.recent.map { document in
                    PaletteNote(id: document.id, title: document.title, folder: notes.first { $0.id == document.id }?.folder)
                }
                _ = PaletteCatalog.sections(for: query, notes: notes, recent: recent)
            }
            perf("palette.keystroke[\(query)]", ms)
            worst = max(worst, ms)
            total += ms
        }
        perf("palette.keystroke.worst(5000 notes)", worst)
        perf("palette.keystroke.mean(5000 notes)", total / Double(Self.queries.count))
    }
}
}
