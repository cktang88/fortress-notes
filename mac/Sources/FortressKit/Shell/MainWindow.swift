import AppKit
import SwiftUI

/// One main window: its own `AppModel` over the shared `Library`, published to the menu
/// bar as the focused scene's model so commands act on this window.
public struct MainWindowScene: View {
    /// The main windows' scene id (`openWindow(id:value:)` opens a note in a new one).
    public static let id = "main"

    let library: Library
    let noteId: String?
    @State private var model: AppModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.undoManager) private var undoManager
    @Environment(\.appearsActive) private var appearsActive

    /// `noteId`: the note this window was opened for (Open in New Window), if any.
    public init(library: Library, noteId: String? = nil) {
        self.library = library
        self.noteId = noteId
        _model = State(initialValue: AppModel(library: library))
    }

    public var body: some View {
        MainWindow(initialNoteId: noteId)
            .environment(model)
            .focusedSceneValue(\.windowModel, model)
            .onAppear {
                model.undoManager = undoManager
                let openWindow = self.openWindow
                library.openWindow = { id in
                    if let id { openWindow(id: Self.id, value: id) } else { openWindow(id: Self.id) }
                }
                library.windowDidBecomeActive(model)
                if library.pendingNewNote {
                    library.pendingNewNote = false
                    Task { await model.newNote() }
                }
            }
            .onChange(of: undoManager) { _, manager in model.undoManager = manager }
            .onChange(of: appearsActive) { _, active in if active { library.windowDidBecomeActive(model) } }
            .onDisappear { library.windowDidClose(model) }
    }
}

/// The main window: sidebar, note list (or search results), editor (or Ask answer),
/// and an inspector. Owned by the Shell area; the columns' contents come from each area.
public struct MainWindow: View {
    @Environment(AppModel.self) private var model
    /// Per-window state restored at relaunch (small ids only, never note content).
    @SceneStorage("columns") private var storedColumns = ColumnLayout.all.rawValue
    @SceneStorage("scope") private var storedScope = ""
    @SceneStorage("note") private var storedNote = ""
    @SceneStorage("inspector") private var storedInspector = false
    @State private var visibility: NavigationSplitViewVisibility = .all
    /// The layout to return to when Focus mode ends.
    @State private var layoutBeforeFocus: NavigationSplitViewVisibility = .all
    @FocusState private var isSearchFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let initialNoteId: String?

    public init(initialNoteId: String? = nil) {
        self.initialNoteId = initialNoteId
    }

    public var body: some View {
        @Bindable var model = model
        NavigationSplitView(columnVisibility: $visibility) {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 300)
        } content: {
            ContentColumn()
                .navigationSplitViewColumnWidth(min: 260, ideal: 300, max: 420)
        } detail: {
            DetailView()
                .frame(minWidth: 480)
                .inspector(isPresented: $model.isInspectorShown) {
                    InspectorView()
                        .inspectorColumnWidth(min: 240, ideal: 280, max: 360)
                }
                .toolbar { DetailToolbar() }
                .overlay(alignment: .bottom) { ToastHost() }
        }
        .navigationTitle(model.windowTitle)
        .navigationSubtitle(model.windowSubtitle)
        .searchable(text: $model.query, placement: .toolbar, prompt: "Search notes")
        .searchFocused($isSearchFocused)
        .onChange(of: model.searchFocusRequest) { _, _ in isSearchFocused = true }
        .overlay(alignment: .top) {
            if model.isPaletteShown {
                CommandPaletteView().padding(.top, 140)
            }
        }
        .modifier(ShellPresentations())
        .frame(minWidth: 900, minHeight: 560)
        .fortressTheme()
        // The window behind the sidebar's glass takes the theme's sunken tone, so the
        // native material picks up the palette instead of being replaced.
        .containerBackground(ThemeStyle(.sunken), for: .window)
        .task {
            if model.connectsOnAppear { await model.connect() }
        }
        .modifier(PictureTextReading())
        .onAppear {
            visibility = ColumnLayout(rawValue: storedColumns)?.visibility ?? .all
            if visibility == .detailOnly { model.isFocusMode = true }
            restoreSelection()
        }
        .onChange(of: model.scope) { _, scope in storedScope = scope.storageKey }
        .onChange(of: model.openNoteId) { _, id in storedNote = id ?? "" }
        .onChange(of: model.isInspectorShown) { _, shown in storedInspector = shown }
        .onChange(of: model.hasLoadedNavigation) { _, loaded in if loaded { dropMissingSelection() } }
        .onChange(of: visibility) { _, new in
            storedColumns = ColumnLayout(new).rawValue
            if new != .detailOnly && model.isFocusMode { model.isFocusMode = false }
        }
        .onChange(of: model.isFocusMode) { _, focused in
            withAnimation(reduceMotion ? nil : .smooth(duration: 0.25)) {
                if focused, visibility != .detailOnly {
                    layoutBeforeFocus = visibility
                    visibility = .detailOnly
                } else if !focused, visibility == .detailOnly {
                    visibility = layoutBeforeFocus == .detailOnly ? .all : layoutBeforeFocus
                }
            }
        }
    }
}

extension MainWindow {
    /// Where the window was at the last quit (or the note it was opened for).
    private func restoreSelection() {
        if let scope = LibraryScope(storageKey: storedScope) { model.scope = scope }
        model.isInspectorShown = storedInspector
        if let initialNoteId {
            model.open(noteId: initialNoteId)
        } else if !storedNote.isEmpty, model.route == .empty {
            model.open(noteId: storedNote)
        }
    }

