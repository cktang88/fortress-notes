import AppKit
import SwiftUI

extension FocusedValues {
    /// The model of the main window in front. Menu commands act on it, so with two
    /// windows open each command reaches the window you're looking at.
    @Entry public var windowModel: AppModel?
}

extension View {
    func keyboardShortcut(_ shortcut: AppShortcut) -> some View {
        keyboardShortcut(shortcut.keyboardShortcut)
    }
}

/// Menu bar commands for the Shell's features (File, Edit, Format, View, Note, Go). Each
/// acts on the focused window's model (`FocusedValues.windowModel`) and is disabled, not
/// hidden, when nothing applies (HIG: the menu bar always shows the same items).
public struct ShellCommands: Commands {
    let library: Library
    @FocusedValue(\.windowModel) private var model
    @FocusedValue(\.noteListTrash) private var listTrash
    @Environment(\.openWindow) private var openWindow
    // The list's Sort and Group menu, also in View (HIG: toolbar items are in the menu bar).
    @AppStorage("noteSort") private var sort: NoteSort = .edited
    @AppStorage("groupByDate") private var groupsByDate = true
    @AppStorage(BlockOutlines.key) private var showsBlocks = false

    public init(library: Library) {
        self.library = library
    }

    public var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button(Shortcuts.newNote.title) {
                if let model { Task { await model.newNote() } } else { library.pendingNewNote = true; openWindow(id: MainWindowScene.id) }
            }
            .keyboardShortcut(Shortcuts.newNote)
            // Like the toolbar's New Note button: off while the backend can't be reached
            // (pressed then, it did nothing at all). With no window it opens one.
            .disabled(model != nil && !library.isConnected)
            Button(Shortcuts.newFolder.title) { if let model { Task { await model.newFolder() } } }
                .keyboardShortcut(Shortcuts.newFolder)
                .disabled(model == nil || !library.isConnected)
            Button("New Window") { openWindow(id: MainWindowScene.id) }
        }

        CommandGroup(replacing: .importExport) {
            Menu("Import") {
                Button("Markdown Files…") { model?.isImporterShown = true }
                    .keyboardShortcut(Shortcuts.importMarkdown)
                Button("From OneNote…") { model?.showOneNoteImport() }
            }
            .disabled(model == nil)
            Button(Shortcuts.exportMarkdown.title) { if let model { Task { await model.exportOpenNote() } } }
                .keyboardShortcut(Shortcuts.exportMarkdown)
                .disabled(model?.openNoteId == nil)
            Divider()
            Button("Back Up Now") { if let model { Task { await model.backUpNow() } } }
                .disabled(model == nil || !library.isConnected)
            Button("Restore from Backup…") { model?.isBackupsShown = true }
                .disabled(model == nil)
            Divider()
            Button("Empty Trash…") {
                guard let model else { return }
                model.query = ""
                model.select(.trash)
                // Asks only once the Trash is known to hold something (never "Permanently
                // delete 0 notes?"); an empty Trash just shows "Trash Is Empty".
                Task {
                    await model.refreshTrash()
                    if !model.trash.isEmpty { model.isEmptyTrashConfirmationShown = true }
                }
            }
            .disabled(model == nil || !library.isConnected || library.trash.isEmpty && model?.scope == .trash)
        }

        CommandGroup(after: .textEditing) {
            Divider()
            Button(Shortcuts.searchAllNotes.title) { model?.searchFocusRequest += 1 }
                .keyboardShortcut(Shortcuts.searchAllNotes)
                .disabled(model == nil)
        }

        FormatCommands(isEnabled: model?.openNoteId != nil)

        CommandGroup(after: .sidebar) {
            Button(model?.isInspectorShown == true ? "Hide Inspector" : "Show Inspector") {
                model?.isInspectorShown.toggle()
            }
            .keyboardShortcut(Shortcuts.toggleInspector)
            .disabled(model == nil)
            Button(model?.isFocusMode == true ? "Exit Focus Mode" : "Enter Focus Mode") {
                model?.isFocusMode.toggle()
            }
            .keyboardShortcut(Shortcuts.focusMode)
            .disabled(model == nil)
            Toggle(Shortcuts.showBlocks.title, isOn: $showsBlocks)
                .keyboardShortcut(Shortcuts.showBlocks)
            Divider()
            Picker("Sort By", selection: $sort) {
                ForEach(NoteSort.allCases) { Text($0.label).tag($0) }
            }
            Toggle("Group by Date", isOn: $groupsByDate)
                .disabled(sort == .title)
            Divider()
        }

        CommandGroup(before: .toolbar) {
            Button(Shortcuts.reloadNotes.title) { if let model { Task { await model.refreshNavigation() } } }
                .keyboardShortcut(Shortcuts.reloadNotes)
                .disabled(model == nil)
            Divider()
        }

        CommandMenu("Note") {
            let id = model?.openNoteId
            Menu("Move To") {
                if let model, let id {
                    Button("All Notes (No Folder)") { Task { await model.moveNote(id: id, toFolder: nil) } }
                    Divider()
                    ForEach(NoteListing.folders(in: library.navigation.items), id: \.folder.id) { entry in
                        Button(String(repeating: "    ", count: entry.depth) + entry.folder.name) {
                            Task { await model.moveNote(id: id, toFolder: entry.folder.id) }
                        }
                    }
                }
            }
            .disabled(id == nil)
            Button("Open in New Window") { if let id { openWindow(id: MainWindowScene.id, value: id) } }
                .disabled(id == nil)
            Divider()
            // ⌘⌫ only while the note list has focus; in the editor it deletes to the line start.
            Button(Shortcuts.moveToTrash.title) {
                if let model, let id = listTrash?.id { Task { await model.trashNote(id: id) } }
            }
            .keyboardShortcut(Shortcuts.moveToTrash)
            .disabled(listTrash == nil || model == nil)
        }

        CommandMenu("Go") {
            Group {
                Button(Shortcuts.allNotes.title) { model?.select(.all) }.keyboardShortcut(Shortcuts.allNotes)
                Button(Shortcuts.rough.title) { model?.select(.rough) }.keyboardShortcut(Shortcuts.rough)
                Button(Shortcuts.polished.title) { model?.select(.polished) }.keyboardShortcut(Shortcuts.polished)
                Button(Shortcuts.recent.title) { model?.select(.recent) }.keyboardShortcut(Shortcuts.recent)
                Divider()
                Button("Trash") { model?.select(.trash) }
            }
            .disabled(model == nil)
        }
    }
}

