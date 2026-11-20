import AppKit
import CoreText
import Darwin
import Testing

@testable import FortressKit

// Performance of the editor with long, rich notes, timed with a real TextKit 2 text view
// on the CI runner. Every number is printed as a `PERF` line (and appended to
// $SNAPSHOT_DIR/perf.txt, so it lands in the artifact). Budgets (DESIGN c): open 150 ms,
// keystroke 8 ms.

@MainActor
enum PerfLog {
    static func record(_ name: String, _ value: Double, unit: String = "ms", budget: Double? = nil) {
        var line = String(format: "PERF %@ %.2f %@", name, value, unit)
        if let budget { line += String(format: " (budget %.0f %@)", budget, unit) }
        print(line)
        guard let directory = ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] else { return }
        try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        let url = URL(fileURLWithPath: directory).appendingPathComponent("perf.txt")
        let data = Data((line + "\n").utf8)
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: url)
        }
    }
}

/// Milliseconds spent in `body`.
@MainActor
func milliseconds(_ body: () throws -> Void) rethrows -> Double {
    let start = DispatchTime.now().uptimeNanoseconds
    try body()
    return Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
}

/// The process's physical footprint in MB (what Activity Monitor shows as Memory).
func footprintMB() -> Double {
    var usage = rusage_info_v4()
    let result = withUnsafeMutablePointer(to: &usage) { pointer in
        pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(getpid(), RUSAGE_INFO_V4, $0) }
    }
    return result == 0 ? Double(usage.ri_phys_footprint) / 1_048_576 : 0
}

/// A long, rich note: 2,000 blocks with deep nesting, 100 images, 20 tables of 50 × 8,
/// 10 column blocks and three 50,000-character paragraphs.
enum LongNote {
    static let documentId = "01PERFDOC0000000000000000"

    static func run(_ text: String, _ marks: [String: JSONValue] = [:]) -> JSONValue {
        var object: [String: JSONValue] = ["type": .string("text"), "text": .string(text)]
        if !marks.isEmpty { object["marks"] = .object(marks) }
        return .object(object)
    }

    static func node(_ id: String, _ type: String, attrs: [String: JSONValue] = [:], runs: [JSONValue]? = nil,
                     payload: JSONValue? = nil, children: [BlockNode] = []) -> BlockNode {
        BlockNode(id: id, documentId: documentId, type: type, attrs: attrs, content: runs.map(JSONValue.array) ?? .null,
                  payload: payload, text: runs.map { $0.map { BlockCodec.inlineText($0) }.joined() } ?? "",
                  createdAt: "2026-11-18T00:00:00+00:00", updatedAt: "2026-11-18T00:00:00+00:00", children: children)
    }

    static let sentence = "The quick brown fox jumps over the lazy dog while the editor keeps every block in step with the server. "

    static func prose(_ index: Int) -> [JSONValue] {
        [run("Block \(index): "), run(sentence), run("bold part", ["bold": .bool(true)]), run(" and "),
         .object(["type": .string("link"), "href": .string("https://example.com/\(index)"), "content": .array([run("a link")])]),
         run(" then " + sentence)]
    }

    static func table(_ index: Int, rows: Int = 50, columns: Int = 8) -> BlockNode {
        node("t\(index)", "table", attrs: ["borders": .bool(true), "header_rows": .number(1)], children: (0..<rows).map { r in
            node("t\(index)r\(r)", "table_row", children: (0..<columns).map { c in
                node("t\(index)r\(r)c\(c)", "table_cell", children: [
                    node("t\(index)r\(r)c\(c)p", "paragraph", runs: [run(r == 0 ? "Column \(c)" : "R\(r)C\(c) value")]),
                ])
            })
        })
    }

    static func columns(_ index: Int) -> BlockNode {
        let columns = (0..<2).map { c in
            node("c\(index)-\(c)", "column", attrs: ["width": .number(0.5)], children: (0..<5).map { p in
                node("c\(index)-\(c)-\(p)", p == 0 ? "heading" : "paragraph", attrs: p == 0 ? ["level": .number(3)] : [:],
                     runs: [run("Column \(c) paragraph \(p) with a few words in it.")])
            })
        }
        return node("k\(index)", "columns", children: columns)
    }

    static func image(_ index: Int, url: String) -> BlockNode {
        node("i\(index)", "image", attrs: ["url": .string(url), "name": .string("photo-\(index).jpg"),
                                           "width": .number(3000), "height": .number(2000),
                                           "caption": .string("Picture \(index)")])
    }

