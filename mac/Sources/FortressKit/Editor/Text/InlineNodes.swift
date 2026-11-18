import AppKit

/// Note links (`docLink`) and block references (`blockRef`) in the text. Each shows its
/// label (the note's current title, the block's current text) as ordinary characters
/// carrying one `NodeBox`, so it draws, wraps and selects like text; these rules make
/// that run behave as one unit, like an attachment:
///
/// - the caret never stops inside a label (it steps over it), and a selection takes
///   whole labels;
/// - an edit that cuts into a label changes the whole node (Delete removes it), and
///   typing never lands inside one (it goes after it);
/// - a node is one contiguous run of its box, read back once per run, so two copies of
///   the same link in a paragraph are two nodes (`DocumentText.runs`), and a copy put
///   right next to its original gets a box of its own (`separateCopies`).
@MainActor
enum InlineNodes {
    /// The node run containing `index`: the characters around it carrying the same box.
    static func range(at index: Int, in text: NSAttributedString) -> NSRange? {
        guard index >= 0, index < text.length else { return nil }
        let paragraph = (text.string as NSString).paragraphRange(for: NSRange(location: index, length: 0))
        var range = NSRange()
        guard text.attribute(.fnNode, at: index, longestEffectiveRange: &range, in: paragraph) != nil else { return nil }
        return range
    }

    /// A node run that `index` falls strictly inside (not at its start).
    private static func cut(at index: Int, in text: NSAttributedString) -> NSRange? {
        guard let node = range(at: index, in: text), node.location < index else { return nil }
        return node
    }

    /// Where a selection may go: a caret inside a label moves to the label's edge (the
    /// one it was heading for, or the nearer one after a click); a selection's ends move
    /// to the edges of the labels they fall in (outwards when it grows, inwards when it
    /// shrinks, so ⇧← can give a label back).
    static func snappedSelection(_ new: NSRange, from old: NSRange, in text: NSAttributedString) -> NSRange {
        guard text.length > 0, new.location != NSNotFound else { return new }
        if new.length == 0 {
            guard let node = cut(at: new.location, in: text) else { return new }
            let start = node.location, end = NSMaxRange(node)
            let mouse: [NSEvent.EventType] = [.leftMouseDown, .leftMouseDragged, .leftMouseUp]
            let byMouse = NSApp?.currentEvent.map { mouse.contains($0.type) } ?? false
            if !byMouse, new.location != old.location {
                // Arrowing (or a word step) into it: through to the side it was heading for.
                return NSRange(location: new.location > old.location ? end : start, length: 0)
            }
            let nearer = new.location - start <= end - new.location ? start : end
            return NSRange(location: nearer, length: 0)
        }
        var start = new.location
        var end = NSMaxRange(new)
        if let node = cut(at: start, in: text) {
            start = start > old.location && old.length > 0 ? NSMaxRange(node) : node.location
        }
        if end < text.length, let node = cut(at: end, in: text) {
            end = end < NSMaxRange(old) && old.length > 0 ? node.location : NSMaxRange(node)
        }
        if end < start { end = start }
        return NSRange(location: start, length: end - start)
    }

    /// The range an edit of `range` really changes: grown to whole nodes when it cuts
    /// into one; an insertion inside a label goes right after it.
    static func wholeNodes(_ range: NSRange, in text: NSAttributedString) -> NSRange {
        if range.length == 0 {
            if let node = cut(at: range.location, in: text) { return NSRange(location: NSMaxRange(node), length: 0) }
            return range
        }
        var start = range.location
        var end = NSMaxRange(range)
        if let node = cut(at: start, in: text) { start = node.location }
        if end < text.length, let node = cut(at: end, in: text) { end = NSMaxRange(node) }
        return NSRange(location: start, length: end - start)
    }

    /// After characters were inserted in `range`: a whole label sharing its box with
    /// the text next to it is a copy of that node (pasted beside its original, or put
    /// there by a program), and gets a box of its own so it reads back as a second node.
    /// A piece of a label (the edit cut through one) is left alone.
    static func separateCopies(in text: NSMutableAttributedString, range: NSRange, label: (JSONValue) -> String) {
        let range = NSIntersectionRange(range, NSRange(location: 0, length: text.length))
        guard range.length > 0 else { return }
        let string = text.string as NSString
        var copies: [(NSRange, NodeBox)] = []
        text.enumerateAttribute(.fnNode, in: range, options: []) { value, run, _ in
            guard let box = value as? NodeBox else { return }
            let paragraph = string.paragraphRange(for: run)
            var whole = NSRange()
            _ = text.attribute(.fnNode, at: run.location, longestEffectiveRange: &whole, in: paragraph)
            guard whole != run else { return }
            // Copies split off one at a time: the inserted run can hold several.
            let shown = label(box.node) as NSString
            guard shown.length > 0, run.length % shown.length == 0 else { return }
            var location = run.location
            while location < NSMaxRange(run) {
                let piece = NSRange(location: location, length: shown.length)
                guard string.substring(with: piece) == shown as String else { return }
                location += shown.length
            }
            location = run.location
            // The original is outside the inserted run: every label in it is a new node.
            while location < NSMaxRange(run) {
                copies.append((NSRange(location: location, length: shown.length), NodeBox(box.node)))
                location += shown.length
            }
        }
        for (piece, box) in copies { text.addAttribute(.fnNode, value: box, range: piece) }
    }
}
