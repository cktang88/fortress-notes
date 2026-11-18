import Foundation

/// What the inspector's Info section shows about a note: where it came from, dates,
/// size and links. Filled from `GET …/info` when the backend has it; otherwise from the
/// sidebar tree and the loaded document.
struct NoteInfo: Hashable, Sendable {
    var createdAt: Date?
    var updatedAt: Date?
    var openedAt: Date?
    /// Folder names, top first.
    var folderPath: [String] = []
    var words: Int?
    var blocks: Int?
    var linksOut: Int?
    var backlinksIn: Int?
    var edits: Int?
    /// Blocks per source kind when a note mixes sources (e.g. imported, then written in).
    var blocksBySource: [String: Int] = [:]
    var provenance: NoteProvenance?
    var conflicts = 0

    init() {}

    init(_ info: DocumentInfo) {
        createdAt = info.createdAt.flatMap(Timestamp.date)
        updatedAt = info.updatedAt.flatMap(Timestamp.date)
        openedAt = info.lastOpenedAt.flatMap(Timestamp.date)
        folderPath = info.folderPath?.map(\.name) ?? []
        words = info.counts?.words
        blocks = info.counts?.blocks
        linksOut = info.counts?.outgoingLinks
        backlinksIn = info.counts?.backlinks
        edits = info.counts?.edits
        blocksBySource = info.blocksBySource ?? [:]
        conflicts = info.conflicts?.count ?? 0
        provenance = info.provenance.map { NoteProvenance($0, sync: info.sync) }
    }

    /// What the sidebar tree already knows.
    static func from(navigation: NavigationResponse, documentId: String) -> NoteInfo {
        var info = NoteInfo()
        func visit(_ nodes: [NavigationNode], path: [String]) {
            for node in nodes {
                switch node {
                case .folder(let child):
                    visit(child.children, path: path + [child.name])
                case .document(let document) where document.id == documentId:
                    info.createdAt = Timestamp.date(document.createdAt)
                    info.updatedAt = Timestamp.date(document.updatedAt)
                    info.folderPath = path
                case .document:
                    break
                }
            }
        }
        visit(navigation.items, path: [])
        return info
    }

    /// The note's `updated_at` in the sidebar tree (its revision stamp), stopping at the note.
    static func updatedAt(of documentId: String, in navigation: NavigationResponse) -> String? {
        func find(_ nodes: [NavigationNode]) -> String? {
            for node in nodes {
                switch node {
                case .document(let document) where document.id == documentId: return document.updatedAt
                case .document: continue
                case .folder(let folder): if let found = find(folder.children) { return found }
                }
            }
            return nil
        }
        return find(navigation.items) ?? navigation.recent.first { $0.id == documentId }?.updatedAt
    }

    /// Adds what the full document says (counts and dates), for backends without `/info`.
    mutating func add(_ document: BlockDocument) {
        createdAt = Timestamp.date(document.createdAt) ?? createdAt
        updatedAt = Timestamp.date(document.updatedAt) ?? updatedAt
        var words = 0
        var blocks = 0
        var links = 0
        func visit(_ nodes: [BlockNode]) {
            for node in nodes {
                blocks += 1
                words += node.text.split(whereSeparator: { $0.isWhitespace })
                    .filter { word in word.contains { $0.isLetter || $0.isNumber } }.count
                links += Self.linkCount(in: node.content)
                visit(node.children)
            }
        }
        visit(document.children)
        self.words = words
        self.blocks = blocks
        self.linksOut = links
    }

    /// Links written in a block's Markdown: [[Note]], ((block)) and [text](url).
    static func linkCount(in content: JSONValue) -> Int {
        guard case .object(let object) = content, let markdown = object["markdown"]?.stringValue else { return 0 }
        return ["[[", "((", "]("].reduce(0) { $0 + markdown.components(separatedBy: $1).count - 1 }
    }

