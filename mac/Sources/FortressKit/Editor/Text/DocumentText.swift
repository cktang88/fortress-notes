import AppKit

/// Editor blocks ⇄ attributed text. Each block is one paragraph ending in "\n" (the last
/// one too: the final "\n" is never selectable, so every paragraph has a character to
/// carry its `BlockInfo`). Soft line breaks inside a block are U+2028.
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
        if block.kind.hasInlineText {
            output.append(inline(block.runs, info: info, context: context, theme: theme))
        } else {
            let attachment = BlockAttachment(info: info, context: context, block: block)
            var attributes = base(info: info, marks: InlineMarks(), theme: theme)
            attributes[.attachment] = attachment
            output.append(NSAttributedString(string: objectReplacement, attributes: attributes))
        }
        output.append(NSAttributedString(string: "\n", attributes: base(info: info, marks: InlineMarks(), theme: theme)))
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
                var attributes = base(info: info, marks: InlineMarks(), theme: theme)
                attributes[.fnNode] = NodeBox(node)
                attributes[.attachment] = InlineNodeAttachment(node: node, context: context)
                // A note link opens its note when clicked (see `clickedOnLink`).
                if let id = NoteLinks.documentTarget(node), let url = NoteLinks.documentURL(id: id) { attributes[.link] = url }
                output.append(NSAttributedString(string: objectReplacement, attributes: attributes))
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
        let string = text.string as NSString
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
        let string = text.string as NSString
        var block = EditorBlock(id: info.id, kind: info.kind, depth: info.depth, checked: info.checked,
                                language: info.language, origin: info.origin?.origin,
                                newProvenance: info.newProvenance, container: info.kind == .quote ? info.container : nil)
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

    static func runs(in text: NSAttributedString, range: NSRange) -> [InlineRun] {
        var runs: [InlineRun] = []
        let string = text.string as NSString
        text.enumerateAttributes(in: range, options: []) { attributes, run, _ in
            if let node = attributes[.fnNode] as? NodeBox {
                for _ in 0..<run.length { runs.append(.atomic(node.node)) }
                return
            }
            var piece = string.substring(with: run)
            // A stray attachment character (dropped file icon etc.) carries no text.
            piece = piece.replacingOccurrences(of: objectReplacement, with: "")
            piece = piece.replacingOccurrences(of: "\r", with: softBreak)
            let marks = (attributes[.fnMarks] as? MarksBox)?.marks ?? InlineMarks()
            runs.append(InlineRun(piece, marks))
        }
        return BlockCodec.normalized(runs)
    }
}
