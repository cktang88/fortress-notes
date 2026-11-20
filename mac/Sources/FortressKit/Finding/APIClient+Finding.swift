import Foundation

/// `GET /api/block-documents/{id}/info`: dates, how the note came in, and counts
/// (spec.md §7a). Every field the inspector doesn't strictly need is optional, so older
/// or newer backends still decode.
public struct DocumentInfo: Decodable, Hashable, Sendable {
    public struct FolderRef: Decodable, Hashable, Sendable {
        public var id: String
        public var name: String
    }

    public struct Counts: Decodable, Hashable, Sendable {
        public var words: Int?
        public var characters: Int?
        public var blocks: Int?
        public var outgoingLinks: Int?
        public var backlinks: Int?

        enum CodingKeys: String, CodingKey {
            case words, characters, blocks, backlinks
            case outgoingLinks = "outgoing_links"
        }
    }

    public var id: String
    public var title: String
    public var createdAt: String?
    public var updatedAt: String?
    public var lastOpenedAt: String?
    public var revision: Int?
    public var folderPath: [FolderRef]?
    /// manual | capture | markdown_import | onenote_import | ai | any lower-case word.
    public var source: String?
    public var counts: Counts?
    /// Text read from each picture and PDF of the note (OCR), not part of its body.
    public var pictureText: [PictureText]?

    enum CodingKeys: String, CodingKey {
        case id, title, revision, source, counts
        case pictureText = "picture_text"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case lastOpenedAt = "last_opened_at"
        case folderPath = "folder_path"
    }
}

/// Endpoints only Finding uses (kept out of `APIClient.swift`).
extension APIClient {
    /// Through the client's store: coalesced with identical requests in flight.
    public func documentInfo(id: String) async throws -> DocumentInfo {
        try await get("api/block-documents/\(id)/info")
    }
}
