import Foundation

/// Converts between the server's canonical blocks (docs/block-model.md) and the editor's
/// flat list of `EditorBlock`s.
///
/// Text blocks become paragraphs of the editor (their children deeper ones); a quote or
/// callout becomes one quote line per paragraph child; tables and columns become one
/// attachment each, holding their child blocks. Blocks the editor doesn't edit are shown
/// read-only and never rewritten.
public enum BlockCodec {
    // MARK: Decoding

    /// Flattens a block tree into paragraphs.
    public static func flatten(_ nodes: [BlockNode], depth: Int = 0, resolved: ResolvedLinks? = nil) -> [EditorBlock] {
        var output: [EditorBlock] = []
        output.reserveCapacity(nodes.count)
        for node in nodes {
            let blocks = decodeAll(node, depth: depth)
            output.append(contentsOf: blocks)
            if blocks.count == 1, let block = blocks.first, !block.kind.takesNoChildren {
                output.append(contentsOf: flatten(node.children, depth: depth + 1, resolved: resolved))
            }
        }
        return output
    }

    /// A node as editor blocks: one, or a quote's lines (its children are not included
    /// for a text block; `flatten` adds them).
    public static func decodeAll(_ node: BlockNode, depth: Int = 0) -> [EditorBlock] {
        if node.type == "quote" || node.type == "callout",
           !node.children.isEmpty, node.children.allSatisfy({ $0.type == "paragraph" && $0.children.isEmpty }) {
            let container = QuoteContainer(id: node.id, type: node.type, attrs: node.attrs)
            return node.children.map { child in
                EditorBlock(id: child.id, kind: .quote, depth: depth, runs: runs(fromContent: child.content),
                            origin: BlockOrigin(child, kind: .quote), container: container)
            }
        }
        return [decode(node, depth: depth)]
    }

    /// The columns of a `columns` block, each with its blocks flattened like the main flow.
    public static func columns(of node: BlockNode) -> [EditorColumn] {
        node.children.map { child in
            var width: Double?
            if case .number(let value)? = child.attrs["width"] { width = value }
            return EditorColumn(id: child.id, width: width, origin: BlockOrigin(child, kind: .embed),
                                blocks: flatten(child.children, depth: 0))
        }
    }

    /// One block (without its children, except for blocks that keep them).
    public static func decode(_ node: BlockNode, depth: Int = 0) -> EditorBlock {
        var block = EditorBlock(id: node.id, kind: .embed, depth: depth)
        switch node.type {
        case "paragraph": block.kind = .paragraph
        case "heading": block.kind = .heading(node.attrs["level"]?.intValue ?? 1)
        case "bullet_item": block.kind = .bullet
        case "numbered_item": block.kind = .numbered
        case "check_item":
            block.kind = .check
            block.checked = node.attrs["checked"]?.boolValue ?? false
        case "code":
            block.kind = .code
            block.language = node.attrs["language"]?.stringValue
        case "divider": block.kind = .divider
        case "image": block.kind = .image
        case "file": block.kind = .file
        case "math": block.kind = .math
        case "table":
            if let table = TableContent(node: node) {
                block.kind = .table
                block.table = table
            }
        case "columns":
            // Columns hold only column blocks; anything else is shown as is.
            if !node.children.isEmpty, node.children.allSatisfy({ $0.type == "column" }) {
                block.kind = .columns
                block.columns = columns(of: node)
            }
        default:
            break
        }
        if block.kind.hasInlineText {
            block.runs = runs(fromContent: node.content)
            if block.kind == .code { block.runs = block.runs.map { InlineRun($0.node == nil ? $0.text : "") }.filter { !$0.text.isEmpty } }
        }
        block.origin = BlockOrigin(node, kind: block.kind, keepingChildren: block.kind == .embed)
        return block
    }

