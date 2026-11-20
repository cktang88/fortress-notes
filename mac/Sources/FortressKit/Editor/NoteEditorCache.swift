import Foundation

/// The notes a window opened lately: each one's session and editor (its text view, and
/// with it the note's undo stack), so going back to a note finds ⌘Z where it was, as
/// switching documents does in other apps. The editor is put back as it was unless the
/// note changed elsewhere meanwhile (then it shows the new version and its history
/// starts over). Kept per window: the same note in two windows has two editors.
@MainActor
final class NoteEditorCache {
    struct Entry {
        let session: EditorSession
        let controller: BlockEditorController
        /// The session's content generation the editor shows.
        var generation: Int
    }

    /// How many notes keep their editor (the least recently shown goes first).
    var limit = 8
    /// Most recently shown last.
    private(set) var entries: [String: Entry] = [:]
    private var order: [String] = []

    func session(for documentId: String) -> EditorSession? {
        entries[documentId]?.session
    }

    /// The editor kept for this session, if it still shows the session's content.
    func controller(for session: EditorSession) -> BlockEditorController? {
        guard let entry = entries[session.documentId], entry.session === session,
              entry.generation == session.contentGeneration else { return nil }
        return entry.controller
    }

    /// Keeps (or refreshes) a note's editor, showing the session's current content.
    func keep(_ session: EditorSession, _ controller: BlockEditorController) {
        let id = session.documentId
        entries[id] = Entry(session: session, controller: controller, generation: session.contentGeneration)
        order.removeAll { $0 == id }
        order.append(id)
        while order.count > limit {
            entries[order.removeFirst()] = nil
        }
    }

    func forget(_ documentId: String) {
        entries[documentId] = nil
        order.removeAll { $0 == documentId }
    }
}
