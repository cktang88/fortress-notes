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

    /// First contact with the backend. When Settings let the app start the backend, it
    /// starts its own (with this launch's token) and only ever talks to that one, never
    /// to whatever already answers on the port; otherwise it loads the sidebar from the
    /// address in Settings (a backend run by hand).
    ///
    /// Every window calls it when it opens; only the first connects (later windows wait
    /// for that attempt, so a second window never starts a second backend).
    public func connect() async {
        if let attempt = library.connectTask {
            await attempt.value
            if scope == .trash { await refreshTrash() }
            return
        }
        let attempt = Task { await self.performConnect() }
        library.connectTask = attempt
        await attempt.value
    }

    private func performConnect() async {
        applyBackendAddress()
        backend.onUnexpectedExit = { [weak self] in
            guard let self else { return }
            self.isConnected = false
            self.startReconnecting()
        }
        if backend.startsAutomatically && backend.canStart && !Self.isRunningTests {
            notice = nil
            // Not connected until it answers: the window shows "Starting Fortress…" (or
            // why it couldn't start) instead of an empty library.
            isConnected = false
            await startBackend()
            return
        }
        await refreshNavigation()
        if isConnected {
            hasLoadedNavigation = true
            notice = nil
            if scope == .trash { await refreshTrash() }
            return
        }
        notice = nil
    }

    /// Tests open windows too; they must never launch a backend.
    static var isRunningTests: Bool {
        let name = ProcessInfo.processInfo.processName.lowercased()
        return name.contains("xctest") || name.contains("swiftpm-testing-helper")
            || ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }

    /// Points the API client at the address in Settings (keeping the session; caches
    /// belong to an address, so a new address starts with empty ones).
    public func applyBackendAddress() {
        if api.baseURL != backend.serverURL { api = APIClient(baseURL: backend.serverURL, session: api.session) }
    }

    /// Whether a healthy server answers that the app may use: the backend this launch
    /// started (its instance id), or, when it started none, whatever is at the address.
    func backendAnswers() async -> Bool {
        do {
            return backend.accepts(instance: try await api.healthInstance())
        } catch {
            return false
        }
    }

    /// Starts the local backend and waits for it to answer: as long as the process is
    /// alive, up to three minutes (the first launch loads models), checking often at
    /// first and then less often.
    public func startBackend() async {
        await startBackend(attemptsLeft: 2)
    }

    private func startBackend(attemptsLeft: Int) async {
        guard backend.start() else { return }
        // The port it serves on is chosen at start.
        applyBackendAddress()
        let started = ContinuousClock.now
        var delay = Duration.milliseconds(250)
        while ContinuousClock.now - started < .seconds(180) {
            try? await Task.sleep(for: delay)
            delay = min(delay * 2, .seconds(2))
            if case .failed = backend.state {
                // Something took the port between choosing and binding it: pick again.
                if backend.logSaysPortInUse() && attemptsLeft > 0 && backend.canStart {
                    await startBackend(attemptsLeft: attemptsLeft - 1)
                }
                return
            }
            if await backendAnswers() {
                backend.markReachable()
                await backendAnswered()
                return
            }
        }
        backend.markUnreachable("The backend didn't answer within three minutes. See the log for details.")
    }

    private func backendAnswered() async {
        isConnected = true
        await refreshNavigation()
        hasLoadedNavigation = isConnected
        notice = nil
        if scope == .trash { await refreshTrash() }
    }

    // MARK: Reconnecting

    /// The backend went away (a request couldn't connect): poll it with backoff until it
    /// answers, restarting it if this app started it and it crashed, then reload. Edits
    /// waiting to save keep retrying on their own (EditorSession) and go through once
    /// it's back.
    public func startReconnecting() {
        guard reconnectTask == nil, connectsOnAppear else { return }
        let library = self.library
        reconnectTask = Task { [weak self] in
            var delay = library.reconnectDelays.first
            while !Task.isCancelled {
                try? await Task.sleep(for: delay)
                guard !Task.isCancelled else { return }
                guard let self else {
                    // The window that started it closed: hand over to another one.
                    library.reconnectTask = nil
                    library.activeWindowModel?.startReconnecting()
                    return
                }
                if await self.backendAnswers() {
                    self.reconnectTask = nil
                    await self.backendAnswered()
                    return
                }
                if self.shouldRestartBackend {
                    self.backendRestarts += 1
                    self.reconnectTask = nil
                    await self.startBackend()
                    if !self.isConnected { self.startReconnecting() }
                    return
                }
                delay = min(delay * 2, self.reconnectDelays.max)
            }
        }
    }

    public var isReconnecting: Bool { reconnectTask != nil }

    /// A backend this app started stopped by itself: start it again (a few times).
    private var shouldRestartBackend: Bool {
        backend.exitedUnexpectedly && backend.startsAutomatically && backend.canStart
            && backendRestarts < 3 && !Self.isRunningTests
    }

    /// Tries the backend again (after it was started by hand, or the address changed).
    public func retryConnection() async {
        applyBackendAddress()
        if backend.ownsServer, !(await backendAnswers()) {
            // The backend this app started is gone (it stopped, or restarting it gave
            // up): Retry starts it again rather than doing nothing.
            if backend.canStart { await startBackend() }
            return
        }
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
        case .note: NoteListing.displayTitle(openNote?.title ?? "")  // Never the app's name (HIG).
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

    /// Lets the editor keep the sidebar and list current while a note is edited, without
    /// reloading the tree on every save: the note's row takes the save's title, snippet
    /// and time, and the note moves to the top of Recent.
    public func noteDidChange(_ document: BlockDocument) {
        func edit(_ note: inout NavigationDocument) {
            note.title = document.title
            note.updatedAt = document.updatedAt
            if let snippet = document.children.first?.text { note.snippet = snippet }
        }
        var updated = navigation
        Self.editDocument(document.id, in: &updated.items, edit)
        if let row = NoteListing.allDocuments(in: updated.items).first(where: { $0.id == document.id }) {
            updated.recent.removeAll { $0.id == document.id }
            updated.recent.insert(row, at: 0)
            if updated.recent.count > Self.recentLimit { updated.recent.removeLast(updated.recent.count - Self.recentLimit) }
        }
        if updated != navigation { navigation = updated }
        recordBlockVersions(of: document)  // Finding: "Seen before" cache key.
    }

    private static func editDocument(
        _ id: String, in items: inout [NavigationNode], _ edit: (inout NavigationDocument) -> Void
    ) {
        for index in items.indices {
            switch items[index] {
            case .document(var note) where note.id == id:
                edit(&note)
                items[index] = .document(note)
            case .folder(var folder):
                editDocument(id, in: &folder.children, edit)
                items[index] = .folder(folder)
            case .document:
                continue
            }
        }
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
        // In the order the list shows them (View ▸ Sort By), so the next note opened is
        // the one that was under it.
        let visible = NoteListing.sorted(NoteListing.notes(in: scope, navigation: navigation), by: listSort)
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
            registerUndo("Move to Trash") { model in
                await model.restore(trashedId: id, announce: false)
                model.open(noteId: id)
            } redo: { model in
                await model.trashNote(id: id)
            }
        } catch {
            report(error)
        }
        await refreshNavigation()
    }

    /// The note list's sort (View ▸ Sort By), as `NoteListView` reads it.
    var listSort: NoteSort {
        listDefaults.string(forKey: "noteSort").flatMap(NoteSort.init(rawValue:)) ?? .edited
    }

    public func moveNote(id: String, toFolder folderId: String?) async {
        let note = NoteListing.allDocuments(in: navigation.items).first { $0.id == id }
        let previous = note?.folderId
        // Dropped back on its own folder: nothing to do (the server would stamp it as
        // edited now and it would jump to the top of the list).
        if note != nil, previous == folderId { return }
        do {
            try await api.moveDocument(id: id, folderId: folderId)
            if previous != folderId {
                registerUndo("Move Note") { await $0.moveNote(id: id, toFolder: previous) } redo: {
                    await $0.moveNote(id: id, toFolder: folderId)
                }
            }
        } catch {
            report(error)
        }
        await refreshNavigation()
    }

    // MARK: Folders

    /// Creates "New Folder" (inside `parentId`) and starts renaming it in place.
    public func newFolder(in parentId: String? = nil) async {
        do {
            let folder = try await api.createFolder(name: uniqueFolderName(in: parentId), parentId: parentId)
            await refreshNavigation()
            scope = .folder(id: folder.id)
            renamingFolderId = folder.id
            let created = folder.id
            registerUndo("New Folder") { await $0.deleteFolder(id: created) } redo: { await $0.newFolder(in: parentId) }
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
        let oldName = NoteListing.folder(id: id, in: navigation.items)?.name
        guard !trimmed.isEmpty, oldName != trimmed else { return }
        do {
            _ = try await api.renameFolder(id: id, name: trimmed)
            if let oldName {
                registerUndo("Rename Folder") { await $0.renameFolder(id: id, to: oldName) } redo: {
                    await $0.renameFolder(id: id, to: trimmed)
                }
            }
        } catch {
            report(error)
        }
        await refreshNavigation()
    }

    /// Deletes a folder; its notes and subfolders move up to its parent.
    public func deleteFolder(id: String) async {
        guard let folder = NoteListing.folder(id: id, in: navigation.items) else { return }
        do {
            // One request for all the moves (was one per child).
            var documents: [(id: String, folderId: String?)] = []
            var folders: [(id: String, parentId: String?)] = []
            for child in folder.children {
                switch child {
                case .document(let note): documents.append((note.id, folder.parentId))
                case .folder(let sub): folders.append((sub.id, folder.parentId))
                }
            }
            try await api.move(documents: documents, folders: folders)
            try await api.deleteFolder(id: id)
            if case .folder(let selected) = scope, NoteListing.isFolder(selected, inside: id, items: navigation.items) {
                scope = folder.parentId.map { .folder(id: $0) } ?? .all
            }
        } catch {
            report(error)
        }
        await refreshNavigation()
    }

    /// Moves a folder (with everything in it) into another folder, or to the top level.
    /// Refused when the destination is the folder itself or inside it.
    public func moveFolder(id: String, toParent parentId: String?) async {
        guard let folder = NoteListing.folder(id: id, in: navigation.items), folder.parentId != parentId,
              Self.canMoveFolder(id, into: parentId, items: navigation.items) else { return }
        let previous = folder.parentId
        do {
            try await api.moveFolder(id: id, parentId: parentId)
            registerUndo("Move Folder") { await $0.moveFolder(id: id, toParent: previous) } redo: {
                await $0.moveFolder(id: id, toParent: parentId)
            }
            await refreshNavigation()
        } catch {
            report(error)
            await refreshNavigation()
        }
    }

    /// A folder can't go into itself or one of its own subfolders.
    static func canMoveFolder(_ id: String, into parentId: String?, items: [NavigationNode]) -> Bool {
        guard let parentId else { return true }
        return parentId != id && !NoteListing.isFolder(parentId, inside: id, items: items)
    }

    // MARK: Undo

    /// Registers an undoable library edit on the window's undo manager (Edit ▸ Undo
    /// "Move to Trash", ⌘Z in the list). The inverse is registered at once (so it lands on
    /// the redo stack), then the work runs without registering again.
    func registerUndo(
        _ name: String,
        undo: @escaping @MainActor @Sendable (AppModel) async -> Void,
        redo: @escaping @MainActor @Sendable (AppModel) async -> Void
    ) {
        guard let undoManager, undoSuppression == 0 else { return }
        undoManager.registerUndo(withTarget: self) { model in
            MainActor.assumeIsolated {
                model.undoSuppression += 1
                model.registerUndoUnsuppressed(name, undo: redo, redo: undo)
                Task {
                    await undo(model)
                    model.undoSuppression -= 1
                }
            }
        }
        undoManager.setActionName(name)
    }

    private func registerUndoUnsuppressed(
        _ name: String,
        undo: @escaping @MainActor @Sendable (AppModel) async -> Void,
        redo: @escaping @MainActor @Sendable (AppModel) async -> Void
    ) {
        let suppression = undoSuppression
        undoSuppression = 0
        registerUndo(name, undo: undo, redo: redo)
        undoSuppression = suppression
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
            registerUndo("Restore") { await $0.trashNote(id: id) } redo: { await $0.restore(trashedId: id) }
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
            // The same as Edit ▸ Undo Move to Trash, so the undo stack stays in step.
            if let undoManager, undoManager.canUndo, undoManager.undoActionName == "Move to Trash" {
                undoManager.undo()
                return
            }
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
            // Every window lets go of the note it showed (restored from Settings, the
            // windows would otherwise keep showing the notes as they were before).
            for window in library.windowModels + [self] { window.route = .empty }
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
