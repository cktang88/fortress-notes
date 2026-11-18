import Foundation

/// A quiet capsule at the bottom of the window, optionally with one action.
public struct Toast: Identifiable, Hashable, Sendable {
    public enum Action: Hashable, Sendable {
        /// Undo a move to the Trash.
        case restore(documentId: String)
    }

    public let id: UUID
    public var message: String
    public var action: Action?

    public init(id: UUID = UUID(), message: String, action: Action? = nil) {
        self.id = id
        self.message = message
        self.action = action
    }

    public var actionLabel: String? {
        switch action {
        case .restore: "Undo"
        case nil: nil
        }
    }
}

/// Markdown files on their way into the import sheet.
public struct MarkdownImportRequest: Identifiable, Hashable, Sendable {
    public let id = UUID()
    public var files: [URL]
    public var folderId: String?

    public init(files: [URL], folderId: String?) {
        self.files = files
        self.folderId = folderId
    }
}

// MARK: Shell actions

extension AppModel {
    // MARK: Connection

    /// First contact with the backend: load the sidebar, or start the backend when it
    /// isn't running and Settings allow it.
    public func connect() async {
        applyBackendAddress()
        await refreshNavigation()
        if isConnected {
            hasLoadedNavigation = true
            notice = nil
            if scope == .trash { await refreshTrash() }
            return
        }
        notice = nil
        if backend.startsAutomatically && backend.canStart && !Self.isRunningTests {
            await startBackend()
        }
    }

    /// Tests open windows too; they must never launch a backend.
    static var isRunningTests: Bool {
        let name = ProcessInfo.processInfo.processName.lowercased()
        return name.contains("xctest") || name.contains("swiftpm-testing-helper")
            || ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }

    /// Points the API client at the address in Settings.
    public func applyBackendAddress() {
        if api.baseURL != backend.baseURL { api = APIClient(baseURL: backend.baseURL) }
    }

    /// Starts the local backend and waits (up to a minute; the first launch loads models)
    /// for it to answer.
    public func startBackend() async {
        applyBackendAddress()
        guard backend.start() else { return }
        for _ in 0..<120 {
            try? await Task.sleep(for: .milliseconds(500))
            if case .failed = backend.state { return }
            if (try? await api.health()) != nil {
                backend.markReachable()
                await refreshNavigation()
                hasLoadedNavigation = isConnected
                notice = nil
                return
            }
        }
        backend.markUnreachable("The backend didn't answer within a minute. See the log for details.")
    }

    /// Tries the backend again (after it was started by hand, or the address changed).
    public func retryConnection() async {
        applyBackendAddress()
        await refreshNavigation()
        if isConnected {
            hasLoadedNavigation = true
            notice = nil
            if scope == .trash { await refreshTrash() }
        }
    }

    // MARK: Window

    /// The open note's summary from the sidebar tree.
    public var openNote: NavigationDocument? {
        guard let id = openNoteId else { return nil }
        return NoteListing.allDocuments(in: navigation.items).first { $0.id == id }
    }

    /// The window title: the open note's title.
    public var windowTitle: String {
        switch route {
        case .note: openNote.map { NoteListing.displayTitle($0.title) } ?? "Fortress Notes"
        case .ask: "Ask"
        case .empty: scopeTitle
        }
    }

    /// The open note's folder, shown under the window title.
    public var windowSubtitle: String {
        guard case .note = route, let folderId = openNote?.folderId else { return "" }
        return NoteListing.folder(id: folderId, in: navigation.items)?.name ?? ""
    }

    public var scopeTitle: String {
        switch scope {
        case .all: "All Notes"
        case .rough: "Rough"
        case .polished: "Polished"
        case .recent: "Recent"
        case .trash: "Trash"
        case .folder(let id): NoteListing.folder(id: id, in: navigation.items)?.name ?? "Folder"
        }
    }

    public func select(_ scope: LibraryScope) {
        guard scope != self.scope else { return }
        self.scope = scope
        selectedTrashId = nil
        if scope == .trash {
            Task { await refreshTrash() }
        }
    }

    public func requestEditorFocus() {
        guard openNoteId != nil else { return }
        editorFocusRequest += 1
    }

