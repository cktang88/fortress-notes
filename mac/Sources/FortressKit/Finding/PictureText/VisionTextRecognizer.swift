import AppKit
import Foundation
import ImageIO
import PDFKit
import Vision

/// The text found in a picture or PDF.
public struct RecognizedText: Sendable, Hashable {
    public var text: String
    /// ocr, pdf-text (the PDF's own text layer), pdf-text+ocr.
    public var method: String
    public var pages: Int?

    public init(text: String, method: String = "ocr", pages: Int? = nil) {
        self.text = text
        self.method = method
        self.pages = pages
    }
}

/// Reads the text in pictures and PDFs (Apple Vision in the app, a fake in tests).
public protocol TextRecognizer: Sendable {
    /// Names the reader and its version for the backend, e.g. `apple-vision/26`.
    var model: String { get }
    func recognize(_ data: Data, mimeType: String) async throws -> RecognizedText
}

public struct UnreadableFile: LocalizedError, Sendable {
    public var errorDescription: String?
    public init(_ reason: String) { errorDescription = reason }
}

/// Apple Vision, on this Mac: `RecognizeDocumentsRequest` (macOS 26: paragraphs and
/// tables in reading order, many scripts, handwriting, the language detected
/// automatically), falling back to `RecognizeTextRequest` in accurate mode.
///
/// PDFs use their own text layer; only pages without one (scans) are rendered and read,
/// like the backend's reader (backend/app/ocr.py).
public struct VisionTextRecognizer: TextRecognizer {
    /// Bump the suffix when what is read changes (requests, pre-processing): the backend
    /// then has every picture read again, lazily.
    public static let model = "apple-vision/\(ProcessInfo.processInfo.operatingSystemVersion.majorVersion)"

    /// Like backend/app/ocr.py: a PDF page with fewer characters than this is read as a picture.
    static let pdfTextMinimumCharacters = 16
    static let pdfMaximumOCRPages = 100
    static let pdfRenderMaximumSide: CGFloat = 2000
    /// Pictures larger than this are scaled down first (Vision gains nothing past it).
    static let imageMaximumSide = 4096
    static let imageMinimumSide = 12
    static let maximumCharacters = 200_000

    public var model: String { Self.model }

    public init() {}

    public func recognize(_ data: Data, mimeType: String) async throws -> RecognizedText {
        if mimeType == "application/pdf" { return try await readPDF(data) }
        guard let (image, orientation) = Self.decodeImage(data) else {
            throw UnreadableFile("The picture couldn’t be opened.")
        }
        guard min(image.width, image.height) >= Self.imageMinimumSide else { return RecognizedText(text: "") }
        return RecognizedText(text: try await Self.read(image, orientation: orientation))
    }

    // MARK: Vision

    static func read(_ image: CGImage, orientation: CGImagePropertyOrientation) async throws -> String {
        let text: String
        do {
            text = try await readDocument(image, orientation: orientation)
        } catch {
            text = try await readLines(image, orientation: orientation)
        }
        return String(text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(maximumCharacters))
    }

    static func readDocument(_ image: CGImage, orientation: CGImagePropertyOrientation) async throws -> String {
        var request = RecognizeDocumentsRequest()
        request.textRecognitionOptions.automaticallyDetectLanguage = true
        request.textRecognitionOptions.useLanguageCorrection = true
        let observations = try await request.perform(on: image, orientation: orientation)
        return observations.map { $0.document.text.transcript }.joined(separator: "\n\n")
    }

    static func readLines(_ image: CGImage, orientation: CGImagePropertyOrientation) async throws -> String {
        var request = RecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.automaticallyDetectsLanguage = true
        request.usesLanguageCorrection = true
        let observations = try await request.perform(on: image, orientation: orientation)
        return observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
    }

    // MARK: Pictures

    /// The first frame, flattened onto white (dark text on a transparent screenshot must
    /// not become dark text on black), at most `imageMaximumSide`, and how it is rotated.
    static func decodeImage(_ data: Data) -> (CGImage, CGImagePropertyOrientation)? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              CGImageSourceGetCount(source) > 0 else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceThumbnailMaxPixelSize: imageMaximumSide,
            kCGImageSourceShouldCacheImmediately: true,
        ]
        let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
        let pixelWidth = properties?[kCGImagePropertyPixelWidth] as? Int ?? 0
        let pixelHeight = properties?[kCGImagePropertyPixelHeight] as? Int ?? 0
        let image: CGImage?
        if max(pixelWidth, pixelHeight) > imageMaximumSide {
            // The thumbnail is not rotated (no kCGImageSourceCreateThumbnailWithTransform):
            // Vision is told the orientation instead.
            image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        } else {
            image = CGImageSourceCreateImageAtIndex(source, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
        }
        guard let image else { return nil }
        let raw = (properties?[kCGImagePropertyOrientation] as? UInt32) ?? 1
        let orientation = CGImagePropertyOrientation(rawValue: raw) ?? .up
        return (flattened(image) ?? image, orientation)
    }

    static func flattened(_ image: CGImage) -> CGImage? {
        switch image.alphaInfo {
        case .none, .noneSkipFirst, .noneSkipLast: return image
        default: break
        }
        guard let context = CGContext(
            data: nil, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
        let rect = CGRect(x: 0, y: 0, width: image.width, height: image.height)
        context.setFillColor(CGColor(gray: 1, alpha: 1))
        context.fill(rect)
        context.draw(image, in: rect)
        return context.makeImage()
    }

    // MARK: PDFs

    func readPDF(_ data: Data) async throws -> RecognizedText {
        guard let document = PDFDocument(data: data) else { throw UnreadableFile("The PDF couldn’t be opened.") }
        if document.isLocked { throw UnreadableFile("The PDF is password-protected.") }
        var parts: [String] = []
        var ocrPages = 0
        var characters = 0
        for index in 0..<document.pageCount {
            try Task.checkCancellation()
            guard let page = document.page(at: index) else { continue }
            var text = (page.string ?? "").replacingOccurrences(of: "\r\n", with: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let visible = text.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) }.count
            if visible < Self.pdfTextMinimumCharacters, ocrPages < Self.pdfMaximumOCRPages,
               let image = Self.render(page) {
                ocrPages += 1
                let read = try await Self.read(image, orientation: .up)
                text = [text, read].filter { !$0.isEmpty }.joined(separator: "\n")
            }
            if !text.isEmpty {
                parts.append(text)
                characters += text.count
                if characters >= Self.maximumCharacters { break }
            }
        }
        let text = String(parts.joined(separator: "\n\n").prefix(Self.maximumCharacters))
        return RecognizedText(text: text, method: ocrPages > 0 ? "pdf-text+ocr" : "pdf-text", pages: document.pageCount)
    }

    /// The page as a picture: at most `pdfRenderMaximumSide` pixels and 300 dpi, on white.
    static func render(_ page: PDFPage) -> CGImage? {
        let bounds = page.bounds(for: .mediaBox)
        let rotated = page.rotation % 180 != 0
        let size = rotated ? CGSize(width: bounds.height, height: bounds.width) : bounds.size
        guard size.width > 0, size.height > 0 else { return nil }
        let scale = min(pdfRenderMaximumSide / max(size.width, size.height), 300 / 72)
        let width = max(1, Int(size.width * scale)), height = max(1, Int(size.height * scale))
        guard let context = CGContext(
            data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
        context.setFillColor(CGColor(gray: 1, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.scaleBy(x: scale, y: scale)
        // Applies the page's rotation and crop, so the text stands upright.
        page.transform(context, for: .mediaBox)
        page.draw(with: .mediaBox, to: context)
        return context.makeImage()
    }
}
