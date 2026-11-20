import AppKit
import SwiftUI

/// The block a handle stands for: a block of the note's text, or of a column's.
struct HandleTarget {
    weak var editor: BlockEditorController?
    let blockId: String
    /// Its first line, in the note's text view.
    let frame: CGRect
    let inColumn: Bool
}

/// The block handle in the left margin (Craft and Notion's ⠿): it shows beside the block
/// under the pointer. Drag it to move the block with its children (a line shows where it
/// goes: between blocks, nested by dragging right, into a column, beside a block to make
/// columns, into a table cell); click it for the block's menu (Turn Into, Duplicate,
/// Delete, Copy Link to Block, Move Up and Down, columns, Info). The `+` beside it adds a
/// block below and opens the `/` menu. Everything here is also in the Format menu and on
/// the keyboard (Esc selects the block).
@MainActor
final class BlockHover: NSObject {
    private weak var controller: BlockEditorController?
    let grip = BlockGripView()
    let plus: NSButton
    let indicator = BlockDropIndicator()
    private(set) var target: HandleTarget?
    private var popover: NSPopover?
    private(set) var isMenuOpen = false
    var isDragging = false

    init(controller: BlockEditorController) {
        self.controller = controller
        plus = NSButton()
        super.init()
        plus.isBordered = false
        plus.image = NSImage(systemSymbolName: "plus", accessibilityDescription: String(localized: "Add Block Below"))?
            .withSymbolConfiguration(.init(pointSize: 11, weight: .medium))
        plus.contentTintColor = .tertiaryLabelColor
        plus.isHidden = true
        plus.toolTip = String(localized: "Add a block below (or type / on an empty line)")
        plus.target = self
        plus.action = #selector(addBlock)
        plus.setAccessibilityLabel(String(localized: "Add Block Below"))
        grip.hover = self
        grip.isHidden = true
        indicator.isHidden = true
    }

    private func install(in textView: NSView) {
        for view in [plus, grip, indicator] as [NSView] where view.superview !== textView { textView.addSubview(view) }
    }

    /// The mouse moved to `point` (the note's text view coordinates), or left the view.
    func hover(at point: NSPoint?) {
        guard let controller, !isDragging, !isMenuOpen else { return }
        guard let point else { hide(); return }
        install(in: controller.textView)
        // Keep the handle while the pointer is on its way to it.
        if !grip.isHidden {
            var area = grip.frame
            if !plus.isHidden { area = area.union(plus.frame) }
            if area.insetBy(dx: -6, dy: -6).contains(point) { return }
        }
        guard let target = controller.handleTarget(at: point) else { hide(); return }
        show(target)
    }

    /// Shows the handle beside `target`'s first line.
    func show(_ target: HandleTarget) {
        guard let controller else { return }
        install(in: controller.textView)
        if self.target?.blockId != target.blockId {
            popover?.close()
            popover = nil
        }
        self.target = target
        let size: CGFloat = 18
        let line = min(target.frame.height, 26)
        let y = target.frame.minY + (line - size) / 2
        grip.frame = CGRect(x: target.frame.minX - (target.inColumn ? 20 : 24), y: y, width: target.inColumn ? 16 : 18, height: size)
        plus.frame = CGRect(x: target.frame.minX - 46, y: y, width: size, height: size)
        plus.isHidden = target.inColumn
        grip.isHidden = false
        grip.describe(target, in: controller)
    }

    func hide() {
        grip.isHidden = true
        plus.isHidden = true
        target = nil
    }

    // MARK: Actions

    @objc func addBlock() {
        guard let target, let editor = target.editor, let range = editor.range(ofBlock: target.blockId) else { return }
        hide()
        editor.insertBlockBelow(at: range.location)
    }

    /// Selects the block (unless it is already among the selected blocks) and shows its menu.
    func showMenu() {
        guard let menu = prepareMenu() else { return }
        isMenuOpen = true
        defer { isMenuOpen = false }
        let anchor = NSPoint(x: 0, y: grip.isFlipped ? grip.bounds.maxY + 4 : -4)
        _ = menu.popUp(positioning: nil, at: anchor, in: grip)
    }