    /// `count` top-level-equivalent blocks (nested ones count too).
    static func document(blocks count: Int = 2000, imageURL: (Int) -> String = { "/media/photo-\($0).jpg" }) -> BlockDocument {
        var nodes: [BlockNode] = []
        var made = 0
        var slot = 0
        var images = 0, tables = 0, columnBlocks = 0, longParagraphs = 0
        while made < count {
            let i = slot
            slot += 1
            if i % 17 == 10, images < 100 {
                nodes.append(image(images, url: imageURL(images))); images += 1; made += 1
            } else if i % 85 == 55, tables < 20 {
                nodes.append(table(tables)); tables += 1; made += 1
            } else if i % 170 == 77, columnBlocks < 10 {
                nodes.append(columns(columnBlocks)); columnBlocks += 1; made += 1
            } else if i % 500 == 300, longParagraphs < 3 {
                let long = String(repeating: sentence, count: 50_000 / sentence.count + 1).prefix(50_000)
                nodes.append(node("l\(longParagraphs)", "paragraph", runs: [run(String(long))])); longParagraphs += 1; made += 1
            } else if i % 50 == 0 {
                nodes.append(node("h\(i)", "heading", attrs: ["level": .number(2)], runs: [run("Section \(i / 50)")])); made += 1
            } else if i % 30 == 1 {
                // A chain nested six deep.
                var chain: BlockNode?
                for depth in (0..<6).reversed() {
                    chain = node("n\(i)-\(depth)", "bullet_item", runs: prose(i + depth), children: chain.map { [$0] } ?? [])
                }
                nodes.append(chain!); made += 6
            } else {
                let types = ["paragraph", "paragraph", "paragraph", "numbered_item", "check_item", "quote", "bullet_item"]
                let type = types[i % types.count]
                if type == "quote" {
                    nodes.append(node("q\(i)", "quote", children: [node("p\(i)", "paragraph", runs: prose(i))]))
                } else {
                    nodes.append(node("p\(i)", type, attrs: type == "check_item" ? ["checked": .bool(i % 2 == 0)] : [:], runs: prose(i)))
                }
                made += 1
            }
        }
        return BlockDocument(id: documentId, revision: 1, titleAuto: true, canUndo: false, canRedo: false, title: "Long note",
                             status: .rough, createdAt: "2026-11-18T00:00:00+00:00", updatedAt: "2026-11-18T00:00:00+00:00",
                             children: nodes)
    }
}

/// An editor in a window at a typical size, the way the app shows it.
@MainActor
final class PerfEditor {
    let controller: BlockEditorController
    let window: NSWindow
    let session: EditorSession

