import AppKit
import Foundation
import SwiftUI
import Testing

@testable import FortressKit

// Tests for the HIG audit (mac/UX_AUDIT.md): shortcuts, per-window state, restoration,
// undo, drag and drop rules, Quick Capture's shortcut and the editor's pasteboard.

// MARK: Shortcuts (principle of least surprise)

@Test func appShortcutsAreUniqueAndNeverShadowTheSystem() {
    var seen: [String: String] = [:]
    for shortcut in Shortcuts.all {
        #expect(seen[shortcut.chord] == nil, "\(shortcut.chord) is both \(seen[shortcut.chord] ?? "") and \(shortcut.title)")
        seen[shortcut.chord] = shortcut.title
        if let meaning = Shortcuts.systemReserved[shortcut.chord] {
            #expect(Shortcuts.sameMeaning.contains(shortcut.chord),
                    "\(shortcut.title) takes \(shortcut.chord), which is macOS’s “\(meaning)”")
        }
    }
    // The ones the HIG calls out: ⌘E finds, ⌥⌘T is the toolbar, ⌘T fonts, ⌥⌘C copy style.
    for chord in ["⌘E", "⌥⌘T", "⌘T", "⌥⌘C", "⌥⌘V", "⇧⌘C", "⌘L", "⌃⌘Space", "⌘Space"] {
        #expect(seen[chord] == nil, "\(chord) must stay the system’s")
    }
    #expect(Shortcuts.addLink.chord == "⌘K")  // As in Notes, Pages, Mail and TextEdit.
    #expect(Shortcuts.inlineCode.chord == "⌥⌘E")
    #expect(Shortcuts.commandPalette.chord == "⇧⌘O")
}

@Test func shortcutsDisplayModifiersInTheMacOrder() {
    #expect(AppShortcut("x", "c", [.command, .control, .option]).display == "⌃⌥⌘C")
    #expect(AppShortcut("x", .return, [.command, .control]).display == "⌃⌘↩")
    #expect(AppShortcut("x", .upArrow, [.control, .command]).display == "⌃⌘↑")
    #expect(Shortcuts.moveToTrash.display == "⌘⌫")
}

@Test @MainActor func paletteHintsAreTheMenuBarsShortcuts() {
    let menu = Set(Shortcuts.all.map(\.display))
    for command in PaletteCommand.allCases where command != .quickCapture {
        guard let hint = command.shortcut else { continue }
        #expect(menu.contains(hint), "\(command.title) shows \(hint), which no menu item has")
    }
    // The palette offers the menu bar's commands too (HIG: every command, one place to find it).
    let titles = Set(PaletteCommand.allCases.map(\.title))
    for title in ["New Note", "New Folder", "New Window", "Import Markdown Files…", "Export as Markdown…",
                  "Back Up Now", "Import from OneNote…", "Reload Notes", "Move Note to Trash"] {
        #expect(titles.contains(title), "\(title) is missing from the palette")
    }
}

// MARK: Quick Capture shortcut

@Test func quickCaptureShortcutStoresAndRefusesSystemKeys() throws {
    let standard = QuickCaptureShortcut.standard
    #expect(standard.display == "⇧⌘Space")
    #expect(standard.systemMeaning == nil && standard.appMeaning == nil)
    #expect(QuickCaptureShortcut(storage: standard.storage) == standard)
    #expect(QuickCaptureShortcut(storage: QuickCaptureShortcut.noneStorage) == nil)

    let spotlight = QuickCaptureShortcut(keyCode: 49, modifiers: .command, key: " ")
    #expect(spotlight.systemMeaning == "Spotlight")
    let emoji = QuickCaptureShortcut(keyCode: 49, modifiers: [.command, .control], key: " ")
    #expect(emoji.systemMeaning == "Emoji & Symbols")
    let inputSource = QuickCaptureShortcut(keyCode: 49, modifiers: .control, key: " ")
    #expect(inputSource.systemMeaning == "Input Source")
    let newNote = QuickCaptureShortcut(keyCode: 45, modifiers: .command, key: "n")
    #expect(newNote.appMeaning == "New Note")
    #expect(QuickCaptureShortcut(keyCode: 38, modifiers: [.control, .option], key: "j").carbonModifiers != 0)

    let defaults = try #require(UserDefaults(suiteName: "ux-audit-\(UUID().uuidString)"))
    #expect(QuickCaptureShortcut.saved(in: defaults) == .standard)
    defaults.set(QuickCaptureShortcut.noneStorage, forKey: QuickCaptureShortcut.defaultsKey)
    #expect(QuickCaptureShortcut.saved(in: defaults) == nil)
}

