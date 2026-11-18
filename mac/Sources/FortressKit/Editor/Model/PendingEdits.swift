import Foundation

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

    /// Where records live; tests point it elsewhere.
    nonisolated(unsafe) public static var directory: URL = {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appending(path: "FortressNotes/PendingEdits", directoryHint: .isDirectory)
    }()

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
        let root = Node(BlockNode(id: "", documentId: "", parentId: nil, position: 0, type: "", attrs: [:], userAttrs: [:],
                                  content: .object([:]), text: "", createdAt: "", updatedAt: "", children: nodes))
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
        for operation in operations {
            guard let id = operation.blockId else { continue }
            let found = byId[id]
            switch operation.operation {
            case .insert:
                guard found == nil, let parent = list(operation.parentId) else { continue }
                let node = Node(BlockNode(
                    id: id, documentId: "", parentId: operation.parentId, position: 0, type: operation.type ?? "paragraph",
                    attrs: operation.attrs ?? [:], userAttrs: [:], content: operation.content ?? .object([:]),
                    text: operation.text ?? "", createdAt: "", updatedAt: "", children: [], provenance: operation.provenance))
                parent.children.insert(node, at: min(max(0, operation.position ?? parent.children.count), parent.children.count))
                parentOf[id] = parent
                byId[id] = node
            case .update:
                guard let node = found else { continue }
                if let type = operation.type { node.block.type = type }
                if let attrs = operation.attrs { node.block.attrs = attrs }
                if let content = operation.content { node.block.content = content }
                if let text = operation.text { node.block.text = text }
            case .setAttrs:
                found?.block.attrs = operation.attrs ?? [:]
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
                guard let node = found, let from = parentOf[id], let to = list(operation.parentId), !isInside(node, to) else { continue }
                from.children.removeAll { $0 === node }
                to.children.insert(node, at: min(max(0, operation.position ?? to.children.count), to.children.count))
                node.block.parentId = operation.parentId
                parentOf[id] = to
            default:
                continue
            }
        }
        return root.children.map(\.tree)
    }
}