    /// Inline content (docs/block-model.md §3) to runs. `docLink`, `blockRef` and any
    /// node this editor doesn't know become one atomic run each, kept verbatim.
    public static func runs(fromContent content: JSONValue) -> [InlineRun] {
        guard case .array(let items) = content else { return [] }
        var output: [InlineRun] = []
        output.reserveCapacity(items.count)
        for item in items {
            switch item["type"]?.stringValue {
            case "text":
                guard let text = item["text"]?.stringValue else { continue }
                output.append(InlineRun(softBreaks(text), marks(from: item["marks"])))
            case "link":
                guard let href = item["href"]?.stringValue, case .array(let inner)? = item["content"] else {
                    output.append(.atomic(item))
                    continue
                }
                for node in inner {
                    guard let text = node["text"]?.stringValue else { continue }
                    var marks = marks(from: node["marks"])
                    marks.link = href
                    output.append(InlineRun(softBreaks(text), marks))
                }
            default:
                output.append(.atomic(item))
            }
        }
        return normalized(output)
    }

    static func marks(from value: JSONValue?) -> InlineMarks {
        var marks = InlineMarks()
        guard case .object(let object)? = value else { return marks }
        for (key, value) in object {
            switch (key, value) {
            case ("bold", .bool(let on)): marks.bold = on
            case ("italic", .bool(let on)): marks.italic = on
            case ("underline", .bool(let on)): marks.underline = on
            case ("strike", .bool(let on)): marks.strike = on
            case ("code", .bool(let on)): marks.code = on
            case ("sup", .bool(let on)): marks.sup = on
            case ("sub", .bool(let on)): marks.sub = on
            case ("color", .string(let color)): marks.textColor = color
            case ("highlight", .string(let color)): marks.highlight = color
            default: break
            }
        }
        return marks
    }

    // MARK: Encoding

    /// The fields to write for a block. Blocks the editor doesn't edit write back what
    /// they read.
    public static func encode(_ block: EditorBlock) -> BlockFields {
        let origin = block.origin
        switch block.kind {
        case .table:
            return BlockFields(type: "table", attrs: block.table?.attrs ?? origin?.attrs ?? [:])
        case .columns:
            return BlockFields(type: "columns", attrs: origin?.attrs ?? [:])
        case .divider, .image, .file, .math, .embed:
            if let origin, origin.kind == block.kind || block.kind == .embed {
                return BlockFields(type: origin.type, attrs: origin.attrs, content: origin.content, payload: origin.payload)
            }
            switch block.kind {
            case .math: return BlockFields(type: "math", payload: origin?.payload ?? .object(["latex": .string("")]))
            case .embed: return BlockFields(type: "paragraph", content: .array([]))
            default: return BlockFields(type: block.kind.canonicalType, attrs: origin?.attrs ?? [:])
            }
        default:
            break
        }
        // A text block: declared attributes of another text type are dropped when the
        // kind changes (the server does the same); extension attributes stay.
        var attrs = origin?.attrs ?? [:]
        attrs["level"] = nil
        attrs["checked"] = nil
        attrs["language"] = nil
        if let level = block.kind.headingLevel { attrs["level"] = .number(Double(level)) }
        if block.kind == .check { attrs["checked"] = .bool(block.checked) }
        if block.kind == .code, let language = block.language, !language.isEmpty { attrs["language"] = .string(language) }
        return BlockFields(type: block.kind.canonicalType, attrs: attrs,
                           content: inlineContent(block.runs, plain: block.kind == .code))
    }

    /// Runs as canonical inline content: text nodes with marks, links around the text
    /// they cover, atomic nodes as they are. `plain`: unmarked text only (code blocks).
    public static func inlineContent(_ runs: [InlineRun], plain: Bool = false) -> JSONValue {
        if plain {
            let text = hardBreaks(runs.filter { $0.node == nil }.map(\.text).joined())
            return .array(text.isEmpty ? [] : [.object(["type": .string("text"), "text": .string(text)])])
        }
        var output: [JSONValue] = []
        var linkHref: String?
        var linkItems: [JSONValue] = []
        func closeLink() {
            if let href = linkHref, !linkItems.isEmpty {
                output.append(.object(["type": .string("link"), "href": .string(href), "content": .array(linkItems)]))
            }
            linkHref = nil
            linkItems = []
        }
        for run in normalized(runs) {
            if let node = run.node {
                closeLink()
                output.append(node)
                continue
            }
            var item: [String: JSONValue] = ["type": .string("text"), "text": .string(hardBreaks(run.text))]
            let marks = Self.marksObject(run.marks)
            if !marks.isEmpty { item["marks"] = .object(marks) }
            if let href = run.marks.link, !href.isEmpty {
                if linkHref != href { closeLink(); linkHref = href }
                linkItems.append(.object(item))
            } else {
                closeLink()
                output.append(.object(item))
            }
        }
        closeLink()
        return .array(output)
    }

