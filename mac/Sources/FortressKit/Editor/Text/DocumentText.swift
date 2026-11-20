import AppKit

/// Editor blocks ⇄ attributed text. Each block is one paragraph ending in "\n" (the last
/// one too: the final "\n" is never selectable, so every paragraph has a character to
/// carry its `BlockInfo`). Soft line breaks inside a block are U+2028. A long paragraph
/// block is several soft paragraphs, some of its spaces stored as U+2029 (`SoftBreaks`).
@MainActor
enum DocumentText {
    /// The note's text, styled as it is built (one pass, with attributes shared per
    /// style): building plain and restyling afterwards re-split every run a second time.
    static func build(_ blocks: [EditorBlock], theme: EditorTheme, context: AttachmentContext) -> NSMutableAttributedString {
        let output = NSMutableAttributedString()
        output.beginEditing()
        for block in blocks.isEmpty ? [EditorBlock()] : blocks {
            output.append(paragraph(block, context: context, theme: theme))
        }
        output.endEditing()
        return output
    }

    /// One block's paragraph, terminator included (styled when given a theme; otherwise
    /// the editor styles it when it goes into the text).
    static func paragraph(_ block: EditorBlock, context: AttachmentContext, theme: EditorTheme? = nil) -> NSAttributedString {
        let info = BlockInfo(block)
        let output = NSMutableAttributedString()
        var split = false
        if block.kind.hasInlineText {
            output.append(inline(block.runs, info: info, context: context, theme: theme))
            split = SoftBreaks.eligible(block.kind) && SoftBreaks.split(output, content: NSRange(location: 0, length: output.length)) != nil
        } else {
            let attachment = BlockAttachment(info: info, context: context, block: block)
            var attributes = base(info: info, marks: InlineMarks(), theme: theme)
            attributes[.attachment] = attachment
            output.append(NSAttributedString(string: objectReplacement, attributes: attributes))
        }
        output.append(NSAttributedString(string: "\n", attributes: base(info: info, marks: InlineMarks(), theme: theme)))
        if split, let theme {
            // Styled as the editor styles it: each soft paragraph with its own spacing.
            for piece in SoftBreaks.pieces(of: NSRange(location: 0, length: output.length), in: output.nsString) where !piece.role.isEmpty {
                if let style = theme.cachedAttributes(info: info, marks: InlineMarks(), role: piece.role)[.paragraphStyle] {
                    output.addAttribute(.paragraphStyle, value: style, range: piece.range)
                }
            }
        }
        return output
    }

    /// What `EditorTheme.restyle` would give a run: presentation, block and marks.
    private static func base(info: BlockInfo, marks: InlineMarks, theme: EditorTheme?) -> [NSAttributedString.Key: Any] {
        var attributes = theme?.cachedAttributes(info: info, marks: marks) ?? [:]
        attributes[.fnBlock] = info
        if !marks.isPlain { attributes[.fnMarks] = MarksBox(marks) }
        return attributes
    }

    static func inline(_ runs: [InlineRun], info: BlockInfo, context: AttachmentContext, theme: EditorTheme? = nil) -> NSAttributedString {
        let output = NSMutableAttributedString()
        for run in runs {
            if let node = run.node {
                // An inline node shows its label as text (a note's current title, a
                // referenced block's text); all of it reads back as the one node.
                var attributes = base(info: info, marks: InlineMarks(), theme: theme)
                attributes[.fnNode] = NodeBox(node)
                // A note link opens its note when clicked (see `clickedOnLink`).
                if let id = NoteLinks.documentTarget(node), let url = NoteLinks.documentURL(id: id) { attributes[.link] = url }
                output.append(NSAttributedString(string: label(of: node, context: context), attributes: attributes))
                continue
            }
            let attributes = base(info: info, marks: run.marks, theme: theme)
            // A paragraph separator inside a block's text would split it in the editor.
            var text = run.text
            if text.utf16.contains(where: { $0 == 0x0D || $0 == 0x0A || $0 == 0x2029 }) {
                text = text.replacingOccurrences(of: "\r\n", with: softBreak).replacingOccurrences(of: "\r", with: softBreak)
                    .replacingOccurrences(of: "\n", with: softBreak).replacingOccurrences(of: "\u{2029}", with: softBreak)
            }
            output.append(NSAttributedString(string: text, attributes: attributes))
        }
        return output
    }

