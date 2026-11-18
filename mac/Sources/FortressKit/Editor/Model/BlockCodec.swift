import Foundation

/// Converts between the backend's blocks and `EditorBlock`s.
///
/// Blocks come in two shapes. Ones saved by an editor carry BlockNote inline content in
/// `content.blocknote` (`[{type:"text", text, styles}, {type:"link", href, content}]`) and
/// BlockNote types (`bulletListItem`, `checkListItem`, `codeBlock`, …). Ones imported from
/// Markdown carry only `content.markdown` with the Markdown syntax still in it and types
/// like `list`, `code` and `thematic_break`. Both decode to the same editor blocks; an
/// edited block is always written back in the BlockNote shape (as the web editor does),
/// keeping every attribute and content key the editor doesn't manage.
public enum BlockCodec {
    // MARK: Decoding

    /// Flattens a block tree into paragraphs. Children of editable blocks become deeper
    /// paragraphs; an embed keeps its subtree to itself.
    public static func flatten(_ nodes: [BlockNode], depth: Int = 0, resolved: ResolvedLinks? = nil) -> [EditorBlock] {
        var output: [EditorBlock] = []
        for node in nodes {
            var block = decode(node, depth: depth)
            if let resolved, !resolved.documents.isEmpty { block.runs = currentLinkTitles(block.runs, resolved) }
            output.append(block)
            if block.kind != .embed {
                output.append(contentsOf: flatten(node.children, depth: depth + 1, resolved: resolved))
            }
        }
        return output
    }

    /// Note links read with the linked note's current title (living links). The stored
    /// text changes only if the block is edited.
    public static func currentLinkTitles(_ runs: [InlineRun], _ resolved: ResolvedLinks) -> [InlineRun] {
        var output: [InlineRun] = []
        var index = 0
        while index < runs.count {
            let run = runs[index]
            guard run.node == nil, let href = run.marks.link, let target = InlineMarkdown.documentLinkTarget(href),
                  let title = resolved.documents[target.id]?.title, !title.isEmpty else {
                output.append(run)
                index += 1
                continue
            }
            var end = index
            var text = ""
            while end < runs.count, runs[end].node == nil, runs[end].marks.link == href {
                text += runs[end].text
                end += 1
            }
            if text == title {
                output.append(contentsOf: runs[index..<end])
            } else {
                var marks = run.marks
                marks.link = InlineMarkdown.documentLinkHref(id: target.id, title: title)
                output.append(InlineRun(title, marks))
            }
            index = end
        }
        return output
    }

