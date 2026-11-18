import AppKit

/// Editor blocks ⇄ attributed text. Each block is one paragraph ending in "\n" (the last
/// one too: the final "\n" is never selectable, so every paragraph has a character to
/// carry its `BlockInfo`). Soft line breaks inside a block are U+2028.
@MainActor
enum DocumentText {
    static func build(_ blocks: [EditorBlock], theme: EditorTheme, context: AttachmentContext) -> NSMutableAttributedString {
        let output = NSMutableAttributedString()
        for block in blocks.isEmpty ? [EditorBlock()] : blocks {
            output.append(paragraph(block, context: context))
        }
        theme.restyle(output, range: NSRange(location: 0, length: output.length))
        return output
    }

    /// One block's paragraph, terminator included (unstyled).
    static func paragraph(_ block: EditorBlock, context: AttachmentContext) -> NSAttributedString {
        let info = BlockInfo(block)
        let output = NSMutableAttributedString()
        if block.kind.hasInlineText {
            output.append(inline(block.runs, info: info, context: context))
        } else {
            let attachment = BlockAttachment(info: info, context: context)
            output.append(NSAttributedString(string: objectReplacement, attributes: [.attachment: attachment, .fnBlock: info]))
        }
        output.append(NSAttributedString(string: "\n", attributes: [.fnBlock: info]))
        return output
    }

    static func inline(_ runs: [InlineRun], info: BlockInfo, context: AttachmentContext) -> NSAttributedString {
        let output = NSMutableAttributedString()
        for run in runs {
            var attributes: [NSAttributedString.Key: Any] = [.fnBlock: info]
            if let node = run.node {
                attributes[.fnNode] = NodeBox(node)
                attributes[.attachment] = InlineNodeAttachment(node: node, context: context)
                output.append(NSAttributedString(string: objectReplacement, attributes: attributes))
                continue
            }
            if !run.marks.isPlain { attributes[.fnMarks] = MarksBox(run.marks) }
            output.append(NSAttributedString(string: run.text, attributes: attributes))
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
                info = BlockInfo(kind: info.kind, depth: info.depth, checked: info.checked, language: info.language,
                                 origin: info.origin, newProvenance: info.newProvenance)
                renamed.append((paragraph, info))
            }
            seen.insert(info.id)
            var block = EditorBlock(id: info.id, kind: info.kind, depth: info.depth, checked: info.checked,
                                    language: info.language, origin: info.origin?.origin,
                                    newProvenance: info.newProvenance)
            var content = paragraph
            if content.length > 0, string.character(at: NSMaxRange(content) - 1) == 0x0A { content.length -= 1 }
            if info.kind.hasInlineText {
                block.runs = runs(in: text, range: content)
            }
            blocks.append(block)
        }
        return (blocks, renamed)
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
