import AppKit
import SwiftUI

/// One choice in the `/` insert menu.
struct SlashItem: Identifiable, Hashable {
    enum Action: Hashable {
        case turn(BlockTurnInto)
        case table(rows: Int, columns: Int)
        case image, file, math, noteLink
    }

    let id: String
    let title: String
    let symbol: String
    /// Other words it is found by ("h1", "todo", "latex").
    let keywords: [String]
    let action: Action

    var undoName: String {
        switch action {
        case .turn(let target): target.undoName
        case .table: UndoNames.insertTable
        case .image, .file: UndoNames.insertBlock
        case .math: UndoNames.math
        case .noteLink: UndoNames.noteLink
        }
    }

    /// Every item, in the menu's order.
    static let catalog: [SlashItem] = [
        SlashItem(id: "text", title: String(localized: "Text"), symbol: "text.alignleft", keywords: ["paragraph", "body", "plain"], action: .turn(.kind(.paragraph))),
        SlashItem(id: "h1", title: String(localized: "Heading 1"), symbol: "textformat.size.larger", keywords: ["h1", "title", "#"], action: .turn(.kind(.heading1))),
        SlashItem(id: "h2", title: String(localized: "Heading 2"), symbol: "textformat.size", keywords: ["h2", "##"], action: .turn(.kind(.heading2))),
        SlashItem(id: "h3", title: String(localized: "Heading 3"), symbol: "textformat.size.smaller", keywords: ["h3", "###"], action: .turn(.kind(.heading3))),
        SlashItem(id: "h4", title: String(localized: "Heading 4"), symbol: "textformat.size.smaller", keywords: ["h4"], action: .turn(.kind(.heading4))),
        SlashItem(id: "h5", title: String(localized: "Heading 5"), symbol: "textformat.size.smaller", keywords: ["h5"], action: .turn(.kind(.heading5))),
        SlashItem(id: "h6", title: String(localized: "Heading 6"), symbol: "textformat.size.smaller", keywords: ["h6"], action: .turn(.kind(.heading6))),
        SlashItem(id: "bullet", title: String(localized: "Bulleted List"), symbol: "list.bullet", keywords: ["bullet", "unordered", "ul", "-"], action: .turn(.kind(.bullet))),
        SlashItem(id: "numbered", title: String(localized: "Numbered List"), symbol: "list.number", keywords: ["ordered", "ol", "1."], action: .turn(.kind(.numbered))),
        SlashItem(id: "check", title: String(localized: "Checklist"), symbol: "checklist", keywords: ["todo", "task", "checkbox", "[]"], action: .turn(.kind(.check))),
        SlashItem(id: "quote", title: String(localized: "Quote"), symbol: "text.quote", keywords: ["blockquote", ">"], action: .turn(.kind(.quote))),
        SlashItem(id: "callout", title: String(localized: "Callout"), symbol: "exclamationmark.bubble", keywords: ["note", "info", "aside", "tip"], action: .turn(.callout)),
        SlashItem(id: "code", title: String(localized: "Code Block"), symbol: "chevron.left.forwardslash.chevron.right", keywords: ["snippet", "pre", "```"], action: .turn(.kind(.code))),
        SlashItem(id: "divider", title: String(localized: "Divider"), symbol: "minus", keywords: ["rule", "line", "hr", "separator", "---"], action: .turn(.divider)),
        SlashItem(id: "table", title: String(localized: "Table"), symbol: "tablecells", keywords: ["grid", "rows"], action: .table(rows: 3, columns: 3)),
        SlashItem(id: "columns2", title: String(localized: "2 Columns"), symbol: "rectangle.split.2x1", keywords: ["columns", "side", "layout", "two"], action: .turn(.columns(2))),
        SlashItem(id: "columns3", title: String(localized: "3 Columns"), symbol: "rectangle.split.3x1", keywords: ["columns", "side", "layout", "three"], action: .turn(.columns(3))),
        SlashItem(id: "image", title: String(localized: "Image…"), symbol: "photo", keywords: ["picture", "photo", "screenshot"], action: .image),
        SlashItem(id: "file", title: String(localized: "File…"), symbol: "doc", keywords: ["attachment", "pdf", "upload"], action: .file),
        SlashItem(id: "math", title: String(localized: "Math…"), symbol: "function", keywords: ["equation", "latex", "formula"], action: .math),
        SlashItem(id: "link", title: String(localized: "Note Link"), symbol: "link", keywords: ["note", "wikilink", "[["], action: .noteLink),
    ]

