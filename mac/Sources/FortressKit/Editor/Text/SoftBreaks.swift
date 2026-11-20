import AppKit

/// Soft paragraphs: a long block shown as several paragraphs of the text that read back
/// as one block.
///
/// TextKit 2 makes one layout fragment per paragraph and lays it out whole: every key
/// typed in a 50,000-character paragraph laid all of it out again (~15 ms in a bare
/// TextKit 2 view, `keystroke.long50k.part.bareTextKitRelayout`). Neither
/// its layout manager's settings nor serving the paragraph in pieces through the content
/// manager's delegate split that work, so the text splits it: in a long paragraph block,
/// a space every ~1,500 characters (after a sentence where there is one) is stored as
/// U+2029 PARAGRAPH SEPARATOR marked `.fnSoft`. TextKit 2 sees short paragraphs and lays
/// out only the one being typed in; everything that reads the text sees the space:
///
/// - the block is the paragraphs up to a "\n" (`NSString.paragraphRange(at:)` steps over
///   soft separators), and reading it back gives a space (`DocumentText.runs`), so
///   saves, the block diff, Markdown and the blocks on the pasteboard never see one;
/// - one character stands for one character: offsets, the caret, selection, undo and
///   block anchors are the same as with the space, and the caret steps over it as over
///   the space where a line wraps;
/// - copied and dragged text has the space (`FortressTextView.writeSelection`);
/// - soft paragraphs share their block's look: no paragraph spacing between them
///   (`Role`), and only `.paragraph` blocks have them (nothing is drawn per paragraph).
///
/// What shows: a long paragraph's line ends early where a soft paragraph ends (after a
/// sentence), about every 1,500 characters. What doesn't work: Find (and the text view's
/// `string`, which Services and accessibility read) sees the U+2029, so a phrase typed
/// with the space doesn't match across that one place. (Overriding `string` copied the
/// whole note on every keystroke: NSTextView reads it a lot.)
///
/// Soft separators are put in when a block's text is built (`DocumentText.paragraph`) and
/// kept right as the text is edited (`repair`, from normalizing): one too close to a
/// line break or another separator, or in a block of another kind, becomes a space again,
/// and a soft paragraph grown past `longest` (typing, a paste) is split. Both swap one
/// character for one, so NSTextView's undo steps stay valid.
enum SoftBreaks {
    static let character: unichar = 0x2029
    static let separator = "\u{2029}"
    /// A soft paragraph longer than this is split (and a block shorter has none).
    static let longest = 3_000
    /// About how long the soft paragraphs of a split are.
    static let target = 1_500

    /// The kinds of block shown as soft paragraphs (ones drawn with no marker or bar).
    static func eligible(_ kind: BlockKind) -> Bool { kind == .paragraph }

    /// Where a soft paragraph sits in its block (it decides the paragraph spacing).
    struct Role: OptionSet, Hashable, Sendable {
        let rawValue: UInt8
        /// Not the block's first soft paragraph: no spacing before it.
        static let continuation = Role(rawValue: 1)
        /// Not the block's last: no spacing after it.
        static let continued = Role(rawValue: 2)
    }

    /// The soft paragraph holding `index` (TextKit's paragraph: up to and including the
    /// next U+2029 or "\n") and where it sits in its block. Only reads that paragraph.
    static func piece(at index: Int, in string: NSString) -> (range: NSRange, role: Role) {
        let range = string.paragraphRange(for: NSRange(location: min(max(0, index), max(0, string.length - 1)), length: 0))
        var role: Role = []
        if range.location > 0, string.character(at: range.location - 1) == character { role.insert(.continuation) }
        if range.length > 0, string.character(at: NSMaxRange(range) - 1) == character { role.insert(.continued) }
        return (range, role)
    }

