import Foundation
import Synchronization

// Mirrors of the backend's JSON (snake_case keys are converted by `APIClient`'s
// decoder). Timestamps stay ISO-8601 strings; use `Timestamp.date(_:)` to display them.

public struct NoteSummary: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var updatedAt: String
    public var snippet: String

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case updatedAt = "updated_at"
        case snippet
    }
}

public struct NavigationDocument: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var updatedAt: String
    public var createdAt: String
    public var snippet: String
    public var folderId: String?
    public var position: Int

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case updatedAt = "updated_at"
        case createdAt = "created_at"
        case snippet
        case folderId = "folder_id"
        case position
    }
}

public struct NavigationFolder: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var parentId: String?
    public var name: String
    public var position: Int
    public var children: [NavigationNode]

    enum CodingKeys: String, CodingKey {
        case id
        case parentId = "parent_id"
        case name
        case position
        case children
    }
}

/// A sidebar entry: a folder (with children) or a note.
public enum NavigationNode: Codable, Hashable, Identifiable, Sendable {
    case folder(NavigationFolder)
    case document(NavigationDocument)

    public var id: String {
        switch self {
        case .folder(let folder): "folder:\(folder.id)"
        case .document(let document): "document:\(document.id)"
        }
    }

    private enum Kind: String, Codable { case folder, document }
    private enum Keys: String, CodingKey { case kind }

    public init(from decoder: Decoder) throws {
        let kind = try decoder.container(keyedBy: Keys.self).decode(Kind.self, forKey: .kind)
        switch kind {
        case .folder: self = .folder(try NavigationFolder(from: decoder))
        case .document: self = .document(try NavigationDocument(from: decoder))
        }
    }

    public func encode(to encoder: Encoder) throws {
        switch self {
        case .folder(let folder): try folder.encode(to: encoder)
        case .document(let document): try document.encode(to: encoder)
        }
    }
}

public struct NavigationResponse: Codable, Hashable, Sendable {
    public var items: [NavigationNode]
    public var recent: [NavigationDocument]
}

public struct Folder: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var parentId: String?
    public var name: String
    public var position: Int

    enum CodingKeys: String, CodingKey {
        case id
        case parentId = "parent_id"
        case name
        case position
    }
}

/// One block as `GET /api/block-documents/{id}` returns it (docs/block-model.md §1):
/// a `type`, `attrs`, inline `content` (text blocks: an array of inline nodes, else
/// null), a `payload` (html only), and its children in document order.
public struct BlockNode: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var documentId: String
    public var parentId: String?
    /// Sibling order key (server-assigned, read-only).
    public var order: String?
    public var type: String
    public var attrs: [String: JSONValue]
    public var userAttrs: [String: JSONValue]
    /// Inline nodes (text blocks), or `.null`.
    public var content: JSONValue
    /// Structured data (html `{html}`), or nil.
    public var payload: JSONValue?
    /// Derived plain text (read-only; never sent).
    public var text: String
    public var createdAt: String
    public var updatedAt: String
    public var children: [BlockNode]
    /// The note revision that last changed this block (nil: unchanged since creation).
    public var revision: Int? = nil

    public init(
        id: String, documentId: String = "", parentId: String? = nil, order: String? = nil, type: String,
        attrs: [String: JSONValue] = [:], userAttrs: [String: JSONValue] = [:], content: JSONValue = .null,
        payload: JSONValue? = nil, text: String = "", createdAt: String = "", updatedAt: String = "",
        children: [BlockNode] = [], revision: Int? = nil
    ) {
        self.id = id
        self.documentId = documentId
        self.parentId = parentId
        self.order = order
        self.type = type
        self.attrs = attrs
        self.userAttrs = userAttrs
        self.content = content
        self.payload = payload
        self.text = text
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.children = children
        self.revision = revision
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        documentId = try c.decodeIfPresent(String.self, forKey: .documentId) ?? ""
        parentId = try c.decodeIfPresent(String.self, forKey: .parentId)
        order = try c.decodeIfPresent(String.self, forKey: .order)
        type = try c.decode(String.self, forKey: .type)
        attrs = try c.decodeIfPresent([String: JSONValue].self, forKey: .attrs) ?? [:]
        userAttrs = try c.decodeIfPresent([String: JSONValue].self, forKey: .userAttrs) ?? [:]
        content = try c.decodeIfPresent(JSONValue.self, forKey: .content) ?? .null
        let payload = try c.decodeIfPresent(JSONValue.self, forKey: .payload)
        self.payload = payload == .null ? nil : payload
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt) ?? ""
        updatedAt = try c.decodeIfPresent(String.self, forKey: .updatedAt) ?? ""
        children = try c.decodeIfPresent([BlockNode].self, forKey: .children) ?? []
        revision = try c.decodeIfPresent(Int.self, forKey: .revision)
    }

    enum CodingKeys: String, CodingKey {
        case revision
        case id
        case documentId = "document_id"
        case parentId = "parent_id"
        case order
        case type
        case attrs
        case userAttrs = "user_attrs"
        case content
        case payload
        case text
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case children
    }
}

