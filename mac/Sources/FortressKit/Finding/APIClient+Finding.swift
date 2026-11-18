import Foundation

/// `GET /api/block-documents/{id}/info`: dates, where the note came from, counts and
/// conflicts with its source (spec.md §7a). Every field the inspector doesn't strictly
/// need is optional, so older or newer backends still decode.
public struct DocumentInfo: Decodable, Hashable, Sendable {
    public struct FolderRef: Decodable, Hashable, Sendable {
        public var id: String
        public var name: String
    }

    public struct Provenance: Decodable, Hashable, Sendable {
        public struct Importer: Decodable, Hashable, Sendable {
            public var name: String
            public var version: String?
        }

        public struct Origin: Decodable, Hashable, Sendable {
            public var operation: String
            public var documentId: String?
            public var documentTitle: String?
            public var blockId: String?
            public var at: String?

            enum CodingKeys: String, CodingKey {
                case operation, at
                case documentId = "document_id"
                case documentTitle = "document_title"
                case blockId = "block_id"
            }
        }

        /// manual | capture | markdown_import | onenote_import | paste | web | ai | any lower-case word.
        public var source: String
        public var externalId: String?
        public var sourcePath: String?
        public var sourceUrl: String?
        public var sourceCreatedAt: String?
        public var sourceModifiedAt: String?
        public var importedAt: String?
        public var syncedAt: String?
        public var importer: Importer?
        public var origin: Origin?

        enum CodingKeys: String, CodingKey {
            case source, importer, origin
            case externalId = "external_id"
            case sourcePath = "source_path"
            case sourceUrl = "source_url"
            case sourceCreatedAt = "source_created_at"
            case sourceModifiedAt = "source_modified_at"
            case importedAt = "imported_at"
            case syncedAt = "synced_at"
        }
    }

    public struct Counts: Decodable, Hashable, Sendable {
        public var words: Int?
        public var characters: Int?
        public var blocks: Int?
        public var outgoingLinks: Int?
        public var backlinks: Int?
        public var edits: Int?

        enum CodingKeys: String, CodingKey {
            case words, characters, blocks, backlinks, edits
            case outgoingLinks = "outgoing_links"
        }
    }

    public struct Sync: Decodable, Hashable, Sendable {
        public var syncedAt: String?
        public var checkedAt: String?
        public var sourceTitle: String?
        public var blocksEditedHere: Int?

        enum CodingKeys: String, CodingKey {
            case syncedAt = "synced_at"
            case checkedAt = "checked_at"
            case sourceTitle = "source_title"
            case blocksEditedHere = "blocks_edited_here"
        }
    }

    public struct Conflict: Decodable, Hashable, Sendable {
        public var kind: String
        public var blockId: String?

        enum CodingKeys: String, CodingKey {
            case kind
            case blockId = "block_id"
        }
    }

    public var id: String
    public var title: String
    public var createdAt: String?
    public var updatedAt: String?
    public var lastOpenedAt: String?
    public var revision: Int?
    public var folderPath: [FolderRef]?
    public var provenance: Provenance?
    public var counts: Counts?
    public var blocksBySource: [String: Int]?
    public var sync: Sync?
    public var conflicts: [Conflict]?

    enum CodingKeys: String, CodingKey {
        case id, title, revision, provenance, counts, sync, conflicts
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case lastOpenedAt = "last_opened_at"
        case folderPath = "folder_path"
        case blocksBySource = "blocks_by_source"
    }
}

/// Endpoints only Finding uses (kept out of `APIClient.swift`).
extension APIClient {
    /// Through the client's store: coalesced with identical requests in flight, answered
    /// from the stored body on a 304, and counted in `store.stats`.
    public func documentInfo(id: String) async throws -> DocumentInfo {
        try await get("api/block-documents/\(id)/info")
    }
}
