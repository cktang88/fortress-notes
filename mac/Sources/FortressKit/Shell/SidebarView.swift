import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Library, folders (nested) and Trash.
struct SidebarView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let selection = Binding<LibraryScope?>(
            get: { model.scope },
            set: { if let scope = $0 { model.select(scope) } })
        List(selection: selection) {
            Section("Library") {
                LibraryRow(title: "All Notes", symbol: "note.text", scope: .all, dropFolder: .some(nil))
                LibraryRow(title: "Recent", symbol: "clock", scope: .recent)
            }
            Section("Folders") {
                OutlineGroup(NoteListing.folderTree(model.navigation.items), children: \.subfolders) { node in
                    FolderRow(node: node)
                }
                LibraryRow(title: "Trash", symbol: "trash", scope: .trash)
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Button {
                Task { await model.newFolder() }
            } label: {
                Label("New Folder", systemImage: "plus.circle")
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .help("New Folder (⌥⌘N)")
            .accessibilityLabel("New Folder")
            .disabled(!model.isConnected)
        }
    }
}

/// A fixed sidebar row. Notes dropped on All Notes move to the top level; on Trash, to the Trash.
private struct LibraryRow: View {
    @Environment(AppModel.self) private var model
    let title: String
    let symbol: String
    let scope: LibraryScope
    /// Where dropped notes and files go: `.some(nil)` = top level; nil = not a drop target.
    var dropFolder: String?? = nil
    @State private var isTargeted = false

    var body: some View {
        Label(title, systemImage: symbol)
            .tag(scope)
            .modifier(dropTarget)
            .dropHighlight(isTargeted)
    }

    private var dropTarget: SidebarDropTarget {
        let model: AppModel = self.model
        let scope: LibraryScope = self.scope
        let destination: String?? = self.dropFolder
        let onNote: @MainActor @Sendable (String) async -> Void = { id in
            if scope == .trash {
                await model.trashNote(id: id)
            } else if let folder = destination {
                await model.moveNote(id: id, toFolder: folder)
            }
        }
        var onFiles: (@MainActor @Sendable ([URL]) -> Void)?
        var onFolder: (@MainActor @Sendable (String) async -> Void)?
        if scope != .trash {
            onFiles = { urls in model.beginImport(urls, into: nil) }
        }
        if let destination {
            onFolder = { id in await model.moveFolder(id: id, toParent: destination) }
        }
        return SidebarDropTarget(
            isEnabled: destination != nil || scope == .trash, isTargeted: $isTargeted,
            onNote: onNote, onFolder: onFolder, onFiles: onFiles)
    }
}

/// A folder: its name (editable in place), a context menu, and a drop target.
private struct FolderRow: View {
    @Environment(AppModel.self) private var model
    let node: FolderNode
    @State private var name = ""
    @State private var isTargeted = false
    @State private var isConfirmingDelete = false
    @FocusState private var isEditing: Bool

    var body: some View {
        Group {
            if model.renamingFolderId == node.id {
                Label {
                    TextField("Folder Name", text: $name)
                        .textFieldStyle(.plain)
                        .focused($isEditing)
                        .onSubmit { commit() }
                        .onExitCommand { model.renamingFolderId = nil }
                        .onAppear {
                            name = node.name
                            isEditing = true
                        }
                        .onChange(of: isEditing) { _, editing in if !editing { commit() } }
                } icon: {
                    Image(systemName: "folder")
                }
            } else {
                Label(node.name, systemImage: model.scope == .folder(id: node.id) ? "folder.fill" : "folder")
            }
        }
        .tag(LibraryScope.folder(id: node.id))
        .draggable(NoteDrag.folderPayload(for: node.id)) {
            Label(node.name, systemImage: "folder").padding(8)
        }
        .modifier(dropTarget)
        .dropHighlight(isTargeted)
        .accessibilityAction(named: "Rename") { model.renamingFolderId = node.id }
        .accessibilityAction(named: "New Note in Folder") { Task { await model.newNote(in: node.id) } }
        .contextMenu {
            Button("New Note") { Task { await model.newNote(in: node.id) } }
            Button("New Folder Inside") { Task { await model.newFolder(in: node.id) } }
            Divider()
            Button("Rename") { model.renamingFolderId = node.id }
            Button("Import Markdown…") {
                model.select(.folder(id: node.id))
                model.isImporterShown = true
            }
            Divider()
            Button("Delete Folder…", role: .destructive) { isConfirmingDelete = true }
        }
        .confirmationDialog(
            "Delete “\(node.name)”?", isPresented: $isConfirmingDelete
        ) {
            Button("Delete Folder", role: .destructive) { Task { await model.deleteFolder(id: node.id) } }
        } message: {
            Text(deleteMessage)
        }
    }

