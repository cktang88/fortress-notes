import Foundation
import Observation

/// What the detail column shows.
public enum Route: Hashable, Sendable {
    case empty
    case note(id: String, focusBlockId: String?)
    case ask(question: String)
}

/// Sidebar selection that filters the note list.
public enum LibraryScope: Hashable, Sendable {
    case all, rough, polished, recent, trash
    case folder(id: String)
}

/// One main window's state (selection, open note, sheets, palette) over the `Library`
/// all windows share. Screens read it and call its methods; each feature area adds its
/// own state and actions in an extension in its own folder (Shell/, Editor/, Finding/).
@MainActor
@Observable
public final class AppModel {
    /// The state every window shares.
    public let library: Library

    // Shared state, kept in the library (forwarded so screens and actions read one model).
    public var api: APIClient { get { library.api } set { library.api = newValue } }
    public var navigation: NavigationResponse { get { library.navigation } set { library.navigation = newValue } }
    public var notice: Notice? { get { library.notice } set { library.notice = newValue } }
    public var isConnected: Bool { get { library.isConnected } set { library.isConnected = newValue } }
    public var hasLoadedNavigation: Bool { get { library.hasLoadedNavigation } set { library.hasLoadedNavigation = newValue } }
    public var backend: BackendController { library.backend }
    public var trash: [TrashedDocument] { get { library.trash } set { library.trash = newValue } }
    public var backups: [Backup] { get { library.backups } set { library.backups = newValue } }
    public var oneNoteImport: OneNoteImporter { library.oneNoteImport }
    public var pictureText: PictureTextReader { library.pictureText }

    /// The window's undo manager (set by the window), for undoable library edits.
    @ObservationIgnored public weak var undoManager: UndoManager?
    /// Above zero while an undo or redo runs (its work must not register again).
    @ObservationIgnored var undoSuppression = 0

    public var scope: LibraryScope = .all
    public var route: Route = .empty {
        didSet { noteMayHaveOpened() }
    }
    /// Search box text; non-empty shows search results in place of the note list.
    public var query = ""
    public var isInspectorShown = false
    public var isPaletteShown = false
    /// Bumped by Edit ▸ Search All Notes (⌥⌘F) to put the caret in the window's search field.
    public var searchFocusRequest = 0
    /// Text waiting for Ask's question field (the search text when Ask was opened); the
    /// Ask view takes it when it appears or when `askFocusRequest` changes.
    public var askDraft = ""
    /// Bumped by the toolbar's Ask button and View ▸ Ask Your Notes… (⇧⌘A) to put the
    /// caret in Ask's question field.
    public var askFocusRequest = 0
    /// The Empty Trash confirmation (Trash toolbar or File ▸ Empty Trash…).
    public var isEmptyTrashConfirmationShown = false

    /// A window over a new library of its own (tests, previews).
    public convenience init(api: APIClient = APIClient()) {
        self.init(library: Library(api: api))
    }

    /// Another window over a shared library.
    public init(library: Library) {
        self.library = library
    }

    // MARK: Shell
    // State owned by the Shell area (window, sidebar, note list, sheets). Actions live in
    // Shell/AppModel+Shell.swift.

    /// The transient capsule at the bottom of the window ("Moved to Trash · Undo").
    public var toast: Toast?
    /// The selected note while the Trash is shown (trashed notes don't open in the editor).
    public var selectedTrashId: String?
    public var isBackupsShown = false
    public var isImporterShown = false
    /// The OneNote import sheet (File ▸ Import ▸ From OneNote…); the import itself is the
    /// library's and keeps running when the sheet is closed.
    public var isOneNoteImportShown = false
    /// Markdown files waiting for the import sheet, with the folder they go into.
    public var pendingImport: MarkdownImportRequest?
    /// A note's Markdown ready for the save panel.
    public var pendingExport: MarkdownFile?
    /// The folder whose name is being edited inline in the sidebar.
    public var renamingFolderId: String?
    /// Hides the sidebar and note list so only the note shows.
    public var isFocusMode = false
    /// Bumped when the note list hands keyboard focus to the editor (↩ on a row).
    /// The editor can watch it with `.onChange` and take focus.
    public var editorFocusRequest = 0
    /// Set false (snapshots, previews) to skip contacting the backend when a window opens.
    public var connectsOnAppear = true