    /// The soft paragraphs of a block's `paragraph` (`paragraph` itself when it has none);
    /// with `near`, only the ones touching it or next to it (an edit changes no others).
    static func pieces(of paragraph: NSRange, near: NSRange? = nil, in string: NSString) -> [(range: NSRange, role: Role)] {
        guard paragraph.length > 0 else { return [(paragraph, [])] }
        let end = NSMaxRange(paragraph)
        let start = near.map { min(max(paragraph.location, $0.location - 1), end - 1) } ?? paragraph.location
        let stop = near.map { min(end, NSMaxRange($0) + 1) } ?? end
        var output: [(range: NSRange, role: Role)] = []
        var location = start
        repeat {
            let piece = piece(at: location, in: string)
            output.append(piece)
            location = NSMaxRange(piece.range)
        } while location < stop && location < end
        return output
    }

    /// The role of the soft paragraph that text typed at `location` goes into.
    static func role(at location: Int, in string: NSString) -> Role {
        piece(at: location, in: string).role
    }

    /// `text` with every soft separator a space again (what copying and reading see).
    static func plain(_ text: NSAttributedString) -> NSAttributedString {
        guard text.nsString.range(of: separator, options: .literal).location != NSNotFound else { return text }
        let copy = NSMutableAttributedString(attributedString: text)
        copy.mutableString.replaceOccurrences(of: separator, with: " ", options: .literal, range: NSRange(location: 0, length: copy.length))
        copy.removeAttribute(.fnSoft, range: NSRange(location: 0, length: copy.length))
        return copy
    }

    static func plain(_ string: String) -> String {
        string.contains(separator) ? string.replacingOccurrences(of: separator, with: " ") : string
    }

    // MARK: Splitting

    /// Splits the soft paragraphs of `content` (a block's text, without its "\n") longer
    /// than `longest` (with `near`, only the ones touching it). The range of what changed.
    @discardableResult
    static func split(_ text: NSMutableAttributedString, content: NSRange, near: NSRange? = nil) -> NSRange? {
        guard content.length > longest else { return nil }
        let string = text.nsString
        let end = NSMaxRange(content)
        var changed: NSRange?
        var start = max(content.location, piece(at: near.map { max(content.location, $0.location - 1) } ?? content.location, in: string).range.location)
        let stop = near.map { min(end, NSMaxRange($0) + 1) } ?? end
        while start < stop {
            let next = NSMaxRange(piece(at: start, in: string).range)
            // Its text: without its separator, or the block's "\n".
            var pieceEnd = min(next, end)
            if pieceEnd == next, pieceEnd > start, string.character(at: pieceEnd - 1) == character { pieceEnd -= 1 }
            var from = start
            if pieceEnd - start > longest {
                // Pieces of about `target`, the last one at least half that.
                while pieceEnd - from > target + target / 2 {
                    let low = from + target / 2, high = min(pieceEnd - target / 2, from + target + target / 2)
                    if let point = breakPoint(near: from + target, low: low, high: high, in: text, content: content) {
                        let range = NSRange(location: point, length: 1)
                        text.replaceCharacters(in: range, with: separator)
                        text.addAttribute(.fnSoft, value: true, range: range)
                        changed = changed.map { NSUnionRange($0, range) } ?? range
                        from = point + 1
                    } else {
                        // No space to break at here (a long URL, base64): look further on.
                        from = high
                    }
                }
            }
            guard next > start else { break }
            start = next
        }
        return changed
    }