public struct BlockDocument: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var revision: Int
    public var titleAuto: Bool?
    public var title: String
    public var createdAt: String
    public var updatedAt: String
    public var children: [BlockNode]
    /// Current titles of linked notes and text of referenced blocks (living links).
    public var resolvedLinks: ResolvedLinks? = nil

    enum CodingKeys: String, CodingKey {
        case resolvedLinks = "resolved_links"
        case id
        case revision
        case titleAuto = "title_auto"
        case title
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case children
    }
}

/// `resolved_links` of a document payload.
public struct ResolvedLinks: Codable, Hashable, Sendable {
    public struct DocumentTarget: Codable, Hashable, Sendable {
        public var title: String
    }

    public struct BlockTarget: Codable, Hashable, Sendable {
        public var documentId: String?
        public var documentTitle: String?
        public var type: String?
        public var text: String

        enum CodingKeys: String, CodingKey {
            case documentId = "document_id"
            case documentTitle = "document_title"
            case type, text
        }
    }

    public var documents: [String: DocumentTarget]
    public var blocks: [String: BlockTarget]

    public init(documents: [String: DocumentTarget] = [:], blocks: [String: BlockTarget] = [:]) {
        self.documents = documents
        self.blocks = blocks
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        documents = (try? container.decode([String: DocumentTarget].self, forKey: .documents)) ?? [:]
        blocks = (try? container.decode([String: BlockTarget].self, forKey: .blocks)) ?? [:]
    }

    enum CodingKeys: String, CodingKey { case documents, blocks }
}

public enum BlockOperationKind: String, Codable, Sendable {
    case insert, update, move, delete
}

/// One operation of a transaction (docs/block-model.md §4). Placement (insert, move):
/// `afterId`, or `beforeId`, or neither (append under `parentId`, nil: top level).
/// An update carries only what changed; `attrs` is a patch (a `.null` value deletes
/// the key, keys not mentioned are kept).
public struct BlockOperation: Codable, Hashable, Sendable {
    public var operation: BlockOperationKind
    public var blockId: String?
    public var parentId: String?
    public var afterId: String?
    public var beforeId: String?
    public var type: String?
    public var attrs: [String: JSONValue]?
    public var content: JSONValue?
    public var payload: JSONValue?

    public init(
        operation: BlockOperationKind, blockId: String? = nil, parentId: String? = nil,
        afterId: String? = nil, beforeId: String? = nil, type: String? = nil,
        attrs: [String: JSONValue]? = nil, content: JSONValue? = nil, payload: JSONValue? = nil
    ) {
        self.operation = operation
        self.blockId = blockId
        self.parentId = parentId
        self.afterId = afterId
        self.beforeId = beforeId
        self.type = type
        self.attrs = attrs
        self.content = content
        self.payload = payload
    }

