import Foundation

/// What the inspector's Info section shows about a note: how it came in, dates,
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
    /// The backend's `source`: manual, capture, markdown_import, onenote_import, ai, …
    var source: String?
    /// The note's pictures and PDFs with the text read from them.
    var pictures: [PictureText] = []

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
        source = info.source
        pictures = info.pictureText ?? []
    }

    /// Whether a picture is still to be read (the inspector then checks back).
    var isReadingPictures: Bool { pictures.contains { !$0.isFinished } }

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

    /// Links in a block's inline content: note links, block references and web links.
    static func linkCount(in content: JSONValue) -> Int {
        guard case .array(let nodes) = content else { return 0 }
        return nodes.filter { ["docLink", "blockRef", "link"].contains($0["type"]?.stringValue ?? "") }.count
    }

    /// How the note entered Fortress in words ("Imported from OneNote"); nil for a note
    /// written here, which needs no line.
    var sourceLabel: String? {
        switch source ?? "manual" {
        case "manual", "": nil
        case "capture": String(localized: "Quick Capture")
        case "markdown_import": String(localized: "Imported from Markdown")
        case "onenote_import": String(localized: "Imported from OneNote")
        case "web": String(localized: "From the web")
        case "ai": String(localized: "Written with Ask")
        case let other: other.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}
