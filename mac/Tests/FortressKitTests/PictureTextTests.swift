import AppKit
import CoreText
import CryptoKit
import Foundation
import ImageIO
import Synchronization
import Testing
import UniformTypeIdentifiers

@testable import FortressKit

// MARK: Fakes

/// The backend's OCR endpoints, in memory: a queue of files and the readings stored.
actor FakeOCRBackend: PictureTextBackend {
    var files: [String: (data: Data, mime: String)] = [:]
    /// Newest first, like the backend.
    var order: [String] = []
    var stored: [String: OCRResult] = [:]
    var pendingAsks = 0
    var refuse: Set<String> = []
    var offline = false

    func add(_ hash: String, _ data: Data, mime: String = "image/png") {
        files[hash] = (data, mime)
        order.insert(hash, at: 0)
    }

    func setOffline(_ value: Bool) { offline = value }
    func setRefused(_ hashes: Set<String>) { refuse = hashes }

    func ocrPending(model: String, limit: Int) async throws -> OCRPending {
        pendingAsks += 1
        if offline { throw URLError(.cannotConnectToHost) }
        let left = order.filter { stored[$0]?.model != model }
        return OCRPending(model: model, total: left.count, items: left.prefix(limit).map {
            OCRPending.Item(contentHash: $0, mediaURL: "/media/\($0).png", mimeType: files[$0]!.mime)
        })
    }

    func media(at path: String) async throws -> Data {
        let hash = String(path.dropFirst("/media/".count).dropLast(".png".count))
        guard let file = files[hash] else { throw APIError(status: 404, message: "Not Found") }
        return file.data
    }

    func storeOCR(contentHash: String, result: OCRResult) async throws {
        if refuse.contains(contentHash) { throw APIError(status: 404, message: "gone") }
        stored[contentHash] = result
    }
}

/// Answers with the file's bytes as text after a pause; "bad" files can't be read.
final class FakeRecognizer: TextRecognizer, Sendable {
    let model = "apple-vision/test"
    let delay: Duration
    private let running = Mutex((now: 0, peak: 0, calls: 0))

    init(delay: Duration = .milliseconds(30)) { self.delay = delay }

    var peak: Int { running.withLock { $0.peak } }
    var calls: Int { running.withLock { $0.calls } }

    func recognize(_ data: Data, mimeType: String) async throws -> RecognizedText {
        #expect(pthread_main_np() == 0, "Reading must happen off the main thread")
        running.withLock {
            $0.now += 1
            $0.calls += 1
            $0.peak = max($0.peak, $0.now)
        }
        defer { running.withLock { $0.now -= 1 } }
        try await Task.sleep(for: delay)
        let text = String(decoding: data, as: UTF8.self)
        if text == "bad" { throw UnreadableFile("The picture couldn’t be opened.") }
        return RecognizedText(text: text, method: mimeType == "application/pdf" ? "pdf-text" : "ocr", pages: nil)
    }
}

// MARK: The reader

@MainActor
@Suite struct PictureTextReaderTests {
    @Test func readsTheBacklogWithBoundedConcurrencyAndStoresEachReading() async {
        let backend = FakeOCRBackend()
        for index in 0..<11 { await backend.add("h\(index)", Data("text \(index)".utf8)) }
        let recognizer = FakeRecognizer()
        let reader = PictureTextReader(recognizer: recognizer, concurrency: 3, batchSize: 4,
                                       idleDelay: .seconds(60), retryDelay: .seconds(60))
        reader.start(backend: backend, key: "test")
        await eventually(timeout: 20) { reader.remaining == 0 && !reader.isWorking }
        reader.stop()

        let stored = await backend.stored
        #expect(stored.count == 11)
        #expect(stored["h7"]?.text == "text 7")
        #expect(stored["h7"]?.status == "done")
        #expect(stored["h7"]?.model == "apple-vision/test")
        #expect(stored.values.allSatisfy { $0.durationMs != nil })
        #expect(recognizer.calls == 11, "each file is read once")
        #expect(recognizer.peak <= 3)
        #expect(reader.peakConcurrency <= 3)
        #expect(reader.peakConcurrency >= 2, "reads overlap")
        #expect(reader.storedCount == 11)
        #expect(reader.reading.isEmpty)
    }

    @Test func anUnreadableFileIsStoredAsFailedAndTheRestGoOn() async {
        let backend = FakeOCRBackend()
        await backend.add("ok", Data("fine".utf8))
        await backend.add("bad", Data("bad".utf8))
        let reader = PictureTextReader(recognizer: FakeRecognizer(), concurrency: 1)
        #expect(await reader.readNextBatch(backend: backend) == .worked)
        let stored = await backend.stored
        #expect(stored["bad"]?.status == "failed")
        #expect(stored["bad"]?.error == "The picture couldn’t be opened.")
        #expect(stored["bad"]?.text == "")
        #expect(stored["ok"]?.text == "fine")
    }

