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

    @Test func chunkedAgainstWhole() {
        for round in 0..<3 {
            let chunked = PerfEditor(document: LongNote.document())
            chunked.open()
            chunked.settle()
            let a = typeInMiddle(chunked, count: 40)
            let whole = PerfEditor(document: LongNote.document())
            whole.open()
            whole.controller.finishLoading()
            let b = typeInMiddle(whole, count: 40)
            print(String(format: "PROFILE round %d chunked median %.2f (insert %.2f) whole median %.2f (insert %.2f)",
                         round, a.median, a.insert, b.median, b.insert))
        }
    }
}
