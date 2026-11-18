import Foundation

// MARK: Palette contents

enum PaletteCommand: String, CaseIterable, Sendable {
    case newNote, newFolder, quickCapture, searchNotes, askNotes, allNotes, trash, backups, toggleInspector, reload, importOneNote
    // The rest of the menu bar, so the palette and the menus offer the same commands (HIG,
    // DESIGN principle 4). Formatting stays in the Format menu: it acts on the text selection.
    case newWindow, importMarkdown, exportMarkdown, backUpNow, focusMode, rough, polished, recent, moveToTrash, openInNewWindow

    var title: String {
        switch self {
        case .newNote: "New Note"
        case .newFolder: "New Folder"
        case .quickCapture: "Quick Capture"
        case .searchNotes: "Search Notes"
        case .askNotes: "Ask Your Notes"
        case .allNotes: "Show All Notes"
        case .trash: "Show Trash"
        case .backups: "Backups"
        case .toggleInspector: "Toggle Inspector"
        case .reload: "Reload Notes"
        case .importOneNote: "Import from OneNote…"
        case .newWindow: "New Window"
        case .importMarkdown: "Import Markdown Files…"
        case .exportMarkdown: "Export as Markdown…"
        case .backUpNow: "Back Up Now"
        case .focusMode: "Toggle Focus Mode"
        case .rough: "Show Rough"
        case .polished: "Show Polished"
        case .recent: "Show Recent"
        case .moveToTrash: "Move Note to Trash"
        case .openInNewWindow: "Open Note in New Window"
        }
    }

    /// Other words people might type for it.
    var keywords: [String] {
        switch self {
        case .newNote: ["create", "write", "add"]
        case .newFolder: ["create", "group"]
        case .quickCapture: ["jot", "inbox", "thought"]
        case .searchNotes: ["find", "look"]
        case .askNotes: ["question", "answer", "ai"]
        case .allNotes: ["library", "everything"]
        case .trash: ["deleted", "bin", "restore"]
        case .backups: ["restore", "snapshot", "back up"]
        case .toggleInspector: ["related", "backlinks", "info", "sidebar"]
        case .reload: ["refresh", "sync"]
        case .importOneNote: ["onenote", "microsoft", "notebook", "migrate", "export"]
        case .newWindow: ["window", "tab"]
        case .importMarkdown: ["markdown", "md", "files"]
        case .exportMarkdown: ["save", "markdown", "md", "share"]
        case .backUpNow: ["backup", "snapshot", "save"]
        case .focusMode: ["distraction", "zen", "full", "hide sidebar"]
        case .rough: ["draft", "status"]
        case .polished: ["done", "status", "finished"]
        case .recent: ["latest", "history"]
        case .moveToTrash: ["delete", "remove", "bin"]
        case .openInNewWindow: ["window", "separate"]
        }
    }

    var symbol: String {
        switch self {
        case .newNote: "square.and.pencil"
        case .newFolder: "folder.badge.plus"
        case .quickCapture: "bolt"
        case .searchNotes: "magnifyingglass"
        case .askNotes: "sparkle.magnifyingglass"
        case .allNotes: "note.text"
        case .trash: "trash"
        case .backups: "clock.arrow.circlepath"
        case .toggleInspector: "sidebar.trailing"
        case .reload: "arrow.clockwise"
        case .importOneNote: "square.and.arrow.down"
        case .newWindow: "macwindow.badge.plus"
        case .importMarkdown: "square.and.arrow.down"
        case .exportMarkdown: "square.and.arrow.up"
        case .backUpNow: "externaldrive.badge.timemachine"
        case .focusMode: "arrow.up.left.and.arrow.down.right"
        case .rough: "circle.dashed"
        case .polished: "checkmark.seal"
        case .recent: "clock"
        case .moveToTrash: "trash"
        case .openInNewWindow: "macwindow"
        }
    }

