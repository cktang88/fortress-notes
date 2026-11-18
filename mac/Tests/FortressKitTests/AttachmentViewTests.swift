import AppKit
import SwiftUI
import Testing

@testable import FortressKit

@MainActor
@Suite struct AttachmentViewTests {
    /// TextKit re-adds attachment views to the viewport on every layout pass (each key):
    /// a picture keeps its one view, drawn by AppKit (a hosting view would re-render its
    /// SwiftUI on each move), and it stays on screen.
    @Test func typingNextToAPictureKeepsItsView() throws {
        let editor = PerfEditor(document: LongNote.document())
        editor.open()
        editor.controller.finishLoading()
        let image = editor.range(ofBlock: "i50")
        let attachment = try #require(editor.controller.storage.attribute(.attachment, at: image.location, effectiveRange: nil) as? BlockAttachment)
        let after = NSRange(location: NSMaxRange(editor.controller.storage.nsString.paragraphRange(at: image.location)) + 5, length: 0)
        editor.controller.textView.setSelectedRange(after)
        editor.controller.scrollRangeToVisibleForTests(after)
        editor.layoutViewport()
        editor.type("x")
        let view = attachment.view
        #expect(view is ImageAttachmentView)
        #expect(view.isDescendant(of: editor.controller.textView), "the picture is on screen")
        for key in ["a", "b", " ", "c"] { editor.type(key) }
        #expect(attachment.view === view)
        #expect(view.isDescendant(of: editor.controller.textView))
        // No picture is hosted in SwiftUI.
        func hosted(_ view: NSView) -> Int {
            view.subviews.reduce(view is NSHostingView<BlockEmbedView> ? 1 : 0) { $0 + hosted($1) }
        }
        let pictures = editor.viewportKinds.components(separatedBy: ",").filter { $0 == "image" }.count
        #expect(pictures > 0)
        #expect(hosted(editor.controller.textView) == 0)
    }

    /// A picture that arrives after its view is shown is drawn without being rebuilt.
    @Test func aPictureArrivingRedrawsTheSameView() {
        let slot = ImageSlot()
        var shown: [NSImage] = []
        slot.whenLoaded { shown.append($0) }
        let image = NSImage(size: CGSize(width: 4, height: 3))
        slot.fill(image)
        #expect(shown.count == 1 && shown.first === image)
        slot.whenLoaded { shown.append($0) }
        #expect(shown.count == 2, "a late ask gets the picture at once")
    }
}
