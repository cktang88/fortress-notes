import AppKit
import Testing

@testable import FortressKit

// Gallery of the block tools, light and dark (written to $SNAPSHOT_DIR): the handle on
// hover, its menu, the `/` menu, selected blocks, Show Blocks.

@MainActor
private enum BlockGallery {
    static func blocks() -> [EditorBlock] {
        [
            EditorBlock(id: "g-h", kind: .heading1, runs: [InlineRun("Kyoto in November")]),
            EditorBlock(id: "g-p", runs: [InlineRun("Leaves turn late this year; the ryokan holds our room until "),
                                          InlineRun("Friday", InlineMarks(bold: true)), InlineRun(".")]),
            EditorBlock(id: "g-b1", kind: .bullet, runs: [InlineRun("Tōfuku-ji at opening time")]),
            EditorBlock(id: "g-b2", kind: .bullet, depth: 1, runs: [InlineRun("bridge first, then the garden")]),
            EditorBlock(id: "g-c", kind: .check, checked: true, runs: [InlineRun("Book the Arashiyama train")]),
            EditorBlock(id: "g-q", kind: .quote, runs: [InlineRun("Take the 6:40 — the platform fills by seven.")],
                        container: QuoteContainer(id: "g-qc", type: "callout")),
            EditorBlock(id: "g-k", kind: .columns, columns: [
                EditorColumn(id: "g-k1", blocks: [EditorBlock(id: "g-k1p", kind: .heading3, runs: [InlineRun("Morning")]),
                                                  EditorBlock(id: "g-k1b", runs: [InlineRun("Fushimi Inari, upper loop")])]),
                EditorColumn(id: "g-k2", blocks: [EditorBlock(id: "g-k2p", kind: .heading3, runs: [InlineRun("Evening")]),
                                                  EditorBlock(id: "g-k2b", runs: [InlineRun("Pontochō, early dinner")])]),
            ]),
            EditorBlock(id: "g-e", runs: []),
        ]
    }

    /// The editor in a window on screen.
    static func window(appearance: NSAppearance.Name) -> (BlockEditorController, NSWindow) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        app.appearance = NSAppearance(named: appearance)
        let controller = BlockEditorController(baseURL: URL(string: "http://127.0.0.1:9")!)
        let window = NSWindow(contentRect: CGRect(x: 80, y: 120, width: 820, height: 600),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: appearance)
        window.isReleasedWhenClosed = false
        window.title = "Kyoto in November"
        window.contentView = controller.scrollView
        controller.noteReference = { (id: "g", title: "Kyoto in November") }
        controller.load(blocks())
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        app.activate()
        window.makeFirstResponder(controller.textView)
        pumpRunLoop(0.6)
        return (controller, window)
    }

    /// The window, or the screen area it covers (so menus and popovers over it show too).
    static func capture(_ window: NSWindow, _ name: String, screenArea: Bool = false) throws {
        guard let directory = ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] else { return }
        try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        let url = URL(fileURLWithPath: directory).appendingPathComponent("\(name).png")
        let capture = Process()
        capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        if screenArea, let screen = window.screen ?? NSScreen.main {
            let frame = window.frame
            let top = screen.frame.maxY - frame.maxY
            capture.arguments = ["-x", "-R", "\(Int(frame.minX)),\(Int(top)),\(Int(frame.width)),\(Int(frame.height))", url.path]
        } else {
            capture.arguments = ["-x", "-o", "-l", String(window.windowNumber), url.path]
        }
        try capture.run()
        capture.waitUntilExit()
    }

    static func point(_ controller: BlockEditorController, onBlock id: String, dx: CGFloat = 12) -> CGPoint {
        let range = controller.range(ofBlock: id)!
        let frame = controller.selectionFrame(controller.paragraph(at: range.location))
        return CGPoint(x: controller.textView.textContainerOrigin.x + dx, y: frame.minY + 6)
    }
}

@MainActor
@Suite(.serialized, .enabled(if: ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] != nil))
struct BlockToolsSnapshotTests {
    private let appearances: [(NSAppearance.Name, String)] = [(.aqua, "light"), (.darkAqua, "dark")]

    @Test func handleOnHover() throws {
        for (appearance, suffix) in appearances {
            let (controller, window) = BlockGallery.window(appearance: appearance)
            controller.hover(at: BlockGallery.point(controller, onBlock: "g-b1"))
            pumpRunLoop(0.3)
            try BlockGallery.capture(window, "blocks-handle-\(suffix)")
            window.close()
        }
    }

    @Test func blockMenu() throws {
        for (appearance, suffix) in appearances {
            let (controller, window) = BlockGallery.window(appearance: appearance)
            controller.hover(at: BlockGallery.point(controller, onBlock: "g-p"))
            let hover = controller.blockHandle
            let menu = try #require(hover.prepareMenu())
            pumpRunLoop(0.3)
            let name = "blocks-menu-\(suffix)"
            // The menu tracks on the main thread; the timer fires there, during tracking.
            nonisolated(unsafe) let tracked = menu
            nonisolated(unsafe) let shown = window
            let timer = Timer(timeInterval: 0.8, repeats: false) { _ in
                MainActor.assumeIsolated {
                    try? BlockGallery.capture(shown, name, screenArea: true)
                    tracked.cancelTracking()
                }
            }
            RunLoop.main.add(timer, forMode: .common)
            _ = menu.popUp(positioning: nil, at: NSPoint(x: 0, y: hover.grip.bounds.maxY + 4), in: hover.grip)
            window.close()
        }
    }

    @Test func slashMenu() throws {
        for (appearance, suffix) in appearances {
            let (controller, window) = BlockGallery.window(appearance: appearance)
            let empty = try #require(controller.range(ofBlock: "g-e"))
            controller.textView.setSelectedRange(NSRange(location: empty.location, length: 0))
            controller.textView.insertText("/", replacementRange: NSRange(location: NSNotFound, length: 0))
            pumpRunLoop(0.6)
            #expect(controller.slashMenu.isActive)
            try BlockGallery.capture(window, "blocks-slash-\(suffix)", screenArea: true)
            controller.slashMenu.close()
            window.close()
        }
    }

    @Test func selectedBlocks() throws {
        for (appearance, suffix) in appearances {
            let (controller, window) = BlockGallery.window(appearance: appearance)
            let start = try #require(controller.range(ofBlock: "g-b1"))
            controller.textView.setSelectedRange(NSRange(location: start.location + 2, length: 0))
            controller.textView.doCommand(by: #selector(NSResponder.cancelOperation(_:)))
            controller.textView.doCommand(by: #selector(NSResponder.moveDownAndModifySelection(_:)))
            pumpRunLoop(0.3)
            #expect(controller.blockSelection?.head == "g-c")
            try BlockGallery.capture(window, "blocks-selection-\(suffix)")
            window.close()
        }
    }

    @Test func showBlocks() throws {
        defer { UserDefaults.standard.removeObject(forKey: BlockOutlines.key) }
        for (appearance, suffix) in appearances {
            UserDefaults.standard.set(true, forKey: BlockOutlines.key)
            let (controller, window) = BlockGallery.window(appearance: appearance)
            controller.hover(at: BlockGallery.point(controller, onBlock: "g-c"))
            pumpRunLoop(0.3)
            try BlockGallery.capture(window, "blocks-outlines-\(suffix)")
            window.close()
        }
    }
}