    /// The note open in the editor, if any.
    public var openNoteId: String? {
        if case .note(let id, _) = route { return id }
        return nil
    }

    public func open(noteId: String, blockId: String? = nil) {
        route = .note(id: noteId, focusBlockId: blockId)
    }

    /// Records "last opened" once a note has stayed open for a moment (skimming the list
    /// opens and closes notes every few milliseconds). Reading a note never writes on the
    /// server, so this is its own fire-and-forget request.
    @ObservationIgnored var openedMarkTask: Task<Void, Never>?
    @ObservationIgnored var lastMarkedOpenId: String?
    /// How long a note stays open before it counts as opened.
    @ObservationIgnored public var openedDwell: Duration = .seconds(1)

    private func noteMayHaveOpened() {
        guard let id = openNoteId else {
            openedMarkTask?.cancel()
            lastMarkedOpenId = nil
            return
        }
        guard id != lastMarkedOpenId else { return }  // a jump within the same note
        openedMarkTask?.cancel()
        lastMarkedOpenId = id
        guard connectsOnAppear else { return }  // previews, snapshots, most tests
        let api = api
        let dwell = openedDwell
        openedMarkTask = Task { [weak self] in
            try? await Task.sleep(for: dwell)
            guard !Task.isCancelled, self?.openNoteId == id else { return }
            await api.markOpened(id: id)
        }
    }

    // MARK: Navigation (data layer)

    /// How many notes Recent holds (the size asked of the server).
    public static let recentLimit = 20

    /// A local change to the tree, re-applied over any fetch that started before it.
    struct NavigationPatch {
        var sequence: Int
        var apply: (inout NavigationResponse) -> Void
    }

    var navigationFetches: Int { get { library.navigationFetches } set { library.navigationFetches = newValue } }
    var newestAppliedFetch: Int { get { library.newestAppliedFetch } set { library.newestAppliedFetch = newValue } }
    var navigationFetchesInFlight: Int {
        get { library.navigationFetchesInFlight } set { library.navigationFetchesInFlight = newValue }
    }
    var patchSequence: Int { get { library.patchSequence } set { library.patchSequence = newValue } }
    var navigationPatches: [NavigationPatch] { get { library.navigationPatches } set { library.navigationPatches = newValue } }
    /// Reconnects after the backend goes away (see `startReconnecting`); one for all windows.
    var reconnectTask: Task<Void, Never>? { get { library.reconnectTask } set { library.reconnectTask = newValue } }
    /// Delays between reconnection attempts (doubling up to the last).
    public var reconnectDelays: (first: Duration, max: Duration) {
        get { library.reconnectDelays } set { library.reconnectDelays = newValue }
    }
    @ObservationIgnored var hoverPrefetchTask: Task<Void, Never>?
    /// How many times the app restarted a backend that stopped by itself.
    var backendRestarts: Int { get { library.backendRestarts } set { library.backendRestarts = newValue } }

    /// Reloads the sidebar tree and recent notes.
    ///
    /// Safe to call often: concurrent calls share one request, an answer that arrives
    /// after a newer one is dropped, and local changes made while it was in flight (an
    /// edit, a move) are re-applied over it instead of being overwritten.
    public func refreshNavigation() async {
        navigationFetches += 1
        let fetch = navigationFetches
        let mark = patchSequence
        navigationFetchesInFlight += 1
        defer {
            navigationFetchesInFlight -= 1
            if navigationFetchesInFlight == 0 { navigationPatches.removeAll() }
        }
        do {
            var fresh = try await api.navigation(recentLimit: Self.recentLimit)
            isConnected = true
            guard fetch > newestAppliedFetch else { return }
            newestAppliedFetch = fetch
            for patch in navigationPatches where patch.sequence > mark { patch.apply(&fresh) }
            navigationPatches.removeAll { $0.sequence <= mark }
            if fresh != navigation { navigation = fresh }
            api.store.reconcile(with: fresh)
        } catch {
            if fetch == navigationFetches { report(error) }
        }
    }

