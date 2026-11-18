import Foundation

/// The block type registry (docs/block-model.md §2): a bundled copy of what
/// `GET /api/block-schema` serves (version 1). The editor uses it to decide which types
/// it may change into each other with an `update` and which may nest where; a type it
/// doesn't know (from a newer server) is shown read-only and never rewritten.
public struct BlockSchema: Decodable, Hashable, Sendable {
    public struct TypeInfo: Decodable, Hashable, Sendable {
        /// `text`, `container` or `atomic`.
        public var category: String
        /// Types allowed as children.
        public var children: [String]
        /// Types it may change into with an update share its group (nil: none).
        public var changeGroup: String?

        enum CodingKeys: String, CodingKey {
            case category, children
            case changeGroup = "change_group"
        }
    }

    public var version: Int
    /// What a document body may hold (also the top level).
    public var flow: [String]
    public var types: [String: TypeInfo]

    /// The registry this app was built against.
    public static let bundled: BlockSchema = {
        let flow = ["bullet_item", "callout", "check_item", "code", "columns", "divider", "file", "heading", "html",
                    "image", "math", "numbered_item", "paragraph", "quote", "table"]
        let inner = flow.filter { $0 != "columns" }
        func type(_ category: String, _ children: [String] = [], _ group: String? = nil) -> TypeInfo {
            TypeInfo(category: category, children: children, changeGroup: group)
        }
        return BlockSchema(version: 1, flow: flow, types: [
            "paragraph": type("text", flow, "text"), "heading": type("text", flow, "text"),
            "bullet_item": type("text", flow, "text"), "numbered_item": type("text", flow, "text"),
            "check_item": type("text", flow, "text"), "code": type("text", [], "text"),
            "quote": type("container", flow, "quote"), "callout": type("container", flow, "quote"),
            "columns": type("container", ["column"]), "column": type("container", inner),
            "table": type("container", ["table_row"]), "table_row": type("container", ["table_cell"]),
            "table_cell": type("container", inner),
            "divider": type("atomic"), "image": type("atomic"), "file": type("atomic"),
            "math": type("atomic"), "html": type("atomic"),
        ])
    }()

    public func knows(_ type: String) -> Bool { types[type] != nil }

    /// Whether an `update` may change a block from `old` to `new` (same type, or the
    /// same change group); otherwise it is deleted and inserted again.
    public func canChangeType(from old: String, to new: String) -> Bool {
        if old == new { return true }
        guard let group = types[old]?.changeGroup else { return false }
        return types[new]?.changeGroup == group
    }

    /// Whether `child` may sit under `parent` (nil: the top level).
    public func allows(_ child: String, under parent: String?) -> Bool {
        guard let parent else { return flow.contains(child) }
        return types[parent]?.children.contains(child) ?? false
    }
}