    enum CodingKeys: String, CodingKey {
        case operation
        case blockId = "block_id"
        case parentId = "parent_id"
        case afterId = "after_id"
        case beforeId = "before_id"
        case type
        case attrs
        case content
        case payload
    }

    /// Encodes only the fields that are set (`parent_id: null` would still mean the
    /// top level, but an update has no placement at all), with `.null` attrs kept as
    /// JSON nulls (they delete the key).
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(operation, forKey: .operation)
        try c.encodeIfPresent(blockId, forKey: .blockId)
        try c.encodeIfPresent(parentId, forKey: .parentId)
        try c.encodeIfPresent(afterId, forKey: .afterId)
        try c.encodeIfPresent(beforeId, forKey: .beforeId)
        try c.encodeIfPresent(type, forKey: .type)
        try c.encodeIfPresent(attrs, forKey: .attrs)
        try c.encodeIfPresent(content, forKey: .content)
        try c.encodeIfPresent(payload, forKey: .payload)
    }
}

public struct BlockTransaction: Codable, Hashable, Sendable {
    public var transactionId: String
    public var baseRevision: Int
    public var operations: [BlockOperation]

    public init(transactionId: String = UUID().uuidString, baseRevision: Int, operations: [BlockOperation]) {
        self.transactionId = transactionId
        self.baseRevision = baseRevision
        self.operations = operations
    }

    enum CodingKeys: String, CodingKey {
        case transactionId = "transaction_id"
        case baseRevision = "base_revision"
        case operations
    }
}

public struct SearchMatch: Codable, Hashable, Identifiable, Sendable {
    public var blockId: String
    public var documentId: String
    public var documentTitle: String
    public var blockType: String
    /// A passage around the first match (may start or end with "…").
    public var text: String
    /// [start, end) UTF-16 offsets of query words inside `text`.
    public var highlights: [[Int]]
    public var matched: [String]
    public var score: Double
    public var id: String { blockId }

    enum CodingKeys: String, CodingKey {
        case blockId = "block_id"
        case documentId = "document_id"
        case documentTitle = "document_title"
        case blockType = "block_type"
        case text
        case highlights
        case matched
        case score
    }
}

public struct RelatedResult: Codable, Hashable, Identifiable, Sendable {
    public var note: NoteSummary
    public var score: Double
    public var matchedBlockId: String?
    public var matchedBlockText: String
    public var id: String { note.id }

    enum CodingKeys: String, CodingKey {
        case note
        case score
        case matchedBlockId = "matched_block_id"
        case matchedBlockText = "matched_block_text"
    }
}

public struct Backlink: Codable, Hashable, Identifiable, Sendable {
    public var sourceBlockId: String
    public var sourceDocumentId: String
    public var sourceDocumentTitle: String
    public var sourceText: String
    public var label: String
    public var targetBlockId: String?
    public var id: String { sourceBlockId + (targetBlockId ?? "") }

    enum CodingKeys: String, CodingKey {
        case sourceBlockId = "source_block_id"
        case sourceDocumentId = "source_document_id"
        case sourceDocumentTitle = "source_document_title"
        case sourceText = "source_text"
        case label
        case targetBlockId = "target_block_id"
    }
}

public struct BlockLinkTarget: Codable, Hashable, Identifiable, Sendable {
    public var blockId: String
    public var documentId: String
    public var documentTitle: String
    public var blockType: String
    public var text: String
    public var id: String { blockId }

    enum CodingKeys: String, CodingKey {
        case blockId = "block_id"
        case documentId = "document_id"
        case documentTitle = "document_title"
        case blockType = "block_type"
        case text
    }
}

public struct AskCitation: Codable, Hashable, Sendable {
    public var blockId: String
    public var documentId: String
    public var documentTitle: String
    public var quote: String