    /// The space nearest `ideal` in `low...high` that can become a separator, preferring
    /// one after the end of a sentence.
    private static func breakPoint(near ideal: Int, low: Int, high: Int, in text: NSAttributedString, content: NSRange) -> Int? {
        let string = text.nsString
        guard low <= high else { return nil }
        let reach = max(ideal - low, high - ideal)
        for sentence in [true, false] {
            for step in 0...(2 * reach) {
                // ideal, ideal - 1, ideal + 1, ideal - 2, …
                let index = step % 2 == 0 ? ideal + step / 2 : ideal - (step + 1) / 2
                guard index >= low, index <= high else { continue }
                guard string.character(at: index) == 0x20, canSeparate(at: index, in: string, content: content, strict: true) else { continue }
                if sentence {
                    let before = string.character(at: index - 1)
                    guard before == 0x2E || before == 0x21 || before == 0x3F else { continue }
                }
                if text.attribute(.fnNode, at: index, effectiveRange: nil) != nil || text.attribute(.attachment, at: index, effectiveRange: nil) != nil {
                    continue
                }
                return index
            }
        }
        return nil
    }

    /// Whether a separator at `index` makes no empty or blank-led line: it has text on
    /// both sides in its block, and no separator or line break next to it (`strict`: no
    /// space either, for the ones put in).
    private static func canSeparate(at index: Int, in string: NSString, content: NSRange, strict: Bool) -> Bool {
        guard index > content.location, index + 1 < NSMaxRange(content) else { return false }
        func blocks(_ unit: unichar) -> Bool {
            unit == character || unit == 0x2028 || unit == 0x0A || unit == 0x0D || unit == 0xFFFC || (strict && unit == 0x20)
        }
        return !blocks(string.character(at: index - 1)) && !blocks(string.character(at: index + 1))
    }

    // MARK: Keeping them right while editing

    /// Fixes the soft separators of one block's paragraph after an edit around `edited`:
    /// `.fnSoft` only on separators (typing after one copies its attributes), a separator
    /// that no longer fits becomes a space again, and a soft paragraph grown too long is
    /// split. The range of the characters it changed (their soft paragraphs need
    /// restyling), nil when none. Reads only the soft paragraphs around the edit.
    @discardableResult
    static func repair(_ text: NSMutableAttributedString, paragraph: NSRange, kind: BlockKind, edited: NSRange) -> NSRange? {
        let string = text.nsString
        guard paragraph.length > 0 else { return nil }
        var content = paragraph
        if string.character(at: NSMaxRange(content) - 1) == 0x0A { content.length -= 1 }
        var changed: NSRange?
        // Only around the edit can a separator have gone wrong (a new kind edits the whole
        // block); in a block of another kind, every separator goes.
        let low = max(paragraph.location, edited.location - 1), high = min(NSMaxRange(paragraph), NSMaxRange(edited) + 1)
        var checked = NSRange(location: low, length: max(0, high - low))
        if !eligible(kind) {
            guard NSMaxRange(piece(at: paragraph.location, in: string).range) < NSMaxRange(paragraph) else { return nil }
            checked = paragraph
        }
        if checked.length > 0 {
            // Every U+2029 left is marked (normalizing made the others "\n"): the marked
            // runs are where they are.
            var stray: [NSRange] = []
            var separators: [Int] = []
            text.enumerateAttribute(.fnSoft, in: checked, options: []) { value, run, _ in
                guard value != nil else { return }
                if run.length == 1, string.character(at: run.location) == character {
                    separators.append(run.location)
                } else {
                    stray.append(run)
                }
            }
            for run in stray {
                text.removeAttribute(.fnSoft, range: run)
                for index in run.location..<NSMaxRange(run) where string.character(at: index) == character {
                    text.addAttribute(.fnSoft, value: true, range: NSRange(location: index, length: 1))
                    separators.append(index)
                }
            }
            for index in separators.sorted() {
                if !eligible(kind) || !canSeparate(at: index, in: string, content: content, strict: false) {
                    let range = NSRange(location: index, length: 1)
                    text.replaceCharacters(in: range, with: " ")
                    text.removeAttribute(.fnSoft, range: range)
                    changed = changed.map { NSUnionRange($0, range) } ?? range
                }
            }
        }
        if eligible(kind), let split = split(text, content: content, near: checked) {
            changed = changed.map { NSUnionRange($0, split) } ?? split
        }
        return changed
    }
}
