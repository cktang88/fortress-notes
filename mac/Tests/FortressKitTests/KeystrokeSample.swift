import AppKit
import Testing

@testable import FortressKit

// Samples the process (`/usr/bin/sample`) while typing in the middle of the long note, so
// two builds can be compared call by call. Off unless KEYSTROKE_PROFILE=1; writes the
// report to KEYSTROKE_SAMPLE_OUT. Uses only API every version of the perf suite has, so it
// can be dropped into an older checkout.
@MainActor
@Suite(.serialized, .enabled(if: ProcessInfo.processInfo.environment["KEYSTROKE_PROFILE"] == "1"))
struct KeystrokeSample {
    @Test(arguments: ["whole", "chunked"]) func sampleTyping(mode: String) throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        // (Finding a block loads the whole note first where loading is chunked.)
        if mode == "chunked" { editor.settle() }
        _ = editor.range(ofBlock: editor.middleParagraph)
        let base = ProcessInfo.processInfo.environment["KEYSTROKE_SAMPLE_OUT"]
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("keystroke-sample.txt").path
        let output = URL(fileURLWithPath: (base as NSString).deletingPathExtension + "-\(mode).txt")
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
        print("PROFILE \(mode) typed \(typed) keystrokes in 6 s: \(String(format: "%.2f", 6000 / Double(max(typed, 1)))) ms each")
    }

    /// Samples the run loop turns after an open (the rest of a long note loading).
    @Test func sampleSettle() throws {
        let base = ProcessInfo.processInfo.environment["KEYSTROKE_SAMPLE_OUT"]
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("keystroke-sample.txt").path
        let output = URL(fileURLWithPath: (base as NSString).deletingPathExtension + "-settle.txt")
        try? FileManager.default.removeItem(at: output)
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        let sampler = Process()
        sampler.executableURL = URL(fileURLWithPath: "/usr/bin/sample")
        sampler.arguments = [String(getpid()), "3", "1", "-mayDie", "-file", output.path]
        try sampler.run()
        Thread.sleep(forTimeInterval: 1)
        var first = 0.0
        var turns: [Double] = []
        repeat {
            turns.append(milliseconds { _ = RunLoop.main.run(mode: .default, before: Date()) })
            if turns.count == 1 { first = turns[0] }
        } while !editor.controller.pendingBlocks.isEmpty && turns.count < 1000
        sampler.waitUntilExit()
        print(String(format: "PROFILE settle first turn %.1f longest %.1f turns %d", first, turns.max() ?? 0, turns.count))
    }
}