    enum CodingKeys: String, CodingKey {
        case blockId = "block_id"
        case documentId = "document_id"
        case documentTitle = "document_title"
        case quote
    }
}

public struct AskSentence: Codable, Hashable, Sendable {
    public var text: String
    public var citations: [AskCitation]
}

public struct AskStep: Codable, Hashable, Sendable {
    public var action: String
    public var detail: String
}

public enum AskStatus: String, Codable, Sendable {
    case answered
    case notFound = "not_found"
    case notConfigured = "not_configured"
    case timeout, unavailable
}

/// A passage Ask looked at (shown when there is no answer, e.g. no model configured).
public struct AskSource: Codable, Hashable, Identifiable, Sendable {
    public var blockId: String
    public var documentId: String
    public var documentTitle: String
    public var text: String
    public var id: String { blockId }

    enum CodingKeys: String, CodingKey {
        case blockId = "block_id"
        case documentId = "document_id"
        case documentTitle = "document_title"
        case text
    }
}

public struct AskResponse: Codable, Hashable, Sendable {
    public var status: AskStatus
    public var answer: [AskSentence]
    public var sources: [AskSource]?
    public var steps: [AskStep]?
    public var elapsedMs: Int?

    enum CodingKeys: String, CodingKey {
        case status
        case answer
        case sources
        case steps
        case elapsedMs = "elapsed_ms"
    }
}

/// One line of the streamed Ask answer.
public enum AskEvent: Hashable, Sendable {
    case sentence(AskSentence)
    case step(AskStep)
    case done(AskResponse)
}

public struct TrashedDocument: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var deletedAt: String
    public var snippet: String

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case deletedAt = "deleted_at"
        case snippet
    }
}

public struct Backup: Codable, Hashable, Identifiable, Sendable {
    public var name: String
    public var kind: String
    public var createdAt: String
    public var size: Int
    public var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case kind
        case createdAt = "created_at"
        case size
    }
}

public struct CaptureResult: Codable, Hashable, Sendable {
    public var documentId: String
    public var title: String
    public var folder: String

    enum CodingKeys: String, CodingKey {
        case documentId = "document_id"
        case title
        case folder
    }
}

public enum Timestamp {
    /// Parses the backend's ISO-8601 timestamps (with or without fractional seconds).
    ///
    /// Every list row and date heading parses its note's stamps on every render, so the
    /// answers are memoized (the strings repeat: one per note).
    public static func date(_ string: String) -> Date? {
        if let cached = memo.withLock({ $0[string] }) { return cached.date }
        let date = parse(string)
        memo.withLock { memo in
            if memo.count >= 20_000 { memo.removeAll(keepingCapacity: true) }
            memo[string] = Parsed(date: date)
        }
        return date
    }

    /// The parse itself, without the memo.
    public static func parse(_ string: String) -> Date? {
        if let date = try? fractional.parse(string) { return date }
        return try? whole.parse(string)
    }

    /// `now` the way the backend writes it (`datetime.now(timezone.utc).isoformat()`), so
    /// stamps made locally (a note moved or created) sort among the server's.
    public static func serverString(_ date: Date = Date()) -> String {
        let seconds = date.timeIntervalSince1970
        let whole = seconds.rounded(.down)
        let micros = min(999_999, Int(((seconds - whole) * 1_000_000).rounded()))
        let base = Date(timeIntervalSince1970: whole).formatted(
            Date.ISO8601FormatStyle(timeZone: TimeZone(identifier: "UTC")!).year().month().day()
                .dateTimeSeparator(.standard).time(includingFractionalSeconds: false))
        let digits = String(micros)
        let padded = String(repeating: "0", count: 6 - digits.count) + digits
        return base + "." + padded + "+00:00"
    }

    private struct Parsed: Sendable { var date: Date? }
    private static let memo = Mutex<[String: Parsed]>([:])
    private static let fractional = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    private static let whole = Date.ISO8601FormatStyle()
}