    /// The block selected as the menu's subject, and the menu (tests use it directly).
    func prepareMenu() -> NSMenu? {
        guard let target, let editor = target.editor, let range = editor.range(ofBlock: target.blockId) else { return nil }
        if let selected = editor.blockSelectionRange(), NSLocationInRange(range.location, selected) {
            editor.showBlockSelection()
        } else {
            editor.selectBlocks(anchor: target.blockId, head: target.blockId)
        }
        return BlockMenu.make(for: editor, at: range, hover: self)
    }

    /// The block's provenance (spec §7a): edited, where it came from, created.
    @objc func showInfo() {
        guard let target, let editor = target.editor, let range = editor.range(ofBlock: target.blockId),
              let info = editor.storage.blockInfo(at: range.location) else { return }
        popover?.close()
        let popover = NSPopover()
        popover.behavior = .transient
        let view = BlockInfoView(details: BlockDetails(info: info, newlyTyped: info.origin == nil)) { url in
            editor.open(url)
        }
        let hosting = NSHostingController(rootView: view)
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        if grip.isHidden || grip.window == nil {
            popover.show(relativeTo: editor.caretRect(), of: editor.textView, preferredEdge: .maxY)
        } else {
            popover.show(relativeTo: grip.bounds, of: grip, preferredEdge: .minX)
        }
        self.popover = popover
    }

    // MARK: Dragging

    /// Starts dragging the block (with its children), or the selected blocks it is among.
    func beginDrag(with event: NSEvent) {
        guard let controller, let target, let editor = target.editor, let first = editor.range(ofBlock: target.blockId) else { return }
        let moving: NSRange
        if let selected = editor.blockSelectionRange(), NSLocationInRange(first.location, selected) {
            moving = selected
        } else {
            moving = editor.subtree(at: first.location)
        }
        let lastParagraph = editor.paragraph(at: NSMaxRange(moving) - 1)
        guard let firstId = editor.storage.blockInfo(at: moving.location)?.id,
              let lastId = editor.storage.blockInfo(at: lastParagraph.location)?.id else { return }
        let start = controller.textView.convert(event.locationInWindow, from: nil)
        BlockDrag.current = BlockDrag(source: editor, firstId: firstId, lastId: lastId, startX: start.x)
        editor.selectBlocks(anchor: firstId, head: lastId)
        isDragging = true

        let item = NSPasteboardItem()
        item.setData(Data(firstId.utf8), forType: BlockDrag.type)
        item.setString(MarkdownCopy.markdown(editor.blocks(in: moving)), forType: .string)
        let dragging = NSDraggingItem(pasteboardWriter: item)
        let area = editor.selectionFrame(moving)
        let shown = CGRect(x: area.minX, y: area.minY, width: area.width, height: min(area.height, 240))
        let image = Self.picture(of: editor.textView, rect: shown)
        dragging.setDraggingFrame(grip.convert(shown, from: editor.textView), contents: image)
        _ = grip.beginDraggingSession(with: [dragging], event: event, source: grip)
    }

    func endDrag() {
        isDragging = false
        indicator.isHidden = true
        BlockDrag.current = nil
    }

    static func picture(of view: NSView, rect: CGRect) -> NSImage {
        let image = NSImage(size: rect.size)
        guard rect.width > 0, rect.height > 0, let rep = view.bitmapImageRepForCachingDisplay(in: rect) else { return image }
        view.cacheDisplay(in: rect, to: rep)
        image.addRepresentation(rep)
        return image
    }
}

