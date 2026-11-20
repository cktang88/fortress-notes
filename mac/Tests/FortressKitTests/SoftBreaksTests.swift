import AppKit
import Testing

@testable import FortressKit

// A long paragraph block is stored as several soft paragraphs (`SoftBreaks`): some of
// its spaces are U+2029 so TextKit 2 lays out ~1,500 characters per keystroke, not the
// whole block. Everything that reads the note must still see one block with its text
// unchanged: the save, the block diff, Markdown, the clipboard, the caret and undo.

@MainActor
@Suite(.serialized)
struct SoftBreaksTests {
    static let sentences = [
        "The quick brown fox jumps over the lazy dog while the editor keeps every block in step. ",
        "Does it wrap well at the edge of the measure? ",
        "It does! ",
        "Some words run on without any full stop at all for a while and then ",
    ]

    /// About `length` characters of prose.
    static func prose(_ length: Int) -> String {
        var text = ""
        var index = 0
        while text.utf16.count < length {
            text += sentences[index % sentences.count]
            index += 1
        }
        return text.trimmingCharacters(in: .whitespaces)
    }

    /// A short block, a long one with a bold run and a link in it, and a short one.
    static func blocks(_ length: Int = 20_000) -> [EditorBlock] {
        let long = prose(length)
        let middle = long.utf16.count / 2
        let head = String(long.utf16.prefix(middle))!
        let tail = String(long.utf16.dropFirst(middle))!
        return [
            EditorBlock(id: "a", runs: [InlineRun("First block.")]),
            EditorBlock(id: "long", runs: [
                InlineRun(head), InlineRun("bold words here", InlineMarks(bold: true)), InlineRun(" and "),
                InlineRun("a link", InlineMarks(link: "https://example.com")), InlineRun(" " + tail),
            ]),
            EditorBlock(id: "c", runs: [InlineRun("Last block.")]),
        ]
    }

    static func plainText(_ block: EditorBlock) -> String { block.runs.map(\.text).joined() }

    /// Where the soft separators are in the text.
    static func separators(_ harness: UndoHarness) -> [Int] {
        let string = harness.controller.storage.nsString
        return (0..<string.length).filter { string.character(at: $0) == SoftBreaks.character }
    }

    static func hasSeparator(_ text: String) -> Bool { text.contains(SoftBreaks.separator) }

    /// Reads every block back from the text (not the blocks kept from loading).
    static func readBack(_ harness: UndoHarness) -> [EditorBlock] {
        harness.controller.discardReadCache()
        return harness.controller.currentBlocks()
    }

    @Test func aLongParagraphIsSoftParagraphsThatReadBackAsTheBlock() throws {
        let blocks = Self.blocks()
        let harness = UndoHarness(blocks)
        let separators = Self.separators(harness)
        // ~20,000 characters in ~1,500-character soft paragraphs.
        #expect(separators.count >= 8 && separators.count <= 16, "\(separators.count) separators")
        let long = try #require(harness.controller.range(ofBlock: "long"))
        for index in separators {
            #expect(NSLocationInRange(index, long))
            #expect(harness.controller.storage.attribute(.fnSoft, at: index, effectiveRange: nil) != nil)
        }
        // Mostly after a sentence.
        let string = harness.controller.storage.nsString
        #expect(separators.filter { [0x2E, 0x21, 0x3F].contains(string.character(at: $0 - 1)) }.count >= separators.count - 2)
        // Three blocks, the long one whole: same ids, runs, marks and text.
        #expect(Self.readBack(harness) == blocks)
        let (read, renamed) = DocumentText.blocks(from: harness.controller.storage)
        #expect(read == blocks && renamed.isEmpty)
        #expect(harness.controller.blockIds() == ["a", "long", "c"])
        #expect(harness.controller.paragraph(at: NSMaxRange(long) - 2) == long)
        #expect(harness.controller.paragraph(at: separators[3] + 1) == long)
        // Nothing to save, and what would be saved has no separator.
        #expect(BlockDiff.operations(from: SyncedTree(blocks), to: Self.readBack(harness)).operations.isEmpty)
        for block in Self.readBack(harness) {
            let fields = BlockCodec.encode(block)
            #expect(!Self.hasSeparator(String(decoding: try JSONEncoder().encode(fields.content), as: UTF8.self)))
            #expect(!Self.hasSeparator(String(decoding: try JSONEncoder().encode(fields.content), as: UTF8.self).replacingOccurrences(of: "\\u2029", with: SoftBreaks.separator)))
        }
        #expect(!Self.hasSeparator(MarkdownCopy.markdown(Self.readBack(harness))))
        #expect(MarkdownCopy.markdown(Self.readBack(harness)) == MarkdownCopy.markdown(blocks))
        // Soft paragraphs have no spacing between them: the block looks like one paragraph.
        let first = try #require(harness.controller.storage.attribute(.paragraphStyle, at: long.location, effectiveRange: nil) as? NSParagraphStyle)
        let middle = try #require(harness.controller.storage.attribute(.paragraphStyle, at: separators[2] + 1, effectiveRange: nil) as? NSParagraphStyle)
        let last = try #require(harness.controller.storage.attribute(.paragraphStyle, at: NSMaxRange(long) - 1, effectiveRange: nil) as? NSParagraphStyle)
        let plain = EditorTheme().paragraphStyle(for: BlockInfo())
        #expect(first.paragraphSpacingBefore == plain.paragraphSpacingBefore && first.paragraphSpacing == 0)
        #expect(middle.paragraphSpacingBefore == 0 && middle.paragraphSpacing == 0)
        #expect(last.paragraphSpacingBefore == 0 && last.paragraphSpacing == plain.paragraphSpacing)
        // A short block has none, and neither has a long block of another kind.
        let heading = UndoHarness([EditorBlock(id: "h", kind: .heading2, runs: [InlineRun(Self.prose(8_000))])])
        #expect(Self.separators(heading).isEmpty)
    }

