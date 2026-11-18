import Foundation

/// Where a note came from and what's in it, for the inspector's Info section.
///
/// Local on purpose: the backend's provenance and document-info endpoints aren't there
/// yet. Today it's filled from the sidebar tree and the loaded document; when the
/// endpoints land, decode their JSON into `NoteProvenance` / these counts and pass it in.
struct NoteInfo: Hashable, Sendable {
    var createdAt: Date?
    var updatedAt: Date?
    var folder: String?
    var words: Int?
    var blocks: Int?
    var linksOut: Int?
    var backlinksIn: Int?
    var revision: Int?
    var provenance: NoteProvenance?

    /// What the sidebar tree already knows.
    static func from(navigation: NavigationResponse, documentId: String) -> NoteInfo {
        var info = NoteInfo()
        func visit(_ nodes: [NavigationNode], folder: String?) {
            for node in nodes {
                switch node {
                case .folder(let child):
                    visit(child.children, folder: child.name)
                case .document(let document) where document.id == documentId:
                    info.createdAt = Timestamp.date(document.createdAt)
                    info.updatedAt = Timestamp.date(document.updatedAt)
                    info.folder = folder
                case .document:
                    break
                }
            }
        }
        visit(navigation.items, folder: nil)
        return info
    }

    /// Adds what the full document says (counts, revision, dates).
    mutating func add(_ document: BlockDocument) {
        createdAt = Timestamp.date(document.createdAt) ?? createdAt
        updatedAt = Timestamp.date(document.updatedAt) ?? updatedAt
        revision = document.revision
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
}

/// How a note entered Fortress (mirrors the planned backend fields).
struct NoteProvenance: Hashable, Sendable {
    enum Kind: String, Sendable {
        case manual, capture
        case markdownImport = "markdown_import"
        case onenoteImport = "onenote_import"
        case webPaste = "web_paste"
        case ai
        case moveInto = "move_into"
    }

    var kind: Kind
    /// e.g. ["Travel notebook", "Japan", "Kyoto"] for notebook › section › page.
    var path: [String] = []
    var url: URL?
    var originalCreatedAt: Date?
    var originalUpdatedAt: Date?
    var importedAt: Date?

    var label: String {
        switch kind {
        case .manual: "Written here"
        case .capture: "Quick Capture"
        case .markdownImport: "Markdown import"
        case .onenoteImport: "OneNote import"
        case .webPaste: "Pasted from the web"
        case .ai: "Written with AI"
        case .moveInto: "Moved in from another note"
        }
    }

    var symbol: String {
        switch kind {
        case .manual: "pencil"
        case .capture: "bolt"
        case .markdownImport: "square.and.arrow.down"
        case .onenoteImport: "square.and.arrow.down.on.square"
        case .webPaste: "globe"
        case .ai: "sparkles"
        case .moveInto: "arrow.right.doc.on.clipboard"
        }
    }
}
