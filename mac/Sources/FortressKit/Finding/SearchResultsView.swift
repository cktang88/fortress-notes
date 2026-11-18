import AppKit
import SwiftUI

/// Unified search results (words, meaning and titles fused by the backend), shown in
/// place of the note list while the toolbar search field has text.
///
/// Keys while typing in the search field: ↑/↓ move, ↩ opens, ⌘↩ asks.
struct SearchResultsView: View {
    @Environment(AppModel.self) private var model
    @State private var controller: SearchController

    init(controller: SearchController = SearchController()) {
        _controller = State(initialValue: controller)
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    AskRow(query: controller.query, isSelected: controller.selection == .ask) {
                        perform(.ask(question: controller.query))
                    }
                    .id(SearchSelection.ask)
                    .padding(.bottom, 6)

                    ForEach(controller.results) { match in
                        PassageRow(match: match, isSelected: controller.selection == .result(match.id)) {
                            controller.selection = .result(match.id)
                            perform(.open(documentId: match.documentId, blockId: match.blockId))
                        }
                        .id(SearchSelection.result(match.id))
                    }

                    footer
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .opacity(controller.isStale && controller.resultsQuery != nil ? 0.55 : 1)
                .animation(.smooth(duration: 0.2), value: controller.isStale)
            }
            .scrollEdgeEffectStyle(.soft, for: .top)
            .onChange(of: controller.selection) { _, selection in
                withAnimation(.snappy(duration: 0.15)) { proxy.scrollTo(selection) }
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
        .onChange(of: model.query, initial: true) { _, query in
            let api = model.api
            controller.update(query: query) { try await api.search($0) }
        }
        .onDisappear { controller.cancel() }
        .onLocalKeyDown(handleKey)
    }

    @ViewBuilder private var footer: some View {
        if let message = controller.errorMessage {
            Label(message, systemImage: "exclamationmark.triangle")
                .font(.callout)
                .foregroundStyle(.secondary)
                .padding(12)
        } else if controller.results.isEmpty, controller.resultsQuery == controller.query, !controller.query.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("No matches for “\(controller.query)”")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.secondary)
                Text("Try other words, or ask your notes with ⌘↩.")
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
            }
            .padding(12)
        }
    }

    private func perform(_ action: SearchAction) {
        switch action {
        case .open(let documentId, let blockId):
            model.open(noteId: documentId, blockId: blockId)
        case .ask(let question):
            model.route = .ask(question: question)
        }
    }

    /// Arrow and return keys typed into the toolbar search field drive the results.
    private func handleKey(_ event: NSEvent) -> Bool {
        guard Self.isSearchFieldFocused(in: event.window) else { return false }
        let modifiers = KeyCode.modifiers(event)
        switch event.keyCode {
        case KeyCode.down where modifiers.isEmpty:
            controller.move(by: 1)
            return true
        case KeyCode.up where modifiers.isEmpty:
            controller.move(by: -1)
            return true
        case KeyCode.returnKey, KeyCode.keypadEnter:
            if modifiers == .command {
                if !controller.query.isEmpty { perform(.ask(question: controller.query)) }
                return true
            }
            guard modifiers.isEmpty else { return false }
            controller.activate(perform: perform)
            return true
        default:
            return false
        }
    }

    /// Whether the caret is in a search field (the toolbar's `.searchable` field).
    static func isSearchFieldFocused(in window: NSWindow?) -> Bool {
        guard let editor = window?.firstResponder as? NSTextView else { return false }
        return editor.delegate is NSSearchField
    }
}

/// "Ask your notes: <query>" — always the first row.
private struct AskRow: View {
    let query: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: "sparkle.magnifyingglass")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.tint)
                    .frame(width: 20)
                Text("Ask your notes: \(Text(verbatim: query).foregroundStyle(.primary))")
                    .foregroundStyle(.secondary)
                    .font(.body)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 8)
                Text("⌘↩")
                    .font(.caption.monospacedDigit().weight(.medium))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 10)
            .frame(height: 36)
            .contentShape(.rect)
            .background(RowBackground(isSelected: isSelected))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Ask your notes: \(query)")
    }
}

/// One matching passage: note title, the passage with matched words highlighted, and why it matched.
private struct PassageRow: View {
    let match: SearchMatch
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(match.documentTitle.isEmpty ? "Untitled" : match.documentTitle)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    MatchReasons(reasons: match.matched)
                }
                Text.highlighted(match.text, ranges: match.highlights)
                    .font(.body)
                    .lineSpacing(2)
                    .foregroundStyle(.primary)
                    .lineLimit(3)
                    .textRenderer(MarkRenderer())
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(.rect)
            .background(RowBackground(isSelected: isSelected))
        }
        .buttonStyle(.plain)
    }
}

/// Small quiet glyphs saying how a passage matched: its words, its meaning, its note's title.
struct MatchReasons: View {
    let reasons: [String]

    var body: some View {
        HStack(spacing: 4) {
            ForEach(reasons, id: \.self) { reason in
                if let info = Self.info[reason] {
                    Image(systemName: info.symbol)
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.tertiary)
                        .frame(width: 16, height: 14)
                        .background(.quaternary.opacity(0.6), in: .capsule)
                        .help(info.help)
                        .accessibilityLabel(info.help)
                }
            }
        }
    }

    static let info: [String: (symbol: String, help: String)] = [
        "words": ("textformat", "Matches your words"),
        "meaning": ("sparkles", "Similar in meaning"),
        "title": ("doc.text", "Matches the note’s title"),
    ]
}

/// The highlight behind the selected row: a quiet accent tint with concentric corners.
struct RowBackground: View {
    let isSelected: Bool

    var body: some View {
        RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(isSelected ? AnyShapeStyle(.tint.opacity(0.13)) : AnyShapeStyle(Color.clear))
    }
}
