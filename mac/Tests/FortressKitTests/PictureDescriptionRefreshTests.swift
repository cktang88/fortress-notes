import Testing
@testable import FortressKit

/// The inspector shows a picture's generated description once it arrives, without
/// waiting for the note to reload.
@Suite struct PictureDescriptionRefreshTests {
    static func picture(_ name: String, mime: String = "image/png", description: String? = nil) -> PictureText {
        PictureText(blockId: "b-\(name)", mediaName: name, contentHash: name, mimeType: mime, status: "done",
                    generatedDescription: description)
    }

    @Test func awaitsDescriptionOnlyForUndescribedPictures() {
        var info = NoteInfo()
        #expect(!info.awaitsDescription)
        info.pictures = [Self.picture("scan.pdf", mime: "application/pdf")]
        #expect(!info.awaitsDescription)  // PDFs are never described.
        info.pictures.append(Self.picture("dog.png", description: "a dog on the beach"))
        #expect(!info.awaitsDescription)
        info.pictures.append(Self.picture("new.png"))
        #expect(info.awaitsDescription)
    }

    @Test func cacheDropsOnlyMatchingEntries() {
        var cache = LRUCache<String, Int>(capacity: 4)
        for (key, value) in [("a", 1), ("b", 2), ("c", 3)] { cache.insert(value, for: key) }
        cache.removeAll { $0 % 2 == 1 }
        #expect(cache.count == 1)
        #expect(cache.peek("b") == 2)
        #expect(cache.peek("a") == nil && cache.peek("c") == nil)
    }

    @MainActor @Test func countChangeWithNothingAwaitingAsksNothing() async {
        let stub = FindingPerf.InspectorRequestTests.stub(latency: 0.01)
        let inspector = InspectorModel(seenDebounce: .milliseconds(50), infoDebounce: .milliseconds(50))
        inspector.show(noteId: InspectorModelTests.note, revision: "r1", fallback: NoteInfo(), api: stub.api)
        await eventually { stub.log.finished.count >= 3 }
        let before = stub.started.count
        inspector.descriptionsChanged(PictureDescriptionStatus(enabled: true, state: "describing", done: 1), api: stub.api)
        inspector.descriptionsChanged(PictureDescriptionStatus(enabled: true, state: "describing", done: 2), api: stub.api)
        try? await Task.sleep(for: .milliseconds(200))
        #expect(stub.started.count == before)  // The open note has no picture awaiting one.
    }
}
