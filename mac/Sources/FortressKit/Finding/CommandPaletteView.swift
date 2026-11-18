import AppKit
import SwiftUI

/// ⌘K: jump to any note or run any command, from the keyboard. A Raycast-style glass
/// panel, 560 wide, floating over the window (the window is not dimmed).
///
/// Keys: ↑/↓ move, ↩ runs, ⌘↩ asks your notes, esc closes. Prefixes: ">" commands,
/// "?" ask, "/" search inside notes.
struct CommandPaletteView: View {
    @Environment(AppModel.self) private var model
    @State private var selectedID: String?
    @State private var isShown = false
    @FocusState private var isFieldFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let rowHeight: CGFloat = 36
    static let visibleRows = 9

    var body: some View {
        @Bindable var model = model
        let sections = self.sections
        let items = sections.flatMap(\.items)
        ZStack(alignment: .top) {
            // Clicking anywhere else closes the palette.
            Color.clear
                .contentShape(.rect)
                .onTapGesture { close() }

            GlassEffectContainer {
                VStack(spacing: 0) {
                    field(text: $model.paletteQuery)
                    if !items.isEmpty {
                        Divider().opacity(0.5).padding(.horizontal, 16)
                        results(sections: sections, items: items)
                    }
                    footer(selected: items.first { $0.id == currentSelection(in: items) })
                }
                .frame(width: 560)
                .glassEffect(.regular, in: .rect(cornerRadius: 22))
            }
            .scaleEffect(isShown || reduceMotion ? 1 : 0.98, anchor: .top)
            .opacity(isShown ? 1 : 0)
            .shadow(color: .black.opacity(0.12), radius: 24, y: 12)
        }
        .onAppear {
            isFieldFocused = true
            withAnimation(.snappy(duration: 0.18)) { isShown = true }
        }
        .onChange(of: model.paletteQuery) { _, _ in selectedID = nil }
        .onLocalKeyDown { event in handleKey(event, items: items) }
        .accessibilityAddTraits(.isModal)
    }

    private var sections: [PaletteSection] {
        let notes = PaletteCatalog.notes(in: model.navigation)
        let recent = model.navigation.recent.map { document in
            PaletteNote(id: document.id, title: document.title, folder: notes.first { $0.id == document.id }?.folder)
        }
        return PaletteCatalog.sections(for: model.paletteQuery, notes: notes, recent: recent)
    }

    private func currentSelection(in items: [PaletteItem]) -> String? {
        if let selectedID, items.contains(where: { $0.id == selectedID }) { return selectedID }
        return items.first?.id
    }

    // MARK: Pieces

