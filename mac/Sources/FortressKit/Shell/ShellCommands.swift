import AppKit
import SwiftUI

/// Menu bar commands for the Shell's features (File, View, Note, Go).
public struct ShellCommands: Commands {
    let model: AppModel
    @FocusedValue(\.noteListTrash) private var listTrash

    public init(model: AppModel) {
        self.model = model
    }

    public var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("New Note") { Task { await model.newNote() } }
                .keyboardShortcut("n")
            Button("New Folder") { Task { await model.newFolder() } }
                .keyboardShortcut("n", modifiers: [.command, .option])
        }

        CommandGroup(replacing: .importExport) {
            Menu("Import") {
                Button("Markdown Files…") { model.isImporterShown = true }
                    .keyboardShortcut("i", modifiers: [.command, .shift])
                Button("From OneNote…") { model.showOneNoteImport() }
            }
            Button("Export as Markdown…") { Task { await model.exportOpenNote() } }
                .keyboardShortcut("e", modifiers: [.command, .shift])
                .disabled(model.openNoteId == nil)
            Divider()
            Button("Back Up Now") { Task { await model.backUpNow() } }
                .disabled(!model.isConnected)
            Button("Restore from Backup…") { model.isBackupsShown = true }
        }

        CommandGroup(after: .sidebar) {
            Button(model.isInspectorShown ? "Hide Inspector" : "Show Inspector") {
                model.isInspectorShown.toggle()
            }
            .keyboardShortcut("i", modifiers: [.command, .option])
            Button(model.isFocusMode ? "Exit Focus Mode" : "Enter Focus Mode") {
                model.isFocusMode.toggle()
            }
            .keyboardShortcut(.return, modifiers: [.command, .control])
            Divider()
        }

        CommandMenu("Note") {
            if let id = model.openNoteId {
                Menu("Move To") {
                    Button("All Notes (No Folder)") { Task { await model.moveNote(id: id, toFolder: nil) } }
                    Divider()
                    ForEach(NoteListing.folders(in: model.navigation.items), id: \.folder.id) { entry in
                        Button(String(repeating: "    ", count: entry.depth) + entry.folder.name) {
                            Task { await model.moveNote(id: id, toFolder: entry.folder.id) }
                        }
                    }
                }
            }
            Divider()
            // ⌘⌫ only while the note list has focus; in the editor it deletes to the line start.
            Button("Move to Trash") {
                if let id = listTrash?.id { Task { await model.trashNote(id: id) } }
            }
            .keyboardShortcut(.delete, modifiers: .command)
            .disabled(listTrash == nil)
        }

        CommandMenu("Go") {
            Button("All Notes") { model.select(.all) }.keyboardShortcut("1")
            Button("Rough") { model.select(.rough) }.keyboardShortcut("2")
            Button("Polished") { model.select(.polished) }.keyboardShortcut("3")
            Button("Recent") { model.select(.recent) }.keyboardShortcut("4")
            Divider()
            Button("Trash") { model.select(.trash) }
        }
    }
}

/// Starts the app as a regular foreground app (also when run from Terminal) and stops a
/// backend the app started when it quits.
@MainActor
public final class FortressAppDelegate: NSObject, NSApplicationDelegate {
    public let model = AppModel()

    public func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    public func applicationWillTerminate(_ notification: Notification) {
        model.backend.stop(waitUpTo: 3)
    }
}
