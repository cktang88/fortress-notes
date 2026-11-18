import AppKit
import SwiftUI

extension Color {
    /// "Fortress Ember", the app's one accent (DESIGN a.4), with dark and increased-contrast variants.
    public static let fortressEmber = Color(nsColor: NSColor(name: "FortressEmber") { appearance in
        let match = appearance.bestMatch(from: [.aqua, .darkAqua, .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua])
        switch match {
        case .darkAqua: return NSColor(srgbRed: 0xF0 / 255, green: 0x91 / 255, blue: 0x5A / 255, alpha: 1)
        case .accessibilityHighContrastDarkAqua: return NSColor(srgbRed: 1, green: 0xB0 / 255, blue: 0x85 / 255, alpha: 1)
        case .accessibilityHighContrastAqua: return NSColor(srgbRed: 0x8F / 255, green: 0x3D / 255, blue: 0x1A / 255, alpha: 1)
        default: return NSColor(srgbRed: 0xB4 / 255, green: 0x53 / 255, blue: 0x2A / 255, alpha: 1)
        }
    })
}

/// The main window: sidebar, note list (or search results), editor (or Ask answer),
/// and an inspector. Owned by the Shell area; the columns' contents come from each area.
public struct MainWindow: View {
    @Environment(AppModel.self) private var model
    /// Column layout, kept per window across launches.
    @SceneStorage("columns") private var storedColumns = ColumnLayout.all.rawValue
    @State private var visibility: NavigationSplitViewVisibility = .all
    /// The layout to return to when Focus mode ends.
    @State private var layoutBeforeFocus: NavigationSplitViewVisibility = .all

    public init() {}

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
        }
        .navigationTitle(model.windowTitle)
        .navigationSubtitle(model.windowSubtitle)
        .searchable(text: $model.query, placement: .toolbar, prompt: "Search notes")
        .overlay(alignment: .top) {
            if model.isPaletteShown {
                CommandPaletteView().padding(.top, 140)
            }
        }
        .overlay(alignment: .bottom) { ToastHost() }
        .modifier(ShellPresentations())
        .frame(minWidth: 900, minHeight: 560)
        .tint(.fortressEmber)
        .task {
            if model.connectsOnAppear { await model.connect() }
        }
        .onAppear {
            visibility = ColumnLayout(rawValue: storedColumns)?.visibility ?? .all
            if visibility == .detailOnly { model.isFocusMode = true }
        }
        .onChange(of: visibility) { _, new in
            storedColumns = ColumnLayout(new).rawValue
            if new != .detailOnly && model.isFocusMode { model.isFocusMode = false }
        }
        .onChange(of: model.isFocusMode) { _, focused in
            withAnimation(.smooth(duration: 0.25)) {
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
            .foregroundStyle(.tertiary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(nsColor: .textBackgroundColor))
    }
}

/// Toolbar items over the editor: export, the note's overflow menu and the inspector toggle.
struct DetailToolbar: ToolbarContent {
    @Environment(AppModel.self) private var model

    var body: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            if model.openNoteId != nil {
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
