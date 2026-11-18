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
    public var route: Route = .empty
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

    /// Reloads the sidebar tree and recent notes.
    public func refreshNavigation() async {
        do {
            navigation = try await api.navigation(recentLimit: 20)
            isConnected = true
        } catch {
            report(error)
        }
    }

    // MARK: Finding

    /// The block the caret is in, set by the editor; drives "Seen before" in the inspector.
    public var focusedBlockId: String?
    /// Text in the ⌘K palette (set it before showing the palette to open in a mode, e.g. "? " for Ask).
    public var paletteQuery = ""

    // MARK: -

    public func report(_ error: Error) {
        if error is URLError { isConnected = false }
        notice = Notice(message: error.localizedDescription)
    }
}

public struct Notice: Identifiable, Hashable, Sendable {
    public let id = UUID()
    public var message: String
    public init(message: String) { self.message = message }
}
