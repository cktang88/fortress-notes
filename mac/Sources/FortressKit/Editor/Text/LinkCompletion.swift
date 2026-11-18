import AppKit
import SwiftUI

/// A note offered by `[[` completion.
struct LinkSuggestion: Identifiable, Hashable, Sendable {
    var documentId: String
    var title: String
    var detail: String
    var id: String { documentId }
}

@MainActor
@Observable
final class LinkCompletionModel {
    var query = ""
    var suggestions: [LinkSuggestion] = []
    var selection = 0
    var choose: ((LinkSuggestion) -> Void)?
}

/// `[[` note-link completion: a popover at the caret that follows the typed query.
/// Arrow keys move, ↩ or Tab inserts, esc closes; the caret stays in the text.
@MainActor
final class LinkCompletion {
    private weak var controller: BlockEditorController?
    private var anchor = 0
    private var popover: NSPopover?
    private let model = LinkCompletionModel()
    private var search: Task<Void, Never>?

    init(controller: BlockEditorController) {
        self.controller = controller
        model.choose = { [weak self] suggestion in self?.insert(suggestion) }
    }

    var isActive: Bool { popover != nil }

    func start(anchor: Int) {
        self.anchor = anchor
        model.query = ""
        model.selection = 0
        show()
        refresh()
    }

    private var queryRange: NSRange? {
        guard let controller else { return nil }
        let caret = controller.textView.selectedRange()
        let text = controller.storage.string as NSString
        guard caret.length == 0, caret.location >= anchor + 2, anchor + 2 <= text.length,
              text.substring(with: NSRange(location: anchor, length: 2)) == "[[" else { return nil }
        let range = NSRange(location: anchor, length: caret.location - anchor)
        let query = text.substring(with: NSRange(location: anchor + 2, length: caret.location - anchor - 2))
        if query.contains("\n") || query.contains("]") || query.count > 80 { return nil }
        return range
    }

    func textDidChange() {
        guard isActive else { return }
        guard let range = queryRange, let controller else { close(); return }
        let text = controller.storage.string as NSString
        model.query = text.substring(with: NSRange(location: range.location + 2, length: range.length - 2))
        refresh()
    }

    func selectionDidChange() {
        guard isActive, queryRange == nil else { return }
        close()
    }

    private func refresh() {
        search?.cancel()
        let query = model.query
        search = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(query.isEmpty ? 0 : 120))
            guard !Task.isCancelled, let self, let searchNotes = self.controller?.searchNotes else { return }
            let results = await searchNotes(query)
            guard !Task.isCancelled else { return }
            self.model.suggestions = Array(results.prefix(8))
            self.model.selection = min(self.model.selection, max(0, results.count - 1))
        }
    }

    func handle(_ selector: Selector) -> Bool {
        switch selector {
        case #selector(NSResponder.moveDown(_:)):
            model.selection = min(model.selection + 1, max(0, model.suggestions.count - 1))
        case #selector(NSResponder.moveUp(_:)):
            model.selection = max(model.selection - 1, 0)
        case #selector(NSResponder.insertNewline(_:)), #selector(NSResponder.insertTab(_:)):
            guard model.suggestions.indices.contains(model.selection) else { close(); return false }
            insert(model.suggestions[model.selection])
        case #selector(NSResponder.cancelOperation(_:)):
            close()
        default:
            return false
        }
        return true
    }

    private func insert(_ suggestion: LinkSuggestion) {
        guard let range = queryRange else { close(); return }
        close()
        controller?.insertNoteLink(suggestion, replacing: range)
    }

    private func show() {
        guard let controller, popover == nil else { return }
        let popover = NSPopover()
        popover.behavior = .semitransient
        popover.animates = false
        let hosting = NSHostingController(rootView: LinkCompletionList(model: model))
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        let rect = controller.caretRect()
        popover.show(relativeTo: rect.insetBy(dx: -2, dy: -2), of: controller.textView, preferredEdge: .maxY)
        controller.textView.window?.makeFirstResponder(controller.textView)
        self.popover = popover
    }

    func close() {
        search?.cancel()
        popover?.close()
        popover = nil
    }
}

private struct LinkCompletionList: View {
    @Bindable var model: LinkCompletionModel

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(model.query.isEmpty ? "Link to a note" : "Link to “\(model.query)”")
                .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                .padding(.horizontal, 8).padding(.top, 2).padding(.bottom, 4)
            if model.suggestions.isEmpty {
                Text(model.query.isEmpty ? "Type to search notes" : "No matching notes")
                    .font(.callout).foregroundStyle(.tertiary)
                    .padding(.horizontal, 8).padding(.vertical, 6)
            }
            ForEach(Array(model.suggestions.enumerated()), id: \.element.id) { index, suggestion in
                Button {
                    model.choose?(suggestion)
                } label: {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(suggestion.title.isEmpty ? "Untitled" : suggestion.title)
                            .font(.body).lineLimit(1)
                        if !suggestion.detail.isEmpty {
                            Text(suggestion.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8).padding(.vertical, 5)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(index == model.selection ? Color.accentColor.opacity(0.18) : .clear))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(8)
        .frame(width: 300)
    }
}

/// The small "Add Link" popover (⌘K).
@MainActor
enum LinkEditor {
    enum Result { case apply(String), remove, cancel }

    static func show(relativeTo rect: NSRect, of view: NSView, initial: String, canRemove: Bool, completion: @escaping (Result) -> Void) {
        let popover = NSPopover()
        popover.behavior = .transient
        var finished = false
        let finish: (Result) -> Void = { result in
            guard !finished else { return }
            finished = true
            popover.close()
            completion(result)
        }
        let hosting = NSHostingController(rootView: LinkEditorForm(text: initial, canRemove: canRemove, finish: finish))
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        popover.show(relativeTo: rect.insetBy(dx: -2, dy: -2), of: view, preferredEdge: .maxY)
    }
}

private struct LinkEditorForm: View {
    @State var text: String
    let canRemove: Bool
    let finish: (LinkEditor.Result) -> Void
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "link").foregroundStyle(.secondary)
            TextField("Paste or type a link", text: $text)
                .textFieldStyle(.plain)
                .focused($focused)
                .onSubmit { finish(text.trimmingCharacters(in: .whitespaces).isEmpty ? (canRemove ? .remove : .cancel) : .apply(text)) }
                .onExitCommand { finish(.cancel) }
                .frame(width: 260)
            if canRemove {
                Button("Remove") { finish(.remove) }.controlSize(.small)
            }
        }
        .padding(12)
        .onAppear { focused = true }
    }
}