    /// `decoded`: the blocks as `EditorSession.load` decodes them (off the main thread).
    init(document: BlockDocument, decoded: [EditorBlock]? = nil, baseURL: URL = URL(string: "http://127.0.0.1:9")!) {
        let api = APIClient(baseURL: baseURL)
        session = decoded.map { EditorSession(document: document, decoded: $0, api: api) } ?? EditorSession(document: document, api: api)
        controller = BlockEditorController(baseURL: baseURL)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 800), styleMask: [.titled, .resizable],
                          backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = controller.scrollView
        controller.scrollView.frame = window.contentView?.bounds ?? .zero
    }

    /// What opening a note does on the main thread: blocks → attributed text (the first
    /// screens of a long note) → viewport layout. The rest of a long note and the save
    /// baseline follow on later run loop turns (`settle`).
    func open() {
        controller.load(session.initialBlocks)
        session.resetBase(to: controller.currentBlocks())
        let controller = controller
        let session = session
        session.currentBlocks = { controller.currentBlocks() }
        controller.onChange = {
            session.editorDidChange(leadingTexts: { controller.leadingTexts() })
        }
        layoutViewport()
    }

    /// Lets the run loop do what follows an open (the rest of a long note goes in, the
    /// baseline is made), timing each turn: the longest is the longest the app can't
    /// answer a key while a note finishes loading.
    ///
    /// `first` is the first turn on its own: what follows any open (AppKit's continuous
    /// spell check of the first screens, the save baseline, the window's first layout),
    /// long note or not; `longestAfterFirst` is the longest of the others (loading the rest).
    @discardableResult
    func settle() -> (total: Double, longest: Double, turns: Int, slowest: [Double], first: Double, longestAfterFirst: Double) {
        var total = 0.0, turns = 0
        var all: [Double] = []
        repeat {
            let turn = milliseconds { _ = RunLoop.main.run(mode: .default, before: Date()) }
            total += turn
            all.append(turn)
            turns += 1
        } while !controller.pendingBlocks.isEmpty && turns < 1000
        let baseline = milliseconds { _ = session.base }
        total += baseline
        all.append(baseline)
        let slowest = Array(all.sorted(by: >).prefix(3))
        return (total, slowest.first ?? 0, turns, slowest, all.first ?? 0, all.dropFirst().max() ?? 0)
    }

    func layoutViewport() {
        controller.scrollView.layoutSubtreeIfNeeded()
        controller.textView.textLayoutManager?.textViewportLayoutController.layoutViewport()
    }

    /// Paragraph fragments with real (not estimated) layout.
    var laidOutFragments: Int {
        guard let manager = controller.textView.textLayoutManager else { return 0 }
        var count = 0
        manager.enumerateTextLayoutFragments(from: manager.documentRange.location, options: []) { fragment in
            if fragment.state == .layoutAvailable { count += 1 }
            return true
        }
        return count
    }

    func range(ofBlock id: String) -> NSRange { controller.range(ofBlock: id)! }

    /// The kinds of the blocks in the viewport (for timing reports).
    var viewportKinds: String {
        guard let manager = controller.textView.textLayoutManager, let contentManager = manager.textContentManager,
              let viewport = manager.textViewportLayoutController.viewportRange else { return "?" }
        let start = contentManager.offset(from: contentManager.documentRange.location, to: viewport.location)
        let end = contentManager.offset(from: contentManager.documentRange.location, to: viewport.endLocation)
        var kinds: [String] = []
        controller.storage.enumerateAttribute(.fnBlock, in: NSRange(location: start, length: max(0, end - start)), options: []) { value, _, _ in
            if let kind = (value as? BlockInfo)?.kind.rawValue, kinds.last != kind { kinds.append(kind) }
        }
        return kinds.joined(separator: ",")
    }

    /// A plain paragraph about halfway down the note.
    var middleParagraph: String {
        let blocks = session.initialBlocks
        return blocks[(blocks.count / 2)...].first { $0.kind == .paragraph && $0.id.hasPrefix("p") }!.id
    }

    func scroll(to y: CGFloat) {
        controller.scrollView.contentView.scroll(to: NSPoint(x: 0, y: y))
        controller.scrollView.reflectScrolledClipView(controller.scrollView.contentView)
        layoutViewport()
    }

    /// One keystroke the way AppKit delivers it, plus the viewport relayout it causes.
    func type(_ text: String) {
        controller.textView.insertText(text, replacementRange: controller.textView.selectedRange())
        layoutViewport()
    }
}

