import SwiftUI

/// Library, folders and Trash. (Shell area — placeholder to be built out.)
struct SidebarView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        List {
            Section("Library") {
                Label("All Notes", systemImage: "tray.full")
                Label("Recent", systemImage: "clock")
            }
        }
        .listStyle(.sidebar)
    }
}
