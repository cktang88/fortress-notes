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

            Button(Shortcuts.askNotes.title) {
                guard let model else { return }
                let query = model.query.trimmingCharacters(in: .whitespacesAndNewlines)
                if query.isEmpty {
                    model.paletteQuery = "? "
                    model.isPaletteShown = true
                } else {
                    model.route = .ask(question: query)
                }
            }
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

extension View {
    /// Starts Finding's app-wide pieces (the global Quick Capture shortcut).
    public func findingSetup(_ model: AppModel) -> some View {
        onAppear { QuickCapture.shared.install(model: model) }
    }
}
