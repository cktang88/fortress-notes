import AppKit
import ImageIO
import Observation

/// One picture's loading state. Each image view observes only its own slot, so a picture
/// arriving re-renders that picture, not every image in the note.
@MainActor
@Observable
final class ImageSlot {
    var image: NSImage?
    init(image: NSImage? = nil) { self.image = image }
}

/// Loads pictures for the editor: fetched through the app's URL session (and its URL
/// cache), decoded and downsampled off the main thread with ImageIO to at most the size
/// they are shown at, and kept in a memory cache shared by every open note.
@MainActor
final class ImagePipeline {
    static let shared = ImagePipeline()

    /// Longest side in pixels: the widest an image is shown (the measure, ~720 pt) at 2×.
    nonisolated static let maxPixelSize: CGFloat = 1600

    private let cache = NSCache<NSURL, NSImage>()
    private var loading: [URL: Task<Decoded?, Never>] = [:]

    /// A decoded picture handed from the decoding task to the main actor (immutable).
    struct Decoded: @unchecked Sendable {
        let image: CGImage
        let size: CGSize
    }

    init() {
        cache.totalCostLimit = 256 * 1024 * 1024
    }

    func cached(_ url: URL) -> NSImage? { cache.object(forKey: url as NSURL) }

    /// The picture at `url`, from the cache or loaded once however many ask at a time.
    func image(for url: URL, session: URLSession) async -> NSImage? {
        if let image = cached(url) { return image }
        let task: Task<Decoded?, Never>
        if let existing = loading[url] {
            task = existing
        } else {
            task = Task.detached(priority: .utility) { await Self.load(url, session: session) }
            loading[url] = task
        }
        guard let decoded = await task.value else {
            loading[url] = nil
            return nil
        }
        loading[url] = nil
        if let image = cached(url) { return image }
        let image = NSImage(cgImage: decoded.image, size: decoded.size)
        cache.setObject(image, forKey: url as NSURL, cost: decoded.image.bytesPerRow * decoded.image.height)
        return image
    }

    func removeAll() { cache.removeAllObjects() }

    /// Fetch and decode, off the main actor.
    nonisolated static func load(_ url: URL, session: URLSession) async -> Decoded? {
        let data: Data
        do {
            if url.isFileURL {
                data = try Data(contentsOf: url, options: .mappedIfSafe)
            } else {
                let (body, response) = try await session.data(from: url)
                if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) { return nil }
                data = body
            }
        } catch {
            return nil
        }
        return downsample(data, maxPixelSize: maxPixelSize)
    }

    /// A decoded thumbnail no larger than `maxPixelSize` on its longest side, and the
    /// point size to show it at (the original's pixel size, scaled the same way, at 1 px = 1 pt).
    nonisolated static func downsample(_ data: Data, maxPixelSize: CGFloat) -> Decoded? {
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary)
        else { return nil }
        var pixelSize = CGSize.zero
        if let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
           let width = properties[kCGImagePropertyPixelWidth] as? CGFloat,
           let height = properties[kCGImagePropertyPixelHeight] as? CGFloat {
            let orientation = properties[kCGImagePropertyOrientation] as? Int ?? 1
            pixelSize = orientation >= 5 ? CGSize(width: height, height: width) : CGSize(width: width, height: height)
        }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        let size = pixelSize.width > 0 ? pixelSize : CGSize(width: image.width, height: image.height)
        return Decoded(image: image, size: size)
    }
}