    var shortcut: String? {
        // The menu bar's own shortcuts (one table, so a hint never names a dead key).
        switch self {
        case .newNote: Shortcuts.newNote.display
        case .newFolder: Shortcuts.newFolder.display
        case .quickCapture: QuickCaptureShortcut.saved()?.display
        case .searchNotes: Shortcuts.searchAllNotes.display
        case .askNotes: Shortcuts.askNotes.display
        case .toggleInspector: Shortcuts.toggleInspector.display
        case .importMarkdown: Shortcuts.importMarkdown.display
        case .exportMarkdown: Shortcuts.exportMarkdown.display
        case .focusMode: Shortcuts.focusMode.display
        case .allNotes: Shortcuts.allNotes.display
        case .rough: Shortcuts.rough.display
        case .polished: Shortcuts.polished.display
        case .recent: Shortcuts.recent.display
        case .reload: Shortcuts.reloadNotes.display
        default: nil
        }
    }

    static let suggested: [PaletteCommand] = [.newNote, .quickCapture, .askNotes, .toggleInspector, .backups]
}

/// A note the palette can jump to.
struct PaletteNote: Equatable, Sendable {
    var id: String
    var title: String
    var folder: String?
}

struct PaletteItem: Identifiable, Equatable {
    enum Action: Equatable {
        case openNote(String)
        case newNote(title: String)
        case search(String)
        case ask(String)
        case command(PaletteCommand)
    }

    var id: String
    var title: String
    var subtitle: String?
    var symbol: String
    /// Right-aligned hint: a shortcut or the kind of row ("Command").
    var accessory: String?
    var action: Action
    /// Character offsets of `title` to draw bold.
    var matched: [Int] = []
    var score = 0
}

struct PaletteSection: Identifiable, Equatable {
    var id: String { title ?? "results" }
    var title: String?
    var items: [PaletteItem]
}

enum PaletteCatalog {
    static let maxMatches = 12

    /// Every note in the sidebar tree, depth first, with the folder it lives in.
    static func notes(in navigation: NavigationResponse) -> [PaletteNote] {
        var notes: [PaletteNote] = []
        func visit(_ nodes: [NavigationNode], folder: String?) {
            for node in nodes {
                switch node {
                case .folder(let child): visit(child.children, folder: child.name)
                case .document(let document): notes.append(PaletteNote(id: document.id, title: document.title, folder: folder))
                }
            }
        }
        visit(navigation.items, folder: nil)
        return notes
    }

    /// Recent notes with the folder each lives in.
    static func recent(in navigation: NavigationResponse, notes: [PaletteNote]) -> [PaletteNote] {
        guard !navigation.recent.isEmpty else { return [] }
        let wanted = Set(navigation.recent.map(\.id))
        var folders: [String: String] = [:]
        for note in notes where wanted.contains(note.id) {
            if let folder = note.folder { folders[note.id] = folder }
        }
        return navigation.recent.map { PaletteNote(id: $0.id, title: $0.title, folder: folders[$0.id]) }
    }

    /// The sections to show for what's typed. Prefixes: ">" commands only, "?" ask, "/" search.
    static func sections(for rawQuery: String, notes: [PaletteNote], recent: [PaletteNote]) -> [PaletteSection] {
        sections(for: rawQuery, index: PaletteIndex(notes: notes, recent: recent))
    }