    /// The items matching what was typed after `/`: every word of the query starts a word
    /// of the title or a keyword ("head 2", "todo", "col"). "table 4x5" offers that size.
    static func items(matching query: String, inColumn: Bool) -> [SlashItem] {
        var items = catalog
        if inColumn { items.removeAll { $0.action == .turn(.columns(2)) || $0.action == .turn(.columns(3)) } }
        let text = query.lowercased().trimmingCharacters(in: .whitespaces)
        if let size = tableSize(text) {
            let title = String(localized: "Table \(size.rows) × \(size.columns)", comment: "The / menu's table of that many rows and columns")
            return [SlashItem(id: "table-\(size.rows)x\(size.columns)", title: title, symbol: "tablecells",
                              keywords: [], action: .table(rows: size.rows, columns: size.columns))]
        }
        guard !text.isEmpty else { return items }
        let tokens = text.split(separator: " ").map(String.init)
        return items.filter { item in
            let words = item.title.lowercased().split(separator: " ").map(String.init) + item.keywords
            return tokens.allSatisfy { token in words.contains { $0.hasPrefix(token) } }
        }
    }

    /// "table 4x5", "4 × 5", "t3x2": rows and columns (1–30 × 1–12).
    static func tableSize(_ text: String) -> (rows: Int, columns: Int)? {
        guard let match = text.firstMatch(of: /^(?:table|tab|t)?\s*(\d{1,2})\s*[x×*]\s*(\d{1,2})$/),
              let rows = Int(match.1), let columns = Int(match.2), (1...30).contains(rows), (1...12).contains(columns)
        else { return nil }
        return (rows, columns)
    }
}

@MainActor
@Observable
final class SlashMenuModel {
    var query = ""
    var items: [SlashItem] = []
    var selection = 0
    var choose: ((SlashItem) -> Void)?
}

/// The `/` insert menu: typed at the start of a block or after a space (never inside a
/// word, a link, inline code or a code block), a popover at the caret lists the kinds of
/// block, filtered as you type. ↑/↓ move, ↩ or Tab inserts, Esc closes and leaves the
/// typed text as it is; the caret stays in the text.
@MainActor
final class SlashMenu {
    private weak var controller: BlockEditorController?
    /// Where the `/` is.
    private(set) var anchor = 0
    private var popover: NSPopover?
    let model = SlashMenuModel()

    init(controller: BlockEditorController) {
        self.controller = controller
        model.choose = { [weak self] item in self?.choose(item) }
    }

    var isActive: Bool { popover != nil }

    /// Whether a `/` just typed at `slash` opens the menu.
    func opens(at slash: Int) -> Bool {
        guard let controller else { return false }
        let storage = controller.storage
        let text = storage.nsString
        guard slash < text.length, text.character(at: slash) == 0x2F else { return false }
        let paragraph = text.paragraphRange(at: slash)
        guard let info = storage.blockInfo(at: paragraph.location), info.kind.hasInlineText, info.kind != .code else { return false }
        if slash > paragraph.location {
            // Only after a space or a line break: never in the middle of a word or a URL.
            guard let before = UnicodeScalar(text.character(at: slash - 1)),
                  CharacterSet.whitespaces.contains(before) || before == "\u{2028}" else { return false }
        }
        let marks = storage.marks(at: slash)
        guard marks.link == nil, !marks.code, storage.attribute(.fnNode, at: slash, effectiveRange: nil) == nil else { return false }
        // Nothing but a space or the end of the line right after it.
        let after = slash + 1
        if after < NSMaxRange(paragraph) - 1, let next = UnicodeScalar(text.character(at: after)),
           !CharacterSet.whitespaces.contains(next) {
            return false
        }
        return true
    }

    func start(anchor: Int) {
        self.anchor = anchor
        model.selection = 0
        refresh()
        show()
    }

    /// `/` and the query typed after it, while the caret is right after them.
    var queryRange: NSRange? {
        guard let controller else { return nil }
        let caret = controller.textView.selectedRange()
        let text = controller.storage.nsString
        guard caret.length == 0, caret.location > anchor, anchor < text.length, text.character(at: anchor) == 0x2F else { return nil }
        let range = NSRange(location: anchor, length: caret.location - anchor)
        let query = text.substring(with: NSRange(location: anchor + 1, length: range.length - 1))
        if query.contains("\n") || query.contains("\u{2028}") || query.count > 32 { return nil }
        return range
    }

    /// After an edit: a `/` just typed may open the menu; typing filters it.
    func textDidChange(inserted: String?) {
        guard let controller else { return }
        if isActive {
            guard queryRange != nil else { close(); return }
            refresh()
            // A space after something that matches nothing: the person is writing, not choosing.
            if model.items.isEmpty, model.query.hasSuffix(" ") { close() }
            return
        }
        guard inserted == "/" else { return }
        let caret = controller.textView.selectedRange()
        guard caret.length == 0, caret.location > 0, opens(at: caret.location - 1) else { return }
        start(anchor: caret.location - 1)
    }