    /// Reads the blocks back. Duplicate ids (a paste of copied paragraphs) get fresh ones,
    /// reported in `renamed` so the caller can write them back to the text.
    static func blocks(from text: NSAttributedString) -> (blocks: [EditorBlock], renamed: [(range: NSRange, info: BlockInfo)]) {
        let string = text.nsString
        var blocks: [EditorBlock] = []
        var renamed: [(NSRange, BlockInfo)] = []
        var seen = Set<String>()
        var location = 0
        while location < string.length {
            let paragraph = string.paragraphRange(at: location)
            location = NSMaxRange(paragraph)
            var info = text.blockInfo(at: paragraph.location) ?? BlockInfo()
            if seen.contains(info.id) || text.blockInfo(at: paragraph.location) == nil {
                info = info.renamed()
                renamed.append((paragraph, info))
            }
            seen.insert(info.id)
            blocks.append(block(in: text, paragraph: paragraph, info: info))
        }
        return (blocks, renamed)
    }

    /// One paragraph read back as a block.
    static func block(in text: NSAttributedString, paragraph: NSRange, info: BlockInfo) -> EditorBlock {
        let string = text.nsString
        var block = EditorBlock(id: info.id, kind: info.kind, depth: info.depth, checked: info.checked,
                                language: info.language, origin: info.origin?.origin,
                                container: info.kind == .quote ? info.container : nil)
        var content = paragraph
        if content.length > 0, string.character(at: NSMaxRange(content) - 1) == 0x0A { content.length -= 1 }
        if info.kind.hasInlineText {
            block.runs = runs(in: text, range: content)
        } else if let attachment = text.attribute(.attachment, at: paragraph.location, effectiveRange: nil) as? BlockAttachment,
                  let live = attachment.live {
            // Tables and columns hold their edits in the attachment.
            live.write(into: &block)
        }
        return block
    }

    /// The text an inline node shows. Never empty: the label is what carries the node.
    @MainActor static func label(of node: JSONValue, context: AttachmentContext) -> String {
        var label: String
        if let id = NoteLinks.documentTarget(node) {
            label = context.documentTitles[id] ?? ""
            if label.isEmpty { label = "Untitled" }
        } else if let id = NoteLinks.blockTarget(node) {
            label = context.referenceLabel(blockId: id, fallback: "block")
        } else {
            label = BlockCodec.inlineText(node)
            if label.isEmpty { label = node["type"]?.stringValue ?? "•" }
        }
        label = label.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
        if label.count > 80 { label = String(label.prefix(79)) + "…" }
        return label.isEmpty ? "•" : label
    }

    /// A piece of a block's text as the block has it: a stray attachment character
    /// (dropped file icon etc.) carries no text, a "\r" is a soft line break and a soft
    /// separator is the space it stands for (`SoftBreaks`). Nil when nothing is left.
    private static func readable(_ piece: String) -> String? {
        var piece = piece
        if piece.utf16.contains(where: { $0 == 0xFFFC || $0 == 0x0D || $0 == SoftBreaks.character }) {
            piece = piece.replacingOccurrences(of: objectReplacement, with: "").replacingOccurrences(of: "\r", with: softBreak)
                .replacingOccurrences(of: SoftBreaks.separator, with: " ")
        }
        return piece.isEmpty ? nil : piece
    }

    /// The inline runs of `range`. A node is one contiguous run of its box (see
    /// `InlineNodes`): read once however its label is split by other attributes, and
    /// twice when two copies of it sit in the paragraph.
    static func runs(in text: NSAttributedString, range: NSRange) -> [InlineRun] {
        var runs: [InlineRun] = []
        let string = text.nsString
        guard range.length > 0 else { return [] }
        // Plain text (no nodes, no marks: most paragraphs of a paste) is one run.
        var plain = NSRange()
        if text.attribute(.fnNode, at: range.location, longestEffectiveRange: &plain, in: range) == nil, plain == range,
           text.attribute(.fnMarks, at: range.location, longestEffectiveRange: &plain, in: range) == nil, plain == range {
            return Self.readable(string.substring(with: range)).map { [InlineRun($0)] } ?? []
        }
        text.enumerateAttribute(.fnNode, in: range, options: []) { value, segment, _ in
            if let node = value as? NodeBox {
                runs.append(.atomic(node.node))
                return
            }
            text.enumerateAttribute(.fnMarks, in: segment, options: []) { marks, run, _ in
                let piece = Self.readable(string.substring(with: run)) ?? ""
                runs.append(InlineRun(piece, (marks as? MarksBox)?.marks ?? InlineMarks()))
            }
        }
        return BlockCodec.normalized(runs)
    }
}
