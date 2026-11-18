import Foundation

/// What one node of the server's tree is made from: an editor block, a table cell's
/// text paragraph, or fixed fields (a quote's container, a table row or cell, a column,
/// a block kept as it came).
enum NodeSource: Hashable, Sendable {
    case block(EditorBlock)
    case cellParagraph(TableCell)
    case fixed(BlockFields)

    var fields: BlockFields? {
        switch self {
        case .block(let block): BlockCodec.encode(block)
        case .cellParagraph(let cell): cell.paragraphFields
        case .fixed(let fields): fields
        }
    }

    var newProvenance: JSONValue? {
        if case .block(let block) = self { return block.newProvenance }
        return nil
    }
}

/// What the server holds for the blocks the editor shows: the tree shape and each
/// block's fields as the editor would write them.
public struct SyncedTree: Hashable, Sendable {
    public var parent: [String: String?] = [:]
    public var children: [String?: [String]] = [:]
    public var fields: [String: BlockFields] = [:]
    /// Every node in document order (parents before their children).
    public var order: [String] = []
    /// What each node's fields are encoded from, so the next diff re-encodes only the
    /// nodes that changed (an unchanged block shares its storage and compares in O(1)).
    var sources: [String: NodeSource] = [:]
    /// Each table's rows, cells and cell blocks, as a tree of their own (its top level
    /// is the table's children): an unchanged table is carried over whole and skipped
    /// by the diff, so 8,000 cells cost nothing when one paragraph is edited.
    public var tables: [String: SyncedTree] = [:]

    public init() {}

    /// The tree a list of editor blocks describes.
    public init(_ blocks: [EditorBlock]) {
        self.init(blocks, reusing: nil)
    }

    /// The tree for `blocks`, taking the fields of nodes unchanged since `previous` from
    /// it instead of encoding them again (encoding is the expensive part of a diff).
    /// `lazily`: fields are encoded only when a node changes (a note's baseline when it
    /// opens: encoding all 2,000 blocks of a long note up front cost more than showing it).
    init(_ blocks: [EditorBlock], reusing previous: SyncedTree?, lazily: Bool = false) {
        children[nil] = []
        parent.reserveCapacity(blocks.count)
        fields.reserveCapacity(lazily ? 0 : blocks.count)
        sources.reserveCapacity(blocks.count)
        order.reserveCapacity(blocks.count)
        var builder = Builder(previous: previous, lazily: lazily)
        builder.add(blocks, under: nil, into: &self)
    }

    /// A node's fields, encoding them now if they were left for later.
    func fields(of id: String) -> BlockFields? {
        fields[id] ?? sources[id]?.fields
    }

    public static func == (lhs: SyncedTree, rhs: SyncedTree) -> Bool {
        lhs.parent == rhs.parent && lhs.children == rhs.children && lhs.fields == rhs.fields && lhs.tables == rhs.tables
    }

    public func hash(into hasher: inout Hasher) {
        hasher.combine(order)
        hasher.combine(fields)
    }

    private struct Builder {
        let previous: SyncedTree?
        let lazily: Bool

        /// Adds one node. Its fields carry over when its source is unchanged (reported).
        @discardableResult
        func node(_ id: String, under parentId: String?, _ source: NodeSource, into tree: inout SyncedTree) -> Bool {
            tree.parent.updateValue(parentId, forKey: id)
            tree.children[parentId, default: []].append(id)
            if tree.children[id] == nil { tree.children[id] = [] }
            var unchanged = false
            if let previous, let old = previous.sources[id], old == source {
                unchanged = true
                if let known = previous.fields[id] { tree.fields[id] = known }
            } else if !lazily, let fields = source.fields {
                tree.fields[id] = fields
            }
            tree.sources[id] = source
            tree.order.append(id)
            return unchanged
        }

        /// A table's own tree: carried over when the table is unchanged.
        func table(_ table: TableContent, id: String, unchanged: Bool) -> SyncedTree {
            if unchanged, let kept = previous?.tables[id] { return kept }
            var tree = SyncedTree()
            tree.children[nil] = []
            let inner = Builder(previous: previous?.tables[id], lazily: lazily)
            for row in table.rows {
                inner.node(row.id, under: nil, .fixed(BlockFields(type: "table_row", attrs: row.attrs)), into: &tree)
                for cell in row.cells {
                    inner.node(cell.id, under: row.id, .fixed(BlockFields(type: "table_cell", attrs: cell.attrs)), into: &tree)
                    if cell.paragraphFields != nil {
                        inner.node(cell.paragraphId, under: cell.id, .cellParagraph(cell), into: &tree)
                    }
                    for nested in cell.nested { inner.verbatim(nested, under: cell.id, into: &tree) }
                }
            }
            return tree
        }