    /// Applies a change to the tree now, and keeps it so a fetch already in flight
    /// doesn't undo it.
    /// `replay` (default: `apply`) is what runs over a fetched tree that started before
    /// the change; it may skip what the fetched tree already has newer.
    func patchNavigation(
        _ apply: @escaping (inout NavigationResponse) -> Void,
        replay: ((inout NavigationResponse) -> Void)? = nil
    ) {
        patchSequence += 1
        var updated = navigation
        apply(&updated)
        if updated != navigation { navigation = updated }
        if navigationFetchesInFlight > 0 {
            navigationPatches.append(NavigationPatch(sequence: patchSequence, apply: replay ?? apply))
        }
    }

    // MARK: Prefetching

    /// Loads these notes ahead of time (cheap: one request, skipped when cached).
    public func prefetch(noteIds: [String]) async {
        guard isConnected, !noteIds.isEmpty else { return }
        await api.prefetchDocuments(ids: noteIds)
    }

    /// The pointer rests on a note in the list: load it after a short pause, unless the
    /// pointer moves on first.
    public func prefetchOnHover(noteId: String?) {
        hoverPrefetchTask?.cancel()
        guard let noteId, noteId != openNoteId else { return }
        hoverPrefetchTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(120))
            guard !Task.isCancelled, let self else { return }
            await self.prefetch(noteIds: [noteId])
        }
    }

    // MARK: Finding

    /// The block the caret is in, set by the editor; drives "Seen before" in the inspector.
    public var focusedBlockId: String?
    /// Text in the palette (Open Quickly…, ⇧⌘O) (set it before showing the palette to open in a mode, e.g. "? " for Ask).
    public var paletteQuery = ""
    /// The open note's blocks as last saved: block id → a version of its text. Set by
    /// `noteDidChange` (via `recordBlockVersions`); "Seen before" caches per block text.
    public internal(set) var savedBlockVersions: [String: Int] = [:]

    // MARK: -

    /// Tells the person about a failure, calmly. Cancelled work (a note closed while it
    /// loaded) is not a failure; losing the backend shows the offline banner and starts
    /// reconnecting instead of a notice per request.
    public func report(_ error: Error) {
        if Self.isCancellation(error) { return }
        if let urlError = error as? URLError, Self.isConnectionLoss(urlError) {
            isConnected = false
            startReconnecting()
            return
        }
        if error is URLError { isConnected = false }
        let message = Self.describe(error)
        if notice?.message != message { notice = Notice(message: message) }
    }

    static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let urlError = error as? URLError, urlError.code == .cancelled { return true }
        return false
    }

    static func isConnectionLoss(_ error: URLError) -> Bool {
        switch error.code {
        case .cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet, .timedOut,
             .cannotFindHost, .dnsLookupFailed, .secureConnectionFailed:
            true
        default:
            false
        }
    }

    /// A sentence for a notice.
    public static func describe(_ error: Error) -> String {
        if let error = error as? APIError { return error.message }
        if let error = error as? URLError {
            if isConnectionLoss(error) { return "Fortress's local server isn't responding." }
            if error.code == .badServerResponse { return "Fortress's server sent a reply the app couldn't read." }
        }
        if error is DecodingError { return APIError.malformed.message }
        return error.localizedDescription
    }
}

public struct Notice: Identifiable, Hashable, Sendable {
    public let id = UUID()
    public var message: String
    public init(message: String) { self.message = message }
}
