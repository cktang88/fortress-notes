import Foundation

@testable import FortressKit

/// A believable library for Shell screenshots and tests, dated relative to now.
@MainActor
enum ShellFixtures {
    static func stamp(daysAgo: Double, hours: Double = 0, from now: Date = Date()) -> String {
        let date = now.addingTimeInterval(-(daysAgo * 86_400 + hours * 3_600))
        return date.formatted(Date.ISO8601FormatStyle(includingFractionalSeconds: true))
    }

    static func note(
        _ id: String, _ title: String, _ snippet: String, folder: String? = nil,
        status: NoteStatus = .rough, edited: Double, hours: Double = 0, created: Double? = nil
    ) -> NavigationDocument {
        NavigationDocument(
            id: id, title: title, status: status, updatedAt: stamp(daysAgo: edited, hours: hours),
            createdAt: stamp(daysAgo: created ?? edited + 3), snippet: snippet, folderId: folder, position: 0)
    }

    static func folder(_ id: String, _ name: String, parent: String? = nil, _ children: [NavigationNode]) -> NavigationNode {
        .folder(NavigationFolder(id: id, parentId: parent, name: name, position: 0, children: children))
    }

    static var notes: [NavigationDocument] {
        [
            note("n1", "Kyoto trip", "We booked a ryokan in Gion for the first three nights, then a small hotel near Kyoto Station for day trips to Nara.", folder: "travel", edited: 0, hours: 0.1),
            note("n2", "Reading notes: The Timeless Way of Building", "Patterns are alive when they resolve the forces in a situation. Alexander calls this the quality without a name.", folder: "reading", status: .polished, edited: 0, hours: 0.3),
            note("n3", "Call the dentist about the cleaning", "Call the dentist about the cleaning", folder: "inbox", edited: 1),
            note("n4", "Q4 planning", "Three bets for the quarter: ship the native app, make search instant, and cut the backlog in half.", folder: "work", status: .polished, edited: 2),
            note("n5", "Packing list", "Passport, charger, rain jacket, the good walking shoes, and a paper map for when the phone dies.", edited: 4),
            note("n6", "Sourdough schedule", "Feed the starter at 9pm. Mix at 8am, bulk ferment until it's jiggly and domed, then shape and cold-proof overnight.", folder: "home", edited: 12),
            note("n7", "Design review feedback", "Sidebar feels heavy. Fewer dividers, softer section headers, and let the list breathe a little more.", folder: "work", edited: 26),
            note("n8", "Gift ideas", "Mom: a good pair of pruning shears. Sam: the new Le Guin collection.", edited: 70, created: 80),
        ]
    }

    static var navigation: NavigationResponse {
        let byFolder = Dictionary(grouping: notes, by: { $0.folderId ?? "" })
        func docs(_ id: String) -> [NavigationNode] { (byFolder[id] ?? []).map(NavigationNode.document) }
        let items: [NavigationNode] = [
            folder("inbox", "Inbox", docs("inbox")),
            folder("work", "Work", docs("work") + [folder("meetings", "Meetings", parent: "work", [])]),
            folder("travel", "Travel", docs("travel")),
            folder("reading", "Reading", docs("reading")),
            folder("home", "Home", docs("home")),
        ] + docs("")
        let recent = notes.sorted { $0.updatedAt > $1.updatedAt }
        return NavigationResponse(items: items, recent: recent)
    }

    static var trash: [TrashedDocument] {
        [
            TrashedDocument(id: "t1", title: "Old packing list", deletedAt: stamp(daysAgo: 0, hours: 2), snippet: "Sunscreen, adapters, the blue backpack."),
            TrashedDocument(id: "t2", title: "Meeting notes 3/4", deletedAt: stamp(daysAgo: 3), snippet: "Agreed to move the launch to after the holidays."),
            TrashedDocument(id: "t3", title: "", deletedAt: stamp(daysAgo: 9), snippet: ""),
        ]
    }

    static var backups: [Backup] {
        [
            Backup(name: "fortress-1-manual.sqlite3", kind: "manual", createdAt: stamp(daysAgo: 0, hours: 1), size: 1_482_752),
            Backup(name: "fortress-2-auto.sqlite3", kind: "auto", createdAt: stamp(daysAgo: 1), size: 1_478_656),
            Backup(name: "fortress-3-auto.sqlite3", kind: "auto", createdAt: stamp(daysAgo: 2), size: 1_470_464),
            Backup(name: "fortress-4-pre-restore.sqlite3", kind: "pre-restore", createdAt: stamp(daysAgo: 5), size: 1_421_312),
        ]
    }

    /// A model that never touches the network, showing the fixture library.
    static func model(scope: LibraryScope = .all, openNote: String? = "n1") -> AppModel {
        let model = AppModel(api: APIClient(baseURL: URL(string: "http://127.0.0.1:9")!))
        model.connectsOnAppear = false
        model.hasLoadedNavigation = true
        model.navigation = navigation
        model.trash = trash
        model.backups = backups
        model.scope = scope
        if let openNote { model.open(noteId: openNote) }
        return model
    }
}