        /// A flat run of blocks under `root`. Quote lines go into their container; a
        /// table brings its rows, cells and cell blocks, a columns block its columns.
        mutating func add(_ blocks: [EditorBlock], under root: String?, into tree: inout SyncedTree) {
            let shaped = BlockDiff.shape(blocks)
            var index = 0
            while index < shaped.count {
                let item = shaped[index]
                let parentId = item.parent ?? root
                if item.block.kind == .quote {
                    // Consecutive lines under one parent form one container: the first
                    // line's container, a line with another container starts a new one.
                    var end = index + 1
                    var container = item.block.container
                    while end < shaped.count, shaped[end].block.kind == .quote, shaped[end].parent == item.parent {
                        if let next = shaped[end].block.container {
                            if container == nil { container = next } else if next.id != container!.id { break }
                        }
                        end += 1
                    }
                    let box = container ?? QuoteContainer(id: BlockDiff.derivedId(item.block.id))
                    node(box.id, under: parentId, .fixed(BlockFields(type: box.type, attrs: box.attrs)), into: &tree)
                    for line in shaped[index..<end] {
                        node(line.block.id, under: box.id, .block(line.block), into: &tree)
                    }
                    index = end
                    continue
                }
                let unchanged = node(item.block.id, under: parentId, .block(item.block), into: &tree)
                if item.block.kind == .table, let table = item.block.table {
                    tree.tables[item.block.id] = self.table(table, id: item.block.id, unchanged: unchanged)
                }
                if item.block.kind == .columns, let columns = item.block.columns {
                    for column in columns {
                        node(column.id, under: item.block.id, .fixed(column.fields), into: &tree)
                        add(column.blocks, under: column.id, into: &tree)
                    }
                }
                index += 1
            }
        }

        /// A block the editor keeps as it came, with its subtree.
        func verbatim(_ block: BlockNode, under parentId: String, into tree: inout SyncedTree) {
            node(block.id, under: parentId, .fixed(BlockFields(block)), into: &tree)
            for child in block.children { verbatim(child, under: block.id, into: &tree) }
        }
    }
}

/// Turns the editor's paragraphs into the block operations that bring the server's
/// tree to the same state (docs/block-model.md §4): inserts and moves placed after the
/// sibling before them, updates with only what changed (attrs as a patch), deletes.
public enum BlockDiff {
    public struct Shaped: Hashable, Sendable {
        public var block: EditorBlock
        public var parent: String?
        public var depth: Int
    }

    /// Parents from depths. A block can be at most one level deeper than the block
    /// above it, and nothing nests under a block that takes no children (atomic blocks,
    /// tables, columns, embeds, quote lines, code).
    public static func shape(_ blocks: [EditorBlock]) -> [Shaped] {
        var output: [Shaped] = []
        output.reserveCapacity(blocks.count)
        var stack: [EditorBlock] = []  // stack[d] = latest block at depth d
        for var block in blocks {
            let maxDepth: Int
            if let previous = output.last {
                maxDepth = previous.block.kind.takesNoChildren ? previous.depth : previous.depth + 1
            } else {
                maxDepth = 0
            }
            let depth = max(0, min(block.depth, maxDepth))
            block.depth = depth
            if stack.count > depth { stack.removeLast(stack.count - depth) }
            let parent = depth > 0 ? stack[depth - 1].id : nil
            stack.append(block)
            output.append(Shaped(block: block, parent: parent, depth: depth))
        }
        return output
    }

    /// A stable id for a new quote container, from its first line (UUID-shaped, so the
    /// server takes it): the same lines give the same container in every diff.
    static func derivedId(_ lineId: String) -> String {
        // Two 64-bit FNV-1a hashes (stable across launches, unlike `Hasher`).
        func fnv(_ seed: UInt64) -> UInt64 {
            var hash = seed
            for byte in ("quote:" + lineId).utf8 {
                hash ^= UInt64(byte)
                hash = hash &* 0x100000001b3
            }
            return hash
        }
        var bytes: [UInt8] = []
        for value in [fnv(0xcbf29ce484222325), fnv(0x84222325cbf29ce4)] {
            for shift in stride(from: 56, through: 0, by: -8) { bytes.append(UInt8(truncatingIfNeeded: value >> UInt64(shift))) }
        }
        bytes[6] = (bytes[6] & 0x0F) | 0x40
        bytes[8] = (bytes[8] & 0x3F) | 0x80
        let hex = bytes.map { String(format: "%02x", $0) }.joined()
        let parts = [hex.prefix(8), hex.dropFirst(8).prefix(4), hex.dropFirst(12).prefix(4), hex.dropFirst(16).prefix(4), hex.dropFirst(20)]
        return parts.map(String.init).joined(separator: "-")
    }

    /// Whether `update` may change a block's type from `old` to `new` (the server's
    /// change groups); otherwise the block is deleted and inserted again.
    static func canChangeType(from old: String, to new: String) -> Bool {
        BlockSchema.bundled.canChangeType(from: old, to: new)
    }

    /// The attrs patch from `old` to `new`: changed and new keys, removed ones as null.
    static func attrsPatch(from old: [String: JSONValue], to new: [String: JSONValue]) -> [String: JSONValue] {
        var patch: [String: JSONValue] = [:]
        for (key, value) in new where old[key] != value { patch[key] = value }
        for key in old.keys where new[key] == nil { patch[key] = .null }
        return patch
    }