/// The ⠿ handle: six dots, drawn. Drag to move the block, click for its menu.
final class BlockGripView: NSView, NSDraggingSource {
    weak var hover: BlockHover?
    private var mouseDownEvent: NSEvent?
    private var dragged = false
    private var highlighted = false { didSet { needsDisplay = true } }
    private var area: NSTrackingArea?

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        if highlighted {
            NSColor.labelColor.withAlphaComponent(0.08).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 4, yRadius: 4).fill()
        }
        BlockGripView.drawDots(in: bounds, color: highlighted ? .secondaryLabelColor : .tertiaryLabelColor)
    }

    /// Six dots in two columns, centered in `rect` (also drawn by Show Blocks).
    nonisolated static func drawDots(in rect: CGRect, color: NSColor) {
        color.setFill()
        let dot: CGFloat = 2.6
        let gap: CGFloat = 2.4
        let width = dot * 2 + gap
        let height = dot * 3 + gap * 2
        let x = rect.midX - width / 2
        let y = rect.midY - height / 2
        for column in 0..<2 {
            for row in 0..<3 {
                let origin = CGPoint(x: x + CGFloat(column) * (dot + gap), y: y + CGFloat(row) * (dot + gap))
                NSBezierPath(ovalIn: CGRect(origin: origin, size: CGSize(width: dot, height: dot))).fill()
            }
        }
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let area { removeTrackingArea(area) }
        let made = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect], owner: self)
        addTrackingArea(made)
        area = made
    }

    override func mouseEntered(with event: NSEvent) { highlighted = true }
    override func mouseExited(with event: NSEvent) { highlighted = false }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        mouseDownEvent = event
        dragged = false
    }

    override func mouseDragged(with event: NSEvent) {
        guard !dragged, let down = mouseDownEvent else { return }
        let dx = event.locationInWindow.x - down.locationInWindow.x
        let dy = event.locationInWindow.y - down.locationInWindow.y
        guard dx * dx + dy * dy > 9 else { return }
        dragged = true
        hover?.beginDrag(with: down)
    }

    override func mouseUp(with event: NSEvent) {
        defer { mouseDownEvent = nil }
        guard !dragged else { return }
        hover?.showMenu()
    }

    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        context == .withinApplication ? .move : .copy
    }

    func draggingSession(_ session: NSDraggingSession, endedAt screenPoint: NSPoint, operation: NSDragOperation) {
        hover?.endDrag()
    }

    // MARK: Accessibility: a button that opens the block's menu, with the moves as actions.

    func describe(_ target: HandleTarget, in controller: BlockEditorController) {
        let kind = target.editor.flatMap { editor in editor.range(ofBlock: target.blockId).flatMap { editor.storage.blockInfo(at: $0.location)?.kind } }
        let name = kind.map(BlockMenu.name(of:)) ?? String(localized: "Block")
        setAccessibilityLabel(String(localized: "\(name) block actions", comment: "VoiceOver: the block handle, e.g. “Heading 1 block actions”"))
        setAccessibilityHelp(String(localized: "Drag to move the block. Press for Turn Into, Duplicate, Delete and more."))
        toolTip = String(localized: "Drag to move · Click for actions")
    }

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .button }
    override func accessibilityPerformPress() -> Bool {
        hover?.showMenu()
        return true
    }

    override func accessibilityCustomActions() -> [NSAccessibilityCustomAction]? {
        func action(_ name: String, _ body: @escaping @MainActor (BlockEditorController) -> Void) -> NSAccessibilityCustomAction {
            NSAccessibilityCustomAction(name: name) { [weak self] in
                MainActor.assumeIsolated {
                    guard let hover = self?.hover, hover.prepareMenu() != nil, let editor = hover.target?.editor else { return false }
                    body(editor)
                    return true
                }
            }
        }
        return [
            action(String(localized: "Move Up")) { $0.moveSelectedBlocks(by: -1) },
            action(String(localized: "Move Down")) { $0.moveSelectedBlocks(by: 1) },
            action(String(localized: "Duplicate")) { $0.duplicateBlocks() },
            action(String(localized: "Delete")) { $0.deleteBlocks() },
        ]
    }
}

/// Where a dragged block would go: a line between blocks (indented to the depth it would
/// get), a vertical line beside a block or column, or a box around a table cell.
final class BlockDropIndicator: NSView {
    var isBox = false { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.themeAccent.setFill()
        NSColor.themeAccent.setStroke()
        if isBox {
            let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 4, yRadius: 4)
            path.lineWidth = 2
            path.stroke()
        } else {
            NSBezierPath(roundedRect: bounds, xRadius: 1, yRadius: 1).fill()
        }
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func isAccessibilityElement() -> Bool { false }
}

/// The handle's menu for a block (built fresh each time: what it offers depends on the block).
@MainActor
enum BlockMenu {
    /// The kinds Turn Into offers, in order.
    static let kinds: [BlockKind] = [.paragraph, .heading1, .heading2, .heading3, .heading4, .heading5, .heading6,
                                     .bullet, .numbered, .check, .quote]

    static func name(of kind: BlockKind) -> String {
        switch kind {
        case .paragraph: String(localized: "Text")
        case .heading1, .heading2, .heading3, .heading4, .heading5, .heading6, .bullet, .numbered, .check, .quote, .code, .divider:
            UndoNames.kind(kind)
        case .image: String(localized: "Image")
        case .file: String(localized: "File")
        case .table: String(localized: "Table")
        case .columns: String(localized: "Columns")
        case .embed: String(localized: "Embedded")
        }
    }

