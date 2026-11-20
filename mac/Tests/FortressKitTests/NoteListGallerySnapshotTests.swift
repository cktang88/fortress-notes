import AppKit
import SwiftUI
import Testing

@testable import FortressKit

/// The note list in a real window (artifact "snapshots").
@MainActor
@Suite(.serialized) struct NoteListGallerySnapshotTests {
    /// The heights of the rows the note list's table shows in its viewport.
    private func visibleRowHeights(_ host: HostWindow) -> [CGFloat] {
        guard let table = host.tableViews.first else { return [] }
        let visible = table.rows(in: table.visibleRect)
        return (visible.location..<visible.location + visible.length).map { table.rect(ofRow: $0).height }
    }

    /// At launch the library is empty until the backend answers; the notes arrive after
    /// the window (and its note list) appeared. Their rows must be as tall as when the
    /// window opens on a loaded library: title, two lines of text, date and folder
    /// (not squashed to a table's default 24 pt, showing only a clipped title).
    @Test func notesArrivingAfterLaunchGetFullHeightRows() throws {
        let loaded = HostWindow(MainWindow().environment(ShellFixtures.model(openNote: "n2")))
        let expected = visibleRowHeights(loaded)
        loaded.close()
        #expect(expected.count > 4)
        #expect(expected.contains { $0 > 60 }, "a note row shows title, text and date: \(expected)")

        let model = AppModel(api: APIClient(baseURL: URL(string: "http://127.0.0.1:9")!))
        model.connectsOnAppear = false
        model.isConnected = false  // "Starting Fortress…"
        let host = HostWindow(MainWindow().environment(model))
        // The backend answers and the library loads; the last note reopens.
        model.isConnected = true
        model.navigation = ShellFixtures.navigation
        model.hasLoadedNavigation = true
        model.open(noteId: "n2")
        host.settle()
        let heights = visibleRowHeights(host)
        try host.capture(named: "shell-launch-loaded-light")
        host.close()
        #expect(heights == expected)
    }

    @Test func selectedRows() throws {
        let size = CGSize(width: 300, height: 470)
        try snapshot(SelectedRowsGallery(), named: "shell-selected-rows-light", size: size)
        try snapshot(SelectedRowsGallery(), named: "shell-selected-rows-dark", size: size, appearance: .darkAqua)
    }
}

/// Note and Trash rows as a list draws them: unselected, selected in a list without
/// focus (grey), and selected in the focused list (accent, `backgroundProminence`
/// `.increased`), where their text takes the selected text colour.
/// (A test window never becomes key, so a real list's selection always draws unfocused.)
struct SelectedRowsGallery: View {
    var body: some View {
        let note = ShellFixtures.notes[2]
        let other = ShellFixtures.notes[0]
        VStack(alignment: .leading, spacing: 4) {
            row(.standard, nil) { NoteRow(note: other, folderName: "Travel") }
            row(.standard, Color(nsColor: .unemphasizedSelectedContentBackgroundColor)) { NoteRow(note: note, folderName: "Inbox") }
            row(.increased, Color(nsColor: .selectedContentBackgroundColor)) { NoteRow(note: note, folderName: "Inbox") }
            row(.increased, Color(nsColor: .selectedContentBackgroundColor)) { NoteRow(note: other, folderName: "Travel") }
            row(.increased, Color(nsColor: .selectedContentBackgroundColor)) { TrashRow(note: ShellFixtures.trash[1]) }
        }
        .padding(10)
        .frame(width: 300, alignment: .top)
        .frame(maxHeight: .infinity, alignment: .top)
        .themedContent(.sunken)
        .fortressTheme()
    }

    private func row<Row: View>(_ prominence: BackgroundProminence, _ fill: Color?, @ViewBuilder _ content: () -> Row) -> some View {
        content()
            .padding(.horizontal, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(fill ?? .clear, in: RoundedRectangle(cornerRadius: 8))
            .environment(\.backgroundProminence, prominence)
    }
}
