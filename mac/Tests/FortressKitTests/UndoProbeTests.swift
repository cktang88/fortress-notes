import AppKit
import Testing

@testable import FortressKit

// Temporary: records what AppKit actually does with the editor's undo (names, grouping,
// removal from the window, menu routing). Prints "PROBE" lines; never fails.

@MainActor private var probeWindows: [NSWindow] = []

@MainActor
private func spin() {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
}

@MainActor
private func probeEditor(_ blocks: [EditorBlock]) -> (BlockEditorController, NSWindow) {
    let controller = BlockEditorController(baseURL: URL(string: "http://127.0.0.1:9")!)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 600), styleMask: [.titled], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = controller.scrollView
    controller.load(blocks)
    window.makeFirstResponder(controller.textView)
    controller.textView.layoutSubtreeIfNeeded()
    probeWindows.append(window)
    return (controller, window)
}

@MainActor
private func describe(_ controller: BlockEditorController) -> String {
    controller.currentBlocks().map { "\($0.kind.rawValue):\($0.plainText)" }.joined(separator: " | ")
}

/// Undoes everything, printing each step's name and the text after it.
@MainActor
private func walk(_ label: String, _ controller: BlockEditorController) {
    let manager = controller.textView.undoManager
    var steps = 0
    print("PROBE \(label) start: [\(describe(controller))]")
    while let manager, manager.canUndo, steps < 20 {
        let name = manager.undoActionName
        manager.undo()
        spin()
        steps += 1
        print("PROBE \(label) undo \(steps) '\(name)': [\(describe(controller))]")
    }
    var redos = 0
    while let manager, manager.canRedo, redos < 20 {
        let name = manager.redoActionName
        manager.redo()
        spin()
        redos += 1
        print("PROBE \(label) redo \(redos) '\(name)': [\(describe(controller))]")
    }
}