    func selectionDidChange() {
        guard isActive, queryRange == nil else { return }
        close()
    }

    private func refresh() {
        guard let controller, let range = queryRange else { return }
        let text = controller.storage.nsString
        model.query = text.substring(with: NSRange(location: range.location + 1, length: range.length - 1))
        model.items = SlashItem.items(matching: model.query, inColumn: controller.isEmbedded)
        model.selection = min(model.selection, max(0, model.items.count - 1))
    }

    func handle(_ selector: Selector) -> Bool {
        switch selector {
        case #selector(NSResponder.moveDown(_:)):
            model.selection = min(model.selection + 1, max(0, model.items.count - 1))
        case #selector(NSResponder.moveUp(_:)):
            model.selection = max(model.selection - 1, 0)
        case #selector(NSResponder.insertNewline(_:)), #selector(NSResponder.insertTab(_:)):
            guard model.items.indices.contains(model.selection) else { close(); return false }
            choose(model.items[model.selection])
        case #selector(NSResponder.cancelOperation(_:)):
            close()
        default:
            return false
        }
        return true
    }

    func choose(_ item: SlashItem) {
        guard let range = queryRange, let controller else { close(); return }
        close()
        controller.performSlashItem(item, replacing: range)
    }

    private func show() {
        guard let controller, popover == nil else { return }
        let popover = NSPopover()
        popover.behavior = .semitransient
        popover.animates = false
        let hosting = NSHostingController(rootView: SlashMenuList(model: model))
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        let rect = controller.caretRect()
        popover.show(relativeTo: rect.insetBy(dx: -2, dy: -2), of: controller.textView, preferredEdge: .maxY)
        controller.textView.window?.makeFirstResponder(controller.textView)
        self.popover = popover
        NSAccessibility.post(element: controller.textView, notification: .layoutChanged)
    }

    func close() {
        popover?.close()
        popover = nil
    }
}

private struct SlashMenuList: View {
    @Bindable var model: SlashMenuModel

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(model.query.isEmpty ? "Insert a block" : "Insert “\(model.query)”")
                .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                .padding(.horizontal, 8).padding(.top, 2).padding(.bottom, 4)
            if model.items.isEmpty {
                Text("No matching blocks").font(.callout).foregroundStyle(.tertiary)
                    .padding(.horizontal, 8).padding(.vertical, 6)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 1) {
                        ForEach(Array(model.items.enumerated()), id: \.element.id) { index, item in
                            Button {
                                model.choose?(item)
                            } label: {
                                HStack(spacing: 8) {
                                    Image(systemName: item.symbol).frame(width: 18).foregroundStyle(.secondary)
                                    Text(item.title).font(.body).lineLimit(1)
                                    Spacer(minLength: 0)
                                }
                                .padding(.horizontal, 8).padding(.vertical, 4)
                                .background(
                                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                                        .fill(index == model.selection ? Color.accentColor.opacity(0.18) : .clear))
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .id(item.id)
                            .accessibilityLabel(item.title)
                            .accessibilityAddTraits(index == model.selection ? .isSelected : [])
                        }
                    }
                }
                .frame(maxHeight: 320)
                .onChange(of: model.selection) { _, selection in
                    if model.items.indices.contains(selection) { proxy.scrollTo(model.items[selection].id) }
                }
            }
        }
        .padding(8)
        .frame(width: 240)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Insert a block")
    }
}

/// The small LaTeX field for a math block (`/math`, Edit Math…).
@MainActor
enum MathEditor {
    static func show(relativeTo rect: NSRect, of view: NSView, initial: String, completion: @escaping (String?) -> Void) {
        let popover = NSPopover()
        popover.behavior = .transient
        var finished = false
        let finish: (String?) -> Void = { result in
            guard !finished else { return }
            finished = true
            popover.close()
            completion(result)
        }
        let hosting = NSHostingController(rootView: MathEditorForm(text: initial, finish: finish))
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        popover.show(relativeTo: rect.insetBy(dx: -2, dy: -2), of: view, preferredEdge: .maxY)
    }
}