    /// Operations from `base` to `target`, and the tree the server will then hold.
    public static func operations(from base: SyncedTree, to target: [EditorBlock]) -> (operations: [BlockOperation], result: SyncedTree) {
        let result = SyncedTree(target, reusing: base)
        return (operations(from: base, to: result, root: nil), result)
    }

    /// Operations from one tree to another; `root` is the block their top level sits
    /// under (a table's, for its rows), nil for the note.
    static func operations(from base: SyncedTree, to result: SyncedTree, root: String?) -> [BlockOperation] {
        var children = base.children
        var parent = base.parent
        if children[nil] == nil { children[nil] = [] }
        var operations: [BlockOperation] = []
        var nextIndex: [String?: Int] = [:]
        var removed = Set(base.order).subtracting(result.order)

        // A type change across groups (text → image, say) is a delete and an insert.
        var replaced: Set<String> = []
        for id in result.order where base.parent[id] != nil {
            // Only nodes that changed can have changed type (the rest share their source).
            guard let new = result.fields[id], let source = base.sources[id], source != result.sources[id],
                  let old = base.fields[id] ?? source.fields,
                  !canChangeType(from: old.type, to: new.type) else { continue }
            replaced.insert(id)
        }

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

            if replaced.contains(id), let currentParent = parent[id] {
                // Keep its surviving children (they move on below), then delete it.
                var anchor = id
                for child in children[id] ?? [] where survives(child) {
                    operations.append(BlockOperation(operation: .move, blockId: child, afterId: anchor))
                    children[id]?.removeAll { $0 == child }
                    let at = (children[currentParent]?.firstIndex(of: anchor) ?? -1) + 1
                    children[currentParent, default: []].insert(child, at: at)
                    parent.updateValue(currentParent, forKey: child)
                    anchor = child
                }
                children[currentParent]?.removeAll { $0 == id }
                forget(id)
                operations.append(BlockOperation(operation: .delete, blockId: id))
            }

            var siblings = children[targetParent] ?? []
            if let currentParent = parent[id] {
                // Siblings before `position` are the target's, already placed, so the block
                // is in place exactly when it sits at `position` (O(1), not a search).
                let inPlace = currentParent == targetParent && siblings.indices.contains(position) && siblings[position] == id
                if !inPlace {
                    children[currentParent]?.removeAll { $0 == id }
                    siblings = children[targetParent] ?? []
                    operations.append(placed(BlockOperation(operation: .move, blockId: id), position: position,
                                             siblings: siblings, parent: targetParent ?? root))
                    children[targetParent, default: []].insert(id, at: min(position, siblings.count))
                    parent.updateValue(targetParent, forKey: id)
                }
                // No fields: unchanged since the base and never encoded, so nothing to update.
                if let new = result.fields[id], let old = base.fields(of: id), old != new {
                    var update = BlockOperation(operation: .update, blockId: id)
                    if old.type != new.type { update.type = new.type }
                    let patch = attrsPatch(from: old.attrs, to: new.attrs)
                    if !patch.isEmpty { update.attrs = patch }
                    if old.content != new.content { update.content = new.content }
                    if old.payload != new.payload { update.payload = new.payload ?? .null }
                    if update.type != nil || update.attrs != nil || update.content != nil || update.payload != nil {
                        operations.append(update)
                    }
                }
            } else if let fields = result.fields(of: id) {
                var insert = BlockOperation(
                    operation: .insert, blockId: id, type: fields.type,
                    attrs: fields.attrs.filter { $0.value != .null },
                    content: fields.content == .null ? nil : fields.content, payload: fields.payload,
                    provenance: result.sources[id]?.newProvenance)
                if insert.attrs?.isEmpty == true { insert.attrs = nil }
                insert = placed(insert, position: position, siblings: siblings, parent: targetParent ?? root)
                operations.append(insert)
                children[targetParent, default: []].insert(id, at: min(position, siblings.count))
                children[id] = []
                parent.updateValue(targetParent, forKey: id)
            }
            // A table's rows and cells: only when the table changed (or is new).
            if let rows = result.tables[id] {
                let isNew = replaced.contains(id) || base.parent[id] == nil
                if isNew || base.sources[id] != result.sources[id] {
                    operations += Self.operations(from: isNew ? SyncedTree() : base.tables[id] ?? SyncedTree(), to: rows, root: id)
                }
            }
        }
        removed.subtract(replaced)

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
        return operations
    }

    /// Placement for a block going to `position` among `siblings` (the parent's children
    /// without it): after the one before it, else before the first, else appended.
    private static func placed(_ operation: BlockOperation, position: Int, siblings: [String], parent: String?) -> BlockOperation {
        var operation = operation
        let at = min(position, siblings.count)
        if at > 0 {
            operation.afterId = siblings[at - 1]
        } else if let first = siblings.first {
            operation.beforeId = first
        } else {
            operation.parentId = parent
        }
        return operation
    }
}
