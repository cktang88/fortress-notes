import FortressKit
import SwiftUI

@main
struct FortressNotesApp: App {
    @NSApplicationDelegateAdaptor(FortressAppDelegate.self) private var delegate

    var body: some Scene {
        // Each window has its own selection over the shared library; a value opens a
        // note in a new window (Note ▸ Open in New Window).
        WindowGroup(id: MainWindowScene.id, for: String.self) { $noteId in
            MainWindowScene(library: delegate.library, noteId: noteId)
                .findingSetup(delegate.model)  // Finding: the global Quick Capture shortcut.
        }
        .defaultSize(width: 1280, height: 820)
        .windowResizability(.contentMinSize)
        .commands {
            SidebarCommands()
            TextEditingCommands()  // Find (⌘F, ⌘G, ⌘E), Spelling, Substitutions, Speech.
            ToolbarCommands()
            ShellCommands(library: delegate.library)
            FindingCommands()  // Ask, Quick Capture.
        }

        Settings {
            SettingsView()
                .environment(delegate.model)
        }
    }
}
