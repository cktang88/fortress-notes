import Foundation

/// The small slice of Markdown inline syntax that imported notes contain: emphasis,
/// strikethrough, code, links, images, note links `[[id|Title]]` and block references
/// `(( id "label" ))`. Parsing turns it into runs; rendering writes the Markdown mirror.
public enum InlineMarkdown {
    static let documentLink = try! NSRegularExpression(pattern: #"\[\[([A-Za-z0-9_-]+)(?:\|([^\]]+))?\]\]"#)
    static let blockReference = try! NSRegularExpression(pattern: #"\(\(\s*([A-Za-z0-9_-]+)(?:\s+["']([^"']*)["'])?\s*\)\)"#)

    public static func isDocumentLink(_ href: String) -> Bool {
        documentLinkTarget(href) != nil
    }

    public static func isBlockReference(_ href: String) -> Bool {
        blockReferenceTarget(href) != nil
    }

    /// `[[id|Title]]` → (id, Title).
    public static func documentLinkTarget(_ href: String) -> (id: String, label: String?)? {
        let text = href.trimmingCharacters(in: .whitespaces)
        let range = NSRange(text.startIndex..., in: text)
        guard let match = documentLink.firstMatch(in: text, range: range), match.range == range else { return nil }
        let label = Range(match.range(at: 2), in: text).map { String(text[$0]).trimmingCharacters(in: .whitespaces) }
        return (String(text[Range(match.range(at: 1), in: text)!]), label)
    }

    /// `(( id "label" ))` → (id, label).
    public static func blockReferenceTarget(_ href: String) -> (id: String, label: String?)? {
        let text = href.trimmingCharacters(in: .whitespaces)
        let range = NSRange(text.startIndex..., in: text)
        guard let match = blockReference.firstMatch(in: text, range: range), match.range == range else { return nil }
        let label = Range(match.range(at: 2), in: text).map { String(text[$0]) }
        return (String(text[Range(match.range(at: 1), in: text)!]), label)
    }

    /// The href the backend records a backlink for: `[[id|Title]]`.
    public static func documentLinkHref(id: String, title: String) -> String {
        let label = title.replacingOccurrences(of: #"[|\]]"#, with: " ", options: .regularExpression)
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        return label.isEmpty ? "[[\(id)]]" : "[[\(id)|\(label)]]"
    }

    /// A block reference as a link node (as the web editor writes it).
    public static func blockReferenceNode(blockId: String, text: String) -> JSONValue {
        let clean = text.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        let label = clean.count > 80 ? String(clean.prefix(79)).trimmingCharacters(in: .whitespaces) + "…" : clean
        return .object([
            "type": .string("link"), "href": .string("(( \(blockId) ))"),
            "content": .array([.object(["type": .string("text"), "text": .string(label.isEmpty ? "block" : label), "styles": .object([:])])]),
        ])
    }

    /// Plain text with `[[…]]` and `((…))` turned into links (old notes stored them as text).
    static func linkifyReferences(_ text: String, _ marks: InlineMarks) -> [InlineRun] {
        guard text.contains("[[") || text.contains("((") else { return [InlineRun(text, marks)] }
        var output: [InlineRun] = []
        let ns = text as NSString
        var last = 0
        var matches = documentLink.matches(in: text, range: NSRange(location: 0, length: ns.length))
        matches += blockReference.matches(in: text, range: NSRange(location: 0, length: ns.length))
        matches.sort { $0.range.location < $1.range.location }
        for match in matches where match.range.location >= last {
            if match.range.location > last {
                output.append(InlineRun(ns.substring(with: NSRange(location: last, length: match.range.location - last)), marks))
            }
            let whole = ns.substring(with: match.range)
            if let target = documentLinkTarget(whole) {
                var linked = marks
                linked.link = whole
                output.append(InlineRun(target.label ?? "Untitled note", linked))
            } else if let target = blockReferenceTarget(whole) {
                output.append(.atomic(.object([
                    "type": .string("link"), "href": .string(whole),
                    "content": .array([.object(["type": .string("text"), "text": .string(target.label ?? "block"), "styles": .object([:])])]),
                ])))
            }
            last = match.range.location + match.range.length
        }
        if last < ns.length { output.append(InlineRun(ns.substring(from: last), marks)) }
        return output
    }

    /// `![alt](url)` when that is all the block holds.
    public static func soleImage(_ markdown: String) -> (alt: String, url: String)? {
        let text = markdown.trimmingCharacters(in: .whitespacesAndNewlines)
        let pattern = try! NSRegularExpression(pattern: #"^!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)$"#)
        guard let match = pattern.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let alt = Range(match.range(at: 1), in: text), let url = Range(match.range(at: 2), in: text)
        else { return nil }
        return (String(text[alt]), String(text[url]))
    }

    // MARK: Parsing

    public static func parse(_ source: String) -> [InlineRun] {
        BlockCodec.normalized(parse(Array(source), InlineMarks()))
    }

    private static func parse(_ chars: [Character], _ marks: InlineMarks) -> [InlineRun] {
        var output: [InlineRun] = []
        var buffer = ""
        func flush() {
            if !buffer.isEmpty {
                output.append(contentsOf: marks.code || marks.link != nil ? [InlineRun(buffer, marks)] : linkifyReferences(buffer, marks))
                buffer = ""
            }
        }
        func starts(_ token: String, at index: Int) -> Bool {
            let token = Array(token)
            guard index + token.count <= chars.count else { return false }
            return Array(chars[index..<index + token.count]) == token
        }
        func find(_ token: String, from index: Int) -> Int? {
            var index = index
            while index < chars.count {
                if chars[index] == "\\" { index += 2; continue }
                if starts(token, at: index) { return index }
                index += 1
            }
            return nil
        }
        /// `[text](url)` starting at `index` (the "["): (text, url, end index after ")").
        func bracketLink(at index: Int) -> (text: [Character], url: String, end: Int)? {
            var depth = 0
            var close: Int?
            var cursor = index
            while cursor < chars.count {
                if chars[cursor] == "\\" { cursor += 2; continue }
                if chars[cursor] == "[" { depth += 1 }
                if chars[cursor] == "]" {
                    depth -= 1
                    if depth == 0 { close = cursor; break }
                }
                cursor += 1
            }
            guard let close, close + 1 < chars.count, chars[close + 1] == "(",
                  let end = chars[(close + 2)...].firstIndex(of: ")") else { return nil }
            var url = String(chars[(close + 2)..<end]).trimmingCharacters(in: .whitespaces)
            if let space = url.firstIndex(of: " ") { url = String(url[..<space]) }
            url = url.trimmingCharacters(in: CharacterSet(charactersIn: "<>"))
            return (Array(chars[(index + 1)..<close]), url, end + 1)
        }
        func isWordCharacter(_ index: Int) -> Bool {
            index >= 0 && index < chars.count && (chars[index].isLetter || chars[index].isNumber)
        }

        var index = 0
        while index < chars.count {
            let char = chars[index]
            if char == "\\", index + 1 < chars.count, chars[index + 1].isPunctuation || chars[index + 1].isSymbol {
                buffer.append(chars[index + 1])
                index += 2
                continue
            }
            if char == "`", let end = chars[(index + 1)...].firstIndex(of: "`"), end > index + 1 {
                flush()
                var code = marks
                code.code = true
                output.append(InlineRun(String(chars[(index + 1)..<end]), code))
                index = end + 1
                continue
            }
            if starts("[[", at: index), let end = find("]]", from: index + 2) {
                let whole = String(chars[index..<(end + 2)])
                if documentLinkTarget(whole) != nil {
                    buffer += whole  // linkified on flush
                    index = end + 2
                    continue
                }
            }
            if char == "!", index + 1 < chars.count, chars[index + 1] == "[", let link = bracketLink(at: index + 1) {
                flush()
                var linked = marks
                linked.link = link.url
                let alt = String(link.text)
                output.append(InlineRun(alt.isEmpty ? link.url : alt, linked))
                index = link.end
                continue
            }
            if char == "[", !starts("[[", at: index), let link = bracketLink(at: index) {
                flush()
                if isBlockReference(link.url) {
                    output.append(.atomic(.object([
                        "type": .string("link"), "href": .string(link.url),
                        "content": .array([.object(["type": .string("text"), "text": .string(String(link.text)), "styles": .object([:])])]),
                    ])))
                } else {
                    var linked = marks
                    linked.link = link.url
                    output.append(contentsOf: parse(link.text, linked))
                }
                index = link.end
                continue
            }
            if starts("**", at: index) || starts("__", at: index) {
                let token = String(chars[index..<(index + 2)])
                if let end = find(token, from: index + 2), end > index + 2 {
                    flush()
                    var bold = marks
                    bold.bold = true
                    output.append(contentsOf: parse(Array(chars[(index + 2)..<end]), bold))
                    index = end + 2
                    continue
                }
            }
            if starts("~~", at: index), let end = find("~~", from: index + 2), end > index + 2 {
                flush()
                var strike = marks
                strike.strike = true
                output.append(contentsOf: parse(Array(chars[(index + 2)..<end]), strike))
                index = end + 2
                continue
            }
            if starts("==", at: index), let end = find("==", from: index + 2), end > index + 2 {
                flush()
                var highlight = marks
                highlight.highlight = "yellow"
                output.append(contentsOf: parse(Array(chars[(index + 2)..<end]), highlight))
                index = end + 2
                continue
            }
            if char == "*" || (char == "_" && !isWordCharacter(index - 1)),
               index + 1 < chars.count, !chars[index + 1].isWhitespace {
                var cursor = index + 1
                var end: Int?
                while let candidate = find(String(char), from: cursor) {
                    let doubled = candidate + 1 < chars.count && chars[candidate + 1] == char
                    if doubled { cursor = candidate + 2; continue }
                    if !chars[candidate - 1].isWhitespace, char == "*" || !isWordCharacter(candidate + 1) {
                        end = candidate
                    }
                    break
                }
                if let end, end > index + 1 {
                    flush()
                    var italic = marks
                    italic.italic = true
                    output.append(contentsOf: parse(Array(chars[(index + 1)..<end]), italic))
                    index = end + 1
                    continue
                }
            }
            if starts("<u>", at: index), let end = find("</u>", from: index + 3) {
                flush()
                var underline = marks
                underline.underline = true
                output.append(contentsOf: parse(Array(chars[(index + 3)..<end]), underline))
                index = end + 4
                continue
            }
            if starts("<sup>", at: index), let end = find("</sup>", from: index + 5) {
                flush()
                var raised = marks
                raised.sup = true
                output.append(contentsOf: parse(Array(chars[(index + 5)..<end]), raised))
                index = end + 6
                continue
            }
            if starts("<sub>", at: index), let end = find("</sub>", from: index + 5) {
                flush()
                var lowered = marks
                lowered.sub = true
                output.append(contentsOf: parse(Array(chars[(index + 5)..<end]), lowered))
                index = end + 6
                continue
            }
            buffer.append(char)
            index += 1
        }
        flush()
        return output
    }

    // MARK: Rendering

    /// Runs as Markdown (lossy for colors and underline, like the web editor's export).
    public static func render(_ runs: [InlineRun]) -> String {
        var output = ""
        var index = 0
        let runs = BlockCodec.normalized(runs)
        while index < runs.count {
            let run = runs[index]
            if let node = run.node {
                if let href = node["href"]?.stringValue {
                    output += isBlockReference(href) ? href : "[\(BlockCodec.inlineText(node))](\(href))"
                } else {
                    output += BlockCodec.inlineText(node)
                }
                index += 1
                continue
            }
            if let href = run.marks.link {
                var group: [InlineRun] = []
                while index < runs.count, runs[index].node == nil, runs[index].marks.link == href {
                    var inner = runs[index]
                    inner.marks.link = nil
                    group.append(inner)
                    index += 1
                }
                if isDocumentLink(href) {
                    output += href
                } else {
                    output += "[\(render(group))](\(href))"
                }
                continue
            }
            output += styled(run.text.replacingOccurrences(of: softBreak, with: "\n"), run.marks)
            index += 1
        }
        return output
    }

    private static func styled(_ text: String, _ marks: InlineMarks) -> String {
        if marks.code { return "`\(text)`" }
        // Keep the whitespace outside the markers, or the Markdown wouldn't parse back.
        let core = text.trimmingCharacters(in: .whitespaces)
        guard !core.isEmpty else { return text }
        let leading = String(text.prefix { $0 == " " })
        let trailing = String(text.reversed().prefix { $0 == " " })
        var result = escape(core)
        if marks.sup { result = "<sup>\(result)</sup>" }
        if marks.sub { result = "<sub>\(result)</sub>" }
        if marks.strike { result = "~~\(result)~~" }
        if marks.italic { result = "*\(result)*" }
        if marks.bold { result = "**\(result)**" }
        return leading + result + trailing
    }

    private static func escape(_ text: String) -> String {
        var output = ""
        for char in text {
            if "*_`~[]\\".contains(char) { output.append("\\") }
            output.append(char)
        }
        return output
    }
}