    /// The sections for what's typed, ranked against a prebuilt index (titles already folded).
    static func sections(for rawQuery: String, index: PaletteIndex) -> [PaletteSection] {
        let trimmed = rawQuery.trimmingCharacters(in: .whitespaces)

        if trimmed.hasPrefix("?") {
            let question = trimmed.dropFirst().trimmingCharacters(in: .whitespaces)
            return [PaletteSection(title: nil, items: [askItem(question)])]
        }
        if trimmed.hasPrefix("/") {
            let words = trimmed.dropFirst().trimmingCharacters(in: .whitespaces)
            return [PaletteSection(title: nil, items: [searchItem(words)])]
        }
        if trimmed.hasPrefix(">") {
            let query = trimmed.dropFirst().trimmingCharacters(in: .whitespaces)
            let commands = PaletteCommand.allCases.compactMap { commandItem($0, query: query) }
            return [PaletteSection(title: "Commands", items: query.isEmpty ? commands : ranked(commands))]
        }

        if trimmed.isEmpty {
            let recentItems = index.recent.prefix(5).map { noteItem($0, match: nil) }
            let suggested = PaletteCommand.suggested.map { commandItem($0, query: "")! }
            return [
                PaletteSection(title: recentItems.isEmpty ? nil : "Recent", items: Array(recentItems)),
                PaletteSection(title: "Suggestions", items: suggested),
            ].filter { !$0.items.isEmpty }
        }

        // Score every title without allocating, keep the best few, and only then build rows
        // (with the offsets to bold) for those.
        let query = FuzzyMatch.Query(trimmed)
        var best = TopScores(capacity: maxMatches)
        FuzzyMatch.scoring(query) { score in
            index.cells.withUnsafeBufferPointer { cells in
                index.bounds.withUnsafeBufferPointer { bounds in
                    index.masks.withUnsafeBufferPointer { masks in
                        for offset in masks.indices {
                            let title = UnsafeBufferPointer(rebasing: cells[bounds[offset]..<bounds[offset + 1]])
                            if let value = score(title, masks[offset]) {
                                best.insert(score: value, offset: offset)
                            }
                        }
                    }
                }
            }
        }
        let commands = PaletteCommand.allCases.compactMap { commandItem($0, query: trimmed) }
        for (offset, command) in commands.enumerated() {
            best.insert(score: command.score, offset: index.entries.count + offset)
        }
        let top: [PaletteItem] = best.ranked.map { offset in
            if offset < index.entries.count {
                let entry = index.entries[offset]
                return noteItem(entry.note, match: FuzzyMatch.match(query, in: entry.folded))
            }
            return commands[offset - index.entries.count]
        }
        let fallbacks = [
            PaletteItem(id: "new:\(trimmed)", title: "New Note “\(trimmed)”", symbol: "square.and.pencil",
                        accessory: nil, action: .newNote(title: trimmed)),
            searchItem(trimmed),
            askItem(trimmed),
        ]
        return [
            PaletteSection(title: nil, items: top),
            PaletteSection(title: top.isEmpty ? nil : "Use “\(trimmed)” with…", items: fallbacks),
        ].filter { !$0.items.isEmpty }
    }

    /// Best first; on a tie, notes before commands, then shorter titles.
    static func ranked(_ items: [PaletteItem]) -> [PaletteItem] {
        items.enumerated().sorted { lhs, rhs in
            if lhs.element.score != rhs.element.score { return lhs.element.score > rhs.element.score }
            return lhs.offset < rhs.offset
        }
        .map(\.element)
    }

    private static func noteItem(_ note: PaletteNote, match: FuzzyMatch.Result?) -> PaletteItem {
        PaletteItem(
            id: "note:\(note.id)", title: note.title.isEmpty ? "Untitled" : note.title, subtitle: note.folder,
            symbol: "doc.text", accessory: nil, action: .openNote(note.id),
            matched: match?.indices ?? [], score: match?.score ?? 0)
    }

    private static func commandItem(_ command: PaletteCommand, query: String) -> PaletteItem? {
        var item = PaletteItem(
            id: "command:\(command.rawValue)", title: command.title, symbol: command.symbol,
            accessory: command.shortcut ?? "Command", action: .command(command))
        guard !query.isEmpty else { return item }
        if let match = FuzzyMatch.match(query, in: command.title) {
            item.matched = match.indices
            item.score = match.score
            return item
        }
        // A keyword hit counts like a word start in the name, without bolding.
        let keywordScore = command.keywords.compactMap { FuzzyMatch.match(query, in: $0) }
            .filter { $0.score >= 7_000 }.map(\.score).max()
        guard let keywordScore else { return nil }
        item.score = keywordScore - 1_500
        return item
    }

