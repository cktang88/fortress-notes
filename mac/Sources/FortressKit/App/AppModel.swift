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
    public let api: APIClient

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

    // MARK: Editor

    /// The block holding the caret in the open note (published by the editor).
    public var focusedBlockId: String?

    public init(api: APIClient = APIClient()) {
        self.api = api
    }

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
