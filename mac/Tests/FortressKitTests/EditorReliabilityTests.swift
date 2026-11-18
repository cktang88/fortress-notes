import AppKit
import Testing

@testable import FortressKit

// The save pipeline under stress (note switches mid-save, quitting, offline, 5xx) and
// editing edge cases (rich paste, IME with shortcuts, undo across block types, emoji,
// focus restore). Each test failed before its fix.

private typealias Op = FortressKit.BlockOperation

/// This suite's own server stub (suites run in parallel; `StubProtocol` is another's).
final class SaveStub: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var replies: [(status: Int, body: Data?)] = []
    nonisolated(unsafe) static var requests: [URLRequest] = []
    nonisolated(unsafe) static var bodies: [Data] = []
    private static let lock = NSLock()

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            body = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(buffer, count: count)
            }
        }
        Self.lock.lock()
        Self.requests.append(request)
        Self.bodies.append(body)
        let reply = Self.replies.isEmpty ? (status: 500, body: nil) : Self.replies.removeFirst()
        Self.lock.unlock()
        if reply.status == 0 {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: reply.body ?? Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@MainActor
private func stubAPI() -> APIClient {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [SaveStub.self]
    SaveStub.replies = []
    SaveStub.requests = []
    SaveStub.bodies = []
    return APIClient(baseURL: URL(string: "http://stub.local")!, session: URLSession(configuration: configuration))
}

private func node(_ id: String, _ text: String) -> BlockNode {
    BlockNode.paragraph(id, text)
}

private func document(revision: Int, _ nodes: [BlockNode]) -> BlockDocument {
    BlockDocument(id: "d", revision: revision, titleAuto: false, canUndo: false, canRedo: false, title: "Note",
                  status: .rough, createdAt: "2026-11-18T00:00:00+00:00", updatedAt: "2026-11-18T00:00:00+00:00",
                  children: nodes)
}

private func sent(_ index: Int) throws -> FortressKit.BlockTransaction {
    guard SaveStub.bodies.indices.contains(index) else { throw URLError(.zeroByteResource) }
    return try JSONDecoder().decode(FortressKit.BlockTransaction.self, from: SaveStub.bodies[index])
}

private func pendingDirectory() -> URL {
    let url = FileManager.default.temporaryDirectory.appending(path: "pending-\(UUID().uuidString)", directoryHint: .isDirectory)
    PendingEdits.directory = url
    return url
}

@MainActor
@Suite(.serialized) struct EditorSaveReliabilityTests {
    /// Switching notes while a save is on the wire used to drop everything typed during
    /// it: `flush` returned at once (one in flight) and `detach` cut the editor off, so the
    /// follow-up flush found no text.
    @Test func closingMidSaveSendsTheEditsTypedDuringIt() async throws {
        let session = EditorSession(document: document(revision: 1, [node("a", "One"), node("b", "Two")]), api: stubAPI())
        session.saveDelay = .milliseconds(1)
        var blocks = BlockCodec.flatten(session.document!.children)
        session.currentBlocks = { blocks }
        SaveStub.replies = [(200, try JSONEncoder().encode(document(revision: 2, [node("a", "One!"), node("b", "Two")]))),
                                (200, try JSONEncoder().encode(document(revision: 3, [node("a", "One!"), node("b", "Two?")])))]
        blocks[0].runs = [InlineRun("One!")]
        session.textDidChange()
        let first = Task { await session.flush() }
        while SaveStub.requests.isEmpty { await Task.yield() }
        // Typed while the first save is in flight, then the note is closed.
        blocks[1].runs = [InlineRun("Two?")]
        session.textDidChange()
        await session.close()
        await first.value
        #expect(SaveStub.bodies.count == 2)
        let second = try sent(1)
        #expect(second.baseRevision == 2)
        #expect(second.operations.map(\.blockId) == ["b"])
        #expect(second.operations.first?.plainText == "Two?")
        #expect(!session.hasUnsavedChanges)
    }

    /// Everything typed within one debounce window is one transaction, one operation per
    /// changed block however many times it changed.
    @Test func editsInOneWindowAreOneTransaction() async throws {
        let session = EditorSession(document: document(revision: 1, [node("a", "One"), node("b", "Two")]), api: stubAPI())
        session.saveDelay = .milliseconds(30)
        var blocks = BlockCodec.flatten(session.document!.children)
        session.currentBlocks = { blocks }
        SaveStub.replies = [(200, try JSONEncoder().encode(document(revision: 2, [node("a", "Onexyz"), node("b", "Two!")])))]
        for text in ["Onex", "Onexy", "Onexyz"] {
            blocks[0].runs = [InlineRun(text)]
            session.textDidChange()
        }
        blocks[1].runs = [InlineRun("Two!")]
        session.textDidChange()
        // The debounce fires once (the main actor may be busy with other suites: wait for it).
        for _ in 0..<200 where SaveStub.bodies.isEmpty || session.hasUnsavedChanges { try await Task.sleep(for: .milliseconds(25)) }
        try await Task.sleep(for: .milliseconds(100))
        #expect(SaveStub.bodies.count == 1)
        let transaction = try sent(0)
        #expect(transaction.operations.count == 2)
        #expect(transaction.operations.map(\.plainText) == ["Onexyz", "Two!"])
    }

    /// A 5xx keeps the edits and retries the same transaction (same id: applied once).
    @Test func serverErrorRetriesTheSameTransaction() async throws {
        let session = EditorSession(document: document(revision: 1, [node("a", "One")]), api: stubAPI())
        var blocks = BlockCodec.flatten(session.document!.children)
        session.currentBlocks = { blocks }
        blocks[0].runs = [InlineRun("One more")]
        SaveStub.replies = [(503, nil), (200, try JSONEncoder().encode(document(revision: 2, [node("a", "One more")])))]
        session.textDidChange()
        await session.flush()
        #expect(session.saveState == .offline)
        await session.flush()
        #expect(session.saveState == .saved)
        #expect(try sent(0) == sent(1))
    }

    /// Quitting used to keep unsent edits only in memory. Now they go to disk and are
    /// replayed (and saved) the next time the note opens.
    @Test func unsavedEditsSurviveQuittingAndAreSavedOnNextOpen() async throws {
        let directory = pendingDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let api = stubAPI()
        let server = document(revision: 4, [node("a", "Kept"), node("b", "Old")])
        SaveStub.replies = [(200, try JSONEncoder().encode(server))]
        let session = EditorSession(documentId: "d", api: api)
        session.saveDelay = .seconds(60)  // quit before the debounce fires
        await session.load()
        var blocks = session.initialBlocks
        session.currentBlocks = { blocks }
        blocks[1].runs = [InlineRun("New text")]
        blocks.append(EditorBlock(id: "c", runs: [InlineRun("Added")]))
        session.textDidChange()
        EditorSession.applicationWillTerminate()
        let record = try #require(PendingEdits.load(documentId: "d"))
        #expect(Set(record.operations.map(\.operation)) == [.update, .insert])

        // Next launch: the note opens with the edits and saves them.
        let relaunched = stubAPI()  // a new launch: nothing in memory
        SaveStub.replies = [(200, try JSONEncoder().encode(server)),
                                (200, try JSONEncoder().encode(document(revision: 5, [node("a", "Kept"), node("b", "New text"), node("c", "Added")])))]
        let reopened = EditorSession(documentId: "d", api: relaunched)
        reopened.saveDelay = .milliseconds(1)
        await reopened.load()
        let shown = reopened.initialBlocks
        #expect(shown.map(\.plainText) == ["Kept", "New text", "Added"])
        reopened.currentBlocks = { shown }
        await reopened.flush()
        let transaction = try sent(SaveStub.bodies.count - 1)
        #expect(transaction.baseRevision == 4)
        #expect(transaction.operations.map(\.blockId) == ["b", "c"])
        #expect(reopened.saveState == .saved)
        #expect(PendingEdits.load(documentId: "d") == nil)
    }

    /// Offline edits are on disk before the request is even sent.
    @Test func offlineEditsAreOnDisk() async throws {
        let directory = pendingDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let session = EditorSession(document: document(revision: 2, [node("a", "One")]), api: stubAPI())
        session.persistsPendingEdits = true
        var blocks = BlockCodec.flatten(session.document!.children)
        session.currentBlocks = { blocks }
        blocks[0].runs = [InlineRun("Written on a plane")]
        SaveStub.replies = [(0, nil)]
        session.textDidChange()
        await session.flush()
        #expect(session.saveState == .offline)
        let record = try #require(PendingEdits.load(documentId: "d"))
        #expect(record.operations.first?.plainText == "Written on a plane")
    }

    /// Skimming the note list (30 selections 2 ms apart, each editor replaced by the next)
    /// used to send 30 document requests; now only the note stopped on loads.
    @Test func skimmingNotesLoadsOnlyTheLastOne() async throws {
        let api = stubAPI()
        SaveStub.replies = [(200, try JSONEncoder().encode(document(revision: 1, [node("a", "A")])))]
        var tasks: [Task<Void, Never>] = []
        var last: EditorSession?
        for index in 0..<30 {
            tasks.last?.cancel()
            let session = EditorSession(documentId: "skim-\(index)", api: api)
            last = session
            tasks.append(Task { await session.load(debounce: .milliseconds(40)) })
            try await Task.sleep(for: .milliseconds(2))
        }
        await tasks.last?.value
        #expect(SaveStub.requests.count == 1)
        #expect(SaveStub.requests.first?.url?.lastPathComponent == "skim-29")
        #expect(last?.document != nil)
    }

    /// Replaying operations never repeats one the server already applied.
    @Test func replayingIsIdempotent() {
        let nodes = [node("a", "A"), node("b", "B")]
        func content(_ text: String) -> JSONValue { .array([.object(["type": .string("text"), "text": .string(text)])]) }
        let operations = [Op(operation: .insert, blockId: "c", afterId: "b", type: "paragraph", content: content("C")),
                          Op(operation: .update, blockId: "a", attrs: ["x": .bool(true)], content: content("A2")),
                          Op(operation: .delete, blockId: "b")]
        let once = PendingEdits.apply(operations, to: nodes)
        let twice = PendingEdits.apply(operations, to: once)
        #expect(once.map(\.id) == ["a", "c"])
        #expect(twice.map(\.id) == ["a", "c"])
        #expect(twice.map { BlockCodec.plainText(BlockCodec.runs(fromContent: $0.content)) } == ["A2", "C"])
        #expect(twice.first?.attrs == ["x": .bool(true)])
    }
}

/// Random edits of random trees: the operations, applied the way the server applies them
/// (`PendingEdits.apply` mirrors it: insert/move at sibling positions, delete subtrees),
/// always produce exactly the editor's tree.
@MainActor
@Suite struct BlockDiffPropertyTests {
    struct Generator: RandomNumberGenerator {
        var state: UInt64
        mutating func next() -> UInt64 {
            state = state &* 6364136223846793005 &+ 1442695040888963407
            return state
        }
    }

    static func tree(_ blocks: [EditorBlock]) -> [BlockNode] {
        var roots: [BlockNode] = []
        func insert(_ node: BlockNode, under parent: String?, into nodes: inout [BlockNode]) -> Bool {
            if parent == nil { nodes.append(node); return true }
            for index in nodes.indices {
                if nodes[index].id == parent { nodes[index].children.append(node); return true }
                if insert(node, under: parent, into: &nodes[index].children) { return true }
            }
            return false
        }
        for item in BlockDiff.shape(blocks) {
            let fields = BlockCodec.encode(item.block)
            let node = BlockNode(id: item.block.id, documentId: "d", parentId: item.parent, type: fields.type,
                                 attrs: fields.attrs, content: fields.content, payload: fields.payload)
            _ = insert(node, under: item.parent, into: &roots)
        }
        return roots
    }

    static func outline(_ nodes: [BlockNode], depth: Int = 0) -> [String] {
        nodes.flatMap { ["\(depth):\($0.id):\(BlockCodec.plainText(BlockCodec.runs(fromContent: $0.content)))"] + outline($0.children, depth: depth + 1) }
    }

    @Test(arguments: 0..<200) func operationsReproduceTheEditorsTree(seed: Int) {
        var random = Generator(state: UInt64(seed) &+ 1)
        var blocks = (0..<Int.random(in: 1...12, using: &random)).map { index in
            EditorBlock(id: "b\(index)", kind: .bullet, depth: Int.random(in: 0...3, using: &random), runs: [InlineRun("t\(index)")])
        }
        blocks = BlockDiff.shape(blocks).map(\.block)
        let base = SyncedTree(blocks)
        let server = Self.tree(blocks)
        var edited = blocks
        var fresh = 0
        for _ in 0..<Int.random(in: 1...6, using: &random) {
            switch Int.random(in: 0..<5, using: &random) {
            case 0 where !edited.isEmpty:
                edited.remove(at: Int.random(in: 0..<edited.count, using: &random))
            case 1:
                fresh += 1
                edited.insert(EditorBlock(id: "n\(fresh)", kind: .bullet, depth: Int.random(in: 0...3, using: &random), runs: [InlineRun("new\(fresh)")]),
                              at: Int.random(in: 0...edited.count, using: &random))
            case 2 where edited.count > 1:
                let from = Int.random(in: 0..<edited.count, using: &random)
                let block = edited.remove(at: from)
                edited.insert(block, at: Int.random(in: 0...edited.count, using: &random))
            case 3 where !edited.isEmpty:
                let index = Int.random(in: 0..<edited.count, using: &random)
                edited[index].depth = Int.random(in: 0...3, using: &random)
            case 4 where !edited.isEmpty:
                let index = Int.random(in: 0..<edited.count, using: &random)
                edited[index].runs = [InlineRun("changed\(index)")]
            default:
                break
            }
        }
        edited = BlockDiff.shape(edited).map(\.block)
        let (operations, result) = BlockDiff.operations(from: base, to: edited)
        let applied = PendingEdits.apply(operations, to: server)
        #expect(Self.outline(applied) == Self.outline(Self.tree(edited)), "seed \(seed): \(operations.map { "\($0.operation.rawValue) \($0.blockId ?? "") \($0.parentId ?? "-") \($0.afterId ?? $0.beforeId ?? "")" })")
        #expect(result == SyncedTree(edited))
        // Nothing left to send afterwards.
        #expect(BlockDiff.operations(from: result, to: edited).operations.isEmpty)
    }
}

/// Windows stay alive for the test run (their undo manager is the text view's).
@MainActor private var openWindows: [NSWindow] = []

@MainActor
private func settle() {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
}

@MainActor
private func editor(_ blocks: [EditorBlock]) -> (BlockEditorController, NSWindow) {
    let controller = BlockEditorController(baseURL: URL(string: "http://127.0.0.1:9")!)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 600), styleMask: [.titled], backing: .buffered, defer: true)
    window.isReleasedWhenClosed = false
    window.contentView = controller.scrollView
    controller.load(blocks)
    window.makeFirstResponder(controller.textView)
    controller.textView.layoutSubtreeIfNeeded()
    controller.textView.textLayoutManager?.textViewportLayoutController.layoutViewport()
    openWindows.append(window)
    return (controller, window)
}

