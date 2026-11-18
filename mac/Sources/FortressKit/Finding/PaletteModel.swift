import Foundation

// MARK: Fuzzy matching

/// How well a query matches a name. Tiers come first, so a command whose name *contains*
/// the query always beats a note that only matches scattered letters.
enum FuzzyMatch {
    struct Result: Equatable {
        var score: Int
        /// Character offsets in the candidate that matched (for bolding).
        var indices: [Int]
    }

    static func match(_ query: String, in candidate: String) -> Result? {
        let needle = fold(query.trimmingCharacters(in: .whitespaces))
        guard !needle.isEmpty else { return Result(score: 1, indices: []) }
        let hay = fold(candidate)
        guard !hay.isEmpty else { return nil }

        if let start = find(needle, in: hay) {
            let indices = Array(start..<(start + needle.count))
            let atWord = start == 0 || !isWordCharacter(hay[start - 1])
            let base: Int
            if start == 0 && needle.count == hay.count {
                base = 10_000  // Exact.
            } else if start == 0 {
                base = 9_000  // Prefix.
            } else if atWord {
                base = 8_000  // Starts a word.
            } else {
                base = 7_000  // Somewhere inside.
            }
            // Earlier and shorter candidates rank higher within a tier.
            return Result(score: base - min(start, 200) * 4 - min(hay.count, 400), indices: indices)
        }

        // Several words in any order, each found at a word start: "trip kyo".
        let words = needle.split(separator: " ").map(Array.init)
        if words.count > 1 {
            var indices: [Int] = []
            for word in words {
                guard let start = findAtWordStart(word, in: hay) else { indices = []; break }
                indices += Array(start..<(start + word.count))
            }
            if !indices.isEmpty {
                return Result(score: 6_000 - min(hay.count, 400), indices: indices.sorted())
            }
        }

        // Scattered letters in order ("kt" → "Kyoto trip"); fewer gaps and word starts score higher.
        let letters = needle.filter { $0 != " " }
        var indices: [Int] = []
        var position = 0
        for letter in letters {
            // Prefer the next occurrence at a word start, else the next occurrence.
            var found: Int?
            var index = position
            var firstPlain: Int?
            while index < hay.count {
                if hay[index] == letter {
                    if firstPlain == nil { firstPlain = index }
                    if index == 0 || !isWordCharacter(hay[index - 1]) { found = index; break }
                }
                index += 1
            }
            guard let hit = found ?? firstPlain else { return nil }
            indices.append(hit)
            position = hit + 1
        }
        let span = (indices.last ?? 0) - (indices.first ?? 0) + 1
        let gaps = span - indices.count
        let wordStarts = indices.filter { $0 == 0 || !isWordCharacter(hay[$0 - 1]) }.count
        let score = 3_000 + wordStarts * 150 - gaps * 25 - (indices.first ?? 0) * 5 - min(hay.count, 400)
        return Result(score: max(1, min(score, 5_999)), indices: indices)
    }

    /// Lowercased, accents removed, one entry per character of the original.
    static func fold(_ text: String) -> [Character] {
        text.map { character in
            String(character).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil).first ?? character
        }
    }

    private static func isWordCharacter(_ character: Character) -> Bool {
        character.isLetter || character.isNumber
    }

    private static func find(_ needle: [Character], in hay: [Character]) -> Int? {
        guard needle.count <= hay.count else { return nil }
        for start in 0...(hay.count - needle.count) where hay[start] == needle[0] {
            if Array(hay[start..<(start + needle.count)]) == needle { return start }
        }
        return nil
    }

    private static func findAtWordStart(_ needle: [Character], in hay: [Character]) -> Int? {
        guard !needle.isEmpty, needle.count <= hay.count else { return nil }
        for start in 0...(hay.count - needle.count)
        where (start == 0 || !isWordCharacter(hay[start - 1])) && Array(hay[start..<(start + needle.count)]) == needle {
            return start
        }
        return nil
    }
}

// MARK: Palette contents

enum PaletteCommand: String, CaseIterable, Sendable {
    case newNote, newFolder, quickCapture, searchNotes, askNotes, allNotes, trash, backups, toggleInspector, reload

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
        }
    }

    var shortcut: String? {
        switch self {
        case .newNote: "⌘N"
        case .newFolder: "⌥⌘N"
        case .quickCapture: "⇧⌘Space"
        case .searchNotes: "⇧⌘F"
        case .askNotes: "⇧⌘A"
        case .toggleInspector: "⌥⌘I"
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

    /// The sections to show for what's typed. Prefixes: ">" commands only, "?" ask, "/" search.
    static func sections(for rawQuery: String, notes: [PaletteNote], recent: [PaletteNote]) -> [PaletteSection] {
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
            let recentItems = recent.prefix(5).map { noteItem($0, match: nil) }
            let suggested = PaletteCommand.suggested.map { commandItem($0, query: "")! }
            return [
                PaletteSection(title: recentItems.isEmpty ? nil : "Recent", items: Array(recentItems)),
                PaletteSection(title: "Suggestions", items: suggested),
            ].filter { !$0.items.isEmpty }
        }

        var matches: [PaletteItem] = notes.compactMap { note in
            FuzzyMatch.match(trimmed, in: note.title.isEmpty ? "Untitled" : note.title).map { noteItem(note, match: $0) }
        }
        matches += PaletteCommand.allCases.compactMap { commandItem($0, query: trimmed) }
        let top = Array(ranked(matches).prefix(maxMatches))
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