@MainActor
@Suite(.serialized) struct EditorPerformanceTests {
    @Test func openLongRichNote() throws {
        let document = LongNote.document()
        let before = footprintMB()
        var editor: PerfEditor!
        var blocks: [EditorBlock] = []
        // Decoding runs off the main thread (EditorSession.decode); timed on its own.
        let decode = milliseconds { blocks = BlockCodec.flatten(document.children, resolved: document.resolvedLinks) }
        // The app has its window and an editor before a note opens: make one first, so
        // AppKit's one-time setup (TextKit, fonts) isn't counted as opening this note.
        let processWarmUp = milliseconds {
            let empty = PerfEditor(document: LongNote.document(blocks: 3))
            empty.open()
        }
        PerfLog.record("open.processWarmUp(empty editor, once per launch)", processWarmUp)
        var stages: [(String, Double)] = []
        let open = milliseconds {
            stages.append(("makeEditor", milliseconds { editor = PerfEditor(document: document, decoded: blocks) }))
            stages.append(("loadText", milliseconds { editor.controller.load(editor.session.initialBlocks) }))
            stages.append(("readBack", milliseconds { editor.session.resetBase(to: editor.controller.currentBlocks()) }))
            stages.append(("layout", milliseconds { editor.layoutViewport() }))
        }
        for (name, time) in stages { PerfLog.record("open.firstPaint.part.\(name)", time) }
        let controller = editor.controller
        let session = editor.session
        session.currentBlocks = { controller.currentBlocks() }
        PerfLog.record("open.decode(off main)", decode)
        PerfLog.record("open.firstPaint(2000 blocks, 100 images, 20 tables 50x8, 10 columns, 3x50k)", open, budget: 150)
        PerfLog.record("open.firstPaint+decode", open + decode, budget: 150)
        let laidOut = editor.laidOutFragments
        let shown = editor.controller.storage.length
        let rest = editor.settle()
        PerfLog.record("open.rest.total(later turns)", rest.total)
        PerfLog.record("open.rest.longestTurn", rest.longest, budget: 50)
        PerfLog.record("open.rest.firstTurn(spell check, baseline, first layout)", rest.first)
        PerfLog.record("open.rest.longestTurnAfterFirst(loading the rest)", rest.longestAfterFirst, budget: 50)
        PerfLog.record("open.rest.turns", Double(rest.turns), unit: "turns")
        PerfLog.record("open.rest.slowestTurns[\(rest.slowest.map { String(format: "%.0f", $0) }.joined(separator: ","))]", rest.slowest.dropFirst().first ?? 0)
        PerfLog.record("open.memory", footprintMB() - before, unit: "MB")
        PerfLog.record("open.firstPaint.characters(of \(editor.controller.storage.length))", Double(shown), unit: "chars")
        // Where opening the whole text at once would spend its time.
        let parts = PerfEditor(document: document)
        let context = parts.controller.context
        var text = NSMutableAttributedString()
        PerfLog.record("open.part.flatten", milliseconds { _ = BlockCodec.flatten(document.children, resolved: document.resolvedLinks) })
        PerfLog.record("open.part.buildAll", milliseconds { text = DocumentText.build(blocks, theme: EditorTheme(), context: context) })
        PerfLog.record("open.part.storageAll", milliseconds { parts.controller.storage.setAttributedString(text) })
        PerfLog.record("open.part.layout", milliseconds { parts.layoutViewport() })
        let plain = NSTextStorage()
        PerfLog.record("open.part.storageWithoutView", milliseconds { plain.setAttributedString(text) })
        PerfLog.record("open.part.baseline(2000)", milliseconds { _ = SyncedTree(blocks, reusing: nil, lazily: true) })
        let columnBlocks = blocks.filter { $0.kind == .columns }
        PerfLog.record("open.part.buildColumns(\(columnBlocks.count))", milliseconds { _ = DocumentText.build(columnBlocks, theme: EditorTheme(), context: context) })
        let paragraphs = blocks.count
        PerfLog.record("open.laidOutFragments(of \(paragraphs))", Double(laidOut), unit: "fragments")
        #expect(editor.controller.storage.length > 150_000)
        #expect(editor.controller.currentBlocks().map(\.id) == blocks.map(\.id))
        // TextKit 2 lays out the viewport, not the whole note.
        #expect(laidOut < paragraphs / 4, "full layout forced: \(laidOut) of \(paragraphs)")
        // Opening and loading the rest changes nothing to save.
        #expect(editor.session.pendingOperationsForTests().isEmpty)

        // A second open of the same note (caches warm).
        let again = milliseconds {
            let other = PerfEditor(document: document, decoded: blocks)
            other.open()
        }
        PerfLog.record("open.warm(firstPaint, second note in the process)", again, budget: 150)
    }

    @Test func keystrokeLatency() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        editor.settle()
        // A paragraph in the middle of the note.
        let middleId = editor.middleParagraph
        let middle = editor.range(ofBlock: middleId)
        editor.controller.textView.setSelectedRange(NSRange(location: middle.location + 10, length: 0))
        var times: [Double] = []
        var inserts: [Double] = []
        var normalizes: [Double] = []
        for index in 0..<40 {
            EditorMetrics.reset()
            var insert = 0.0
            times.append(milliseconds {
                insert = milliseconds { editor.controller.textView.insertText(index % 8 == 7 ? " " : "a", replacementRange: editor.controller.textView.selectedRange()) }
                editor.layoutViewport()
            })
            inserts.append(insert)
            normalizes.append(EditorMetrics.normalize)
        }
        let sorted = times.sorted()
        PerfLog.record("keystroke.middle.median", sorted[sorted.count / 2], budget: 8)
        PerfLog.record("keystroke.middle.part.insert.median", inserts.sorted()[inserts.count / 2])
        PerfLog.record("keystroke.middle.part.normalize.median", normalizes.sorted()[normalizes.count / 2])
        PerfLog.record("keystroke.middle.max", sorted.last!)

