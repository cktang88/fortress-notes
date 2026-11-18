import Foundation

/// Note links (`docLink`) and block references (`blockRef`): inline nodes that carry
/// only the target's id (docs/block-model.md §3). Their labels are the target's current
/// title or text from the document's `resolved_links`; they are never stored.
public enum NoteLinks {
    /// A link to another note.
    public static func documentNode(id: String) -> JSONValue {
        .object(["type": .string("docLink"), "id": .string(id)])
    }

    /// A reference to a block.
    public static func blockNode(id: String) -> JSONValue {
        .object(["type": .string("blockRef"), "id": .string(id)])
    }

    /// The note a `docLink` node points at.
    public static func documentTarget(_ node: JSONValue) -> String? {
        node["type"]?.stringValue == "docLink" ? node["id"]?.stringValue : nil
    }

    /// The block a `blockRef` node points at.
    public static func blockTarget(_ node: JSONValue) -> String? {
        node["type"]?.stringValue == "blockRef" ? node["id"]?.stringValue : nil
    }

    /// The URL a note link carries in the text view, so clicking it opens the note.
    public static func documentURL(id: String) -> URL? { URL(string: "fortress-note:\(id)") }

    /// The note id of a clicked `fortress-note:` URL.
    public static func documentId(from link: Any) -> String? {
        let href = (link as? URL)?.absoluteString ?? (link as? String) ?? ""
        guard href.hasPrefix("fortress-note:") else { return nil }
        let id = String(href.dropFirst("fortress-note:".count))
        return id.isEmpty ? nil : id
    }
}