    @Test func aRefusedReadingIsNotAskedForAgainAndTheReaderGoesIdle() async {
        let backend = FakeOCRBackend()
        await backend.add("gone", Data("x".utf8))
        await backend.add("fine", Data("y".utf8))
        await backend.setRefused(["gone"])
        let recognizer = FakeRecognizer(delay: .zero)
        let reader = PictureTextReader(recognizer: recognizer, concurrency: 2)
        #expect(await reader.readNextBatch(backend: backend) == .worked)
        #expect(reader.skipped == ["gone"])
        // "gone" still pends on the backend, but isn't read again: nothing to do.
        #expect(await reader.readNextBatch(backend: backend) == .idle)
        #expect(recognizer.calls == 2)
        #expect(reader.remaining == 1)
    }

    @Test func anUnreachableBackendBacksOffAndWakeLooksAtOnce() async {
        let backend = FakeOCRBackend()
        await backend.setOffline(true)
        let reader = PictureTextReader(recognizer: FakeRecognizer(delay: .zero), concurrency: 1,
                                       idleDelay: .seconds(600), retryDelay: .seconds(600))
        #expect(await reader.readNextBatch(backend: backend) == .unreachable)
        #expect(reader.lastError != nil)

        reader.start(backend: backend, key: "test")
        await eventually(timeout: 10) { reader.lastError != nil }
        let asks = await backend.pendingAsks
        await backend.setOffline(false)
        await backend.add("new", Data("pasted".utf8))
        reader.wake()  // instead of waiting ten minutes
        await eventually(timeout: 10) { reader.storedCount == 1 }
        #expect(await backend.pendingAsks > asks)
        #expect(reader.lastError == nil)
        reader.stop()
        #expect(!reader.isRunning)
    }

    @Test func startingAgainForTheSameBackendKeepsTheRunningReader() async {
        let backend = FakeOCRBackend()
        let reader = PictureTextReader(recognizer: FakeRecognizer(delay: .zero), idleDelay: .seconds(600))
        reader.start(backend: backend, key: "a")
        await eventually(timeout: 10) { reader.remaining == 0 }
        reader.start(backend: backend, key: "a")
        try? await Task.sleep(for: .milliseconds(100))
        #expect(await backend.pendingAsks == 1)
        reader.start(backend: backend, key: "b")
        try? await Task.sleep(for: .milliseconds(200))
        #expect(await backend.pendingAsks == 2)
        reader.stop()
    }
}

// MARK: Decoding

@Suite struct PictureTextDecodingTests {
    @Test func infoCarriesEachPicturesText() throws {
        let json = """
        {"id": "n1", "title": "Scans", "picture_text": [
          {"block_id": "b1", "media_name": "01J.png", "content_hash": "abc", "mime_type": "image/png",
           "status": "done", "method": "ocr", "pages": null, "error": null, "model": "apple-vision/26",
           "read_at": "2026-11-18T10:00:00+00:00", "text": "Привет", "text_chars": 6},
          {"block_id": "b2", "media_name": "01K.pdf", "content_hash": "def", "mime_type": "application/pdf",
           "status": "failed", "method": null, "pages": null, "error": "FileNotFoundError: 01K.pdf",
           "model": "rapidocr-ppocrv6-small/1", "read_at": null, "text": "", "text_chars": 0}
        ]}
        """
        let info = try JSONDecoder().decode(DocumentInfo.self, from: Data(json.utf8))
        let note = NoteInfo(info)
        #expect(note.pictures.count == 2)
        #expect(note.pictures[0].text == "Привет")
        #expect(!note.pictures[0].isTruncated)
        #expect(note.pictures[1].isPDF)
        #expect(!note.isReadingPictures)
        #expect(PictureTextCard.reason(note.pictures[1].error!) == "The file is missing.")
        #expect(PictureTextCard.readerName(model: "apple-vision/26", method: "pdf-text+ocr") == "PDF text + Apple Vision")
    }

