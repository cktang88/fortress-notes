import Foundation

/// What the server holds for the blocks the editor shows: the tree shape and each
/// block's fields as the editor would write them.
public struct SyncedTree: Hashable, Sendable {
    public var parent: [String: String?] = [:]
    public var children: [String?: [String]] = [:]
    public var fields: [String: BlockFields] = [:]
    /// Every block in document order.
    public var order: [String] = []

    public init() {}

    /// The tree a list of editor blocks describes.
    public init(_ blocks: [EditorBlock]) {
        let shaped = BlockDiff.shape(blocks)
        children[nil] = []
        for item in shaped {
            parent.updateValue(item.parent, forKey: item.block.id)
            children[item.parent, default: []].append(item.block.id)
            children[item.block.id] = children[item.block.id] ?? []
            fields[item.block.id] = BlockCodec.encode(item.block)
            order.append(item.block.id)
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
                maxDepth = previous.block.kind == .embed ? previous.depth : previous.depth + 1
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
        let result = SyncedTree(target)
        var newProvenance: [String: JSONValue] = [:]
        for block in target { if let provenance = block.newProvenance { newProvenance[block.id] = provenance } }
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
            let fields = result.fields[id]!

            if let currentParent = parent[id] {
                let siblings = children[currentParent] ?? []
                if currentParent != targetParent || siblings.firstIndex(of: id) != position {
                    children[currentParent]?.removeAll { $0 == id }
                    children[targetParent, default: []].insert(id, at: min(position, children[targetParent, default: []].count))
                    parent.updateValue(targetParent, forKey: id)
                    operations.append(BlockOperation(operation: .move, blockId: id, parentId: targetParent, position: position))
                }
                if base.fields[id] != fields {
                    operations.append(BlockOperation(
                        operation: .update, blockId: id, type: fields.type, attrs: fields.attrs,
                        content: fields.content, text: fields.text))
                }
            } else {
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