// MARK: Windows

@Test @MainActor func windowsShareTheLibraryButNotTheSelection() {
    let library = Library(api: APIClient(baseURL: URL(string: "http://127.0.0.1:9")!))
    let first = AppModel(library: library)
    let second = AppModel(library: library)
    first.connectsOnAppear = false
    second.connectsOnAppear = false
    first.navigation = ShellFixtures.navigation
    #expect(second.navigation == first.navigation)

    first.select(.folder(id: "travel"))
    first.open(noteId: "n1")
    first.isInspectorShown = true
    #expect(second.scope == .all)
    #expect(second.openNoteId == nil)
    #expect(!second.isInspectorShown)

    first.isConnected = false
    #expect(!second.isConnected)  // The connection is the library's.

    library.windowDidBecomeActive(first)
    library.windowDidBecomeActive(second)
    #expect(library.activeWindowModel === second)
    library.windowDidClose(second)
    #expect(library.activeWindowModel === first)
    library.openNote("n2")
    #expect(first.openNoteId == "n2")
}

@Test func scopesRoundTripThroughSceneStorage() {
    let scopes: [LibraryScope] = [.all, .rough, .polished, .recent, .trash, .folder(id: "a:b")]
    for scope in scopes {
        #expect(LibraryScope(storageKey: scope.storageKey) == scope)
    }
    #expect(LibraryScope(storageKey: "") == nil)
    #expect(LibraryScope(storageKey: "folder:") == nil)
}

// MARK: Drag and drop

@MainActor
@Test func foldersNeverMoveIntoThemselves() {
    let items = ShellFixtures.navigation.items
    #expect(!AppModel.canMoveFolder("work", into: "work", items: items))
    #expect(!AppModel.canMoveFolder("work", into: "meetings", items: items))  // its own child
    #expect(AppModel.canMoveFolder("meetings", into: "travel", items: items))
    #expect(AppModel.canMoveFolder("meetings", into: nil, items: items))
    #expect(NoteDrag.folderId(from: NoteDrag.folderPayload(for: "work")) == "work")
    #expect(NoteDrag.noteId(from: NoteDrag.folderPayload(for: "work")) == nil)
}

// MARK: Undo

@Test @MainActor func moveToTrashIsUndoableAndRedoable() async throws {
    let stub = FindingStub { request in
        if request.url?.path == "/api/navigation" { return .json(NavigationResponse(items: [], recent: [])) }
        return FindingStub.Reply(body: Data("{}".utf8))
    }
    let model = AppModel(api: stub.api)
    model.connectsOnAppear = false
    model.navigation = ShellFixtures.navigation
    let undo = UndoManager()
    model.undoManager = undo

    await model.trashNote(id: "n3")
    #expect(undo.undoActionName == "Move to Trash")
    undo.undo()
    try await waitUntil { stub.started.contains { $0.path == "/api/trash/n3/restore" } }
    #expect(stub.started.contains { $0.path == "/api/trash/n3/restore" })
    #expect(undo.canRedo)
    #expect(undo.redoActionName == "Move to Trash")
}

@Test @MainActor func movingANoteIsUndoable() async throws {
    let stub = FindingStub { _ in FindingStub.Reply(body: Data("{}".utf8)) }
    let model = AppModel(api: stub.api)
    model.connectsOnAppear = false
    model.navigation = ShellFixtures.navigation
    let undo = UndoManager()
    model.undoManager = undo

    await model.moveNote(id: "n1", toFolder: "reading")
    #expect(undo.undoActionName == "Move Note")
    undo.undo()
    try await waitUntil {
        NoteListing.allDocuments(in: model.navigation.items).first { $0.id == "n1" }?.folderId == "travel"
    }
    #expect(NoteListing.allDocuments(in: model.navigation.items).first { $0.id == "n1" }?.folderId == "travel")
}

// MARK: Editor undo stays apart from the window's

@Test @MainActor func theNoteKeepsItsOwnUndoStack() {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 300), styleMask: [.titled], backing: .buffered, defer: true)
    let text = FortressTextView.make()
    window.contentView = text
    #expect(text.undoManager != nil)
    #expect(text.undoManager !== window.undoManager)
    let cell = NSView()
    text.addSubview(cell)
    #expect(cell.noteUndoManager === text.undoManager)
}

