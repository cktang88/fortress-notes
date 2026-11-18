import SwiftUI

/// Title, meta line and the block editor for one note. (Editor area — placeholder.)
struct NoteEditorView: View {
    let documentId: String
    let focusBlockId: String?

    var body: some View {
        Text(documentId).foregroundStyle(.secondary)
    }
}
