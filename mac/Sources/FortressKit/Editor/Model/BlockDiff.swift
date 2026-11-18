import Foundation

/// What the server holds for the blocks the editor shows: the tree shape and each
/// block's fields as the editor would write them.
public struct SyncedTree: Hashable, Sendable {
    public var parent: [String: String?] = [:]
    public var children: [String?: [String]] = [:]
    public var fields: [String: BlockFields] = [:]
    /// Every block in document order.
    public var order: [String] = []
    /// The editor blocks `fields` were encoded from, so the next diff re-encodes only the
    /// blocks that changed (an unchanged block shares its storage and compares in O(1)).
    var encoded: [String: EditorBlock] = [:]

    public init() {}

    /// The tree a list of editor blocks describes.
    public init(_ blocks: [EditorBlock]) {
        self.init(blocks, reusing: nil)
    }

    /// The tree for `blocks`, taking the fields of blocks unchanged since `previous` from it
    /// instead of encoding them again (encoding is the expensive part of a diff).
    /// `lazily`: fields are encoded only when a block changes (a note's baseline when it
    /// opens: encoding all 2,000 blocks of a long note up front cost more than showing it).
    init(_ blocks: [EditorBlock], reusing previous: SyncedTree?, lazily: Bool = false) {
        children[nil] = []
        parent.reserveCapacity(blocks.count)
        fields.reserveCapacity(lazily ? 0 : blocks.count)
        encoded.reserveCapacity(blocks.count)
        order.reserveCapacity(blocks.count)
        add(blocks, under: nil, reusing: previous, lazily: lazily)
    }

    /// A block's fields, encoding them now if they were left for later.
    func fields(of id: String) -> BlockFields? {
        fields[id] ?? encoded[id].map(BlockCodec.encode)
    }

    public static func == (lhs: SyncedTree, rhs: SyncedTree) -> Bool {
        lhs.order == rhs.order && lhs.parent == rhs.parent && lhs.children == rhs.children && lhs.fields == rhs.fields
    }

    public func hash(into hasher: inout Hasher) {
        hasher.combine(order)
        hasher.combine(fields)
    }

    /// Whether `block` is unchanged since `previous` (its fields then carry over).
    private func unchanged(_ block: EditorBlock, since previous: SyncedTree?) -> Bool {
        guard let previous, let old = previous.encoded[block.id] else { return false }
        return old == block
    }

    /// Adds a flat run of blocks under `root`; a columns block brings its column blocks
    /// and their blocks along (they are ordinary children on the server).
    private mutating func add(_ blocks: [EditorBlock], under root: String?, reusing previous: SyncedTree?, lazily: Bool) {
        for item in BlockDiff.shape(blocks) {
            let id = item.block.id
            let parentId = item.parent ?? root
            parent.updateValue(parentId, forKey: id)
            children[parentId, default: []].append(id)
            if children[id] == nil { children[id] = [] }
            if unchanged(item.block, since: previous) {
                // Carried over, or still left for later when it was never encoded.
                if let known = previous?.fields[id] { fields[id] = known }
            } else if !lazily {
                fields[id] = BlockCodec.encode(item.block)
            }
            encoded[id] = item.block
            order.append(id)
            guard item.block.kind == .columns, let columns = item.block.columns else { continue }
            for column in columns {
                parent.updateValue(id, forKey: column.id)
                children[id, default: []].append(column.id)
                children[column.id] = children[column.id] ?? []
                fields[column.id] = column.fields
                order.append(column.id)
                add(column.blocks, under: column.id, reusing: previous, lazily: lazily)
            }
        }
    }

    public var isEmpty: Bool { order.isEmpty }
}

/// Turns the editor's paragraphs into the block operations that bring the server's
/// tree to the same state: inserts and moves with sibling positions and parent ids,
/// updates for blocks whose fields changed, then deletes.
public enum BlockDiff {
    public struct Shaped: Hashable, Sendable {
        public var block: EditorBlock
        public var parent: String?
        public var depth: Int
    }