/// The Format menu, in its standard place after Edit. Each item sends the editor's own
/// action up the responder chain (the note's text view or a table cell handles it), so
/// a chosen menu item and its key do exactly the same thing. The system's Font and Text
/// submenus are left out on purpose: they would set fonts behind the editor's marks.
struct FormatCommands: Commands {
    let isEnabled: Bool

    var body: some Commands {
        CommandGroup(replacing: .textFormatting) {
            Group {
                item(Shortcuts.bold, "fortressToggleBold:")
                item(Shortcuts.italic, "fortressToggleItalic:")
                item(Shortcuts.underline, "fortressToggleUnderline:")
                item(Shortcuts.strikethrough, "fortressToggleStrikethrough:")
                item(Shortcuts.inlineCode, "fortressToggleInlineCode:")
                item(Shortcuts.highlight, "fortressHighlight:")
                item(Shortcuts.superscript, "fortressToggleSuperscript:")
                item(Shortcuts.subscript, "fortressToggleSubscript:")
            }
            Divider()
            Group {
                item(Shortcuts.body, "fortressBody:")
                item(Shortcuts.heading1, "fortressHeading1:")
                item(Shortcuts.heading2, "fortressHeading2:")
                item(Shortcuts.heading3, "fortressHeading3:")
                item(Shortcuts.bulletedList, "fortressBulletedList:")
                item(Shortcuts.numberedList, "fortressNumberedList:")
                item(Shortcuts.checklist, "fortressChecklist:")
                item(Shortcuts.quote, "fortressQuote:")
                item(Shortcuts.codeBlock, "fortressCodeBlock:")
            }
            Divider()
            Group {
                item(Shortcuts.toggleCheckbox, "fortressToggleCheckbox:")
                item(Shortcuts.indent, "fortressIndent:")
                item(Shortcuts.outdent, "fortressOutdent:")
                item(Shortcuts.moveBlockUp, "fortressMoveBlockUp:")
                item(Shortcuts.moveBlockDown, "fortressMoveBlockDown:")
            }
            // What the block handle's menu does, for the keyboard (Esc selects a block).
            Menu("Block") {
                plain("Select Block", "fortressSelectBlock:")
                plain("Insert Block…", "fortressInsertBlock:")
                item(Shortcuts.duplicateBlock, "fortressDuplicateBlock:")
                plain("Delete Block", "fortressDeleteBlock:")
                plain("Copy Link to Block", "fortressCopyBlockLink:")
                Divider()
                plain("Turn into 2 Columns", "fortressTurnIntoTwoColumns:")
                plain("Turn into 3 Columns", "fortressTurnIntoThreeColumns:")
                plain("Add Column", "fortressAddColumn:")
                plain("Remove Column", "fortressRemoveColumn:")
                plain("Unwrap Columns", "fortressUnwrapColumns:")
                Divider()
                plain("Edit Math…", "fortressEditMath:")
                plain("Block Info", "fortressBlockInfo:")
            }
            .disabled(!isEnabled)
            Divider()
            item(Shortcuts.addLink, "fortressAddLink:")
            Button("Insert Table") { send("fortressInsertTable:") }
                .disabled(!isEnabled)
        }
    }

    private func plain(_ title: LocalizedStringKey, _ selector: String) -> some View {
        Button(title) { send(selector) }
            .disabled(!isEnabled)
    }

    private func item(_ shortcut: AppShortcut, _ selector: String) -> some View {
        Button(shortcut.title) { send(selector) }
            .keyboardShortcut(shortcut)
            .disabled(!isEnabled)
    }

    private func send(_ selector: String) {
        NSApp.sendAction(Selector(selector), to: nil, from: nil)
    }
}

/// Starts the app as a regular foreground app (also when run from Terminal) and stops a
/// backend the app started when it quits.
@MainActor
public final class FortressAppDelegate: NSObject, NSApplicationDelegate {
    /// What every window shares.
    public let library = Library()
    /// App-wide actions with no window of their own (Quick Capture, Settings).
    public lazy var model = AppModel(library: library)

    public func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    /// Quitting with unsaved edits: send them first (at most two seconds), then quit.
    /// Nothing is lost either way: what didn't go is kept on disk and saved next launch.
    public func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard EditorSession.hasUnsavedEdits else { return .terminateNow }
        Task { @MainActor in
            await withTaskGroup(of: Void.self) { group in
                group.addTask { await EditorSession.saveAllForQuit() }
                group.addTask { try? await Task.sleep(for: .seconds(2)) }
                await group.next()
                group.cancelAll()
            }
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    /// Closing the last window keeps the app running (like Notes): Quick Capture still
    /// works, and the Dock icon or ⌘N opens a window again.
    public func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    public func applicationWillTerminate(_ notification: Notification) {
        model.backend.terminateForQuit()
    }
}