    @Test func typingSavesTheBlockWithoutSeparators() throws {
        let blocks = Self.blocks()
        let harness = UndoHarness(blocks)
        let separators = Self.separators(harness)
        // Right after a separator (the start of a soft paragraph's line), and right before one.
        harness.select(NSRange(location: separators[4] + 1, length: 0))
        harness.type("XY")
        harness.select(NSRange(location: separators[2], length: 0))
        harness.type("Z")
        let read = Self.readBack(harness)
        #expect(read.map(\.id) == ["a", "long", "c"])
        var expected = Self.plainText(blocks[1]) as NSString
        let start = try #require(harness.controller.range(ofBlock: "long")).location
        expected = expected.replacingCharacters(in: NSRange(location: separators[4] + 1 - start, length: 0), with: "XY") as NSString
        expected = expected.replacingCharacters(in: NSRange(location: separators[2] - start, length: 0), with: "Z") as NSString
        #expect(Self.plainText(read[1]) == expected as String)
        #expect(!Self.hasSeparator(Self.plainText(read[1])))
        // The save: one update of the long block, with no separator in it.
        let operations = BlockDiff.operations(from: SyncedTree(blocks), to: read).operations
        #expect(operations.count == 1)
        let body = String(decoding: try JSONEncoder().encode(operations), as: UTF8.self)
        #expect(!Self.hasSeparator(body) && !body.contains("\\u2029"))
        // Undo takes both edits back: the text is the loaded one again.
        harness.undo()
        harness.undo()
        #expect(Self.readBack(harness) == blocks)
        harness.redo()
        harness.redo()
        #expect(Self.plainText(Self.readBack(harness)[1]) == expected as String)
    }

