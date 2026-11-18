import Foundation
import Synchronization

/// Edits not yet confirmed by the server, kept on disk (Application Support) so quitting,
/// a crash or a lost connection never loses them. The native counterpart of the web
/// editor's localStorage queue: written before each save is sent, removed once the server
/// has everything, and replayed onto the note the next time it opens.
public enum PendingEdits {
    public struct Record: Codable, Hashable, Sendable {
        public var documentId: String
        /// The revision the operations were computed against.
        public var baseRevision: Int
        public var operations: [BlockOperation]
        public var savedAt: Date

        enum CodingKeys: String, CodingKey {
            case documentId = "document_id"
            case baseRevision = "base_revision"
            case operations
            case savedAt = "saved_at"
        }
    }

    /// Where records live; tests point it elsewhere. (Read from the main actor and from
    /// the write queue, so it sits behind a lock rather than being `nonisolated(unsafe)`.)
    public static var directory: URL {
        get { storedDirectory.withLock { $0 } }
        set { storedDirectory.withLock { $0 = newValue } }
    }

    private static let storedDirectory = Mutex<URL>({
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appending(path: "FortressNotes/PendingEdits", directoryHint: .isDirectory)
    }())

    /// Writes happen in order, off the main thread.
    private static let queue = DispatchQueue(label: "app.fortress.notes.pending-edits", qos: .utility)

    static func url(for documentId: String) -> URL {
        let safe = documentId.replacingOccurrences(of: "/", with: "_")
        return directory.appending(path: safe + ".json")
    }

    /// Saves the edits for a note (in the background unless `synchronously`).
    static func save(_ record: Record, synchronously: Bool = false) {
        guard let data = try? JSONEncoder().encode(record) else { return }
        let url = url(for: record.documentId)
        let directory = directory
        let write: @Sendable () -> Void = {
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try? data.write(to: url, options: .atomic)
        }
        if synchronously { queue.sync(execute: write) } else { queue.async(execute: write) }
    }

    static func remove(documentId: String, synchronously: Bool = false) {
        let url = url(for: documentId)
        let remove: @Sendable () -> Void = { try? FileManager.default.removeItem(at: url) }
        if synchronously { queue.sync(execute: remove) } else { queue.async(execute: remove) }
    }

    /// The edits saved for a note, if any (waits for writes still queued).
    static func load(documentId: String) -> Record? {
        let url = url(for: documentId)
        return queue.sync {
            guard let data = try? Data(contentsOf: url) else { return nil }
            return try? JSONDecoder().decode(Record.self, from: data)
        }
    }

    // MARK: Replaying

    private final class Node {
        var block: BlockNode
        var children: [Node]
        init(_ block: BlockNode) {
            self.block = block
            children = block.children.map(Node.init)
        }
        var tree: BlockNode {
            var copy = block
            copy.children = children.map(\.tree)
            return copy
        }
    }

    /// The note as it will be once `operations` are saved, so a note reopened before
    /// they reach the server shows them. Like the web editor's recovery, operations the
    /// server may already have applied are skipped rather than repeated.
    public static func apply(_ operations: [BlockOperation], to nodes: [BlockNode]) -> [BlockNode] {
        let root = Node(BlockNode(id: "", type: "", children: nodes))
        var parentOf: [String: Node] = [:]
        var byId: [String: Node] = [:]
        func index(_ node: Node) {
            for child in node.children {
                parentOf[child.block.id] = node
                byId[child.block.id] = child
                index(child)
            }
        }
        index(root)
        func list(_ parentId: String?) -> Node? { parentId.map { byId[$0] } ?? root }
        func isInside(_ node: Node, _ candidate: Node?) -> Bool {
            var current = candidate
            while let check = current {
                if check === node { return true }
                current = parentOf[check.block.id]
            }
            return false
        }
        /// Where an insert or move puts the block: after `after_id`, before `before_id`,
        /// or at the end of `parent_id`'s children.
        func placement(_ operation: BlockOperation, skipping node: Node? = nil) -> (Node, Int)? {
            if let anchorId = operation.afterId ?? operation.beforeId {
                guard let anchor = byId[anchorId], anchor !== node, let parent = parentOf[anchorId],
                      let at = parent.children.firstIndex(where: { $0 === anchor }) else { return nil }
                return (parent, operation.afterId != nil ? at + 1 : at)
            }
            guard let parent = list(operation.parentId) else { return nil }
            return (parent, parent.children.count)
        }
        for operation in operations {
            guard let id = operation.blockId else { continue }
            let found = byId[id]
            switch operation.operation {
            case .insert:
                guard found == nil, let (parent, at) = placement(operation) else { continue }
                let node = Node(BlockNode(
                    id: id, parentId: parent === root ? nil : parent.block.id, type: operation.type ?? "paragraph",
                    attrs: (operation.attrs ?? [:]).filter { $0.value != .null }, content: operation.content ?? .null,
                    payload: operation.payload, provenance: operation.provenance))
                parent.children.insert(node, at: min(max(0, at), parent.children.count))
                parentOf[id] = parent
                byId[id] = node
            case .update:
                guard let node = found else { continue }
                if let type = operation.type { node.block.type = type }
                for (key, value) in operation.attrs ?? [:] { node.block.attrs[key] = value == .null ? nil : value }
                if let content = operation.content { node.block.content = content }
                if let payload = operation.payload { node.block.payload = payload == .null ? nil : payload }
            case .delete:
                guard let node = found, let parent = parentOf[id] else { continue }
                parent.children.removeAll { $0 === node }
                func forget(_ node: Node) {
                    byId[node.block.id] = nil
                    parentOf[node.block.id] = nil
                    node.children.forEach(forget)
                }
                forget(node)
            case .move:
                guard let node = found, let from = parentOf[id] else { continue }
                from.children.removeAll { $0 === node }
                guard let (to, at) = placement(operation, skipping: node), !isInside(node, to) else {
                    from.children.append(node)
                    continue
                }
                to.children.insert(node, at: min(max(0, at), to.children.count))
                node.block.parentId = to === root ? nil : to.block.id
                parentOf[id] = to
            }
        }
        return root.children.map(\.tree)
    }
}
