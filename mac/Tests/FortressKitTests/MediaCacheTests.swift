import AppKit
import Foundation
import Testing

@testable import FortressKit

/// Images: one download however many ask, decoded off the main thread, then from memory.
/// (In the data-layer suite: it shares `SpyProtocol`, so it must not run alongside it.)
extension DataLayerTests {
    @Test func concurrentAsksShareOneDownloadThenComeFromMemory() async throws {
        let jpeg = try Data(contentsOf: try #require(Bundle.module.url(forResource: "kyoto", withExtension: "jpg", subdirectory: "Fixtures")))
        SpyProtocol.reset { _ in SpyProtocol.Reply(status: 200, body: jpeg, headers: ["Content-Type": "image/jpeg"], delay: 0.05) }
        let directory = FileManager.default.temporaryDirectory.appending(path: "media-cache-\(UUID().uuidString)")
        let cache = MediaCache(directory: directory, protocolClasses: [SpyProtocol.self])
        let url = URL(string: "http://spy.local/media/kyoto.jpg")!

        async let a = cache.image(for: url)
        async let b = cache.image(for: url)
        async let c = cache.image(for: url)
        let images = await [a, b, c]
        #expect(images.allSatisfy { $0 != nil })
        #expect(SpyProtocol.count("GET", "/media/kyoto.jpg") == 1)
        #expect(cache.cachedImage(for: url) != nil)
        #expect(await cache.image(for: url) != nil)
        #expect(cache.stats.downloads == 1)
    }

    @Test func brokenImagesAreNil() async throws {
        SpyProtocol.reset { seen in
            seen.path.hasSuffix("missing.png")
                ? SpyProtocol.Reply(status: 404, body: Data())
                : SpyProtocol.Reply(status: 200, body: Data("not an image".utf8))
        }
        let cache = MediaCache(directory: FileManager.default.temporaryDirectory.appending(path: "media-\(UUID().uuidString)"),
                               protocolClasses: [SpyProtocol.self])
        #expect(await cache.image(for: URL(string: "http://spy.local/media/missing.png")!) == nil)
        #expect(await cache.image(for: URL(string: "http://spy.local/media/garbage.png")!) == nil)
    }
}