    /// Lets the editor keep the sidebar and list current while a note is edited.
    public func noteDidChange(_ document: BlockDocument) {
        let snippet = document.children.first?.text
        func patch(_ note: inout NavigationDocument) {
            guard note.id == document.id else { return }
            note.title = document.title
            note.status = document.status
            note.updatedAt = document.updatedAt
            if let snippet { note.snippet = snippet }
        }
        func patch(_ nodes: inout [NavigationNode]) {
            for index in nodes.indices {
                switch nodes[index] {
                case .document(var note):
                    patch(&note)
                    nodes[index] = .document(note)
                case .folder(var folder):
                    patch(&folder.children)
                    nodes[index] = .folder(folder)
                }
            }
        }
        patch(&navigation.items)
        for index in navigation.recent.indices { patch(&navigation.recent[index]) }
        recordBlockVersions(of: document)  // Finding: "Seen before" cache key.
    }

    // MARK: Notes

    /// Creates a note (in the selected folder, if any) and opens it.
    public func newNote(in folderId: String? = nil) async {
        var folder = folderId
        if folder == nil, case .folder(let selected) = scope { folder = selected }
        do {
            let document = try await api.createDocument(folderId: folder)
            if let folder { scope = .folder(id: folder) } else if scope != .all && scope != .recent { scope = .all }
            selectedTrashId = nil
            await refreshNavigation()
            open(noteId: document.id)
            requestEditorFocus()
        } catch {
            report(error)
        }
    }

    /// Moves a note to the Trash and offers Undo.
    public func trashNote(id: String) async {
        let visible = NoteListing.sorted(NoteListing.notes(in: scope, navigation: navigation), by: .edited)
        do {
            try await api.trashDocument(id: id)
            if openNoteId == id {
                // Keep the list's place: open the neighbour, like Mail.
                let index = visible.firstIndex { $0.id == id }
                let remaining = visible.filter { $0.id != id }
                if let index, !remaining.isEmpty {
                    open(noteId: remaining[min(index, remaining.count - 1)].id)
                } else {
                    route = .empty
                }
            }
            let title = visible.first { $0.id == id }.map { NoteListing.displayTitle($0.title) }
            show(Toast(message: title.map { "“\($0)” moved to Trash" } ?? "Moved to Trash", action: .restore(documentId: id)))
            await refreshNavigation()
        } catch {
            report(error)
        }
    }

    public func moveNote(id: String, toFolder folderId: String?) async {
        do {
            try await api.moveDocument(id: id, folderId: folderId)
            await refreshNavigation()
        } catch {
            report(error)
        }
    }

    // MARK: Folders

    /// Creates "New Folder" (inside `parentId`) and starts renaming it in place.
    public func newFolder(in parentId: String? = nil) async {
        do {
            let folder = try await api.createFolder(name: uniqueFolderName(in: parentId), parentId: parentId)
            await refreshNavigation()
            scope = .folder(id: folder.id)
            renamingFolderId = folder.id
        } catch {
            report(error)
        }
    }

    private func uniqueFolderName(in parentId: String?) -> String {
        let siblings: [NavigationNode]
        if let parentId {
            siblings = NoteListing.folder(id: parentId, in: navigation.items)?.children ?? []
        } else {
            siblings = navigation.items
        }
        let names = Set(siblings.compactMap { if case .folder(let f) = $0 { f.name } else { nil } })
        var name = "New Folder"
        var counter = 2
        while names.contains(name) {
            name = "New Folder \(counter)"
            counter += 1
        }
        return name
    }

    public func renameFolder(id: String, to name: String) async {
        renamingFolderId = nil
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, NoteListing.folder(id: id, in: navigation.items)?.name != trimmed else { return }
        do {
            _ = try await api.renameFolder(id: id, name: trimmed)
            await refreshNavigation()
        } catch {
            report(error)
        }
    }

    /// Deletes a folder; its notes and subfolders move up to its parent.
    public func deleteFolder(id: String) async {
        guard let folder = NoteListing.folder(id: id, in: navigation.items) else { return }
        do {
            for child in folder.children {
                switch child {
                case .document(let note): try await api.moveDocument(id: note.id, folderId: folder.parentId)
                case .folder(let sub): try await api.moveFolder(id: sub.id, parentId: folder.parentId)
                }
            }
            try await api.deleteFolder(id: id)
            if case .folder(let selected) = scope, NoteListing.isFolder(selected, inside: id, items: navigation.items) {
                scope = folder.parentId.map { .folder(id: $0) } ?? .all
            }
            await refreshNavigation()
        } catch {
            report(error)
            await refreshNavigation()
        }
    }

    // MARK: Trash

    public func refreshTrash() async {
        do {
            trash = try await api.trash()
            if let selected = selectedTrashId, !trash.contains(where: { $0.id == selected }) {
                selectedTrashId = nil
            }
        } catch {
            report(error)
        }
    }