    @Test func caretAndSelectionCrossASoftBoundaryAsOverASpace() throws {
        let harness = UndoHarness(Self.blocks())
        let separators = Self.separators(harness)
        let at = separators[3]
        // → and ← step over it one character at a time, as over a space.
        harness.select(NSRange(location: at, length: 0))
        harness.command(#selector(NSResponder.moveRight(_:)))
        #expect(harness.view.selectedRange() == NSRange(location: at + 1, length: 0))
        harness.command(#selector(NSResponder.moveRight(_:)))
        #expect(harness.view.selectedRange() == NSRange(location: at + 2, length: 0))
        harness.command(#selector(NSResponder.moveLeft(_:)))
        harness.command(#selector(NSResponder.moveLeft(_:)))
        #expect(harness.view.selectedRange() == NSRange(location: at, length: 0))
        harness.select(NSRange(location: at - 1, length: 0))
        for _ in 0..<3 { harness.command(#selector(NSResponder.moveRightAndModifySelection(_:))) }
        #expect(harness.view.selectedRange() == NSRange(location: at - 1, length: 3))
        // ↓ from the line before the boundary goes to the next line, not past it.
        harness.select(NSRange(location: at - 5, length: 0))
        harness.command(#selector(NSResponder.moveDown(_:)))
        let below = harness.view.selectedRange().location
        #expect(below > at && below < at + 200, "↓ went from \(at - 5) to \(below)")
        // Selection across it and Copy: the space, in every flavour on the pasteboard.
        let range = NSRange(location: at - 20, length: 40)
        let expected = (harness.controller.storage.nsString.substring(with: range) as NSString)
            .replacingOccurrences(of: SoftBreaks.separator, with: " ")
        harness.select(range)
        NSPasteboard.general.clearContents()
        harness.view.copy(nil)
        let pasteboard = NSPasteboard.general
        #expect(pasteboard.string(forType: .string) == expected)
        let rtf = try #require(pasteboard.data(forType: .rtf).flatMap { NSAttributedString(rtf: $0, documentAttributes: nil) })
        #expect(rtf.string == expected)
        let markdown = try #require(pasteboard.string(forType: BlockEditorController.markdownPasteboardType))
        #expect(!Self.hasSeparator(markdown) && !markdown.isEmpty)
        let blocksData = try #require(pasteboard.data(forType: BlockEditorController.blocksPasteboardType))
        let blocksJSON = String(decoding: blocksData, as: UTF8.self)
        #expect(!Self.hasSeparator(blocksJSON) && !blocksJSON.contains("\\u2029"))
        // ⌘A and Copy: the note's text with spaces.
        harness.send("selectAll:")
        NSPasteboard.general.clearContents()
        harness.view.copy(nil)
        let all = try #require(NSPasteboard.general.string(forType: .string))
        #expect(!Self.hasSeparator(all))
        #expect(all.hasPrefix("First block.\n") && all.contains(String(Self.prose(20_000).prefix(3_000))))
        // The view's text (Find, Services, accessibility) has the spaces too.
        #expect(!Self.hasSeparator(harness.view.string))
        #expect(harness.view.string == harness.controller.storage.string.replacingOccurrences(of: SoftBreaks.separator, with: " "))
        // Pasting what was copied gives the same text back.
        harness.select(NSRange(location: harness.controller.range(ofBlock: "c")!.location, length: 0))
        harness.paste(expected, plain: true)
        #expect(Self.plainText(Self.readBack(harness)[2]).hasPrefix(expected))
    }

    @Test func findMatchesTextAcrossASoftBoundary() throws {
        let harness = UndoHarness(Self.blocks())
        let at = Self.separators(harness)[2]
        let string = harness.controller.storage.nsString
        let range = NSRange(location: at - 6, length: 12)
        let needle = (string.substring(with: range) as NSString).replacingOccurrences(of: SoftBreaks.separator, with: " ")
        harness.select(NSRange(location: 0, length: 0))
        let find = NSPasteboard(name: .find)
        find.clearContents()
        find.setString(needle, forType: .string)
        let item = NSMenuItem()
        item.tag = NSTextFinder.Action.nextMatch.rawValue
        harness.view.performTextFinderAction(item)
        settleRunLoop(0.2)
        #expect(harness.view.selectedRange() == range, "searched for \(needle.debugDescription)")
    }

    @Test func editsAtASoftBoundaryKeepTheTextRight() throws {
        let blocks = Self.blocks()
        let harness = UndoHarness(blocks)
        var separators = Self.separators(harness)
        let original = Self.plainText(blocks[1])
        let start = try #require(harness.controller.range(ofBlock: "long")).location
        // Return at the start of a soft paragraph's line: two blocks, the space ending the first.
        let at = separators[3]
        harness.select(NSRange(location: at + 1, length: 0))
        harness.command(#selector(NSResponder.insertNewline(_:)))
        var read = Self.readBack(harness)
        #expect(read.count == 4)
        #expect(Self.plainText(read[1]) + Self.plainText(read[2]) == original)
        #expect(Self.plainText(read[1]).hasSuffix(" "))
        // No separator is left next to a line break (it would show an empty line).
        let string = harness.controller.storage.nsString
        for index in Self.separators(harness) {
            #expect(![0x0A, 0x2028, SoftBreaks.character].contains(string.character(at: index - 1)))
            #expect(![0x0A, 0x2028, SoftBreaks.character].contains(string.character(at: index + 1)))
        }
        harness.undo()
        #expect(Self.readBack(harness) == blocks)

        // Return right before one: the second block doesn't start with a separator.
        separators = Self.separators(harness)
        harness.select(NSRange(location: separators[5], length: 0))
        harness.command(#selector(NSResponder.insertNewline(_:)))
        read = Self.readBack(harness)
        #expect(Self.plainText(read[1]) + "\n" + Self.plainText(read[2]) == (original as NSString).replacingCharacters(
            in: NSRange(location: separators[5] - start, length: 0), with: "\n"))
        let second = try #require(harness.controller.range(ofBlock: read[2].id))
        #expect(harness.controller.storage.nsString.character(at: second.location) != SoftBreaks.character)
        harness.undo()
        #expect(Self.readBack(harness) == blocks)

        // Deleting the text between two separators leaves one, never two in a row.
        separators = Self.separators(harness)
        harness.select(NSRange(location: separators[1] + 1, length: separators[2] - separators[1] - 1))
        harness.command(#selector(NSResponder.deleteBackward(_:)))
        let after = Self.separators(harness)
        #expect(zip(after, after.dropFirst()).allSatisfy { $1 - $0 > 1 })
        harness.undo()
        #expect(Self.readBack(harness) == blocks)

        // Backspace at the start of a soft paragraph's line deletes the space it stands for.
        separators = Self.separators(harness)
        harness.select(NSRange(location: separators[0] + 1, length: 0))
        harness.command(#selector(NSResponder.deleteBackward(_:)))
        read = Self.readBack(harness)
        #expect(read.count == 3)
        #expect(Self.plainText(read[1]) == (original as NSString).replacingCharacters(in: NSRange(location: separators[0] - start, length: 1), with: ""))
        harness.undo()
        #expect(Self.readBack(harness) == blocks)

        // Another kind: the separators become spaces (headings draw nothing per paragraph,
        // but lists and quotes do); the text is the same.
        harness.select(NSRange(location: start + 10, length: 0))
        harness.controller.turnInto(.kind(.bullet))
        settleRunLoop()
        #expect(Self.separators(harness).isEmpty)
        read = Self.readBack(harness)
        #expect(read[1].kind == .bullet && read[1].runs == blocks[1].runs)
        harness.undo()
        read = Self.readBack(harness)
        #expect(read[1].kind == .paragraph && read[1].runs == blocks[1].runs)
        // And back to text: soft paragraphs again.
        harness.controller.turnInto(.kind(.bullet))
        settleRunLoop()
        harness.controller.turnInto(.kind(.paragraph))
        settleRunLoop()
        #expect(Self.separators(harness).count >= 8)
        #expect(Self.readBack(harness) == blocks)
    }

    @Test func pastedLongTextIsSplitAndForeignSeparatorsStillSplitBlocks() throws {
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun("Start. ")])])
        harness.select(NSRange(location: 7, length: 0))
        let long = Self.prose(10_000)
        harness.paste(long, plain: true)
        #expect(Self.separators(harness).count >= 4)
        var read = Self.readBack(harness)
        #expect(read.count == 1 && Self.plainText(read[0]) == "Start. " + long)
        harness.undo()
        #expect(Self.plainText(Self.readBack(harness)[0]) == "Start. ")
        #expect(Self.separators(harness).isEmpty)
        // A U+2029 from elsewhere (Word, a web page) is still a paragraph break.
        harness.select(NSRange(location: 7, length: 0))
        harness.view.insertText("one\u{2029}two", replacementRange: NSRange(location: 7, length: 0))
        settleRunLoop()
        read = Self.readBack(harness)
        #expect(read.map(Self.plainText) == ["Start. one", "two"])
        #expect(Self.separators(harness).isEmpty)
    }

    @Test func aBlockGrownPastTheLimitIsSplit() throws {
        let short = Self.prose(2_900)
        let harness = UndoHarness([EditorBlock(id: "a", runs: [InlineRun(short)])])
        #expect(Self.separators(harness).isEmpty)
        harness.select(NSRange(location: (short as NSString).length, length: 0))
        harness.type(" More words go here. And then some more words go here too.")
        harness.type(" " + String(repeating: "Typing on. ", count: 20))
        #expect(!Self.separators(harness).isEmpty)
        #expect(Self.plainText(Self.readBack(harness)[0]) == short + " More words go here. And then some more words go here too." + " " + String(repeating: "Typing on. ", count: 20))
    }
}
