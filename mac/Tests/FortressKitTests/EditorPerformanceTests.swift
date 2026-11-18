import AppKit
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

    static func run(_ text: String, _ styles: [String: JSONValue] = [:]) -> JSONValue {
        .object(["type": .string("text"), "text": .string(text), "styles": .object(styles)])
    }

    static func node(_ id: String, _ type: String, attrs: [String: JSONValue] = [:], runs: [JSONValue]? = nil,
                     content: [String: JSONValue] = [:], children: [BlockNode] = []) -> BlockNode {
        var content = content
        if let runs { content["blocknote"] = .array(runs) }
        let text = runs.map { $0.map(BlockCodec.inlineText).joined() } ?? ""
        return BlockNode(id: id, documentId: documentId, parentId: nil, position: 0, type: type, attrs: attrs,
                         userAttrs: [:], content: .object(content), text: text, createdAt: "2026-11-18T00:00:00+00:00",
                         updatedAt: "2026-11-18T00:00:00+00:00", children: children)
    }

    static let sentence = "The quick brown fox jumps over the lazy dog while the editor keeps every block in step with the server. "

    static func prose(_ index: Int) -> [JSONValue] {
        [run("Block \(index): "), run(sentence), run("bold part", ["bold": .bool(true)]), run(" and "),
         .object(["type": .string("link"), "href": .string("https://example.com/\(index)"), "content": .array([run("a link")])]),
         run(" then " + sentence)]
    }

    static func table(_ index: Int, rows: Int = 50, columns: Int = 8) -> BlockNode {
        let rowValues: [JSONValue] = (0..<rows).map { r in
            .object(["cells": .array((0..<columns).map { c in
                .object(["type": .string("tableCell"), "props": .object([:]),
                         "content": .array([run(r == 0 ? "Column \(c)" : "R\(r)C\(c) value")])])
            })])
        }
        let content: JSONValue = .object(["type": .string("tableContent"), "headerRows": .number(1), "rows": .array(rowValues)])
        return node("t\(index)", "table", attrs: ["borders": .bool(true)], content: ["blocknote": content])
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
                                           "caption": .string("Picture \(index)")],
             content: ["markdown": .string("![photo-\(index).jpg](\(url))")])
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
                    chain = node("n\(i)-\(depth)", "bulletListItem", runs: prose(i + depth), children: chain.map { [$0] } ?? [])
                }
                nodes.append(chain!); made += 6
            } else {
                let types = ["paragraph", "paragraph", "paragraph", "numberedListItem", "checkListItem", "quote", "bulletListItem"]
                let type = types[i % types.count]
                nodes.append(node("p\(i)", type, attrs: type == "checkListItem" ? ["checked": .bool(i % 2 == 0)] : [:], runs: prose(i)))
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

    init(document: BlockDocument, baseURL: URL = URL(string: "http://127.0.0.1:9")!) {
        session = EditorSession(document: document, api: APIClient(baseURL: baseURL))
        controller = BlockEditorController(baseURL: baseURL)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 800), styleMask: [.titled, .resizable],
                          backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = controller.scrollView
        controller.scrollView.frame = window.contentView?.bounds ?? .zero
    }

    /// What opening a note does: blocks → attributed text → viewport layout.
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
        var flattenTime = 0.0
        var blocks: [EditorBlock] = []
        flattenTime = milliseconds { blocks = BlockCodec.flatten(document.children, resolved: document.resolvedLinks) }
        let open = milliseconds {
            editor = PerfEditor(document: document)
            editor.open()
        }
        PerfLog.record("open.flatten", flattenTime)
        // Where opening spends its time.
        let parts = PerfEditor(document: document)
        let context = parts.controller.context
        var text = NSMutableAttributedString()
        PerfLog.record("open.part.build", milliseconds { text = DocumentText.build(blocks, theme: EditorTheme(), context: context) })
        PerfLog.record("open.part.storage", milliseconds { parts.controller.storage.setAttributedString(text) })
        PerfLog.record("open.part.layout", milliseconds { parts.layoutViewport() })
        let paragraphsOnly = blocks.filter { $0.kind.hasInlineText }
        PerfLog.record("open.part.buildTextOnly(\(paragraphsOnly.count))", milliseconds { _ = DocumentText.build(paragraphsOnly, theme: EditorTheme(), context: context) })
        let tableNodes = document.children.filter { $0.type == "table" }
        PerfLog.record("open.part.flattenTables(\(tableNodes.count))", milliseconds { _ = BlockCodec.flatten(tableNodes) })
        let textNodes = document.children.filter { $0.type != "table" && $0.type != "columns" }
        PerfLog.record("open.part.flattenText(\(textNodes.count))", milliseconds { _ = BlockCodec.flatten(textNodes) })
        let columnBlocks = blocks.filter { $0.kind == .columns }
        PerfLog.record("open.part.buildColumns(\(columnBlocks.count))", milliseconds { _ = DocumentText.build(columnBlocks, theme: EditorTheme(), context: context) })
        PerfLog.record("open.total(2000 blocks, 100 images, 20 tables 50x8, 10 columns, 3x50k)", open, budget: 150)
        PerfLog.record("open.memory", footprintMB() - before, unit: "MB")
        let paragraphs = blocks.count
        let laidOut = editor.laidOutFragments
        PerfLog.record("open.laidOutFragments(of \(paragraphs))", Double(laidOut), unit: "fragments")
        #expect(editor.controller.storage.length > 150_000)
        // TextKit 2 lays out the viewport, not the whole note.
        #expect(laidOut < paragraphs / 4, "full layout forced: \(laidOut) of \(paragraphs)")

        // A second open of the same note (caches warm).
        let again = milliseconds {
            let other = PerfEditor(document: document)
            other.open()
        }
        PerfLog.record("open.warm", again, budget: 150)
    }

    @Test func keystrokeLatency() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        // A paragraph in the middle of the note.
        let middleId = editor.middleParagraph
        let middle = editor.range(ofBlock: middleId)
        editor.controller.textView.setSelectedRange(NSRange(location: middle.location + 10, length: 0))
        var times: [Double] = []
        for index in 0..<40 {
            times.append(milliseconds { editor.type(index % 8 == 7 ? " " : "a") })
        }
        let sorted = times.sorted()
        PerfLog.record("keystroke.middle.median", sorted[sorted.count / 2], budget: 8)
        PerfLog.record("keystroke.middle.max", sorted.last!)

        // Inside a 50,000-character paragraph.
        let long = editor.range(ofBlock: "l0")
        editor.controller.textView.setSelectedRange(NSRange(location: long.location + 25_000, length: 0))
        editor.controller.scrollRangeToVisibleForTests(NSRange(location: long.location + 25_000, length: 0))
        editor.layoutViewport()
        times = []
        for _ in 0..<20 { times.append(milliseconds { editor.type("b") }) }
        let longSorted = times.sorted()
        PerfLog.record("keystroke.long50k.median", longSorted[longSorted.count / 2], budget: 8)
        PerfLog.record("keystroke.long50k.max", longSorted.last!)

        // Return (splits a block: structure changes).
        editor.controller.textView.setSelectedRange(NSRange(location: editor.range(ofBlock: middleId).location + 5, length: 0))
        let newline = milliseconds {
            editor.controller.textView.doCommand(by: #selector(NSResponder.insertNewline(_:)))
            editor.layoutViewport()
        }
        PerfLog.record("keystroke.return", newline, budget: 8)
    }

    @Test func richBlockLayout() throws {
        let document = LongNote.document()
        let editor = PerfEditor(document: document)
        editor.open()
        func live(_ id: String) -> (any LiveBlock)? {
            (editor.controller.storage.attribute(.attachment, at: editor.range(ofBlock: id).location, effectiveRange: nil) as? BlockAttachment)?.live
        }
        let table = try #require(live("t3"))
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
        let range = editor.range(ofBlock: "t0")
        let table = try #require((editor.controller.storage.attribute(.attachment, at: range.location, effectiveRange: nil) as? BlockAttachment)?.live as? TableBlockController)
        _ = table.height(forWidth: 600)
        let cell = table.cells[25][3]
        cell.setSelectedRange(NSRange(location: cell.string.utf16.count, length: 0))
        var times: [Double] = []
        for _ in 0..<20 {
            times.append(milliseconds { cell.insertText("c", replacementRange: cell.selectedRange()) })
        }
        let sorted = times.sorted()
        PerfLog.record("keystroke.tableCell.median", sorted[sorted.count / 2], budget: 8)
        PerfLog.record("keystroke.tableCell.max", sorted.last!)
    }

    @Test func saveDiffCostAfterOneEdit() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
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
        editor.type("y")
        let second = milliseconds { operations = editor.session.pendingOperationsForTests() }
        PerfLog.record("save.diff.second", second, budget: 8)
        #expect(operations.count == 1)
    }

    @Test func pastingFiveMegabytes() throws {
        let editor = PerfEditor(document: LongNote.document(blocks: 50))
        editor.open()
        let line = String(repeating: "Pasted log line with some words in it. ", count: 5) + "\n"
        let text = String(repeating: line, count: 5_000_000 / line.utf8.count)
        editor.controller.textView.setSelectedRange(NSRange(location: 0, length: 0))
        let paste = milliseconds { editor.type(text) }
        PerfLog.record("paste.5MB(\(text.utf8.count / 1_000_000) MB, \(5_000_000 / line.utf8.count) lines)", paste)
        var operations: [FortressKit.BlockOperation] = []
        let diff = milliseconds { operations = editor.session.pendingOperationsForTests() }
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
        let height = editor.controller.textView.frame.height
        var times: [Double] = []
        var y: CGFloat = 0
        var slow: [(Double, String)] = []
        while y < min(height, 60_000) {
            y += 700
            let time = milliseconds { editor.scroll(to: y) }
            times.append(time)
            if time > 50 { slow.append((time, editor.viewportKinds)) }
        }
        for (time, kinds) in slow.sorted(by: { $0.0 > $1.0 }).prefix(5) {
            PerfLog.record("scroll.slowStep[\(kinds)]", time)
        }
        let sorted = times.sorted()
        PerfLog.record("scroll.step.median", sorted[sorted.count / 2], budget: 16)
        PerfLog.record("scroll.step.max", sorted.last ?? 0)
        PerfLog.record("scroll.steps", Double(times.count), unit: "steps")
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