    @Test func statusRemainingPrefersTheMacsBacklog() throws {
        let cpu = try JSONDecoder().decode(OCRStatus.self, from: Data(#"{"enabled": true, "queued": 12, "external": null}"#.utf8))
        #expect(cpu.remaining == 12)
        let mac = try JSONDecoder().decode(OCRStatus.self, from: Data(
            #"{"enabled": false, "queued": 12, "external": {"model": "apple-vision/26", "queued": 40, "done": 3, "failed": 0}}"#.utf8))
        #expect(mac.remaining == 40)
        let off = try JSONDecoder().decode(OCRStatus.self, from: Data(#"{"enabled": false, "queued": 12}"#.utf8))
        #expect(off.remaining == 0)
    }
}

// MARK: Real Apple Vision

/// Pictures and PDFs drawn at runtime, read by the real Vision framework.
@Suite struct VisionRecognizerTests {
    static func textImage(_ lines: [String], size: CGFloat = 44, transparent: Bool = false) -> CGImage {
        let width = 1400, height = Int(CGFloat(lines.count) * size * 1.8 + 80)
        let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        if !transparent {
            context.setFillColor(CGColor(gray: 1, alpha: 1))
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }
        draw(lines, size: size, in: context, height: CGFloat(height))
        return context.makeImage()!
    }

    static func draw(_ lines: [String], size: CGFloat, in context: CGContext, height: CGFloat, x: CGFloat = 40) {
        let font = CTFontCreateWithName("Helvetica" as CFString, size, nil)
        for (index, line) in lines.enumerated() {
            let attributed = NSAttributedString(string: line, attributes: [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTForegroundColorFromContextAttributeName as String): true,
            ])
            context.setFillColor(CGColor(gray: 0, alpha: 1))
            let ctLine = CTLineCreateWithAttributedString(attributed)
            context.textPosition = CGPoint(x: x, y: height - 40 - size * 1.8 * CGFloat(index + 1) + size * 0.5)
            CTLineDraw(ctLine, context)
        }
    }

    static func png(_ image: CGImage) -> Data {
        let data = NSMutableData()
        let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, image, nil)
        CGImageDestinationFinalize(destination)
        return data as Data
    }

    @Test func readsLatinAndCyrillicFromAPicture() async throws {
        let data = Self.png(Self.textImage(["Invoice 48213 Northwind", "Счёт на оплату Москва"]))
        let found = try await VisionTextRecognizer().recognize(data, mimeType: "image/png")
        print("VISION picture: \(found.text.replacingOccurrences(of: "\n", with: " | "))")
        #expect(found.method == "ocr")
        #expect(found.text.contains("Northwind"))
        #expect(found.text.contains("48213"))
        #expect(found.text.lowercased().contains("москва"))
    }

    /// The macOS 26 document request itself (not only through its fallback).
    @Test func theDocumentRequestReadsParagraphs() async throws {
        let image = Self.textImage(["Meeting notes", "Ship the Vision reader on Friday"])
        let text = try await VisionTextRecognizer.readDocument(image, orientation: .up)
        print("VISION document: \(text.replacingOccurrences(of: "\n", with: " | "))")
        #expect(text.contains("Friday"))
    }

    @Test func readsDarkTextOnATransparentScreenshot() async throws {
        let data = Self.png(Self.textImage(["Quarterly offsite in Reykjavik"], transparent: true))
        let found = try await VisionTextRecognizer().recognize(data, mimeType: "image/png")
        #expect(found.text.contains("Reykjavik"))
    }

    @Test func aPDFUsesItsTextLayerAndReadsOnlyTheScannedPage() async throws {
        let data = NSMutableData()
        var box = CGRect(x: 0, y: 0, width: 612, height: 792)
        let consumer = CGDataConsumer(data: data as CFMutableData)!
        let pdf = CGContext(consumer: consumer, mediaBox: &box, nil)!
        pdf.beginPDFPage(nil)
        Self.draw(["Lease agreement with Aurora Holdings"], size: 18, in: pdf, height: 792, x: 72)
        pdf.endPDFPage()
        pdf.beginPDFPage(nil)
        pdf.draw(Self.textImage(["Bill to Cobalt Logistics"], size: 60), in: CGRect(x: 36, y: 500, width: 540, height: 90))
        pdf.endPDFPage()
        pdf.closePDF()

        let found = try await VisionTextRecognizer().recognize(data as Data, mimeType: "application/pdf")
        print("VISION pdf: \(found.text.replacingOccurrences(of: "\n", with: " | "))")
        #expect(found.method == "pdf-text+ocr")
        #expect(found.pages == 2)
        #expect(found.text.contains("Aurora Holdings"))
        #expect(found.text.contains("Cobalt"))
    }

    @Test func aBrokenFileIsAReadableError() async {
        await #expect(throws: UnreadableFile.self) {
            _ = try await VisionTextRecognizer().recognize(Data("not a picture".utf8), mimeType: "image/png")
        }
    }
}

// MARK: Against the real backend

/// A pasted screenshot read by Apple Vision on this Mac, stored on the real backend
/// (the `integration` CI job), whose text then outranks the CPU model's.
@MainActor
@Suite(.serialized, .enabled(if: IntegrationBackend.url != nil))
struct PictureTextIntegrationTests {
    @Test func visionReadingReachesTheBackend() async throws {
        let api = APIClient(baseURL: IntegrationBackend.url!)
        let unique = Int.random(in: 10_000...99_999)
        let data = VisionRecognizerTests.png(VisionRecognizerTests.textImage(
            ["Boarding pass \(unique)", "Посадочный талон Zeppelin"]))
        _ = try await EditorUpload.upload(data, name: "pass.png", type: "image/png", baseURL: api.baseURL, session: api.session)
        let hash = SHA256Hex.of(data)

        let reader = PictureTextReader(concurrency: 2)
        var outcome = PictureTextReader.BatchOutcome.worked
        while outcome == .worked { outcome = await reader.readNextBatch(backend: api) }
        #expect(outcome == .idle)
        #expect(reader.remaining == 0)

        let reading = try await api.ocrReading(contentHash: hash)
        print("[ocr] \(reading.model): \(reading.text.replacingOccurrences(of: "\n", with: " | "))")
        #expect(reading.model == VisionTextRecognizer.model)
        #expect(reading.text.contains("\(unique)"))
        let status = try await api.ocrStatus()
        #expect(status.external?.model == VisionTextRecognizer.model)
        #expect(status.external?.queued == 0)
    }
}

enum SHA256Hex {
    static func of(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