private func paragraph(_ id: String, _ text: String, kind: BlockKind = .paragraph) -> EditorBlock {
    EditorBlock(id: id, kind: kind, runs: [InlineRun(text)])
}

@MainActor
@Suite(.serialized) struct EditorEdgeCaseTests {
    /// Pasting rich text with Windows line ends (Word, some browsers) used to replace the
    /// whole paste with the first character's attributes: every bold word and link lost.
    @Test func pastedRichTextWithCRLFKeepsItsFormatting() {
        let (controller, _) = editor([paragraph("a", "")])
        let pasted = NSMutableAttributedString(string: "Plain ", attributes: [.font: NSFont.systemFont(ofSize: 12)])
        pasted.append(NSAttributedString(string: "bold", attributes: [.font: NSFont.boldSystemFont(ofSize: 12)]))
        pasted.append(NSAttributedString(string: "\r\nnext ", attributes: [.font: NSFont.systemFont(ofSize: 12)]))
        pasted.append(NSAttributedString(string: "link", attributes: [.font: NSFont.systemFont(ofSize: 12), .link: URL(string: "https://example.com")!]))
        // What NSTextView's paste does with rich text: the pasted attributes go in as they are.
        controller.replace(NSRange(location: 0, length: 0), with: pasted)
        let blocks = controller.currentBlocks()
        #expect(blocks.map(\.plainText) == ["Plain bold", "next link"])
        #expect(blocks[0].runs.contains { $0.text == "bold" && $0.marks.bold })
        #expect(blocks[1].runs.contains { $0.text == "link" && $0.marks.link == "https://example.com" })
        #expect(Set(blocks.map(\.id)).count == 2)
    }

