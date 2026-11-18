import SwiftUI

/// Menu-bar commands for finding things: the ⌘K palette, Ask, and Quick Capture.
/// Added to the app's scene with `.commands { FindingCommands(model: model) }`.
public struct FindingCommands: Commands {
    let model: AppModel

    public init(model: AppModel) {
        self.model = model
    }

    public var body: some Commands {
        CommandGroup(after: .sidebar) {
            Button("Command Palette") {
                if !model.isPaletteShown { model.paletteQuery = "" }
                model.isPaletteShown.toggle()
            }
            .keyboardShortcut("k", modifiers: .command)

            Button("Ask Your Notes…") {
                let query = model.query.trimmingCharacters(in: .whitespacesAndNewlines)
                if query.isEmpty {
                    model.paletteQuery = "? "
                    model.isPaletteShown = true
                } else {
                    model.route = .ask(question: query)
                }
            }
            .keyboardShortcut("a", modifiers: [.command, .shift])

            Button("Quick Capture") { QuickCapture.shared.toggle() }
                .keyboardShortcut(.space, modifiers: [.command, .shift])
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