// MARK: Pasteboard (copy out: blocks, Markdown, rich and plain text; paste in: the same)

@Test func copiedBlocksBecomeMarkdown() {
    let blocks = [
        EditorBlock(kind: .heading1, runs: [InlineRun("Kyoto")]),
        EditorBlock(kind: .paragraph, runs: [
            InlineRun("We booked a "), InlineRun("ryokan", InlineMarks(bold: true)), InlineRun(" in "),
            InlineRun("Gion", InlineMarks(link: "https://example.com")), InlineRun(", see "),
            InlineRun("notes.md", InlineMarks(code: true)),
        ]),
        EditorBlock(kind: .bullet, runs: [InlineRun("Passport")]),
        EditorBlock(kind: .bullet, depth: 1, runs: [InlineRun("Copy")]),
        EditorBlock(kind: .numbered, runs: [InlineRun("One")]),
        EditorBlock(kind: .numbered, runs: [InlineRun("Two")]),
        EditorBlock(kind: .check, checked: true, runs: [InlineRun("Book train")]),
        EditorBlock(kind: .quote, runs: [InlineRun("Quoted")]),
        EditorBlock(kind: .code, language: "swift", runs: [InlineRun("let a = 1")]),
        EditorBlock(kind: .divider),
    ]
    let markdown = MarkdownCopy.markdown(blocks)
    #expect(markdown == """
        # Kyoto

        We booked a **ryokan** in [Gion](https://example.com), see `notes.md`

        - Passport
          - Copy
        1. One
        2. Two
        - [x] Book train

        > Quoted

        ```swift
        let a = 1
        ```

        ---
        """)
}

@Test @MainActor func theEditorOffersRichPlainAndMarkdownOnCopyAndReadsTheUsualTypes() {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 300), styleMask: [.titled], backing: .buffered, defer: true)
    let text = FortressTextView.make()
    window.contentView = text
    text.string = "Kyoto"
    text.setSelectedRange(NSRange(location: 0, length: 5))  // The text system offers types for a selection.
    let writable = text.writablePasteboardTypes
    #expect(writable.contains(BlockEditorController.blocksPasteboardType))
    #expect(writable.contains(NSPasteboard.PasteboardType(MarkdownCopy.typeIdentifier)))
    // The text system may list the legacy names ("NeXT Rich Text Format…", "NSStringPboardType").
    let rtf: Set<String> = ["public.rtf", "NeXT Rich Text Format v1.0 pasteboard type"]
    let plain: Set<String> = ["public.utf8-plain-text", "NSStringPboardType"]
    #expect(writable.contains { rtf.contains($0.rawValue) })
    #expect(writable.contains { plain.contains($0.rawValue) })
    let readable = text.readablePasteboardTypes
    #expect(readable.contains(BlockEditorController.blocksPasteboardType))
    #expect(readable.contains { rtf.contains($0.rawValue) })
    #expect(readable.contains { plain.contains($0.rawValue) })
}

// MARK: Trashing the open note opens the one below it, in the list's own order

@Test @MainActor func trashingTheOpenNoteOpensItsNeighbourInTheListsSortOrder() async throws {
    let stub = FindingStub { _ in FindingStub.Reply(body: Data("{}".utf8)) }
    let model = AppModel(api: stub.api)
    model.connectsOnAppear = false
    model.navigation = ShellFixtures.navigation
    let defaults = try #require(UserDefaults(suiteName: "trash-neighbour-\(UUID().uuidString)"))
    defaults.set(NoteSort.title.rawValue, forKey: "noteSort")
    model.listDefaults = defaults
    // By title: … "Kyoto trip", "Packing list", "Q4 planning", … (by date edited, Sourdough is next).
    model.open(noteId: "n5")
    await model.trashNote(id: "n5")
    #expect(model.openNoteId == "n4")
}

@Test @MainActor func droppingANoteOnItsOwnFolderChangesNothing() async {
    let stub = FindingStub { _ in FindingStub.Reply(body: Data("{}".utf8)) }
    let model = AppModel(api: stub.api)
    model.connectsOnAppear = false
    model.navigation = ShellFixtures.navigation
    let before = model.navigation
    await model.moveNote(id: "n1", toFolder: "travel")  // already in Travel
    #expect(stub.started.isEmpty)
    #expect(model.navigation == before)
}