    /// "12 from OneNote · 3 written here", when the note mixes sources.
    var mixedSources: String? {
        guard blocksBySource.count > 1 else { return nil }
        return blocksBySource.sorted { $0.value > $1.value }
            .map { "\($0.value) \(NoteProvenance.shortLabel(for: $0.key))" }
            .joined(separator: " · ")
    }
}

/// How a note entered Fortress.
struct NoteProvenance: Hashable, Sendable {
    /// The backend's `source`: manual, capture, markdown_import, onenote_import, paste, web, ai, …
    var source: String
    /// "Work › Meetings › Standup", or a file path.
    var path: String?
    var url: URL?
    var originalCreatedAt: Date?
    var originalUpdatedAt: Date?
    var importedAt: Date?
    var syncedAt: Date?
    var importer: String?
    /// For moved, duplicated or split blocks: the note they came from.
    var originTitle: String?
    var originDocumentId: String?
    var originOperation: String?

    init(source: String, path: String? = nil, url: URL? = nil, originalCreatedAt: Date? = nil,
         originalUpdatedAt: Date? = nil, importedAt: Date? = nil, syncedAt: Date? = nil, importer: String? = nil) {
        self.source = source
        self.path = path
        self.url = url
        self.originalCreatedAt = originalCreatedAt
        self.originalUpdatedAt = originalUpdatedAt
        self.importedAt = importedAt
        self.syncedAt = syncedAt
        self.importer = importer
    }

    init(_ provenance: DocumentInfo.Provenance, sync: DocumentInfo.Sync?) {
        self.init(
            source: provenance.source,
            path: provenance.sourcePath,
            url: provenance.sourceUrl.flatMap(URL.init(string:)),
            originalCreatedAt: provenance.sourceCreatedAt.flatMap(Timestamp.date),
            originalUpdatedAt: provenance.sourceModifiedAt.flatMap(Timestamp.date),
            importedAt: provenance.importedAt.flatMap(Timestamp.date),
            syncedAt: (sync?.syncedAt ?? provenance.syncedAt).flatMap(Timestamp.date),
            importer: provenance.importer.map { [$0.name, $0.version].compactMap { $0 }.joined(separator: " ") })
        originTitle = provenance.origin?.documentTitle
        originDocumentId = provenance.origin?.documentId
        originOperation = provenance.origin?.operation
    }

    /// Whether this is worth a card (a note simply written here isn't).
    var isNotable: Bool {
        source != "manual" || path != nil || url != nil || originTitle != nil
    }

    var label: String {
        switch source {
        case "manual": "Written here"
        case "capture": "Quick Capture"
        case "markdown_import": "Imported from Markdown"
        case "onenote_import": "Imported from OneNote"
        case "paste": "Pasted"
        case "web": "From the web"
        case "ai": "Written with Ask"
        default: source.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func shortLabel(for source: String) -> String {
        switch source {
        case "manual": "written here"
        case "capture": "captured"
        case "markdown_import": "from Markdown"
        case "onenote_import": "from OneNote"
        case "paste": "pasted"
        case "web": "from the web"
        case "ai": "from Ask"
        default: source.replacingOccurrences(of: "_", with: " ")
        }
    }

    var symbol: String {
        switch source {
        case "manual": "pencil"
        case "capture": "bolt"
        case "markdown_import": "doc.text"
        case "onenote_import": "square.and.arrow.down.on.square"
        case "paste": "doc.on.clipboard"
        case "web": "globe"
        case "ai": "sparkle.magnifyingglass"
        default: "square.and.arrow.down"
        }
    }

    /// "Moved here from “Kyoto trip”"
    var originLine: String? {
        guard let originTitle else { return nil }
        let verb = switch originOperation {
        case "move_into": "Moved here from"
        case "duplicate": "Duplicated from"
        case "split": "Split from"
        default: "From"
        }
        return "\(verb) “\(originTitle.isEmpty ? "Untitled" : originTitle)”"
    }
}
