import SwiftUI

/// Notes in the selected scope. (Shell area — placeholder to be built out.)
struct NoteListView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        List(model.navigation.recent) { note in
            Button(note.title.isEmpty ? "Untitled" : note.title) { model.open(noteId: note.id) }
                .buttonStyle(.plain)
        }
    }
}
