import SwiftUI

/// Temporary root view; replaced by the real main window.
public struct PlaceholderView: View {
    public init() {}

    public var body: some View {
        NavigationSplitView {
            List {
                Label("All Notes", systemImage: "note.text")
                Label("Uncategorized", systemImage: "tray")
            }
            .navigationSplitViewColumnWidth(min: 200, ideal: 240)
        } detail: {
            Text("Fortress Notes")
                .font(.largeTitle.weight(.semibold))
        }
    }
}