    public func restore(trashedId id: String, announce: Bool = true) async {
        do {
            try await api.restore(documentId: id)
            if announce {
                let title = trash.first { $0.id == id }.map { NoteListing.displayTitle($0.title) }
                show(Toast(message: title.map { "Restored “\($0)”" } ?? "Restored"))
            }
            await refreshNavigation()
            if scope == .trash { await refreshTrash() }
        } catch {
            report(error)
        }
    }

    public func deleteForever(trashedId id: String) async {
        do {
            try await api.deleteForever(documentId: id)
            await refreshTrash()
        } catch {
            report(error)
        }
    }

    public func emptyTrash() async {
        do {
            try await api.emptyTrash()
            selectedTrashId = nil
            await refreshTrash()
        } catch {
            report(error)
        }
    }

    // MARK: Toasts

    public func show(_ toast: Toast) {
        self.toast = toast
    }

    public func perform(_ action: Toast.Action) async {
        toast = nil
        switch action {
        case .restore(let id):
            await restore(trashedId: id, announce: false)
            open(noteId: id)
        }
    }

    // MARK: Backups

    public func refreshBackups() async {
        do {
            backups = try await api.backups()
        } catch {
            report(error)
        }
    }

    public func backUpNow() async {
        do {
            _ = try await api.createBackup()
            show(Toast(message: "Backed up"))
            await refreshBackups()
        } catch {
            report(error)
        }
    }

    /// Restores a backup (the backend saves a pre-restore backup first).
    public func restoreBackup(_ backup: Backup) async -> Bool {
        do {
            try await api.restoreBackup(name: backup.name)
            route = .empty
            await refreshNavigation()
            await refreshBackups()
            show(Toast(message: "Restored the backup"))
            return true
        } catch {
            report(error)
            return false
        }
    }

    // MARK: Import and export

    /// Opens the OneNote import sheet: a running or finished import shows where it is,
    /// otherwise step 1 with the selected folder as the destination.
    public func showOneNoteImport() {
        let importer = oneNoteImport
        if case .choosing = importer.phase, importer.file == nil {
            importer.folderId = if case .folder(let id) = scope { id } else { nil }
        }
        importer.onFinished = { [weak self] job in
            guard let self else { return }
            Task { @MainActor in
                await self.refreshNavigation()
                if !self.isOneNoteImportShown, let result = job.result {
                    self.toast = Toast(message: OneNoteImport.headline(result))
                }
            }
        }
        importer.resumeRunningImport(api: api)
        isOneNoteImportShown = true
    }

    /// "Show in Sidebar" from the import report: selects the notebook's folder.
    public func revealImportedFolder(_ folderId: String) async {
        await refreshNavigation()
        query = ""
        select(.folder(id: folderId))
        isOneNoteImportShown = false
    }

    /// Opens the import sheet for these files (folders are searched for Markdown files).
    public func beginImport(_ urls: [URL], into folderId: String?) {
        let files = MarkdownFiles.collect(urls)
        guard !files.isEmpty else {
            notice = Notice(message: "There are no Markdown files to import.")
            return
        }
        pendingImport = MarkdownImportRequest(files: files, folderId: folderId)
    }

    public func importMarkdown(_ request: MarkdownImportRequest, into folderId: String?) async -> MarkdownImportResult? {
        var files: [(name: String, data: Data)] = []
        var unreadable: [MarkdownImportResult.Failure] = []
        for url in request.files {
            let scoped = url.startAccessingSecurityScopedResource()
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            if let data = try? Data(contentsOf: url) {
                files.append((name: url.lastPathComponent, data: data))
            } else {
                unreadable.append(.init(name: url.lastPathComponent, detail: "couldn't be read"))
            }
        }
        do {
            var result = try await api.importMarkdown(files: files, folderId: folderId)
            result.errors += unreadable
            if let folderId { scope = .folder(id: folderId) }
            await refreshNavigation()
            if let first = result.imported.first { open(noteId: first.id) }
            return result
        } catch {
            report(error)
            return nil
        }
    }

    /// Fetches the open note's Markdown and opens the save panel.
    public func exportOpenNote() async {
        guard let id = openNoteId else { return }
        do {
            let text = try await api.markdown(documentId: id)
            let title = openNote.map { NoteListing.displayTitle($0.title) } ?? "Note"
            pendingExport = MarkdownFile(text: text, filename: MarkdownFiles.filename(for: title))
        } catch {
            report(error)
        }
    }
}