    private func field(text: Binding<String>) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(.secondary)
            TextField("Search notes and commands", text: text)
                .textFieldStyle(.plain)
                .font(.system(size: 20))
                .focused($isFieldFocused)
                .onSubmit { }  // ↩ is handled by the key monitor.
        }
        .padding(.horizontal, 18)
        .frame(height: 56)
    }

    private func results(sections: [PaletteSection], items: [PaletteItem]) -> some View {
        let selection = currentSelection(in: items)
        let headerCount = sections.filter { $0.title != nil }.count
        let height = min(
            CGFloat(items.count) * Self.rowHeight + CGFloat(headerCount) * 26 + 12,
            CGFloat(Self.visibleRows) * Self.rowHeight + 2 * 26 + 12)
        return ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(sections) { section in
                        if let title = section.title {
                            Text(title)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 10)
                                .frame(height: 26, alignment: .bottomLeading)
                        }
                        ForEach(section.items) { item in
                            PaletteRow(item: item, isSelected: item.id == selection)
                                .id(item.id)
                                .onTapGesture { run(item) }
                                .onHover { if $0 { selectedID = item.id } }
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
            }
            .frame(height: height)
            .scrollBounceBehavior(.basedOnSize)
            .scrollIndicators(.automatic)
            .onChange(of: selectedID) { _, id in
                if let id { proxy.scrollTo(id) }
            }
        }
    }

    private func footer(selected: PaletteItem?) -> some View {
        HStack(spacing: 14) {
            Spacer()
            if let selected {
                hint(Self.verb(for: selected.action), key: "↩")
            }
            hint("Ask", key: "⌘↩")
            hint("Close", key: "esc")
        }
        .font(.caption)
        .padding(.horizontal, 18)
        .frame(height: 34)
        .background(alignment: .top) { Divider().opacity(0.5) }
    }

    private func hint(_ label: String, key: String) -> some View {
        HStack(spacing: 5) {
            Text(label).foregroundStyle(.secondary)
            Text(key)
                .font(.caption.weight(.medium).monospacedDigit())
                .foregroundStyle(.tertiary)
        }
    }

    static func verb(for action: PaletteItem.Action) -> String {
        switch action {
        case .openNote: "Open Note"
        case .newNote: "Create"
        case .search: "Search"
        case .ask: "Ask"
        case .command: "Run"
        }
    }

    // MARK: Keys and actions

    private func handleKey(_ event: NSEvent, items: [PaletteItem]) -> Bool {
        let modifiers = KeyCode.modifiers(event)
        switch event.keyCode {
        case KeyCode.escape:
            close()
            return true
        case KeyCode.down, KeyCode.up:
            guard !items.isEmpty else { return true }
            let current = items.firstIndex { $0.id == currentSelection(in: items) } ?? 0
            let next = event.keyCode == KeyCode.down ? min(current + 1, items.count - 1) : max(current - 1, 0)
            selectedID = items[next].id
            return true
        case KeyCode.returnKey, KeyCode.keypadEnter:
            if modifiers == .command {
                let query = model.paletteQuery.trimmingCharacters(in: CharacterSet(charactersIn: "?/> ").union(.whitespaces))
                run(PaletteItem(id: "ask", title: "", symbol: "", action: .ask(query)))
            } else if let item = items.first(where: { $0.id == currentSelection(in: items) }) {
                run(item)
            }
            return true
        default:
            // ⌘1…⌘9 run the nth result.
            if modifiers == .command, let digit = Int(event.charactersIgnoringModifiers ?? ""), (1...9).contains(digit),
               digit <= items.count {
                run(items[digit - 1])
                return true
            }
            return false
        }
    }

    private func close() {
        model.isPaletteShown = false
        model.paletteQuery = ""
    }

    private func run(_ item: PaletteItem) {
        let model = self.model
        switch item.action {
        case .openNote(let id):
            close()
            model.open(noteId: id)
        case .newNote(let title):
            close()
            Task { await Self.createNote(title: title, in: model) }
        case .search(let words):
            if words.isEmpty { model.paletteQuery = "/ "; return }
            close()
            model.query = words
        case .ask(let question):
            if question.isEmpty { model.paletteQuery = "? "; return }
            close()
            model.route = .ask(question: question)
        case .command(let command):
            switch command {
            case .searchNotes:
                model.paletteQuery = "/ "
                return
            case .askNotes:
                model.paletteQuery = "? "
                return
            default:
                close()
                Self.run(command, in: model)
            }
        }
    }

    static func run(_ command: PaletteCommand, in model: AppModel) {
        switch command {
        case .newNote:
            Task { await createNote(title: "", in: model) }
        case .newFolder:
            Task {
                do {
                    _ = try await model.api.createFolder(name: "New Folder")
                    await model.refreshNavigation()
                } catch { model.report(error) }
            }
        case .quickCapture:
            QuickCapture.shared.show()
        case .searchNotes, .askNotes:
            model.paletteQuery = command == .askNotes ? "? " : "/ "
            model.isPaletteShown = true
        case .allNotes:
            model.query = ""
            model.scope = .all
        case .trash:
            model.query = ""
            model.scope = .trash
        case .backups:
            model.isBackupsRequested = true
        case .toggleInspector:
            model.isInspectorShown.toggle()
        case .reload:
            Task { await model.refreshNavigation() }
        }
    }

    static func createNote(title: String, in model: AppModel) async {
        do {
            let document = try await model.api.createDocument(title: title)
            await model.refreshNavigation()
            model.open(noteId: document.id)
        } catch {
            model.report(error)
        }
    }
}

private struct PaletteRow: View {
    let item: PaletteItem
    let isSelected: Bool

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: item.symbol)
                .font(.system(size: 14))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(isSelected ? AnyShapeStyle(.tint) : AnyShapeStyle(.secondary))
                .frame(width: 20)
            title
                .font(.body)
                .lineLimit(1)
            if let subtitle = item.subtitle {
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            if let accessory = item.accessory {
                Text(accessory)
                    .font(.caption.weight(.medium).monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 10)
        .frame(height: CommandPaletteView.rowHeight)
        .contentShape(.rect)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(isSelected ? AnyShapeStyle(.primary.opacity(0.08)) : AnyShapeStyle(.clear)))
    }

    /// The title with matched characters in semibold.
    private var title: Text {
        let matched = Set(item.matched)
        guard !matched.isEmpty else { return Text(verbatim: item.title) }
        return Text.joined(item.title.enumerated().map { offset, character in
            matched.contains(offset)
                ? Text(verbatim: String(character)).fontWeight(.semibold).foregroundStyle(.primary)
                : Text(verbatim: String(character))
        })
        .foregroundStyle(.primary.opacity(0.85))
    }
}
