import Foundation
import Observation

/// What every window shares: the backend connection, the note tree, the Trash and the
/// backups. Each main window has its own `AppModel` (selection, open note, sheets)
/// over one `Library`, so two windows never mirror each other's selection and
/// menu commands act on the window in front (`FocusedValues.windowModel`).
@MainActor
@Observable
public final class Library {
    /// Replaced when the backend address changes in Settings.
    public var api: APIClient
    public var navigation = NavigationResponse(items: [], recent: [])
    /// The last error worth telling the person about (shown as a quiet banner).
    public var notice: Notice?
    /// Whether the backend answered the last request.
    public var isConnected = true
    /// Whether the sidebar tree has loaded once (so empty states don't flash at launch).
    public var hasLoadedNavigation = false
    /// Starts and stops a local backend when it isn't already running.
    public let backend = BackendController()
    /// Notes in the Trash, loaded when the Trash is shown.
    public var trash: [TrashedDocument] = []
    public var backups: [Backup] = []
    /// The OneNote import, which keeps running when its sheet (or its window) is closed.
    public let oneNoteImport = OneNoteImporter()
    /// Reads the text in pictures and PDFs with Apple Vision (Finding/PictureText).
    public let pictureText = PictureTextReader()

    // Data layer bookkeeping (see AppModel.refreshNavigation).
    @ObservationIgnored var navigationFetches = 0
    @ObservationIgnored var newestAppliedFetch = 0
    @ObservationIgnored var rowEditSequence = 0
    @ObservationIgnored var rowEdits: [String: (sequence: Int, document: BlockDocument)] = [:]
    @ObservationIgnored var reconnectTask: Task<Void, Never>?
    @ObservationIgnored public var reconnectDelays: (first: Duration, max: Duration) = (.milliseconds(500), .seconds(30))
    @ObservationIgnored var backendRestarts = 0
    /// The first window's connection attempt; later windows wait for it instead of
    /// starting a second backend.
    @ObservationIgnored var connectTask: Task<Void, Never>?

    // MARK: Windows

    private final class WeakModel {
        weak var model: AppModel?
        init(_ model: AppModel) { self.model = model }
    }

    @ObservationIgnored private var windows: [WeakModel] = []
    /// File ▸ New Note with no window open: the window it opens makes the note.
    @ObservationIgnored public var pendingNewNote = false
    /// Opens a main window showing a note (set by the first window from its environment).
    @ObservationIgnored public var openWindow: (@MainActor (String?) -> Void)?

    public init(api: APIClient = APIClient()) {
        self.api = api
    }

    /// A main window became active: it is where app-wide actions (Quick Capture's
    /// "Save & Open", Settings' import button) land.
    func windowDidBecomeActive(_ model: AppModel) {
        windows.removeAll { $0.model == nil || $0.model === model }
        windows.append(WeakModel(model))
    }

    func windowDidClose(_ model: AppModel) {
        windows.removeAll { $0.model == nil || $0.model === model }
    }

    /// Every open main window's model.
    var windowModels: [AppModel] { windows.compactMap(\.model) }

    /// The main window most recently in front that is still open.
    public var activeWindowModel: AppModel? {
        windows.last { $0.model != nil }?.model
    }

    /// Opens a note in the most recent main window, or in a new one when none is open.
    public func openNote(_ id: String) {
        if let model = activeWindowModel {
            model.open(noteId: id)
        } else {
            openWindow?(id)
        }
    }
}