        // Inside a 50,000-character paragraph.
        let long = editor.range(ofBlock: "l0")
        editor.controller.textView.setSelectedRange(NSRange(location: long.location + 25_000, length: 0))
        editor.controller.scrollRangeToVisibleForTests(NSRange(location: long.location + 25_000, length: 0))
        editor.layoutViewport()
        times = []
        var longInserts: [Double] = [], longLayouts: [Double] = [], longNormalizes: [Double] = [], longRestyles: [Double] = []
        var longCallbacks: [Double] = []
        for _ in 0..<20 {
            EditorMetrics.reset()
            var insert = 0.0, layout = 0.0
            times.append(milliseconds {
                insert = milliseconds {
                    editor.controller.textView.insertText("b", replacementRange: editor.controller.textView.selectedRange())
                }
                layout = milliseconds { editor.layoutViewport() }
            })
            longInserts.append(insert)
            longLayouts.append(layout)
            longNormalizes.append(EditorMetrics.normalize)
            longRestyles.append(EditorMetrics.restyle)
            longCallbacks.append(EditorMetrics.callbacks)
        }
        let longSorted = times.sorted()
        PerfLog.record("keystroke.long50k.median", longSorted[longSorted.count / 2], budget: 8)
        PerfLog.record("keystroke.long50k.max", longSorted.last!)
        PerfLog.record("keystroke.long50k.part.insert.median", longInserts.sorted()[10])
        PerfLog.record("keystroke.long50k.part.layout.median", longLayouts.sorted()[10])
        PerfLog.record("keystroke.long50k.part.normalize.median", longNormalizes.sorted()[10])
        PerfLog.record("keystroke.long50k.part.restyle.median", longRestyles.sorted()[10])
        PerfLog.record("keystroke.long50k.part.callbacks.median", longCallbacks.sorted()[10])
        // Laying the long paragraph out again with no edit (TextKit 2 lays out a whole
        // paragraph per fragment).
        if let manager = editor.controller.textView.textLayoutManager,
           let range = editor.controller.textRange(for: editor.range(ofBlock: "l0")) {
            var relayouts: [Double] = []
            for _ in 0..<5 {
                relayouts.append(milliseconds {
                    manager.invalidateLayout(for: range)
                    editor.layoutViewport()
                })
            }
            PerfLog.record("keystroke.long50k.part.relayoutOnly.median", relayouts.sorted()[2])
        }

        // What long50k measured before the caret fix (UNDO_AUDIT.md): the first key went
        // into the long paragraph and moved the caret past it, so the rest were typed at
        // the start of the paragraph after it. Kept to compare like with like.
        editor.controller.textView.setSelectedRange(NSRange(location: NSMaxRange(editor.range(ofBlock: "l0")), length: 0))
        editor.layoutViewport()
        times = []
        for _ in 0..<20 { times.append(milliseconds { editor.type("b") }) }
        PerfLog.record("keystroke.afterLong50k.median", times.sorted()[times.count / 2])

