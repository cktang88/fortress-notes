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

/// The app's shared state. Screens read it and call its methods; each feature area adds
/// its own state and actions in an extension in its own folder (Shell/, Editor/, Finding/).
@MainActor
@Observable
public final class AppModel {
    /// Replaced when the backend address changes in Settings.
    public var api: APIClient

    public var navigation = NavigationResponse(items: [], recent: [])
    public var scope: LibraryScope = .all
    public var route: Route = .empty {
        didSet { noteMayHaveOpened() }
    }
    /// Search box text; non-empty shows search results in place of the note list.
    public var query = ""
    public var isInspectorShown = false
    public var isPaletteShown = false
    /// The last error worth telling the person about (shown as a quiet banner).
    public var notice: Notice?
    /// Whether the backend answered the last request.
    public var isConnected = true

    public init(api: APIClient = APIClient()) {
        self.api = api
    }

    // MARK: Shell
    // State owned by the Shell area (window, sidebar, note list, sheets). Actions live in
    // Shell/AppModel+Shell.swift.

    /// Starts and stops a local backend when it isn't already running.
    public let backend = BackendController()
    /// The transient capsule at the bottom of the window ("Moved to Trash · Undo").
    public var toast: Toast?
    /// Notes in the Trash, loaded when the Trash is shown.
    public var trash: [TrashedDocument] = []
    /// The selected note while the Trash is shown (trashed notes don't open in the editor).
    public var selectedTrashId: String?
    public var backups: [Backup] = []
    public var isBackupsShown = false
    public var isImporterShown = false
    /// The OneNote import sheet (File ▸ Import ▸ From OneNote…) and its import, which keeps
    /// running when the sheet is closed.
    public var isOneNoteImportShown = false
    public let oneNoteImport = OneNoteImporter()
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
    /// Whether the sidebar tree has loaded once (so empty states don't flash at launch).
    public var hasLoadedNavigation = false
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

    @ObservationIgnored var navigationFetches = 0
    @ObservationIgnored var newestAppliedFetch = 0
    @ObservationIgnored var navigationFetchesInFlight = 0
    @ObservationIgnored var patchSequence = 0
    @ObservationIgnored var navigationPatches: [NavigationPatch] = []
    /// Reconnects after the backend goes away (see `startReconnecting`).
    @ObservationIgnored var reconnectTask: Task<Void, Never>?
    /// Delays between reconnection attempts (doubling up to the last).
    @ObservationIgnored public var reconnectDelays: (first: Duration, max: Duration) = (.milliseconds(500), .seconds(30))
    @ObservationIgnored var hoverPrefetchTask: Task<Void, Never>?
    /// How many times the app restarted a backend that stopped by itself.
    @ObservationIgnored var backendRestarts = 0

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
    /// Text in the ⌘K palette (set it before showing the palette to open in a mode, e.g. "? " for Ask).
    public var paletteQuery = ""
    /// The open note's blocks as last saved: block id → a version of its text. Set by
    /// `noteDidChange` (via `recordBlockVersions`); "Seen before" caches per block text.
    public internal(set) var savedBlockVersions: [String: Int] = [:]
    /// Reads the text in pictures and PDFs with Apple Vision (Finding/PictureText).
    public let pictureText = PictureTextReader()

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