    private static func askItem(_ question: String) -> PaletteItem {
        PaletteItem(
            id: "ask:\(question)", title: question.isEmpty ? "Ask your notes…" : "Ask your notes “\(question)”",
            symbol: "sparkle.magnifyingglass", accessory: "⌘↩", action: .ask(question))
    }

    private static func searchItem(_ words: String) -> PaletteItem {
        PaletteItem(
            id: "search:\(words)", title: words.isEmpty ? "Search inside notes…" : "Search inside notes for “\(words)”",
            symbol: "magnifyingglass", accessory: nil, action: .search(words))
    }
}

// MARK: Index and per-keystroke cache

/// Every note title folded for matching, built once per sidebar change.
struct PaletteIndex: Sendable {
    struct Entry: Sendable {
        var note: PaletteNote
        var folded: FoldedText
    }

    let notes: [PaletteNote]
    let recent: [PaletteNote]
    let entries: [Entry]
    /// Every title's cells end to end; title i is `cells[bounds[i]..<bounds[i + 1]]`.
    /// One flat buffer, so ranking touches no per-title array (no retain/release).
    let cells: [UInt32]
    let bounds: [Int]
    let masks: [UInt64]

    init(notes: [PaletteNote], recent: [PaletteNote]) {
        self.notes = notes
        self.recent = recent
        entries = notes.map { Entry(note: $0, folded: FoldedText($0.title.isEmpty ? "Untitled" : $0.title)) }
        var cells: [UInt32] = []
        var bounds: [Int] = [0]
        cells.reserveCapacity(entries.reduce(0) { $0 + $1.folded.count })
        bounds.reserveCapacity(entries.count + 1)
        for entry in entries {
            cells += entry.folded.cells
            bounds.append(cells.count)
        }
        self.cells = cells
        self.bounds = bounds
        masks = entries.map(\.folded.mask)
    }

    init(navigation: NavigationResponse) {
        let notes = PaletteCatalog.notes(in: navigation)
        self.init(notes: notes, recent: PaletteCatalog.recent(in: navigation, notes: notes))
    }
}

/// The best `capacity` (score, offset) pairs: higher score first, then lower offset (the
/// order the candidates were offered in), like a stable sort.
struct TopScores {
    let capacity: Int
    private(set) var items: [(score: Int, offset: Int)] = []

    init(capacity: Int) {
        self.capacity = capacity
        items.reserveCapacity(capacity + 1)
    }

    var ranked: [Int] { items.map(\.offset) }

    /// Offsets must be offered in increasing order.
    mutating func insert(score: Int, offset: Int) {
        if items.count == capacity, let last = items.last, score <= last.score { return }
        var position = items.count
        while position > 0 && items[position - 1].score < score { position -= 1 }
        items.insert((score, offset), at: position)
        if items.count > capacity { items.removeLast() }
    }
}

/// What the palette view keeps between renders: the index (rebuilt only when the
/// sidebar's notes change) and the sections for the last query (hover and selection
/// changes re-render the view without ranking again).
@MainActor
final class PaletteCache {
    private var navigation: NavigationResponse?
    private(set) var index = PaletteIndex(notes: [], recent: [])
    private var last: (query: String, sections: [PaletteSection])?
    /// For tests: how often the index was built and the notes ranked.
    private(set) var indexBuilds = 0
    private(set) var rankings = 0

    init() {}

    func sections(for query: String, navigation: NavigationResponse) -> [PaletteSection] {
        if self.navigation != navigation {
            self.navigation = navigation
            // A save changes dates in the tree, not titles: keep the folded index then.
            let notes = PaletteCatalog.notes(in: navigation)
            let recent = PaletteCatalog.recent(in: navigation, notes: notes)
            if notes != index.notes || recent != index.recent {
                index = PaletteIndex(notes: notes, recent: recent)
                indexBuilds += 1
                last = nil
            }
        }
        if let last, last.query == query { return last.sections }
        rankings += 1
        let interval = Signposts.begin("Palette results")
        let sections = PaletteCatalog.sections(for: query, index: index)
        Signposts.end("Palette results", interval)
        last = (query, sections)
        return sections
    }
}
