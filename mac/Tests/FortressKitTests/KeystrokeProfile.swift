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

    @Test func sampleTyping() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        editor.settle()
        let output = FileManager.default.temporaryDirectory.appendingPathComponent("keystroke-sample.txt")
        try? FileManager.default.removeItem(at: output)
        let sampler = Process()
        sampler.executableURL = URL(fileURLWithPath: "/usr/bin/sample")
        sampler.arguments = [String(getpid()), "6", "1", "-mayDie", "-file", output.path]
        try sampler.run()
        // `sample` needs a moment to attach.
        Thread.sleep(forTimeInterval: 1)
        let deadline = Date().addingTimeInterval(6)
        var typed = 0
        let middle = editor.range(ofBlock: editor.middleParagraph)
        editor.controller.textView.setSelectedRange(NSRange(location: middle.location + 10, length: 0))
        while Date() < deadline {
            editor.controller.textView.insertText(typed % 8 == 7 ? " " : "a",
                                                 replacementRange: editor.controller.textView.selectedRange())
            editor.layoutViewport()
            typed += 1
        }
        sampler.waitUntilExit()
        print("PROFILE typed \(typed) keystrokes in 6 s: \(String(format: "%.2f", 6000 / Double(max(typed, 1)))) ms each")
        let text = (try? String(contentsOf: output, encoding: .utf8)) ?? "no sample output"
        // The call graph of the main thread, trimmed, then the top-of-stack summary.
        for line in text.split(separator: "\n", omittingEmptySubsequences: false).prefix(1200) {
            print("PROFILE \(line)")
        }
        if let summary = text.range(of: "Sort by top of stack") {
            for line in text[summary.lowerBound...].split(separator: "\n").prefix(80) { print("PROFILE \(line)") }
        }
    }
}
