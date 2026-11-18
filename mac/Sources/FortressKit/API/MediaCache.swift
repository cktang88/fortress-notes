import AppKit
import ImageIO
import Synchronization

/// Images and files from `/media/…`: decoded images in memory (NSCache, cleared under
/// memory pressure), bytes on disk (URLCache: the backend's StaticFiles answers carry
/// ETag/Last-Modified, so a revisit is a 304 or no request at all). Decoding happens off
/// the main thread, and concurrent requests for one URL share one download.
public final class MediaCache: Sendable {
    public static let shared = MediaCache()

    /// A decoded image (NSImage isn't Sendable; this one is never mutated once made).
    public final class Image: @unchecked Sendable {
        public let image: NSImage
        init(_ image: NSImage) { self.image = image }
    }

    /// `@unchecked Sendable`: NSCache is documented thread-safe, and `cache` is a `let`.
    private final class Memory: @unchecked Sendable {
        let cache = NSCache<NSURL, Image>()
    }

    private let memory = Memory()
    let session: URLSession
    private let inFlight = Mutex<[URL: Task<Image?, Never>]>([:])
    private let counters = Mutex<(downloads: Int, memoryHits: Int)>((0, 0))

    /// - Parameters:
    ///   - memoryBytes: decoded image budget (NSCache cost is width × height × 4).
    ///   - diskBytes: downloaded bytes kept on disk between launches.
    public init(memoryBytes: Int = 128 << 20, diskBytes: Int = 512 << 20, directory: URL? = nil, protocolClasses: [AnyClass]? = nil) {
        memory.cache.totalCostLimit = memoryBytes
        let configuration = URLSessionConfiguration.default
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = RequestKind.document.timeout
        configuration.requestCachePolicy = .useProtocolCachePolicy
        configuration.httpAdditionalHeaders = BackendAuth.headers
        let folder = directory ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first?
            .appending(path: "Fortress Notes/Media")
        configuration.urlCache = URLCache(memoryCapacity: 16 << 20, diskCapacity: diskBytes, directory: folder)
        if let protocolClasses { configuration.protocolClasses = protocolClasses + (configuration.protocolClasses ?? []) }
        session = URLSession(configuration: configuration)
    }

    /// The image when it is already decoded in memory (synchronous: for layout).
    public func cachedImage(for url: URL) -> NSImage? {
        guard let image = memory.cache.object(forKey: url as NSURL) else { return nil }
        counters.withLock { $0.memoryHits += 1 }
        return image.image
    }

    /// Downloads (or reads from disk) and decodes the image, once however many ask.
    public func image(for url: URL) async -> Image? {
        if let cached = memory.cache.object(forKey: url as NSURL) {
            counters.withLock { $0.memoryHits += 1 }
            return cached
        }
        let task = inFlight.withLock { tasks -> Task<Image?, Never> in
            if let running = tasks[url] { return running }
            let task = Task<Image?, Never>.detached(priority: .userInitiated) { [self] in
                let image = await self.load(url)
                _ = self.inFlight.withLock { $0.removeValue(forKey: url) }
                return image
            }
            tasks[url] = task
            return task
        }
        return await task.value
    }

    /// How many downloads went out, and how many asks memory answered (for tests).
    public var stats: (downloads: Int, memoryHits: Int) { counters.withLock { $0 } }

    private func load(_ url: URL) async -> Image? {
        let data: Data
        if url.isFileURL {
            guard let read = try? Data(contentsOf: url) else { return nil }
            data = read
        } else {
            counters.withLock { $0.downloads += 1 }
            guard let (body, response) = try? await session.data(from: url),
                  (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? true
            else { return nil }
            data = body
        }
        guard let image = Self.decode(data) else { return nil }
        let size = image.image.size
        memory.cache.setObject(image, forKey: url as NSURL, cost: max(1, Int(size.width * size.height * 4)))
        return image
    }

    /// Decodes fully now (not lazily at first draw on the main thread). Formats ImageIO
    /// can't read (SVG ink drawings) fall back to NSImage.
    static func decode(_ data: Data) -> Image? {
        let options = [kCGImageSourceShouldCacheImmediately: true] as CFDictionary
        if let source = CGImageSourceCreateWithData(data as CFData, nil),
           let cgImage = CGImageSourceCreateImageAtIndex(source, 0, options) {
            var size = CGSize(width: cgImage.width, height: cgImage.height)
            // Respect the image's DPI (a Retina screenshot is half its pixel size).
            if let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
               let dpi = properties[kCGImagePropertyDPIWidth] as? Double, dpi > 72 {
                size = CGSize(width: size.width * 72 / dpi, height: size.height * 72 / dpi)
            }
            return Image(NSImage(cgImage: cgImage, size: size))
        }
        return NSImage(data: data).map(Image.init)
    }
}
