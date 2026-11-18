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

    @Test func loadingVariants() {
        for round in 0..<3 {
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                let rest = editor.settle()
                let log = editor.controller.appendLog
                if round == 0 {
                    for (index, entry) in log.enumerated() {
                        print(String(format: "EXP chunk %d blocks %d chars %d build %.1f append %.1f", index, entry.blocks, entry.characters, entry.build, entry.append))
                    }
                    print("EXP settle slowest \(rest.slowest) turns \(rest.turns)")
                }
                let views = viewCount(editor)
                let result = typeInMiddle(editor)
                let fragments = laidOut(editor)
                line(round, "chunked", result, String(format: "longest %.1f appendTotal %.1f buildTotal %.1f views %d fragments %d/%d", rest.longest,
                                                       log.reduce(0) { $0 + $1.append }, log.reduce(0) { $0 + $1.build }, views, fragments.available, fragments.all))
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                let finish = milliseconds { editor.controller.finishLoading() }
                editor.layoutViewport()
                let views = viewCount(editor)
                let result = typeInMiddle(editor)
                let fragments = laidOut(editor)
                line(round, "whole", result, String(format: "finish %.1f views %d fragments %d/%d", finish, views, fragments.available, fragments.all))
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.controller.loadTuning.transaction = true
                editor.open()
                let rest = editor.settle()
                line(round, "chunked+transaction", typeInMiddle(editor), String(format: "longest %.1f", rest.longest))
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.controller.loadTuning.deferred = true
                editor.open()
                let rest = editor.settle()
                let last = editor.controller.appendLog.last?.append ?? 0
                if round == 0 { print("EXP deferred settle slowest \(rest.slowest) turns \(rest.turns)") }
                line(round, "deferredSingleAppend", typeInMiddle(editor), String(format: "longest %.1f finalAppend %.1f", rest.longest, last))
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                editor.settle()
                let storage = editor.controller.storage
                var time = 0.0
                if let contentStorage = editor.controller.textView.textContentStorage {
                    time = milliseconds {
                        let copy = NSAttributedString(attributedString: storage)
                        contentStorage.performEditingTransaction { storage.setAttributedString(copy) }
                    }
                }
                editor.layoutViewport()
                line(round, "chunked+setWholeInTransaction", typeInMiddle(editor), String(format: "reset %.1f", time))
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                editor.settle()
                let storage = editor.controller.storage
                var time = 0.0
                time = milliseconds {
                    let copy = NSAttributedString(attributedString: storage)
                    storage.setAttributedString(copy)
                }
                editor.layoutViewport()
                line(round, "chunked+setWhole", typeInMiddle(editor), String(format: "reset %.1f", time))
            }
            do {
                // Whole, then every fragment made (not laid out): do fragment objects cost?
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                editor.controller.finishLoading()
                editor.layoutViewport()
                let fragments = laidOut(editor)
                line(round, "whole+enumerateAll", typeInMiddle(editor), "fragments \(fragments.available)/\(fragments.all)")
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                editor.controller.loadTuning.maxHeight = 12_000
                editor.controller.loadTuning.maxCount = 320
                let rest = editor.settle()
                line(round, "chunked4x", typeInMiddle(editor), String(format: "longest %.1f turns %d", rest.longest, rest.turns))
            }
            do {
                // Text storage alone: 25 appends against one set (no view).
                let editor = PerfEditor(document: LongNote.document())
                let blocks = editor.session.initialBlocks
                let context = editor.controller.context
                let chunks = stride(from: 0, to: blocks.count, by: 80).map {
                    DocumentText.build(Array(blocks[$0..<min(blocks.count, $0 + 80)]), theme: EditorTheme(), context: context)
                }
                let appended = NSTextStorage()
                for chunk in chunks { appended.append(chunk) }
                let whole = NSTextStorage(attributedString: appended)
                func edits(_ storage: NSTextStorage) -> Double {
                    let at = storage.length / 2
                    return milliseconds {
                        for _ in 0..<200 {
                            storage.replaceCharacters(in: NSRange(location: at, length: 0), with: "a")
                            _ = storage.attribute(.fnBlock, at: at + 1000, effectiveRange: nil)
                        }
                    }
                }
                print(String(format: "EXP round %d bareStorage appended %.2f whole %.2f (200 edits)", round, edits(appended), edits(whole)))
            }
            do {
                let editor = PerfEditor(document: LongNote.document())
                editor.open()
                editor.settle()
                var time = 0.0
                if let manager = editor.controller.textView.textLayoutManager, let content = manager.textContentManager {
                    time = milliseconds { manager.replace(content) }
                }
                editor.layoutViewport()
                line(round, "chunked+replaceContentManager", typeInMiddle(editor), String(format: "reset %.1f", time))
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
