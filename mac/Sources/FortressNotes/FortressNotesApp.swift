import FortressKit
import SwiftUI

@main
struct FortressNotesApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            MainWindow()
                .environment(model)
                .findingSetup(model)  // Finding: global Quick Capture shortcut (⇧⌘Space).
        }
        .defaultSize(width: 1280, height: 820)
        .windowResizability(.contentMinSize)
        .commands { FindingCommands(model: model) }  // Finding: ⌘K palette, Ask, Quick Capture.
    }
}
