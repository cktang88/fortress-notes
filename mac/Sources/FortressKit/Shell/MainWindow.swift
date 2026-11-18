import SwiftUI

/// The main window: sidebar, note list (or search results), editor (or Ask answer),
/// and an inspector. Owned by the Shell area; the columns' contents come from each area.
public struct MainWindow: View {
    @Environment(AppModel.self) private var model
    @State private var visibility: NavigationSplitViewVisibility = .all

    public init() {}

    public var body: some View {
        @Bindable var model = model
        NavigationSplitView(columnVisibility: $visibility) {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 300)
        } content: {
            Group {
                if model.query.trimmingCharacters(in: .whitespaces).isEmpty {
                    NoteListView()
                } else {
                    SearchResultsView()
                }
            }
            .navigationSplitViewColumnWidth(min: 260, ideal: 300, max: 420)
        } detail: {
            DetailView()
                .inspector(isPresented: $model.isInspectorShown) {
                    InspectorView()
                        .inspectorColumnWidth(min: 240, ideal: 280, max: 360)
                }
        }
        .searchable(text: $model.query, placement: .toolbar, prompt: "Search notes")
        .overlay(alignment: .top) {
            if model.isPaletteShown {
                CommandPaletteView().padding(.top, 140)
            }
        }
        .task { await model.refreshNavigation() }
    }
}

/// Detail column: the open note, an Ask answer, or the empty state.
struct DetailView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        switch model.route {
        case .empty:
            ContentUnavailableView("No Note Selected", systemImage: "note.text",
                                   description: Text("Choose a note or press ⌘N to start one."))
        case .note(let id, let focusBlockId):
            NoteEditorView(documentId: id, focusBlockId: focusBlockId)
                .id(id)
        case .ask(let question):
            AskView(question: question)
                .id(question)
        }
    }
}