    private var deleteMessage: String {
        let notes = node.folder.children.filter { if case .document = $0 { true } else { false } }.count
        let folders = node.folder.children.count - notes
        guard notes + folders > 0 else { return "The folder is empty." }
        let destination = node.folder.parentId.flatMap { NoteListing.folder(id: $0, in: model.navigation.items)?.name } ?? "All Notes"
        var parts: [String] = []
        if notes > 0 { parts.append(notes == 1 ? "1 note" : "\(notes) notes") }
        if folders > 0 { parts.append(folders == 1 ? "1 folder" : "\(folders) folders") }
        return "Its \(parts.joined(separator: " and ")) will move to \(destination)."
    }

    private var dropTarget: SidebarDropTarget {
        let model: AppModel = self.model
        let folderId = node.id
        let onNote: @MainActor @Sendable (String) async -> Void = { id in
            await model.moveNote(id: id, toFolder: folderId)
        }
        let onFiles: @MainActor @Sendable ([URL]) -> Void = { urls in
            model.beginImport(urls, into: folderId)
        }
        let onFolder: @MainActor @Sendable (String) async -> Void = { id in
            // A folder can't go into itself or its own subfolder.
            guard AppModel.canMoveFolder(id, into: folderId, items: model.navigation.items) else {
                NSSound.beep()
                return
            }
            await model.moveFolder(id: id, toParent: folderId)
        }
        return SidebarDropTarget(
            isEnabled: true, isTargeted: $isTargeted, onNote: onNote, onFolder: onFolder, onFiles: onFiles)
    }

    private func commit() {
        guard model.renamingFolderId == node.id else { return }
        let newName = name
        Task { await model.renameFolder(id: node.id, to: newName) }
    }
}

extension View {
    /// Shows a sidebar row is a valid drop destination while something is over it.
    func dropHighlight(_ isTargeted: Bool) -> some View {
        background {
            if isTargeted {
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .strokeBorder(.selection, lineWidth: 2)
                    .padding(-4)
            }
        }
    }
}

/// Accepts notes and folders dragged in the window (as "fortress-note:<id>" /
/// "fortress-folder:<id>" text) and Markdown files.
struct SidebarDropTarget: ViewModifier {
    let isEnabled: Bool
    @Binding var isTargeted: Bool
    let onNote: @MainActor @Sendable (String) async -> Void
    var onFolder: (@MainActor @Sendable (String) async -> Void)?
    let onFiles: (@MainActor @Sendable ([URL]) -> Void)?

    func body(content: Content) -> some View {
        if isEnabled {
            content
                .onDrop(of: [.fileURL, .utf8PlainText, .plainText], isTargeted: $isTargeted) { providers in
                    handle(providers)
                }
        } else {
            content
        }
    }

    private func handle(_ providers: [NSItemProvider]) -> Bool {
        let files = providers.filter { $0.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) }
        if !files.isEmpty {
            guard let onFiles else { return false }
            let collector = URLCollector(expected: files.count) { urls in onFiles(urls) }
            for provider in files {
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    Task { @MainActor in collector.add(url) }
                }
            }
            return true
        }
        guard let provider = providers.first(where: { $0.canLoadObject(ofClass: NSString.self) }) else { return false }
        let onNote = self.onNote
        let onFolder = self.onFolder
        _ = provider.loadObject(ofClass: NSString.self) { object, _ in
            guard let text = object as? NSString else { return }
            let string = String(text)
            Task { @MainActor in
                if let id = NoteDrag.noteId(from: string) {
                    await onNote(id)
                } else if let id = NoteDrag.folderId(from: string) {
                    if let onFolder { await onFolder(id) } else { NSSound.beep() }
                }
            }
        }
        return true
    }
}

/// Gathers the URLs of a multi-file drop, then hands them over at once.
@MainActor
private final class URLCollector {
    private var urls: [URL] = []
    private var remaining: Int
    private let done: @MainActor ([URL]) -> Void

    init(expected: Int, done: @escaping @MainActor ([URL]) -> Void) {
        remaining = expected
        self.done = done
    }

    func add(_ url: URL?) {
        if let url { urls.append(url) }
        remaining -= 1
        if remaining == 0 { done(urls) }
    }
}

/// The text a dragged note carries.
enum NoteDrag {
    static let prefix = "fortress-note:"
    static func payload(for id: String) -> String { prefix + id }
    static func noteId(from text: String) -> String? {
        text.hasPrefix(prefix) ? String(text.dropFirst(prefix.count)) : nil
    }

    static let folderPrefix = "fortress-folder:"
    static func folderPayload(for id: String) -> String { folderPrefix + id }
    static func folderId(from text: String) -> String? {
        text.hasPrefix(folderPrefix) ? String(text.dropFirst(folderPrefix.count)) : nil
    }
}