    /// Parents from depths. A block can be at most one level deeper than the block
    /// above it, and nothing nests under an embed (its children are its own).
    public static func shape(_ blocks: [EditorBlock]) -> [Shaped] {
        var output: [Shaped] = []
        var stack: [EditorBlock] = []  // stack[d] = latest block at depth d
        for var block in blocks {
            let maxDepth: Int
            if let previous = output.last {
                maxDepth = previous.block.kind.holdsOwnChildren ? previous.depth : previous.depth + 1
            } else {
                maxDepth = 0
            }
            let depth = max(0, min(block.depth, maxDepth))
            block.depth = depth
            stack = Array(stack.prefix(depth))
            let parent = depth > 0 ? stack[depth - 1].id : nil
            stack.append(block)
            output.append(Shaped(block: block, parent: parent, depth: depth))
        }
        return output
    }

    /// Operations from `base` to `target`, and the tree the server will then hold.
    public static func operations(from base: SyncedTree, to target: [EditorBlock]) -> (operations: [BlockOperation], result: SyncedTree) {
        let result = SyncedTree(target, reusing: base)
        var newProvenance: [String: JSONValue] = [:]
        func collect(_ blocks: [EditorBlock]) {
            for block in blocks {
                if let provenance = block.newProvenance { newProvenance[block.id] = provenance }
                for column in block.columns ?? [] { collect(column.blocks) }
            }
        }
        collect(target)
        var children = base.children
        var parent = base.parent
        if children[nil] == nil { children[nil] = [] }
        var operations: [BlockOperation] = []
        var nextIndex: [String?: Int] = [:]
        let removed = Set(base.order).subtracting(result.order)

        // Delete first what goes away with its whole subtree, so positions of the
        // blocks that stay don't need moves just to close the gaps.
        func survives(_ id: String) -> Bool {
            !removed.contains(id) || (children[id] ?? []).contains(where: survives)
        }
        func forget(_ id: String) {
            for child in children[id] ?? [] { forget(child) }
            children[id] = nil
            parent[id] = nil
        }
        for id in base.order where removed.contains(id) && !survives(id) {
            guard let currentParent = parent[id] else { continue }  // already gone with an ancestor
            children[currentParent]?.removeAll { $0 == id }
            forget(id)
            operations.append(BlockOperation(operation: .delete, blockId: id))
        }

        for id in result.order {
            let targetParent = result.parent[id] ?? nil
            let position = nextIndex[targetParent, default: 0]
            nextIndex[targetParent] = position + 1
            // No fields: unchanged since the base and never encoded, so nothing to update.
            let fields = result.fields[id]

            if let currentParent = parent[id] {
                // Siblings before `position` are the target's, already placed, so the block
                // is in place exactly when it sits at `position` (O(1), not a search).
                let siblings = children[currentParent] ?? []
                let inPlace = currentParent == targetParent && siblings.indices.contains(position) && siblings[position] == id
                if !inPlace {
                    children[currentParent]?.removeAll { $0 == id }
                    children[targetParent, default: []].insert(id, at: min(position, children[targetParent, default: []].count))
                    parent.updateValue(targetParent, forKey: id)
                    operations.append(BlockOperation(operation: .move, blockId: id, parentId: targetParent, position: position))
                }
                if let fields, base.fields(of: id) != fields {
                    operations.append(BlockOperation(
                        operation: .update, blockId: id, type: fields.type, attrs: fields.attrs,
                        content: fields.content, text: fields.text))
                }
            } else if let fields = fields ?? result.fields(of: id) {
                children[targetParent, default: []].insert(id, at: min(position, children[targetParent, default: []].count))
                children[id] = []
                parent.updateValue(targetParent, forKey: id)
                operations.append(BlockOperation(
                    operation: .insert, blockId: id, parentId: targetParent, position: position,
                    type: fields.type, attrs: fields.attrs, content: fields.content, text: fields.text,
                    provenance: newProvenance[id]))
            }
        }

        for id in base.order where removed.contains(id) && parent[id] != nil {
            // Deleting a block deletes its subtree; skip ones inside another deleted block.
            var ancestor = parent[id] ?? nil
            var insideRemoved = false
            while let current = ancestor {
                if removed.contains(current) { insideRemoved = true; break }
                ancestor = parent[current] ?? nil
            }
            if !insideRemoved {
                operations.append(BlockOperation(operation: .delete, blockId: id))
            }
        }
        return (operations, result)
    }
}
