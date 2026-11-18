import AppKit
import Testing

@testable import FortressKit

// Where a keystroke in the middle of a long note spends its time. Off unless
// KEYSTROKE_PROFILE=1 (a diagnostic, not a check): it compares the two ways a note's
// text can get into the view (in chunks over later run loop turns, as the app opens a
// long note, or all at once) and samples the process while typing, printing the
// heaviest frames ("PROFILE" lines).
@MainActor
@Suite(.serialized, .enabled(if: ProcessInfo.processInfo.environment["KEYSTROKE_PROFILE"] == "1"))
struct KeystrokeProfile {
    /// Median of `count` keystrokes (insert + viewport layout) in the middle paragraph.
    private func typeInMiddle(_ editor: PerfEditor, count: Int) -> (median: Double, insert: Double) {
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

    /// Spins the run loop for `seconds` (what the app does between keys: idle work such
    /// as spell checking and attachment loading gets its turn).
    private func idle(_ seconds: TimeInterval) {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end { _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.01)) }
    }

    @Test func chunkedAgainstWhole() {
        for round in 0..<3 {
            var results: [(String, (median: Double, insert: Double))] = []
            let chunked = PerfEditor(document: LongNote.document())
            chunked.open()
            chunked.settle()
            results.append(("chunked+settle", typeInMiddle(chunked, count: 40)))
            let whole = PerfEditor(document: LongNote.document())
            whole.open()
            whole.controller.finishLoading()
            results.append(("whole,noRunLoop", typeInMiddle(whole, count: 40)))
            let few = PerfEditor(document: LongNote.document())
            few.open()
            for _ in 0..<3 { _ = RunLoop.main.run(mode: .default, before: Date()) }
            few.controller.finishLoading()
            results.append(("chunked3+finish", typeInMiddle(few, count: 40)))
            let relaid = PerfEditor(document: LongNote.document())
            relaid.open()
            relaid.settle()
            if let manager = relaid.controller.textView.textLayoutManager {
                manager.invalidateLayout(for: manager.documentRange)
            }
            relaid.layoutViewport()
            results.append(("chunked+settle+invalidateLayout", typeInMiddle(relaid, count: 40)))
            let reset = PerfEditor(document: LongNote.document())
            reset.open()
            reset.settle()
            let copy = NSAttributedString(attributedString: reset.controller.storage)
            reset.controller.storage.setAttributedString(copy)
            reset.layoutViewport()
            results.append(("chunked+settle+setWhole", typeInMiddle(reset, count: 40)))
            let wholeIdle = PerfEditor(document: LongNote.document())
            wholeIdle.open()
            wholeIdle.controller.finishLoading()
            idle(1)
            results.append(("whole+idle1s", typeInMiddle(wholeIdle, count: 40)))
            let noSpelling = PerfEditor(document: LongNote.document())
            noSpelling.controller.textView.isContinuousSpellCheckingEnabled = false
            noSpelling.open()
            noSpelling.settle()
            idle(1)
            results.append(("chunked+settle+idle,noSpellcheck", typeInMiddle(noSpelling, count: 40)))
            let spelling = PerfEditor(document: LongNote.document())
            spelling.open()
            spelling.settle()
            idle(1)
            results.append(("chunked+settle+idle", typeInMiddle(spelling, count: 40)))
            for (name, result) in results {
                print(String(format: "PROFILE round %d %@ median %.2f insert %.2f", round, name, result.median, result.insert))
            }
        }
    }
}
