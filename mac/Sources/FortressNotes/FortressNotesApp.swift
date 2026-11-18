import FortressKit
import SwiftUI

@main
struct FortressNotesApp: App {
    @NSApplicationDelegateAdaptor(FortressAppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            MainWindow()
                .environment(delegate.model)
                .findingSetup(delegate.model)  // Finding: global Quick Capture shortcut (⇧⌘Space).
        }
        .defaultSize(width: 1280, height: 820)
        .windowResizability(.contentMinSize)
        .commands {
            SidebarCommands()
            ShellCommands(model: delegate.model)
            FindingCommands(model: delegate.model)  // Finding: ⌘K palette, Ask, Quick Capture.
        }

        Settings {
            SettingsView()
                .environment(delegate.model)
        }
    }
}
