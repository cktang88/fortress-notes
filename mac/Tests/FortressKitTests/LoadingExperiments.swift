import AppKit
import Testing

@testable import FortressKit

// EXPERIMENT (temporary): why typing is slower after a chunked load, and which loading
// strategy keeps both budgets. KEYSTROKE_PROFILE=1 only.
@MainActor
@Suite(.serialized, .enabled(if: ProcessInfo.processInfo.environment["KEYSTROKE_PROFILE"] == "1"))
struct LoadingExperiments {
    private func typeInMiddle(_ editor: PerfEditor, count: Int = 40) -> (median: Double, insert: Double) {
        let middle = editor.range(ofBlock: editor.middleParagraph)
        editor.controller.textView.setSelectedRange(NSRange(location: middle.location + 10, length: 0))
        var times: [Double] = [], inserts: [Double] = []
        for index in 0..<count {
            var insert = 0.0
            times.append(milliseconds {
                insert = milliseconds {
                    editor.controller.textView.insertText(index % 8 == 7 ? " " : "a",
                                                         replacementRange: editor.controller.textView.selectedRange())
                }
                editor.layoutViewport()
            })
            inserts.append(insert)
        }
        return (times.sorted()[times.count / 2], inserts.sorted()[inserts.count / 2])
    }

    private func line(_ round: Int, _ name: String, _ result: (median: Double, insert: Double), _ extra: String = "") {
        print(String(format: "EXP round %d %@ median %.2f insert %.2f %@", round, name, result.median, result.insert, extra))
    }

    private func laidOut(_ editor: PerfEditor) -> (all: Int, available: Int) {
        guard let manager = editor.controller.textView.textLayoutManager else { return (0, 0) }
        var all = 0, available = 0
        manager.enumerateTextLayoutFragments(from: manager.documentRange.location, options: []) { fragment in
            all += 1
            if fragment.state == .layoutAvailable { available += 1 }
            return true
        }
        return (all, available)
    }

    private func viewCount(_ editor: PerfEditor) -> Int {
        func count(_ view: NSView) -> Int { view.subviews.reduce(view.subviews.count) { $0 + count($1) } }
        return count(editor.controller.textView)
    }

    /// `settle`, keeping each turn's time and whether it finished loading.
    private func turns(_ editor: PerfEditor) -> [(ms: Double, pending: Int)] {
        var all: [(Double, Int)] = []
        repeat {
            let before = BlockEditorController.chunksBuilt
            let turn = milliseconds { _ = RunLoop.main.run(mode: .default, before: Date()) }
            // "pending" now: chunks built this turn, or -2 once loaded.
            all.append((turn, editor.controller.pendingBlocks.isEmpty ? -2 : BlockEditorController.chunksBuilt - before))
        } while !editor.controller.pendingBlocks.isEmpty && all.count < 1000
        all.append((milliseconds { _ = editor.session.base }, -1))
        return all
    }

    @Test func loadingVariants() {
        for round in 0..<3 {
            for (name, chunks, timer) in [("oldChunkedAppends", true, false), ("buildThenOneAppend", false, false), ("buildThenOneAppend+timer", false, true)] {
                BlockEditorController.appendsInChunks = chunks
                BlockEditorController.chainsWithTimer = timer
                defer { BlockEditorController.chainsWithTimer = false }
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                let all = turns(editor)
                let slowest = all.enumerated().sorted { $0.element.ms > $1.element.ms }.prefix(4)
                    .map { String(format: "#%d %.1f (chunks %d)", $0.offset, $0.element.ms, $0.element.pending) }.joined(separator: ", ")
                let result = typeInMiddle(editor)
                line(round, name, result, String(format: "longest %.1f turns %d total %.0f slowest %@", all.map(\.ms).max() ?? 0, all.count,
                                                  all.reduce(0) { $0 + $1.ms }, slowest))
            }
            BlockEditorController.appendsInChunks = false
            for variant in ["baseFirst", "baseFirst+noSpelling", "noSpelling"] {
                let editor = PerfEditor(document: LongNote.document())
                if variant.contains("noSpelling") { editor.controller.textView.isContinuousSpellCheckingEnabled = false }
                editor.open()
                var base = 0.0
                if variant.hasPrefix("baseFirst") { base = milliseconds { _ = editor.session.base } }
                let all = turns(editor)
                let slowest = all.enumerated().sorted { $0.element.ms > $1.element.ms }.prefix(4)
                    .map { String(format: "#%d %.1f (chunks %d)", $0.offset, $0.element.ms, $0.element.pending) }.joined(separator: ", ")
                print(String(format: "EXP round %d turn0 %@ base %.1f first %.1f slowest %@", round, variant, base, all.first?.ms ?? 0, slowest))
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                let finish = milliseconds { editor.controller.finishLoading() }
                editor.layoutViewport()
                line(round, "wholeAtOnce", typeInMiddle(editor), String(format: "finish %.1f", finish))
            }
        }
    }

    /// Typing in the paragraph after a picture, the picture on screen: how often its
    /// view is made, loaded and moved per key, with and without reuse.
    @Test func typingNextToPictures() {
        for round in 0..<3 {
            for reuse in [false, true] {
                BlockAttachment.reusesViews = reuse
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                editor.controller.finishLoading()
                editor.layoutViewport()
                let image = editor.range(ofBlock: "i50")
                let after = NSRange(location: NSMaxRange(editor.controller.storage.nsString.paragraphRange(at: image.location)) + 5, length: 0)
                editor.controller.textView.setSelectedRange(after)
                editor.controller.scrollRangeToVisibleForTests(after)
                editor.layoutViewport()
                editor.type("x")
                let made = BlockAttachment.viewsMade, loaded = BlockAttachment.viewsLoaded, moves = BlockAttachment.hostingMoves
                var times: [Double] = []
                for index in 0..<40 {
                    times.append(milliseconds { editor.type(index % 8 == 7 ? " " : "a") })
                }
                print(String(format: "EXP round %d pictures reuse %@ median %.2f perKey made %.2f loaded %.2f moves %.2f kinds %@", round,
                             reuse ? "yes" : "no", times.sorted()[20], Double(BlockAttachment.viewsMade - made) / 40,
                             Double(BlockAttachment.viewsLoaded - loaded) / 40, Double(BlockAttachment.hostingMoves - moves) / 40,
                             editor.viewportKinds))
            }
        }
        BlockAttachment.reusesViews = true
    }
}
