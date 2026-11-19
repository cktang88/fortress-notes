import SwiftUI

/// Menu-bar commands for finding things: the palette (Open Quickly…), Ask, and Quick
/// Capture. They act on the focused window (`FocusedValues.windowModel`).
public struct FindingCommands: Commands {
    @FocusedValue(\.windowModel) private var model
    @AppStorage(QuickCaptureShortcut.defaultsKey) private var captureShortcut = QuickCaptureShortcut.standard.storage

    public init() {}

    public var body: some Commands {
        CommandGroup(after: .sidebar) {
            Button(Shortcuts.commandPalette.title) {
                guard let model else { return }
                if !model.isPaletteShown { model.paletteQuery = "" }
                model.isPaletteShown.toggle()
            }
            .keyboardShortcut(Shortcuts.commandPalette)
            .disabled(model == nil)

            Button(Shortcuts.askNotes.title) { model?.openAsk() }
            .keyboardShortcut(Shortcuts.askNotes)
            .disabled(model == nil)

            // Shows the global shortcut set in Settings (registered system-wide by QuickCapture).
            let shortcut = QuickCaptureShortcut(storage: captureShortcut)
            Button("Quick Capture") { QuickCapture.shared.toggle() }
                .keyboardShortcut(shortcut?.keyboardShortcut)
            Divider()
        }
    }
}

extension AppModel {
    /// Shows Ask with the caret in its question field, holding the search text if there is
    /// any (the toolbar's Ask button and View ▸ Ask Your Notes…, ⇧⌘A). An answer already
    /// on screen stays: the field is then its follow-up.
    public func openAsk() {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        isPaletteShown = false
        // The Trash shows trashed notes in the detail column, never an answer.
        if scope == .trash { select(.all) }
        askDraft = text
        if case .ask = route {} else { route = .ask(question: "") }
        askFocusRequest += 1
    }
}

extension View {
    /// Starts Finding's app-wide pieces (the global Quick Capture shortcut).
    public func findingSetup(_ model: AppModel) -> some View {
        onAppear { QuickCapture.shared.install(model: model) }
    }
}
