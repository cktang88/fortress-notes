import Foundation

/// One picture or PDF of a note and the text read from it (`picture_text` in
/// `GET /api/block-documents/{id}/info`; backend/app/sources.py).
public struct PictureText: Decodable, Hashable, Sendable, Identifiable {
    public var blockId: String
    public var mediaName: String
    public var contentHash: String?
    public var mimeType: String?
    /// queued, running (being read, or waiting for a retry), done, failed.
    public var status: String
    /// ocr, pdf-text, pdf-text+ocr.
    public var method: String?
    public var pages: Int?
    public var error: String?
    /// The reading whose text is used: `apple-vision/26`, `rapidocr-…`.
    public var model: String?
    public var readAt: String?
    /// The start of the text (the backend sends at most a few thousand characters).
    public var text: String
    /// The length of the whole text.
    public var textChars: Int?
    /// What a picture shows, in a sentence the backend's optional local model wrote
    /// (Settings › General › Picture Descriptions; backend/app/captions.py). Searchable
    /// like the text, never part of the note; nil until described.
    public var generatedDescription: String?

    public var id: String { "\(blockId)/\(mediaName)" }
    public var isPDF: Bool { mimeType == "application/pdf" || mediaName.lowercased().hasSuffix(".pdf") }
    /// Python counts code points, as `unicodeScalars` does.
    public var isTruncated: Bool { (textChars ?? 0) > text.unicodeScalars.count }
    public var isFinished: Bool { status == "done" || status == "failed" }

    public init(blockId: String, mediaName: String, contentHash: String? = nil, mimeType: String? = nil,
                status: String, method: String? = nil, pages: Int? = nil, error: String? = nil,
                model: String? = nil, readAt: String? = nil, text: String = "", textChars: Int? = nil,
                generatedDescription: String? = nil) {
        self.blockId = blockId
        self.mediaName = mediaName
        self.contentHash = contentHash
        self.mimeType = mimeType
        self.status = status
        self.method = method
        self.pages = pages
        self.error = error
        self.model = model
        self.readAt = readAt
        self.text = text
        self.textChars = textChars ?? text.unicodeScalars.count
        self.generatedDescription = generatedDescription
    }

    enum CodingKeys: String, CodingKey {
        case status, method, pages, error, model, text
        case blockId = "block_id"
        case mediaName = "media_name"
        case contentHash = "content_hash"
        case mimeType = "mime_type"
        case readAt = "read_at"
        case textChars = "text_chars"
        case generatedDescription = "description"
    }
}

/// `GET /api/ocr`: the workspace's background reading of pictures and PDFs.
public struct OCRStatus: Decodable, Hashable, Sendable {
    /// The Mac app's reader while it is working.
    public struct External: Decodable, Hashable, Sendable {
        public var model: String
        public var queued: Int
        public var done: Int?
        public var failed: Int?
    }

    /// Whether the backend's own (CPU) reader runs.
    public var enabled: Bool?
    public var model: String?
    /// Pictures and PDFs nobody has read yet.
    public var queued: Int
    public var done: Int?
    public var failed: Int?
    public var running: Bool?
    public var external: External?

    /// How many pictures and PDFs are still to be read: the Mac's own backlog while it
    /// reads (it re-reads what the CPU model read), else what nobody has read.
    public var remaining: Int {
        if let external { return external.queued }
        return enabled == false ? 0 : queued
    }
}

/// `GET /api/ocr/pending`: what an external reader has yet to read.
public struct OCRPending: Decodable, Hashable, Sendable {
    public struct Item: Decodable, Hashable, Sendable {
        public var contentHash: String
        public var mediaURL: String
        public var mimeType: String
        public var attempts: Int?

        public init(contentHash: String, mediaURL: String, mimeType: String, attempts: Int? = nil) {
            self.contentHash = contentHash
            self.mediaURL = mediaURL
            self.mimeType = mimeType
            self.attempts = attempts
        }

        enum CodingKeys: String, CodingKey {
            case attempts
            case contentHash = "content_hash"
            case mediaURL = "media_url"
            case mimeType = "mime_type"
        }
    }

    public var model: String
    public var total: Int
    public var items: [Item]

    public init(model: String, total: Int, items: [Item]) {
        self.model = model
        self.total = total
        self.items = items
    }
}

/// What a reader found (or why it couldn't), as `PUT /api/ocr/{content_hash}` takes it.
public struct OCRResult: Encodable, Hashable, Sendable {
    public var model: String
    /// done or failed.
    public var status: String
    public var text: String = ""
    public var method: String = "ocr"
    public var pages: Int?
    public var error: String?
    public var durationMs: Int?

    enum CodingKeys: String, CodingKey {
        case model, status, text, method, pages, error
        case durationMs = "duration_ms"
    }
}

/// `GET /api/ocr/{content_hash}`: all the text of one picture or PDF.
public struct OCRReading: Decodable, Hashable, Sendable {
    public var model: String
    public var text: String
}

/// What the picture reader needs from the backend (a fake in tests).
public protocol PictureTextBackend: Sendable {
    func ocrPending(model: String, limit: Int) async throws -> OCRPending
    func media(at path: String) async throws -> Data
    func storeOCR(contentHash: String, result: OCRResult) async throws
}

extension APIClient: PictureTextBackend {
    public func ocrStatus() async throws -> OCRStatus {
        try await get("api/ocr")
    }

    public func ocrReading(contentHash: String) async throws -> OCRReading {
        try await get("api/ocr/\(contentHash)")
    }

    public func ocrPending(model: String, limit: Int) async throws -> OCRPending {
        // Not coalesced or cached: each ask also tells the backend the reader is active.
        let request = try makeRequest("GET", "api/ocr/pending", query: ["model": model, "limit": "\(limit)"])
        let (data, response) = try await load(request)
        try check(response, body: data)
        return try Self.decode(OCRPending.self, from: data)
    }

    /// The bytes of a picture or PDF (`/media/…`, relative to the backend).
    public func media(at path: String) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.timeoutInterval = RequestKind.upload.timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await load(request)
        try check(response, body: nil)
        return data
    }

    /// Stores a reading. Not a "mutation" for the client's caches: no note changes, and
    /// the info that shows it is ETag-validated anyway.
    public func storeOCR(contentHash: String, result: OCRResult) async throws {
        var request = try makeRequest("PUT", "api/ocr/\(contentHash)", body: try JSONEncoder().encode(result))
        request.timeoutInterval = RequestKind.mutation.timeout
        let (data, response) = try await load(request)
        try check(response, body: data)
    }
}
