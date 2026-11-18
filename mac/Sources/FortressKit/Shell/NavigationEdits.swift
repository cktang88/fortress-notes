import Foundation

/// Edits to the sidebar tree made from a mutation's answer, so a change doesn't reload
/// the whole tree (it used to: every new note, move, trash and folder change refetched
/// `/api/navigation`). They mirror what the server does, so the next real reload agrees.
public enum NavigationEdits {
    /// Changes the note with this id wherever it is in the tree.
    public static func updateDocument(
        _ id: String, in items: inout [NavigationNode], _ update: (inout NavigationDocument) -> Void
    ) {
        for index in items.indices {
            switch items[index] {
            case .document(var note) where note.id == id:
                update(&note)
                items[index] = .document(note)
                return
            case .folder(var folder):
                guard contains(id, in: folder.children) else { continue }
                updateDocument(id, in: &folder.children, update)
                items[index] = .folder(folder)
                return
            case .document:
                continue
            }
        }
    }

    /// Takes the note out of the tree; the server closes the gap in its folder's order.
    @discardableResult
    public static func removeDocument(_ id: String, from items: inout [NavigationNode]) -> NavigationDocument? {
        for index in items.indices {
            switch items[index] {
            case .document(let note) where note.id == id:
                items.remove(at: index)
                renumberDocuments(in: &items)
                return note
            case .folder(var folder):
                guard contains(id, in: folder.children) else { continue }
                let removed = removeDocument(id, from: &folder.children)
                items[index] = .folder(folder)
                return removed
            case .document:
                continue
            }
        }
        return nil
    }

    /// Adds a note at the end of its folder (top level when the folder isn't there).
    public static func insertDocument(_ note: NavigationDocument, in items: inout [NavigationNode]) {
        var note = note
        if let folderId = note.folderId, modifyFolder(folderId, in: &items, { folder in
            note.position = documentCount(folder.children)
            folder.children.append(.document(note))
        }) {
            return
        }
        note.folderId = nil
        note.position = documentCount(items)
        items.append(.document(note))
    }

    /// Adds a folder after its siblings' subfolders (folders list before notes).
    public static func insertFolder(_ folder: NavigationFolder, in items: inout [NavigationNode]) {
        func insert(into children: inout [NavigationNode]) {
            let lastFolder = children.lastIndex { if case .folder = $0 { true } else { false } }
            children.insert(.folder(folder), at: lastFolder.map { $0 + 1 } ?? 0)
        }
        if let parentId = folder.parentId, modifyFolder(parentId, in: &items, { insert(into: &$0.children) }) {
            return
        }
        insert(into: &items)
    }

    public static func updateFolder(_ id: String, in items: inout [NavigationNode], _ update: (inout NavigationFolder) -> Void) {
        modifyFolder(id, in: &items, update)
    }

    /// Removes a folder and moves what it held up to its parent; the moved notes are
    /// stamped `stamp` (the server marks a moved note edited). Returns the moved notes.
    @discardableResult
    public static func dissolveFolder(_ id: String, stamp: String, in items: inout [NavigationNode]) -> [NavigationDocument] {
        guard let folder = NoteListing.folder(id: id, in: items) else { return [] }
        removeFolder(id, from: &items)
        var moved: [NavigationDocument] = []
        for child in folder.children {
            switch child {
            case .folder(var sub):
                sub.parentId = folder.parentId
                insertFolder(sub, in: &items)
            case .document(var note):
                note.folderId = folder.parentId
                note.updatedAt = max(note.updatedAt, stamp)
                insertDocument(note, in: &items)
                moved.append(note)
            }
        }
        return moved
    }

    /// Puts an edited (or new) note where Recent would list it: newest first, at most `limit`.
    public static func touchRecent(_ note: NavigationDocument, in recent: inout [NavigationDocument], limit: Int) {
        recent.removeAll { $0.id == note.id }
        recent.append(note)
        // The server's order: updated_at DESC, id ASC.
        recent.sort { $0.updatedAt != $1.updatedAt ? $0.updatedAt > $1.updatedAt : $0.id < $1.id }
        if recent.count > limit { recent.removeLast(recent.count - limit) }
    }

    // MARK: Helpers

    static func contains(_ documentId: String, in items: [NavigationNode]) -> Bool {
        items.contains { node in
            switch node {
            case .document(let note): note.id == documentId
            case .folder(let folder): contains(documentId, in: folder.children)
            }
        }
    }

    static func documentCount(_ items: [NavigationNode]) -> Int {
        items.reduce(0) { count, node in if case .document = node { count + 1 } else { count } }
    }

    private static func renumberDocuments(in items: inout [NavigationNode]) {
        var position = 0
        for index in items.indices {
            guard case .document(var note) = items[index] else { continue }
            note.position = position
            items[index] = .document(note)
            position += 1
        }
    }

    @discardableResult
    private static func modifyFolder(
        _ id: String, in items: inout [NavigationNode], _ update: (inout NavigationFolder) -> Void
    ) -> Bool {
        for index in items.indices {
            guard case .folder(var folder) = items[index] else { continue }
            if folder.id == id {
                update(&folder)
                items[index] = .folder(folder)
                return true
            }
            if modifyFolder(id, in: &folder.children, update) {
                items[index] = .folder(folder)
                return true
            }
        }
        return false
    }

    @discardableResult
    private static func removeFolder(_ id: String, from items: inout [NavigationNode]) -> Bool {
        for index in items.indices {
            guard case .folder(var folder) = items[index] else { continue }
            if folder.id == id {
                items.remove(at: index)
                return true
            }
            if removeFolder(id, from: &folder.children) {
                items[index] = .folder(folder)
                return true
            }
        }
        return false
    }
}
