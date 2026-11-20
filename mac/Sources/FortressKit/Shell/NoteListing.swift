import Foundation

/// How the note list is ordered.
public enum NoteSort: String, CaseIterable, Identifiable, Sendable {
    case edited, created, title
    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .edited: String(localized: "Date Edited")
        case .created: String(localized: "Date Created")
        case .title: String(localized: "Title")
        }
    }
}

/// A run of notes under one date heading ("Today", "Previous 7 Days", "October"…).
public struct NoteSection: Identifiable, Hashable, Sendable {
    /// Empty when the list isn't grouped.
    public var title: String
    public var notes: [NavigationDocument]
    public var id: String { title }
}

/// A folder in the sidebar tree, with its subfolders (nil when it has none, so
/// `OutlineGroup` shows no disclosure triangle).
public struct FolderNode: Identifiable, Hashable, Sendable {
    public var folder: NavigationFolder
    public var subfolders: [FolderNode]?
    public var id: String { folder.id }
    public var name: String { folder.name }
}

/// Pure functions behind the sidebar and note list, kept apart from the views so they
/// can be tested.
public enum NoteListing {
    // MARK: Tree

    /// Every live note in the navigation tree, depth-first.
    public static func allDocuments(in items: [NavigationNode]) -> [NavigationDocument] {
        items.flatMap { node -> [NavigationDocument] in
            switch node {
            case .document(let document): [document]
            case .folder(let folder): allDocuments(in: folder.children)
            }
        }
    }

    /// Every folder, depth-first, with its depth (0 = top level).
    public static func folders(in items: [NavigationNode], depth: Int = 0) -> [(folder: NavigationFolder, depth: Int)] {
        items.flatMap { node -> [(folder: NavigationFolder, depth: Int)] in
            guard case .folder(let folder) = node else { return [] }
            return [(folder, depth)] + folders(in: folder.children, depth: depth + 1)
        }
    }

    public static func folder(id: String, in items: [NavigationNode]) -> NavigationFolder? {
        folders(in: items).first { $0.folder.id == id }?.folder
    }

    /// The folder tree for the sidebar.
    public static func folderTree(_ items: [NavigationNode]) -> [FolderNode] {
        items.compactMap { node in
            guard case .folder(let folder) = node else { return nil }
            let children = folderTree(folder.children)
            return FolderNode(folder: folder, subfolders: children.isEmpty ? nil : children)
        }
    }

    /// Folder names by id, for the meta line under each note.
    public static func folderNames(_ items: [NavigationNode]) -> [String: String] {
        Dictionary(folders(in: items).map { ($0.folder.id, $0.folder.name) }, uniquingKeysWith: { a, _ in a })
    }

    /// Whether `folderId` is `ancestorId` or sits somewhere inside it.
    public static func isFolder(_ folderId: String, inside ancestorId: String, items: [NavigationNode]) -> Bool {
        guard let ancestor = folder(id: ancestorId, in: items) else { return false }
        return folderId == ancestorId || folders(in: ancestor.children).contains { $0.folder.id == folderId }
    }

    // MARK: Scope

    /// The notes a sidebar selection shows (the Trash is listed separately).
    public static func notes(in scope: LibraryScope, navigation: NavigationResponse) -> [NavigationDocument] {
        let all = allDocuments(in: navigation.items)
        switch scope {
        case .all: return all
        case .recent: return navigation.recent
        case .trash: return []
        case .folder(let id): return all.filter { $0.folderId == id }
        }
    }

    /// The notes just before and after `id` in the list (next first: lists are read down).
    public static func neighbours(of id: String, in ordered: [String]) -> [String] {
        guard let index = ordered.firstIndex(of: id) else { return [] }
        var result: [String] = []
        if index + 1 < ordered.count { result.append(ordered[index + 1]) }
        if index > 0 { result.append(ordered[index - 1]) }
        return result
    }

    public static func sorted(_ notes: [NavigationDocument], by sort: NoteSort) -> [NavigationDocument] {
        switch sort {
        case .edited:
            return notes.sorted { ($0.updatedAt, $1.id) > ($1.updatedAt, $0.id) }
        case .created:
            return notes.sorted { ($0.createdAt, $1.id) > ($1.createdAt, $0.id) }
        case .title:
            return notes.sorted {
                let order = displayTitle($0.title).localizedStandardCompare(displayTitle($1.title))
                return order == .orderedSame ? $0.id < $1.id : order == .orderedAscending
            }
        }
    }