    /// A restored folder or note that no longer exists (deleted elsewhere) is let go.
    private func dropMissingSelection() {
        let items = model.navigation.items
        if case .folder(let id) = model.scope, NoteListing.folder(id: id, in: items) == nil { model.scope = .all }
        if let id = model.openNoteId, !NoteListing.allDocuments(in: items).contains(where: { $0.id == id }) {
            model.route = .empty
        }
    }
}

extension LibraryScope {
    /// The scope as a short string for `@SceneStorage`.
    var storageKey: String {
        switch self {
        case .all: "all"
        case .rough: "rough"
        case .polished: "polished"
        case .recent: "recent"
        case .trash: "trash"
        case .folder(let id): "folder:" + id
        }
    }

    init?(storageKey: String) {
        switch storageKey {
        case "all": self = .all
        case "rough": self = .rough
        case "polished": self = .polished
        case "recent": self = .recent
        case "trash": self = .trash
        default:
            guard storageKey.hasPrefix("folder:"), storageKey.count > 7 else { return nil }
            self = .folder(id: String(storageKey.dropFirst(7)))
        }
    }
}

/// `NavigationSplitViewVisibility` as a string for `@SceneStorage`.
enum ColumnLayout: String {
    case all, doubleColumn, detailOnly

    init(_ visibility: NavigationSplitViewVisibility) {
        switch visibility {
        case .doubleColumn: self = .doubleColumn
        case .detailOnly: self = .detailOnly
        default: self = .all
        }
    }

    var visibility: NavigationSplitViewVisibility {
        switch self {
        case .all: .all
        case .doubleColumn: .doubleColumn
        case .detailOnly: .detailOnly
        }
    }
}

/// Middle column: search results while searching, otherwise the scope's notes or the Trash.
struct ContentColumn: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            if !model.query.trimmingCharacters(in: .whitespaces).isEmpty {
                SearchResultsView()
            } else if model.scope == .trash {
                TrashListView()
            } else {
                NoteListView()
            }
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            if !model.isConnected && !model.navigation.items.isEmpty {
                OfflineBanner()
            }
        }
    }
}

/// Detail column: the open note, an Ask answer, a trashed note, or a quiet empty state.
struct DetailView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        if !model.isConnected && model.navigation.items.isEmpty && model.route == .empty {
            ConnectionUnavailableView()
        } else if model.scope == .trash && model.query.isEmpty {
            TrashDetailView()
        } else {
            switch model.route {
            case .empty:
                NoSelectionView()
            case .note(let id, let focusBlockId):
                NoteEditorView(documentId: id, focusBlockId: focusBlockId)
                    .id(id)
            case .ask(let question):
                AskView(question: question)
                    .id(question)
            }
        }
    }
}

/// "No note selected", centered and quiet, like Notes.
struct NoSelectionView: View {
    var body: some View {
        Text("No Note Selected")
            .font(.body)
            .foregroundStyle(.themeTertiary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(.themeBackground)
    }
}

/// Toolbar items over the editor: export, the note's overflow menu and the inspector toggle.
struct DetailToolbar: ToolbarContent {
    @Environment(AppModel.self) private var model

    var body: some ToolbarContent {
        // The whole group comes and goes (hiding only its contents leaves an empty capsule).
        if model.openNoteId != nil {
            ToolbarItemGroup(placement: .primaryAction) {
                Button {
                    Task { await model.exportOpenNote() }
                } label: {
                    Label("Export as Markdown", systemImage: "square.and.arrow.up")
                }
                .help("Export as Markdown (⇧⌘E)")

                NoteActionsMenu()
            }
        }
        ToolbarSpacer(.fixed, placement: .primaryAction)
        ToolbarItem(placement: .primaryAction) {
            Toggle(isOn: Binding(get: { model.isInspectorShown }, set: { model.isInspectorShown = $0 })) {
                Label("Inspector", systemImage: "sidebar.trailing")
            }
            .help("Show or hide the inspector (⌥⌘I)")
        }
    }
}

/// The ⋯ menu for the open note.
struct NoteActionsMenu: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Menu {
            if let id = model.openNoteId {
                MoveToFolderMenu(noteId: id)
                Button("Export as Markdown…") { Task { await model.exportOpenNote() } }
                Divider()
                Button("Move to Trash", role: .destructive) { Task { await model.trashNote(id: id) } }
            }
        } label: {
            Label("More", systemImage: "ellipsis")
        }
        .menuIndicator(.hidden)
        .help("More actions")
    }
}

/// "Move to" submenu listing every folder (nested ones indented).
struct MoveToFolderMenu: View {
    @Environment(AppModel.self) private var model
    let noteId: String

    var body: some View {
        let current = NoteListing.allDocuments(in: model.navigation.items).first { $0.id == noteId }?.folderId
        Menu("Move To") {
            Button("All Notes (No Folder)") { Task { await model.moveNote(id: noteId, toFolder: nil) } }
                .disabled(current == nil)
            Divider()
            ForEach(NoteListing.folders(in: model.navigation.items), id: \.folder.id) { entry in
                Button {
                    Task { await model.moveNote(id: noteId, toFolder: entry.folder.id) }
                } label: {
                    Text(String(repeating: "    ", count: entry.depth) + entry.folder.name)
                }
                .disabled(current == entry.folder.id)
            }
        }
    }
}