    /// ⌘B during Japanese composition used to bold the marked (uncommitted) text.
    @Test func formattingShortcutsWaitForIMEComposition() throws {
        let (controller, _) = editor([paragraph("a", "日本")])
        let view = controller.textView
        view.setSelectedRange(NSRange(location: 2, length: 0))
        view.setMarkedText("ご", selectedRange: NSRange(location: 1, length: 0), replacementRange: NSRange(location: 2, length: 0))
        #expect(view.hasMarkedText())
        let event = try #require(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [.command], timestamp: 0,
                                                  windowNumber: 0, context: nil, characters: "b", charactersIgnoringModifiers: "b",
                                                  isARepeat: false, keyCode: 11))
        #expect(!controller.handleKeyEquivalent(event))
        #expect(view.hasMarkedText())
        view.insertText("語", replacementRange: view.markedRange())
        let block = controller.currentBlocks()[0]
        #expect(block.plainText == "日本語")
        #expect(block.runs.allSatisfy { !$0.marks.bold })
    }

    /// Undo and redo across block type changes restore kinds and ids.
    @Test func undoAcrossBlockTypeChanges() {
        let (controller, window) = editor([paragraph("a", "Title"), paragraph("b", "item")])
        _ = window
        controller.textView.setSelectedRange(NSRange(location: 1, length: 0))
        controller.setKind(.heading1)
        settle()
        controller.textView.setSelectedRange(NSRange(location: 7, length: 0))
        controller.setKind(.check)
        settle()
        #expect(controller.currentBlocks().map(\.kind) == [.heading1, .check])
        controller.textView.undoManager?.undo()
        settle()
        #expect(controller.currentBlocks().map(\.kind) == [.heading1, .paragraph])
        controller.textView.undoManager?.undo()
        settle()
        #expect(controller.currentBlocks().map(\.kind) == [.paragraph, .paragraph])
        controller.textView.undoManager?.redo()
        let blocks = controller.currentBlocks()
        #expect(blocks.map(\.kind) == [.heading1, .paragraph])
        #expect(blocks.map(\.id) == ["a", "b"])
    }

    /// Emoji (surrogate pairs, ZWJ sequences) and combining marks survive marks and saving:
    /// offsets are UTF-16 throughout.
    @Test func emojiAndCombiningCharactersRoundTrip() {
        let text = "👩‍👩‍👧 cafe\u{301} 🇯🇵 x"
        let (controller, _) = editor([paragraph("a", text)])
        let utf16 = (text as NSString).length
        controller.textView.setSelectedRange(NSRange(location: 0, length: utf16 - 2))
        controller.toggleMark(\.bold)
        controller.textView.setSelectedRange(NSRange(location: utf16, length: 0))
        controller.textView.insertText("!", replacementRange: controller.textView.selectedRange())
        let block = controller.currentBlocks()[0]
        #expect(block.plainText == text + "!")
        #expect(block.runs.first?.marks.bold == true)
        let fields = BlockCodec.encode(block)
        #expect(BlockCodec.plainText(BlockCodec.runs(fromContent: fields.content)) == text + "!")
    }

    /// Text loaded from the server with a CR or paragraph separator inside a block stays one block.
    @Test func separatorsInsideLoadedTextDontSplitTheBlock() {
        let (controller, _) = editor([paragraph("a", "one\r\ntwo\u{2029}three"), paragraph("b", "next")])
        let blocks = controller.currentBlocks()
        #expect(blocks.map(\.id) == ["a", "b"])
        #expect(blocks[0].plainText == "one\ntwo\nthree")
    }

    /// A save after one keystroke reads and encodes only that block.
    @Test func saveReadsOnlyTheEditedBlock() {
        let blocks = (0..<50).map { paragraph("p\($0)", "Paragraph \($0)") }
        let (controller, _) = editor(blocks)
        let base = SyncedTree(controller.currentBlocks())
        let range = controller.range(ofBlock: "p20")!
        controller.textView.setSelectedRange(NSRange(location: range.location + 3, length: 0))
        controller.textView.insertText("x", replacementRange: controller.textView.selectedRange())
        settle()
        let (operations, _) = BlockDiff.operations(from: base, to: controller.currentBlocks())
        #expect(operations.map(\.blockId) == ["p20"])
        #expect(operations.first?.plainText == "Parxagraph 20")
        // Undo brings back the text and the next diff sends just that block again.
        controller.textView.undoManager?.undo()
        #expect(controller.currentBlocks()[20].plainText == "Paragraph 20")
    }

    /// Switching back to a note puts the caret where it was.
    @Test func caretIsRestoredWhenANoteReopens() {
        let blocks = (0..<30).map { paragraph("p\($0)", "Paragraph number \($0)") }
        let (controller, _) = editor(blocks)
        let range = controller.range(ofBlock: "p17")!
        controller.textView.setSelectedRange(NSRange(location: range.location + 6, length: 0))
        let anchor = controller.selectionAnchor()
        #expect(anchor?.blockId == "p17")
        let (reopened, _) = editor(blocks)
        #expect(reopened.restoreSelection(anchor!))
        #expect(reopened.textView.selectedRange().location == reopened.range(ofBlock: "p17")!.location + 6)
    }

    /// Tab in a table's last cell adds a row even before the table was ever laid out
    /// (cells are made lazily).
    @Test func tabInLastCellAddsARow() throws {
        let table = EditorBlock(id: "t", kind: .table, table: .blank(rows: 2, columns: 2))
        let (controller, window) = editor([paragraph("a", "Above"), table, paragraph("b", "Below")])
        let range = controller.range(ofBlock: "t")!
        let live = try #require((controller.storage.attribute(.attachment, at: range.location, effectiveRange: nil) as? BlockAttachment)?.live as? TableBlockController)
        let last = live.cells[1][1]
        window.makeFirstResponder(last)
        last.doCommand(by: #selector(NSResponder.insertTab(_:)))
        #expect(live.table.rows.count == 3)
        #expect(window.firstResponder === live.cells[2][0])
        live.cells[2][0].insertText("new", replacementRange: NSRange(location: 0, length: 0))
        let saved = controller.currentBlocks()[1]
        #expect(saved.table?.rows.count == 3)
        #expect(saved.table?.cell(row: 2, column: 0)?.plainText == "new")
    }
}