    /// Sorts the notes and, when sorted by a date and `grouped`, splits them into date sections.
    public static func sections(
        _ notes: [NavigationDocument], sort: NoteSort, grouped: Bool,
        now: Date = Date(), calendar: Calendar = .current
    ) -> [NoteSection] {
        let ordered = sorted(notes, by: sort)
        guard grouped, sort != .title else {
            return ordered.isEmpty ? [] : [NoteSection(title: "", notes: ordered)]
        }
        var sections: [NoteSection] = []
        for note in ordered {
            let stamp = sort == .created ? note.createdAt : note.updatedAt
            let title = Timestamp.date(stamp).map { dateBucket(for: $0, now: now, calendar: calendar) } ?? String(localized: "Earlier")
            if sections.last?.title == title {
                sections[sections.count - 1].notes.append(note)
            } else {
                sections.append(NoteSection(title: title, notes: [note]))
            }
        }
        return sections
    }

    /// The Apple Notes–style heading a date falls under.
    public static func dateBucket(for date: Date, now: Date, calendar: Calendar) -> String {
        let today = calendar.startOfDay(for: now)
        func daysAgo(_ days: Int) -> Date { calendar.date(byAdding: .day, value: -days, to: today)! }
        if date >= today { return String(localized: "Today") }
        if date >= daysAgo(1) { return String(localized: "Yesterday") }
        if date >= daysAgo(7) { return String(localized: "Previous 7 Days") }
        if date >= daysAgo(30) { return String(localized: "Previous 30 Days") }
        let year = calendar.component(.year, from: date)
        guard year == calendar.component(.year, from: now) else {
            // The calendar's own year (not "2,025", and not the Gregorian year in a Buddhist calendar).
            return date.formatted(Date.FormatStyle(locale: calendar.locale ?? .current, calendar: calendar, timeZone: calendar.timeZone).year())
        }
        return calendar.standaloneMonthSymbols[calendar.component(.month, from: date) - 1]
    }

    // MARK: Row text

    public static func displayTitle(_ title: String) -> String {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? String(localized: "New Note") : trimmed
    }

    /// The snippet under a row's title: Markdown markers stripped, and empty when it only
    /// repeats the title.
    public static func preview(title: String, snippet: String) -> String {
        var text = snippet.trimmingCharacters(in: .whitespacesAndNewlines)
        let markers = ["#", ">", "- [ ]", "- [x]", "[ ]", "[x]", "- ", "* ", "+ "]
        var stripped = true
        while stripped {
            stripped = false
            for marker in markers where text.hasPrefix(marker) {
                text = String(text.dropFirst(marker.count)).trimmingCharacters(in: .whitespaces)
                stripped = true
            }
        }
        if let dot = text.firstIndex(of: "."), dot != text.startIndex,
           text[..<dot].allSatisfy(\.isNumber), text[text.index(after: dot)...].hasPrefix(" ") {
            text = String(text[text.index(after: dot)...]).trimmingCharacters(in: .whitespaces)
        }
        text = text.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "")
        let collapsed = text.split(whereSeparator: \.isNewline).joined(separator: " ")
        return collapsed.caseInsensitiveCompare(title.trimmingCharacters(in: .whitespaces)) == .orderedSame ? "" : collapsed
    }

    /// The date on a row's meta line: a time today, "Yesterday", a weekday this week, else a short date.
    public static func listDate(_ date: Date, now: Date, calendar: Calendar) -> String {
        let locale = calendar.locale ?? .current
        let today = calendar.startOfDay(for: now)
        if date >= today {
            return date.formatted(Date.FormatStyle(date: .omitted, time: .shortened, locale: locale, calendar: calendar, timeZone: calendar.timeZone))
        }
        if date >= calendar.date(byAdding: .day, value: -1, to: today)! { return String(localized: "Yesterday") }
        if date >= calendar.date(byAdding: .day, value: -6, to: today)! {
            return date.formatted(Date.FormatStyle(locale: locale, calendar: calendar, timeZone: calendar.timeZone).weekday(.wide))
        }
        return date.formatted(Date.FormatStyle(date: .numeric, time: .omitted, locale: locale, calendar: calendar, timeZone: calendar.timeZone))
    }

    /// "Deleted 3 days ago" for Trash rows.
    public static func deletedLabel(_ stamp: String, now: Date) -> String {
        guard let date = Timestamp.date(stamp) else { return String(localized: "Deleted") }
        if now.timeIntervalSince(date) < 60 { return String(localized: "Deleted just now") }
        // One sentence with the date as its argument (never "Deleted " + date: word order differs).
        let relative = date.formatted(.relative(presentation: .named, unitsStyle: .wide))
        return String(localized: "Deleted \(relative)", comment: "Trash row; the argument is a relative date such as “3 days ago”")
    }
}
