import Foundation

/// Copied note text as Markdown, put on the pasteboard next to rich and plain text so a
/// copy pastes well into Markdown editors and code tools (VS Code, GitHub, Obsidian).
/// Mirrors the server's Markdown export for what the editor holds (marks, lists, checks,
/// quotes, code, tables, pictures and files); atomic inline nodes are left out.
enum MarkdownCopy {
    /// The pasteboard type Markdown editors read.
    static let typeIdentifier = "net.daringfireball.markdown"

    static func markdown(_ blocks: [EditorBlock]) -> String {
        var lines: [String] = []
        var numbers: [Int: Int] = [:]  // depth → last number
        var previousKind: BlockKind?
        for block in blocks {
            if block.kind != .numbered { numbers[block.depth] = nil }
            let indent = String(repeating: "  ", count: max(0, block.depth))
            let text = inline(block.runs)
            let line: String
            switch block.kind {
            case .heading1, .heading2, .heading3, .heading4, .heading5, .heading6:
                line = String(repeating: "#", count: block.kind.headingLevel ?? 1) + " " + text
            case .bullet: line = indent + "- " + text
            case .numbered:
                let number = (numbers[block.depth] ?? 0) + 1
                numbers[block.depth] = number
                line = indent + "\(number). " + text
            case .check: line = indent + (block.checked ? "- [x] " : "- [ ] ") + text
            case .quote: line = "> " + text
            case .code:
                let body = block.runs.map(\.text).joined().replacingOccurrences(of: softBreak, with: "\n")
                line = "```" + (block.language ?? "") + "\n" + body + "\n```"
            case .divider: line = "---"
            case .image: line = "![\(name(block))](\(url(block)))"
            case .file: line = "[\(name(block))](\(url(block)))"
            case .table: line = table(block.table)
            case .paragraph: line = text
            case .columns, .embed: line = block.origin?.text ?? ""
            }
            // Blocks are separated by a blank line, except consecutive list items and quote lines.
            if let previousKind, !(previousKind.isList && block.kind.isList), !(previousKind == .quote && block.kind == .quote) {
                lines.append("")
            }
            lines.append(line)
            previousKind = block.kind
        }
        return lines.joined(separator: "\n")
    }

    static func inline(_ runs: [InlineRun]) -> String {
        var out = ""
        for run in runs where run.node == nil {
            var text = run.text.replacingOccurrences(of: softBreak, with: "  \n")
                .replacingOccurrences(of: objectReplacement, with: "")
            guard !text.isEmpty else { continue }
            let marks = run.marks
            if marks.code {
                text = "`" + text + "`"
            } else {
                if marks.bold { text = "**" + text + "**" }
                if marks.italic { text = "*" + text + "*" }
                if marks.strike { text = "~~" + text + "~~" }
            }
            if let link = marks.link { text = "[" + text + "](" + link + ")" }
            out += text
        }
        return out
    }

    private static func name(_ block: EditorBlock) -> String {
        block.origin?.attrs["name"]?.stringValue ?? block.origin?.text ?? ""
    }

    private static func url(_ block: EditorBlock) -> String {
        block.origin?.attrs["url"]?.stringValue ?? ""
    }

    private static func table(_ table: TableContent?) -> String {
        guard let table, let first = table.rows.first else { return "" }
        func row(_ cells: [String]) -> String { "| " + cells.joined(separator: " | ") + " |" }
        let rows = table.rows.map { row in row.cells.map { inline($0.runs).replacingOccurrences(of: "|", with: "\\|") } }
        let width = first.cells.count
        var lines = [row(rows[0]), row(Array(repeating: "---", count: width))]
        lines += rows.dropFirst().map(row)
        return lines.joined(separator: "\n")
    }
}