    public static func decode(_ node: BlockNode, depth: Int = 0) -> EditorBlock {
        let native = node.content["blocknote"]
        let markdown = node.content["markdown"]?.stringValue ?? node.text
        var origin = BlockOrigin(
            type: node.type, attrs: node.attrs, userAttrs: node.userAttrs, content: node.content,
            text: node.text, kind: .paragraph, native: native != nil,
            createdAt: node.createdAt, updatedAt: node.updatedAt,
            provenance: node.provenance, revision: node.revision)
        var block = EditorBlock(id: node.id, kind: .paragraph, depth: depth)

        func inline(stripping pattern: String? = nil) -> [InlineRun] {
            if let native {
                if case .array(let items) = native { return runs(fromBlockNote: items) }
                if case .string(let text) = native { return InlineMarkdown.parse(text) }
                return []
            }
            var source = markdown
            if let pattern {
                source = source.split(separator: "\n", omittingEmptySubsequences: false)
                    .map { $0.replacingOccurrences(of: pattern, with: "", options: .regularExpression) }
                    .joined(separator: "\n")
            }
            return InlineMarkdown.parse(source)
        }

        switch node.type {
        case "paragraph":
            if native == nil, let image = InlineMarkdown.soleImage(markdown) {
                block.kind = .image
                origin.attrs["url"] = origin.attrs["url"] ?? .string(image.url)
                if origin.attrs["name"] == nil, !image.alt.isEmpty { origin.attrs["name"] = .string(image.alt) }
            } else {
                block.runs = inline()
            }
        case "heading":
            let hashes = markdown.prefix { $0 == "#" }.count
            block.kind = .heading(node.attrs["level"]?.intValue ?? max(hashes, 1))
            block.runs = inline(stripping: #"^\s*#{1,6}\s+"#)
        case "list":
            if let checked = node.attrs["checked"]?.boolValue {
                block.kind = .check
                block.checked = checked
            } else {
                block.kind = node.attrs["ordered"]?.boolValue == true ? .numbered : .bullet
            }
            block.runs = inline(stripping: #"^\s*(?:(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?|\s+)"#)
        case "bulletListItem", "toggleListItem":
            block.kind = .bullet
            block.runs = inline()
        case "numberedListItem":
            block.kind = .numbered
            block.runs = inline()
        case "checkListItem":
            block.kind = .check
            block.checked = node.attrs["checked"]?.boolValue ?? false
            block.runs = inline()
        case "quote", "blockquote", "callout":
            block.kind = .quote
            block.runs = inline(stripping: #"^\s*>\s?"#)
        case "code", "codeBlock":
            block.kind = .code
            block.language = node.attrs["language"]?.stringValue
            if let native {
                block.runs = [InlineRun(inlineText(native))]
            } else {
                var lines = markdown.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
                if let first = lines.first, first.trimmingCharacters(in: .whitespaces).hasPrefix("```")
                    || first.trimmingCharacters(in: .whitespaces).hasPrefix("~~~") {
                    let info = first.trimmingCharacters(in: .whitespaces).dropFirst(3)
                        .trimmingCharacters(in: .whitespaces)
                    if block.language == nil, !info.isEmpty { block.language = info }
                    lines.removeFirst()
                    if let last = lines.last, ["```", "~~~"].contains(last.trimmingCharacters(in: .whitespaces)) {
                        lines.removeLast()
                    }
                }
                block.runs = [InlineRun(lines.joined(separator: softBreak))]
            }
        case "thematic_break", "divider", "hr":
            block.kind = .divider
        case "image":
            block.kind = .image
        case "file", "audio", "video", "pdf":
            block.kind = .file
        default:
            block.kind = .embed
        }
        // Rich content the editor can't edit inline (a table's rows, say) makes it an embed.
        if block.kind.hasInlineText, let native, !isInlineArray(native), native != .null,
           native.stringValue == nil {
            block.kind = .embed
            block.runs = []
        }
        if block.kind == .embed { origin.children = node.children }
        block.runs = normalized(block.runs.map { run in
            var run = run
            if run.node == nil { run.text = run.text.replacingOccurrences(of: "\n", with: softBreak) }
            return run
        })
        origin.kind = block.kind
        block.origin = origin
        return block
    }

    private static func isInlineArray(_ value: JSONValue) -> Bool {
        if case .array = value { return true }
        return false
    }

    static func runs(fromAny value: JSONValue) -> [InlineRun] {
        switch value {
        case .array(let items): runs(fromBlockNote: items)
        case .string(let text): [InlineRun(text)]
        default: []
        }
    }

    /// BlockNote inline content to runs. Note and block links written as plain text in
    /// older notes (`[[id|Title]]`, `(( id "label" ))`) become real links.
    public static func runs(fromBlockNote items: [JSONValue], link: String? = nil) -> [InlineRun] {
        var output: [InlineRun] = []
        for item in items {
            switch item {
            case .string(let text):
                var marks = InlineMarks()
                marks.link = link
                output.append(contentsOf: link == nil ? InlineMarkdown.linkifyReferences(text, marks) : [InlineRun(text, marks)])
            case .object(let object):
                let type = object["type"]?.stringValue
                if type == "text", let text = object["text"]?.stringValue {
                    var marks = self.marks(from: object["styles"])
                    marks.link = link
                    if link == nil, !marks.code {
                        output.append(contentsOf: InlineMarkdown.linkifyReferences(text, marks))
                    } else {
                        output.append(InlineRun(text, marks))
                    }
                } else if type == "link", let href = object["href"]?.stringValue, link == nil,
                          !InlineMarkdown.isBlockReference(href) {
                    let inner: [JSONValue]
                    switch object["content"] {
                    case .array(let items): inner = items
                    case .string(let text): inner = [.string(text)]
                    default: inner = []
                    }
                    output.append(contentsOf: runs(fromBlockNote: inner, link: href))
                } else {
                    // Block references (living: shown with the block's current text),
                    // mentions and anything newer stay one atomic, untouched node.
                    output.append(.atomic(item))
                }
            default:
                continue
            }
        }
        return output
    }

    static func marks(from styles: JSONValue?) -> InlineMarks {
        var marks = InlineMarks()
        guard case .object(let styles)? = styles else { return marks }
        for (key, value) in styles {
            switch (key, value) {
            case ("bold", .bool(let on)): marks.bold = on
            case ("italic", .bool(let on)): marks.italic = on
            case ("underline", .bool(let on)): marks.underline = on
            case ("strike", .bool(let on)), ("strikethrough", .bool(let on)): marks.strike = on
            case ("code", .bool(let on)): marks.code = on
            case ("textColor", .string(let color)): marks.textColor = color == "default" ? nil : color
            case ("backgroundColor", .string(let color)): marks.highlight = color == "default" ? nil : color
            default: marks.extraStyles[key] = value
            }
        }
        return marks
    }

    // MARK: Encoding

    /// The fields to write for a block, keeping what the editor doesn't manage.
    public static func encode(_ block: EditorBlock) -> BlockFields {
        let origin = block.origin
        if !block.kind.hasInlineText {
            if let origin, origin.kind == block.kind {
                return BlockFields(type: origin.type, attrs: origin.attrs, content: origin.content, text: origin.text)
            }
            let attrs = origin?.attrs ?? [:]
            switch block.kind {
            case .divider:
                return BlockFields(type: "divider", attrs: attrs, content: .object(["markdown": .string("---")]), text: "")
            case .image:
                let url = attrs["url"]?.stringValue ?? ""
                let name = attrs["name"]?.stringValue ?? ""
                return BlockFields(type: "image", attrs: attrs, content: .object(["markdown": .string("![\(name)](\(url))")]), text: name)
            case .file:
                let url = attrs["url"]?.stringValue ?? ""
                let name = attrs["name"]?.stringValue ?? "file"
                return BlockFields(type: "file", attrs: attrs, content: .object(["markdown": .string("[\(name)](\(url))")]), text: name)
            default:
                return BlockFields(
                    type: origin?.type ?? "paragraph", attrs: attrs,
                    content: origin?.content ?? .object([:]), text: origin?.text ?? "")
            }
        }

        let keepType = origin.map { $0.kind == block.kind && $0.native } ?? false
        let type = keepType ? origin!.type : block.kind.canonicalType
        var attrs = origin?.attrs ?? [:]
        if !keepType {
            attrs["level"] = nil
            attrs["checked"] = nil
            attrs["ordered"] = nil
            if block.kind != .code { attrs["language"] = nil }
        }
        if let level = block.kind.headingLevel { attrs["level"] = .number(Double(level)) }
        if block.kind == .check { attrs["checked"] = .bool(block.checked) }
        if block.kind == .code {
            if let language = block.language, !language.isEmpty {
                attrs["language"] = .string(language)
            }
        }
        var content: [String: JSONValue] = [:]
        if case .object(let existing)? = origin?.content { content = existing }
        content["blocknote"] = blockNote(block.runs, code: block.kind == .code)
        content["markdown"] = .string(markdown(block))
        return BlockFields(type: type, attrs: attrs, content: .object(content), text: plainText(block.runs))
    }

    /// Runs as BlockNote inline content.
    public static func blockNote(_ runs: [InlineRun], code: Bool = false) -> JSONValue {
        var output: [JSONValue] = []
        var linkHref: String?
        var linkItems: [JSONValue] = []
        func closeLink() {
            if let href = linkHref {
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
            let text = run.text.replacingOccurrences(of: softBreak, with: "\n")
            let item: JSONValue = .object([
                "type": .string("text"), "text": .string(text),
                "styles": code ? .object([:]) : styles(run.marks),
            ])
            if let href = run.marks.link, !code {
                if linkHref != href { closeLink(); linkHref = href }
                linkItems.append(item)
            } else {
                closeLink()
                output.append(item)
            }
        }
        closeLink()
        return .array(output)
    }

    static func styles(_ marks: InlineMarks) -> JSONValue {
        var styles = marks.extraStyles
        if marks.bold { styles["bold"] = .bool(true) }
        if marks.italic { styles["italic"] = .bool(true) }
        if marks.underline { styles["underline"] = .bool(true) }
        if marks.strike { styles["strike"] = .bool(true) }
        if marks.code { styles["code"] = .bool(true) }
        if let color = marks.textColor { styles["textColor"] = .string(color) }
        if let color = marks.highlight { styles["backgroundColor"] = .string(color) }
        return .object(styles)
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

    /// What search, titles and snippets read: the text with links as their labels.
    public static func plainText(_ runs: [InlineRun]) -> String {
        runs.map { run in
            if let node = run.node { return inlineText(node) }
            return run.text.replacingOccurrences(of: softBreak, with: "\n")
        }.joined()
    }

    /// The visible text of any inline node.
    public static func inlineText(_ value: JSONValue) -> String {
        switch value {
        case .string(let text): return text
        case .array(let items): return items.map(inlineText).joined()
        case .object(let object):
            if let text = object["text"]?.stringValue { return text }
            if let content = object["content"] { return inlineText(content) }
            return ""
        default: return ""
        }
    }

    /// Portable Markdown for one block (its children are rendered by the backend).
    public static func markdown(_ block: EditorBlock) -> String {
        let body = InlineMarkdown.render(block.runs)
        switch block.kind {
        case .paragraph: return body
        case .heading1: return "# " + body
        case .heading2: return "## " + body
        case .heading3: return "### " + body
        case .bullet: return "- " + body.replacingOccurrences(of: "\n", with: "\n  ")
        case .numbered: return "1. " + body.replacingOccurrences(of: "\n", with: "\n   ")
        case .check: return (block.checked ? "- [x] " : "- [ ] ") + body.replacingOccurrences(of: "\n", with: "\n  ")
        case .quote: return "> " + body.replacingOccurrences(of: "\n", with: "\n> ")
        case .code:
            return "```\(block.language ?? "")\n" + plainText(block.runs) + "\n```"
        case .divider: return "---"
        case .image, .file, .embed: return block.origin?.content["markdown"]?.stringValue ?? ""
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