@MainActor
@Suite(.serialized) struct UndoProbeTests {
    @Test func typingAndReturn() {
        let (controller, _) = probeEditor([EditorBlock(id: "a", runs: [InlineRun("")])])
        let view = controller.textView
        for character in "abc def" {
            view.insertText(String(character), replacementRange: view.selectedRange())
        }
        spin()
        view.doCommand(by: #selector(NSResponder.insertNewline(_:)))
        spin()
        for character in "ghi" {
            view.insertText(String(character), replacementRange: view.selectedRange())
        }
        spin()
        view.doCommand(by: #selector(NSResponder.deleteBackward(_:)))
        spin()
        walk("typing", controller)
    }

    @Test func pastes() {
        let (controller, _) = probeEditor([EditorBlock(id: "a", runs: [InlineRun("XY")])])
        let view = controller.textView
        view.setSelectedRange(NSRange(location: 1, length: 0))
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString("one", forType: .string)
        view.paste(nil)
        spin()
        pasteboard.clearContents()
        pasteboard.setString("two\nthree", forType: .string)
        view.paste(nil)
        spin()
        pasteboard.clearContents()
        pasteboard.setString("a\r\nb", forType: .string)
        view.paste(nil)
        spin()
        print("PROBE paste storage \(view.string.debugDescription)")
        walk("paste", controller)
    }

    @Test func structuralEdits() {
        let (controller, _) = probeEditor([EditorBlock(id: "a", runs: [InlineRun("one")]), EditorBlock(id: "b", runs: [InlineRun("two")])])
        let view = controller.textView
        view.setSelectedRange(NSRange(location: 0, length: 3))
        controller.toggleMark(\.bold)
        spin()
        controller.setKind(.bullet)
        spin()
        view.setSelectedRange(NSRange(location: 4, length: 0))
        view.doCommand(by: #selector(NSResponder.deleteBackward(_:)))
        spin()
        view.doCommand(by: #selector(NSResponder.deleteBackward(_:)))
        spin()
        controller.moveBlocks(by: -1)
        spin()
        walk("structural", controller)
    }

    @Test func shortcut() {
        let (controller, _) = probeEditor([EditorBlock(id: "a", runs: [InlineRun("")])])
        let view = controller.textView
        for character in "- x" {
            view.insertText(String(character), replacementRange: view.selectedRange())
            spin()
        }
        walk("shortcut", controller)
    }

    @Test func removalFromWindow() {
        let (controller, window) = probeEditor([EditorBlock(id: "a", runs: [InlineRun("")])])
        let view = controller.textView
        view.insertText("abc", replacementRange: view.selectedRange())
        spin()
        let before = view.undoManager?.canUndo ?? false
        controller.scrollView.removeFromSuperview()
        let detached = view.undoManager?.canUndo ?? false
        window.contentView = controller.scrollView
        let after = view.undoManager?.canUndo ?? false
        print("PROBE removal canUndo before \(before) detached \(detached) after \(after)")
    }

    @Test func menuRouting() {
        let (controller, window) = probeEditor([EditorBlock(id: "a", runs: [InlineRun("")])])
        let view = controller.textView
        view.insertText("abc", replacementRange: view.selectedRange())
        spin()
        var responder: NSResponder? = window.firstResponder
        var chain: [String] = []
        var target: NSResponder?
        while let current = responder {
            chain.append(String(describing: type(of: current)))
            if target == nil, current.responds(to: Selector(("undo:"))) { target = current }
            responder = current.nextResponder
        }
        if !chain.contains("NSWindow"), window.responds(to: Selector(("undo:"))) { chain.append("(window responds)") }
        print("PROBE chain \(chain) target \(target.map { String(describing: type(of: $0)) } ?? "none")")
        print("PROBE window undoManager same as view's: \(window.undoManager === view.undoManager)")
        let item = NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let validated = (target as? NSMenuItemValidation)?.validateMenuItem(item) ?? (window as NSObject as? NSMenuItemValidation)?.validateMenuItem(item)
        print("PROBE validate \(String(describing: validated)) title '\(item.title)' manager title '\(view.undoManager?.undoMenuItemTitle ?? "")'")
        _ = NSApp.sendAction(Selector(("undo:")), to: nil, from: item)
        print("PROBE after sendAction: '\(view.string)'")
        if view.string.hasPrefix("abc") {
            _ = window.firstResponder?.tryToPerform(Selector(("undo:")), with: item)
            print("PROBE after tryToPerform: '\(view.string)'")
        }
    }

    @Test func tableCells() {
        let (controller, _) = probeEditor([EditorBlock(id: "a", runs: [InlineRun("before")])])
        controller.textView.setSelectedRange(NSRange(location: 0, length: 0))
        controller.insertTable()
        spin()
        var live: TableBlockController?
        controller.storage.enumerateAttribute(.attachment, in: NSRange(location: 0, length: controller.storage.length), options: []) { value, _, _ in
            if let found = (value as? BlockAttachment)?.live as? TableBlockController { live = found }
        }
        guard let table = live else { print("PROBE table missing"); return }
        controller.textView.layoutSubtreeIfNeeded()
        table.focus(row: 0, column: 0, atEnd: true)
        if let cell = table.editor {
            cell.insertText("cell", replacementRange: cell.selectedRange())
            spin()
            print("PROBE cell manager is note's: \(cell.undoManager === controller.textView.undoManager)")
        } else {
            print("PROBE no cell editor")
        }
        table.perform("Insert Row") { $0.insertRow(at: 1); return true }
        spin()
        let manager = controller.textView.undoManager
        var step = 0
        while let manager, manager.canUndo, step < 8 {
            let name = manager.undoActionName
            manager.undo()
            spin()
            step += 1
            print("PROBE table undo \(step) '\(name)': rows \(table.table.rows.count) c00 '\(table.table.cell(row: 0, column: 0)?.plainText ?? "-")' text '\(controller.storage.string.debugDescription)'")
        }
    }
}