private struct MathEditorForm: View {
    @State var text: String
    let finish: (String?) -> Void
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "function").foregroundStyle(.secondary)
            TextField("LaTeX, e.g. E = mc^2", text: $text)
                .textFieldStyle(.plain)
                .font(.system(.body, design: .monospaced))
                .focused($focused)
                .onSubmit {
                    let latex = text.trimmingCharacters(in: .whitespacesAndNewlines)
                    finish(latex.isEmpty ? nil : latex)
                }
                .onExitCommand { finish(nil) }
                .frame(width: 280)
                .accessibilityLabel("LaTeX")
        }
        .padding(12)
        .onAppear { focused = true }
    }
}

// MARK: Performing an item

extension BlockEditorController {
    /// Does what a `/` menu item says, in place of the `/` and its query (`range`).
    func performSlashItem(_ item: SlashItem, replacing range: NSRange) {
        let typed = string.substring(with: range)
        /// The `/` text if it is still where it was.
        func slashRange() -> NSRange? {
            guard NSMaxRange(range) <= string.length, string.substring(with: range) == typed else { return nil }
            return range
        }
        switch item.action {
        case .noteLink:
            let current = info(at: range.location)
            undoStep(UndoNames.noteLink) {
                replace(range, with: attributed("[[", info: current), selecting: NSRange(location: range.location + 2, length: 0))
            }
            maybeStartCompletion()
        case .math:
            editMath(at: nil, initial: "") { [weak self] latex in
                guard let self else { return }
                self.undoStep(UndoNames.math) {
                    if let slash = slashRange() { self.replace(slash, with: NSAttributedString(), selecting: NSRange(location: slash.location, length: 0)) }
                    let line = self.paragraph(at: min(range.location, max(0, self.string.length - 1)))
                    self.insertBlocks([Self.mathBlock(latex)], at: line)
                }
            }
        case .image, .file:
            let images = item.action == .image
            let picker = chooseFiles ?? { images, completion in Self.openPanel(images: images, completion: completion) }
            picker(images) { [weak self] urls in
                guard let self, !urls.isEmpty else { return }
                var anchor = range.location
                self.undoStep(UndoNames.insertBlock) {
                    guard let slash = slashRange() else { return }
                    self.replace(slash, with: NSAttributedString(), selecting: NSRange(location: slash.location, length: 0))
                    let line = self.paragraph(at: slash.location)
                    if self.contentRange(of: line).length == 0, line.location > 0 {
                        // The empty line goes; the files follow the block above.
                        anchor = line.location - 1
                        self.replace(line, with: NSAttributedString(), selecting: NSRange(location: anchor, length: 0))
                    }
                }
                self.insertFiles(urls, after: anchor)
            }
        case .table(let rows, let columns):
            undoStep(item.undoName) {
                replace(range, with: NSAttributedString(), selecting: NSRange(location: range.location, length: 0))
                let depth = info(at: range.location).depth
                insertBlocks([EditorBlock(kind: .table, depth: depth, table: .blank(rows: rows, columns: columns))],
                             at: paragraph(at: range.location))
            }
        case .turn(let target):
            if case .columns = target, isEmbedded { NSSound.beep(); return }
            undoStep(item.undoName) {
                replace(range, with: NSAttributedString(), selecting: NSRange(location: range.location, length: 0))
                insertOrTurn(target, at: paragraph(at: range.location))
            }
        }
    }

    /// The empty block at `paragraph` becomes `target`; a block with text gets a new
    /// block of that kind below it.
    private func insertOrTurn(_ target: BlockTurnInto, at paragraph: NSRange) {
        let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
        let empty = info.kind.hasInlineText && contentRange(of: paragraph).length == 0
        switch target {
        case .kind, .callout:
            var place = paragraph
            if !empty {
                let at = NSMaxRange(subtree(at: paragraph.location))
                replace(NSRange(location: at, length: 0), with: attributed("\n", info: BlockInfo(depth: info.depth)),
                        selecting: NSRange(location: at, length: 0))
                place = self.paragraph(at: at)
            }
            if case .kind(let kind) = target, kind != .quote {
                updateBlocks([place]) { $0.with(kind: kind, checked: false) }
            } else {
                let type = target == .callout ? "callout" : "quote"
                makeQuoteLines([place], container: quoteContainer(around: [place], type: type))
            }
            textView.setSelectedRange(NSRange(location: place.location, length: 0))
            syncTypingAttributes()
        case .divider:
            insertBlocks([EditorBlock(kind: .divider, depth: info.depth)], at: paragraph)
        case .table:
            insertBlocks([EditorBlock(kind: .table, depth: info.depth, table: .blank())], at: paragraph)
        case .columns(let count):
            let columns = (0..<count).map { _ in EditorColumn(blocks: [EditorBlock()]) }
            insertBlocks([EditorBlock(kind: .columns, depth: info.depth, columns: columns)], at: paragraph)
        }
    }
}
