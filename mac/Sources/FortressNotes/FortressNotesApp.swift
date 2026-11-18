import FortressKit
import SwiftUI

@main
struct FortressNotesApp: App {
    @NSApplicationDelegateAdaptor(FortressAppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            MainWindow()
                .environment(delegate.model)
        }
        .defaultSize(width: 1280, height: 820)
        .windowResizability(.contentMinSize)
        .commands {
            SidebarCommands()
            ShellCommands(model: delegate.model)
        }

        Settings {
            SettingsView()
                .environment(delegate.model)
        }
    }
}