    static func make(for editor: BlockEditorController, at range: NSRange, hover: BlockHover?) -> NSMenu {
        let actions = BlockMenuActions(editor: editor, hover: hover)
        let menu = BlockActionsMenu(title: String(localized: "Block"))
        menu.blockActions = actions
        menu.autoenablesItems = false
        let info = editor.storage.blockInfo(at: range.location) ?? BlockInfo()
        let textual = info.kind.hasInlineText

        let turnInto = NSMenu(title: String(localized: "Turn Into"))
        turnInto.autoenablesItems = false
        func turn(_ title: String, _ target: BlockTurnInto, enabled: Bool? = nil, on: Bool = false) {
            let item = NSMenuItem(title: title, action: #selector(BlockMenuActions.turnInto(_:)), keyEquivalent: "")
            item.target = actions
            item.representedObject = TurnIntoBox(target)
            item.isEnabled = enabled ?? textual
            item.state = on ? .on : .off
            turnInto.addItem(item)
        }
        for kind in kinds {
            let isCallout = info.kind == .quote && info.container?.type == "callout"
            turn(name(of: kind), .kind(kind), on: info.kind == kind && !isCallout)
        }
        turn(String(localized: "Callout"), .callout, on: info.kind == .quote && info.container?.type == "callout")
        turn(name(of: .code), .kind(.code), on: info.kind == .code)
        let empty = textual && editor.block(at: editor.paragraph(at: range.location)).runs.isEmpty
        turn(name(of: .divider), .divider, enabled: empty)
        turnInto.addItem(.separator())
        turn(String(localized: "Table"), .table)
        let canColumns = !editor.isEmbedded && info.kind != .columns
        turn(String(localized: "2 Columns"), .columns(2), enabled: canColumns)
        turn(String(localized: "3 Columns"), .columns(3), enabled: canColumns)
        let turnItem = NSMenuItem(title: String(localized: "Turn Into"), action: nil, keyEquivalent: "")
        turnItem.submenu = turnInto
        menu.addItem(turnItem)
        menu.addItem(.separator())

        func add(_ title: String, _ action: Selector, key: String = "", modifiers: NSEvent.ModifierFlags = [], enabled: Bool = true) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            item.target = actions
            item.isEnabled = enabled
            menu.addItem(item)
        }
        add(String(localized: "Duplicate"), #selector(BlockMenuActions.duplicate(_:)), key: "d", modifiers: .command)
        add(String(localized: "Delete"), #selector(BlockMenuActions.delete(_:)), key: "\u{8}")
        add(String(localized: "Copy Link to Block"), #selector(BlockMenuActions.copyLink(_:)))
        menu.addItem(.separator())
        let up = String(UnicodeScalar(UInt16(NSUpArrowFunctionKey)).map(Character.init) ?? "↑")
        let down = String(UnicodeScalar(UInt16(NSDownArrowFunctionKey)).map(Character.init) ?? "↓")
        add(String(localized: "Move Up"), #selector(BlockMenuActions.moveUp(_:)), key: up, modifiers: [.control, .command])
        add(String(localized: "Move Down"), #selector(BlockMenuActions.moveDown(_:)), key: down, modifiers: [.control, .command])
        if info.kind == .columns || editor.columnsHost != nil {
            menu.addItem(.separator())
            add(String(localized: "Add Column"), #selector(BlockMenuActions.addColumn(_:)))
            add(String(localized: "Remove Column"), #selector(BlockMenuActions.removeColumn(_:)))
            add(String(localized: "Unwrap Columns"), #selector(BlockMenuActions.unwrapColumns(_:)))
        }
        menu.addItem(.separator())
        add(String(localized: "Info…"), #selector(BlockMenuActions.info(_:)), enabled: hover != nil)
        return menu
    }
}

/// A block menu, holding what its items do (an item's target is weak).
final class BlockActionsMenu: NSMenu {
    var blockActions: BlockMenuActions?
}

final class TurnIntoBox: NSObject {
    let target: BlockTurnInto
    init(_ target: BlockTurnInto) { self.target = target }
}

/// What the block menu's items do (on the selected blocks of `editor`).
@MainActor
final class BlockMenuActions: NSObject {
    weak var editor: BlockEditorController?
    weak var hover: BlockHover?

    init(editor: BlockEditorController, hover: BlockHover?) {
        self.editor = editor
        self.hover = hover
    }

    @objc func turnInto(_ sender: NSMenuItem) {
        guard let box = sender.representedObject as? TurnIntoBox else { return }
        editor?.turnInto(box.target)
    }

    @objc func duplicate(_ sender: Any?) { editor?.duplicateBlocks() }
    @objc func delete(_ sender: Any?) { editor?.deleteBlocks() }
    @objc func copyLink(_ sender: Any?) { editor?.copyLinkToBlock() }
    @objc func moveUp(_ sender: Any?) { editor?.moveSelectedBlocks(by: -1) }
    @objc func moveDown(_ sender: Any?) { editor?.moveSelectedBlocks(by: 1) }
    @objc func addColumn(_ sender: Any?) { editor?.addColumn() }
    @objc func removeColumn(_ sender: Any?) { editor?.removeColumn() }
    @objc func unwrapColumns(_ sender: Any?) { editor?.unwrapColumns() }
    @objc func info(_ sender: Any?) { hover?.showInfo() }
}

/// What the info popover says about a block.
struct BlockDetails: Equatable {
    var edited: Date?
    var created: Date?
    var source: String?
    var sourceURL: URL?
    var inherited = false
    var newlyTyped = false

    init(info: BlockInfo, newlyTyped: Bool) {
        self.newlyTyped = newlyTyped
        let origin = info.origin?.origin
        edited = origin.flatMap { Timestamp.date($0.updatedAt) }
        created = origin.flatMap { Timestamp.date($0.createdAt) }
        let provenance = origin?.provenance ?? info.newProvenance
        inherited = provenance?["inherited"]?.boolValue ?? false
        source = Self.describe(provenance)
        if let string = provenance?["source_url"]?.stringValue { sourceURL = URL(string: string) }
        if let sourceCreated = provenance?["source_created_at"]?.stringValue.flatMap(Timestamp.date) {
            created = sourceCreated
        }
    }

    /// "Imported from OneNote › Work › Standups", "Pasted from example.com", "Folded in from Plans".
    static func describe(_ provenance: JSONValue?) -> String? {
        guard let provenance else { return nil }
        if let origin = provenance["origin"], let operation = origin["operation"]?.stringValue {
            let title = origin["document_title"]?.stringValue ?? String(localized: "another note", comment: "Block provenance: a note whose title is unknown")
            switch operation {
            case "move_into": return String(localized: "Folded in from \(title)")
            case "duplicate": return String(localized: "Duplicated from \(title)")
            case "split": return String(localized: "Split from \(title)")
            default: break
            }
        }
        let path = provenance["source_path"]?.stringValue
        let host = provenance["source_url"]?.stringValue.flatMap { URL(string: $0)?.host() }
        func with(_ base: String) -> String {
            guard let path, !path.isEmpty else { return base }
            return String(localized: "\(base) › \(path)", comment: "Block provenance: source, then the path inside it")
        }
        let kind = provenance["source"]?.stringValue ?? ""
        switch (kind, host) {
        case ("capture", let site?): return String(localized: "Captured from \(site)")
        case ("paste", let site?): return String(localized: "Pasted from \(site)")
        case ("web", let site?): return String(localized: "From \(site)")
        default: return with(NoteProvenance.label(for: kind))
        }
    }
}

private struct BlockInfoView: View {
    let details: BlockDetails
    let open: (URL) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if details.newlyTyped && details.edited == nil {
                row("pencil", String(localized: "Written just now"))
            }
            if let edited = details.edited {
                row("pencil", EditorDates.edited(edited, now: Date()))
            }
            if let source = details.source {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.down.forward.circle").frame(width: 14)
                    Text(source)
                    if let url = details.sourceURL {
                        Button { open(url) } label: { Image(systemName: "arrow.up.forward.square") }
                            .buttonStyle(.plain).foregroundStyle(.tint).help(url.absoluteString)
                    }
                }
            }
            if let created = details.created {
                row("calendar", String(localized: "Created \(created.formatted(.dateTime.month(.abbreviated).year()))"))
            }
        }
        .font(.callout)
        .foregroundStyle(.secondary)
        .padding(12)
        .fixedSize()
    }

    private func row(_ symbol: String, _ text: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: symbol).frame(width: 14)
            Text(text)
        }
    }
}