@MainActor
@Suite struct ImagePipelineTests {
    static func jpeg(width: Int, height: Int) -> Data {
        let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
        context.setFillColor(CGColor(red: 0.2, green: 0.5, blue: 0.8, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let rep = NSBitmapImageRep(cgImage: context.makeImage()!)
        return rep.representation(using: .jpeg, properties: [.compressionFactor: 0.7])!
    }

    /// Big pictures are decoded at display size (not 3000 × 2000 × 4 bytes each) and keep
    /// their natural size for layout.
    @Test func downsamplesToDisplaySize() throws {
        let data = Self.jpeg(width: 3000, height: 2000)
        let decoded = try #require(ImagePipeline.downsample(data, maxPixelSize: 1600))
        #expect(decoded.image.width == 1600)
        #expect(decoded.image.height <= 1067)
        #expect(decoded.size == CGSize(width: 3000, height: 2000))
    }

    @Test func loadsFileImagesOffTheMainThreadAndCaches() async throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "pipeline-\(UUID().uuidString).jpg")
        try Self.jpeg(width: 2400, height: 1200).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        let pipeline = ImagePipeline()
        #expect(pipeline.cached(url) == nil)
        let image = await pipeline.image(for: url, session: .shared)
        #expect(image?.size == CGSize(width: 2400, height: 1200))
        #expect(pipeline.cached(url) === image)
    }
}