        // Return (splits a block: structure changes).
        editor.controller.textView.setSelectedRange(NSRange(location: editor.range(ofBlock: middleId).location + 5, length: 0))
        let newline = milliseconds {
            editor.controller.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
            editor.layoutViewport()
        }
        PerfLog.record("keystroke.return", newline, budget: 8)
    }

    /// Where laying out a 50,000-character paragraph spends its time (diagnostic numbers
    /// for the long-paragraph keystroke): CoreText alone, a bare TextKit 2 stack, and the
    /// line break strategies.
    @Test func longParagraphLayoutCosts() throws {
        let long = String(String(repeating: LongNote.sentence, count: 50_000 / LongNote.sentence.count + 1).prefix(50_000))
        let font = EditorTheme().bodyFont()
        let width: CGFloat = 650
        func median(_ body: () -> Void) -> Double {
            var samples: [Double] = []
            for _ in 0..<5 { samples.append(milliseconds(body)) }
            return samples.sorted()[2]
        }
        // CoreText: break and make every line.
        let plain = NSAttributedString(string: long, attributes: [.font: font])
        PerfLog.record("diag.long50k.coreTextLines", median {
            let typesetter = CTTypesetterCreateWithAttributedString(plain)
            var start = 0
            while start < plain.length {
                let count = CTTypesetterSuggestLineBreak(typesetter, start, Double(width))
                _ = CTTypesetterCreateLine(typesetter, CFRange(location: start, length: count))
                start += max(1, count)
            }
        })
        func textKit(_ strategy: NSParagraphStyle.LineBreakStrategy?) -> Double {
            let style = NSMutableParagraphStyle()
            if let strategy { style.lineBreakStrategy = strategy }
            let text = NSAttributedString(string: long, attributes: [.font: font, .paragraphStyle: style])
            let content = NSTextContentStorage()
            let manager = NSTextLayoutManager()
            content.addTextLayoutManager(manager)
            let container = NSTextContainer(size: CGSize(width: width, height: 0))
            container.lineFragmentPadding = 0
            manager.textContainer = container
            content.textStorage?.setAttributedString(text)
            manager.ensureLayout(for: manager.documentRange)
            return median {
                manager.invalidateLayout(for: manager.documentRange)
                manager.ensureLayout(for: manager.documentRange)
            }
        }
        PerfLog.record("diag.long50k.textKit.defaultStrategy", textKit(nil))
        PerfLog.record("diag.long50k.textKit.noStrategy", textKit([]))
        PerfLog.record("diag.long50k.textKit.standardStrategy", textKit(.standard))
        PerfLog.record("diag.long50k.defaultStrategyRaw", Double(NSMutableParagraphStyle().lineBreakStrategy.rawValue), unit: "")
        PerfLog.record("diag.themeStrategyRaw", Double(EditorTheme().paragraphStyle(for: BlockInfo()).lineBreakStrategy.rawValue), unit: "")
    }

    @Test func richBlockLayout() throws {
        let document = LongNote.document()
        let editor = PerfEditor(document: document)
        editor.open()
        editor.settle()
        func live(_ id: String) -> (any LiveBlock)? {
            (editor.controller.storage.attribute(.attachment, at: editor.range(ofBlock: id).location, effectiveRange: nil) as? BlockAttachment)?.live
        }
        let table = try #require(live("t3"))
        if let grid = live("t4") as? TableBlockController {
            PerfLog.record("layout.table50x8.buildCells", milliseconds { _ = grid.text(row: 0, column: 0) })
            PerfLog.record("layout.table50x8.measure", milliseconds { _ = grid.height(forWidth: 650) })
            grid.grid.frame = CGRect(x: 0, y: 0, width: 650, height: grid.height(forWidth: 650))
            let bitmap = grid.grid.bitmapImageRepForCachingDisplay(in: grid.grid.bounds)!
            PerfLog.record("layout.table50x8.drawAll(400 cells)", milliseconds { grid.grid.cacheDisplay(in: grid.grid.bounds, to: bitmap) })
            #expect(grid.grid.subviews.filter { $0 is CellTextView }.isEmpty, "cells are drawn, not text views")
        }
        PerfLog.record("layout.table50x8.first", milliseconds { _ = table.height(forWidth: 650) })
        let columns = try #require(live("k3"))
        PerfLog.record("layout.columns.first", milliseconds { _ = columns.height(forWidth: 650) })
        let long = editor.range(ofBlock: "l1")
        PerfLog.record("layout.long50k.scrollTo", milliseconds {
            editor.controller.scrollRangeToVisibleForTests(NSRange(location: long.location, length: 0))
            editor.layoutViewport()
        })
    }

    @Test func keystrokeInATableCell() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        editor.settle()
        let range = editor.range(ofBlock: "t0")
        let table = try #require((editor.controller.storage.attribute(.attachment, at: range.location, effectiveRange: nil) as? BlockAttachment)?.live as? TableBlockController)
        _ = table.height(forWidth: 600)
        var made: CellTextView?
        PerfLog.record("tableCell.openEditor", milliseconds { made = table.editor(row: 25, column: 3) })
        let cell = try #require(made)
        cell.setSelectedRange(NSRange(location: cell.string.utf16.count, length: 0))
        var times: [Double] = []
        for _ in 0..<20 {
            times.append(milliseconds { cell.insertText("c", replacementRange: cell.selectedRange()) })
        }
        let sorted = times.sorted()
        PerfLog.record("keystroke.tableCell.median", sorted[sorted.count / 2], budget: 8)
        PerfLog.record("keystroke.tableCell.max", sorted.last!)
        var operations: [FortressKit.BlockOperation] = []
        PerfLog.record("save.diff.tableCell", milliseconds { operations = editor.session.pendingOperationsForTests() }, budget: 8)
        #expect(operations.map(\.blockId) == ["t0r25c3p"])
    }

    @Test func saveDiffCostAfterOneEdit() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        editor.settle()
        let middleId = editor.middleParagraph
        let middle = editor.range(ofBlock: middleId)
        editor.controller.textView.setSelectedRange(NSRange(location: middle.location + 3, length: 0))
        editor.type("z")
        var operations: [FortressKit.BlockOperation] = []
        // What a debounced flush does before sending.
        let diff = milliseconds { operations = editor.session.pendingOperationsForTests() }
        PerfLog.record("save.diff(one edited block of 2000)", diff, budget: 8)
        editor.type("w")
        var read: [EditorBlock] = []
        PerfLog.record("save.diff.part.currentBlocks", milliseconds { read = editor.controller.currentBlocks() })
        PerfLog.record("save.diff.part.syncedTree", milliseconds { _ = SyncedTree(read, reusing: editor.session.base) })
        PerfLog.record("save.diff.part.operations", milliseconds { _ = BlockDiff.operations(from: editor.session.base, to: read) })
        #expect(operations.count == 1)
        #expect(operations.first?.blockId == middleId)

        // A second edit after a save: still only that block.
        editor.session.resetBase(to: editor.controller.currentBlocks())
        // The baseline is made on the next quiet run loop turn, as in the app.
        editor.settle()
        editor.type("y")
        let second = milliseconds { operations = editor.session.pendingOperationsForTests() }
        PerfLog.record("save.diff.second", second, budget: 8)
        #expect(operations.count == 1)

        // Return (a new block): the paragraphs change, so the whole note is walked.
        editor.controller.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
        let structural = milliseconds { operations = editor.session.pendingOperationsForTests() }
        PerfLog.record("save.diff.afterReturn", structural)
        #expect(operations.contains { $0.operation == .insert })
    }

    @Test func pastingFiveMegabytes() throws {
        let editor = PerfEditor(document: LongNote.document(blocks: 50))
        editor.open()
        editor.settle()
        let line = String(repeating: "Pasted log line with some words in it. ", count: 5) + "\n"
        let text = String(repeating: line, count: 5_000_000 / line.utf8.count)
        editor.controller.textView.setSelectedRange(NSRange(location: 0, length: 0))
        EditorMetrics.reset()
        let paste = milliseconds { editor.type(text) }
        PerfLog.record("paste.5MB(\(text.utf8.count / 1_000_000) MB, \(5_000_000 / line.utf8.count) lines)", paste)
        PerfLog.record("paste.5MB.part.normalize", EditorMetrics.normalize)
        PerfLog.record("paste.5MB.part.restyle", EditorMetrics.restyle)
        // The same text into a text storage with no editor attached (AppKit's own share).
        let bare = NSTextStorage(attributedString: editor.controller.storage)
        let attributes = editor.controller.textView.typingAttributes
        PerfLog.record("paste.5MB.part.storageOnly", milliseconds { bare.replaceCharacters(in: NSRange(location: 0, length: 0), with: NSAttributedString(string: text, attributes: attributes)) })
        var operations: [FortressKit.BlockOperation] = []
        var blocks: [EditorBlock] = []
        PerfLog.record("paste.5MB.part.read", milliseconds { blocks = editor.controller.currentBlocks() })
        let diff = milliseconds { operations = BlockDiff.operations(from: editor.session.base, to: blocks).operations }
        PerfLog.record("paste.5MB.saveDiff", diff)
        #expect(operations.filter { $0.operation == .insert }.count >= 5_000_000 / line.utf8.count - 1)
        let keystroke = milliseconds { editor.type("a") }
        PerfLog.record("paste.5MB.keystrokeAfter", keystroke, budget: 8)
    }

    @Test func hundredLargeImages() async throws {
        let folder = FileManager.default.temporaryDirectory.appending(path: "perf-images-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let jpeg = ImagePipelineTests.jpeg(width: 3000, height: 2000)
        for index in 0..<100 { try jpeg.write(to: folder.appending(path: "photo-\(index).jpg")) }
        ImagePipeline.shared.removeAll()
        let document = LongNote.document(blocks: 2000) { folder.appending(path: "photo-\($0).jpg").absoluteString }
        let before = footprintMB()
        let editor = PerfEditor(document: document)
        let open = milliseconds { editor.open() }
        PerfLog.record("images.open", open, budget: 150)
        editor.settle()
        // Scroll through the whole note so every picture is asked for, drawing each screen.
        var y: CGFloat = 0
        var draws: [Double] = []
        let bitmap = editor.controller.scrollView.bitmapImageRepForCachingDisplay(in: editor.controller.scrollView.bounds)!
        while y < editor.controller.textView.frame.height {
            editor.scroll(to: y)
            draws.append(milliseconds { editor.controller.scrollView.cacheDisplay(in: editor.controller.scrollView.bounds, to: bitmap) })
            y += 700
        }
        // Wait for decoding (off the main thread).
        for _ in 0..<100 { try await Task.sleep(for: .milliseconds(50)) }
        y = 0
        var loadedDraws: [Double] = []
        while y < editor.controller.textView.frame.height {
            editor.scroll(to: y)
            loadedDraws.append(milliseconds { editor.controller.scrollView.cacheDisplay(in: editor.controller.scrollView.bounds, to: bitmap) })
            y += 700
        }
        let sorted = loadedDraws.sorted()
        PerfLog.record("images.frame.median", sorted[sorted.count / 2], budget: 16)
        PerfLog.record("images.frame.max", sorted.last ?? 0)
        PerfLog.record("images.memory(100 × 3000x2000 JPEG)", footprintMB() - before, unit: "MB")
    }

    @Test func scrollingLaysOutOnlyTheViewport() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        editor.settle()
        let height = editor.controller.textView.frame.height
        var times: [Double] = []
        var frames: [Double] = []
        var y: CGFloat = 0
        var slow: [(Double, String)] = []
        let scrollView = editor.controller.scrollView
        let bitmap = scrollView.bitmapImageRepForCachingDisplay(in: scrollView.bounds)!
        while y < min(height, 60_000) {
            y += 700
            let time = milliseconds { editor.scroll(to: y) }
            // What the screen then shows, drawn (tables draw their cells' text).
            let draw = milliseconds { scrollView.cacheDisplay(in: scrollView.bounds, to: bitmap) }
            times.append(time)
            frames.append(time + draw)
            if time + draw > 50 { slow.append((time + draw, editor.viewportKinds)) }
        }
        for (time, kinds) in slow.sorted(by: { $0.0 > $1.0 }).prefix(5) {
            PerfLog.record("scroll.slowFrame[\(kinds)]", time)
        }
        let sorted = times.sorted()
        PerfLog.record("scroll.step.median", sorted[sorted.count / 2], budget: 16)
        PerfLog.record("scroll.step.max", sorted.last ?? 0)
        let framesSorted = frames.sorted()
        PerfLog.record("scroll.frame(layout+draw).median", framesSorted[framesSorted.count / 2], budget: 16)
        PerfLog.record("scroll.frame(layout+draw).max", framesSorted.last ?? 0)
        PerfLog.record("scroll.steps", Double(times.count), unit: "steps")
    }

    /// Scrolling a 50 × 8 table into view, a screen at a time, drawing each screen.
    @Test func scrollingThroughALargeTable() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        editor.settle()
        let range = editor.range(ofBlock: "t5")
        editor.controller.scrollRangeToVisibleForTests(NSRange(location: max(0, range.location - 2000), length: 0))
        editor.layoutViewport()
        let scrollView = editor.controller.scrollView
        let bitmap = scrollView.bitmapImageRepForCachingDisplay(in: scrollView.bounds)!
        var y = scrollView.contentView.bounds.minY
        var frames: [Double] = []
        for _ in 0..<12 {
            y += 300
            frames.append(milliseconds {
                editor.scroll(to: y)
                scrollView.cacheDisplay(in: scrollView.bounds, to: bitmap)
            })
        }
        let sorted = frames.sorted()
        PerfLog.record("scroll.table50x8.frame.median", sorted[sorted.count / 2], budget: 16)
        PerfLog.record("scroll.table50x8.frame.max", sorted.last ?? 0)
        let table = try #require((editor.controller.storage.attribute(.attachment, at: range.location, effectiveRange: nil) as? BlockAttachment)?.live as? TableBlockController)
        #expect(table.grid.subviews.filter { $0 is CellTextView }.isEmpty)
    }
}

extension BlockEditorController {
    func scrollRangeToVisibleForTests(_ range: NSRange) {
        textView.scrollRangeToVisible(range)
    }
}

extension EditorSession {
    /// The operations a flush would send now (without sending them).
    func pendingOperationsForTests() -> [FortressKit.BlockOperation] {
        guard let blocks = currentBlocks?() else { return [] }
        return BlockDiff.operations(from: base, to: blocks).operations
    }
}