    static func marksObject(_ marks: InlineMarks) -> [String: JSONValue] {
        var object: [String: JSONValue] = [:]
        if marks.bold { object["bold"] = .bool(true) }
        if marks.italic { object["italic"] = .bool(true) }
        if marks.underline { object["underline"] = .bool(true) }
        if marks.strike { object["strike"] = .bool(true) }
        if marks.code { object["code"] = .bool(true) }
        if marks.sup { object["sup"] = .bool(true) } else if marks.sub { object["sub"] = .bool(true) }
        if let color = marks.textColor, !color.isEmpty { object["color"] = .string(color) }
        if let color = marks.highlight, !color.isEmpty { object["highlight"] = .string(color) }
        return object
    }

    /// "\n" → U+2028, without a Foundation round trip when there is none (most runs:
    /// decoding a long note did this bridging copy for every run).
    static func softBreaks(_ text: String) -> String {
        text.utf8.contains(0x0A) ? text.replacingOccurrences(of: "\n", with: softBreak) : text
    }

    /// U+2028 → "\n" (U+2028 is E2 80 A8 in UTF-8).
    static func hardBreaks(_ text: String) -> String {
        text.utf8.contains(0xE2) ? text.replacingOccurrences(of: softBreak, with: "\n") : text
    }

    /// Merges neighbouring runs with the same marks and drops empty ones.
    public static func normalized(_ runs: [InlineRun]) -> [InlineRun] {
        var output: [InlineRun] = []
        for run in runs where !run.text.isEmpty || run.node != nil {
            if run.node == nil, let last = output.last, last.node == nil, last.marks == run.marks {
                output[output.count - 1].text += run.text
            } else {
                output.append(run)
            }
        }
        return output
    }

    /// The visible text of the runs (note links and block references as their labels
    /// when `resolved` knows them).
    public static func plainText(_ runs: [InlineRun], resolved: ResolvedLinks? = nil) -> String {
        runs.map { run in
            if let node = run.node { return inlineText(node, resolved: resolved) }
            return Self.hardBreaks(run.text)
        }.joined()
    }

    /// The visible text of an inline node.
    public static func inlineText(_ value: JSONValue, resolved: ResolvedLinks? = nil) -> String {
        switch value {
        case .string(let text): return text
        case .array(let items): return items.map { inlineText($0, resolved: resolved) }.joined()
        case .object(let object):
            switch object["type"]?.stringValue {
            case "docLink": return object["id"]?.stringValue.flatMap { resolved?.documents[$0]?.title } ?? ""
            case "blockRef": return object["id"]?.stringValue.flatMap { resolved?.blocks[$0]?.text } ?? ""
            default: break
            }
            if let text = object["text"]?.stringValue { return text }
            if let content = object["content"] { return inlineText(content, resolved: resolved) }
            return ""
        default: return ""
        }
    }

    // MARK: Titles

    /// The backend's auto-title rule: first non-empty line, list/heading markers removed,
    /// cut to 80 characters at a word.
    public static func firstLineTitle(_ texts: [String]) -> String {
        for text in texts {
            for line in text.split(whereSeparator: { $0 == "\n" || $0 == "\u{2028}" }) {
                let trimmed = String(line)
                    .replacingOccurrences(of: #"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)"#, with: "", options: .regularExpression)
                    .trimmingCharacters(in: .whitespaces)
                if trimmed.isEmpty { continue }
                if trimmed.count <= 80 { return trimmed }
                let cut = String(trimmed.prefix(80))
                let word = cut.range(of: " ", options: .backwards).map { String(cut[..<$0.lowerBound]) } ?? cut
                return (word.isEmpty ? cut : word).trimmingCharacters(in: CharacterSet(charactersIn: " ,;:")) + "…"
            }
        }
        return "Untitled"
    }
}
