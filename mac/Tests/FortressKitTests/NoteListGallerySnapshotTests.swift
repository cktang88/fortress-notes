import AppKit
import SwiftUI
import Testing

@testable import FortressKit

/// The note list in a real window: its rows' heights when the notes arrive after launch.
@MainActor
@Suite(.serialized) struct NoteListGallerySnapshotTests {
    /// A window whose library hasn't loaded yet, as at launch.
    private func launchingModel() -> AppModel {
        let model = AppModel(api: APIClient(baseURL: URL(string: "http://127.0.0.1:9")!))
        model.connectsOnAppear = false
        return model
    }

    private func load(_ model: AppModel, openNote: String? = "n2") {
        model.isConnected = true
        model.navigation = ShellFixtures.navigation
        model.hasLoadedNavigation = true
        if let openNote { model.open(noteId: openNote) }
    }

    private func dump(_ label: String, _ host: HostWindow) {
        for (index, table) in host.tableViews.enumerated() {
            let heights = (0..<table.numberOfRows).map { Int(table.rect(ofRow: $0).height) }
            print("DIAG \(label) table\(index) \(type(of: table)) rows=\(table.numberOfRows) rowHeight=\(table.rowHeight) auto=\(table.usesAutomaticRowHeights) width=\(Int(table.bounds.width)) heights=\(heights)")
        }
    }

    @Test func diagListAlone() throws {
        let model = launchingModel()
        model.hasLoadedNavigation = false
        let host = HostWindow(NavigationStack { NoteListView() }.environment(model).fortressTheme(), size: CGSize(width: 340, height: 660))
        dump("alone-empty", host)
        load(model)
        host.settle()
        dump("alone-loaded", host)
        try host.capture(named: "diag-alone")
        host.close()
    }

    @Test func diagMainWindowLaunch() throws {
        let model = launchingModel()
        let host = HostWindow(MainWindow().environment(model))
        dump("main-start", host)
        model.isConnected = false
        host.settle()
        dump("main-disconnected", host)
        load(model)
        host.settle()
        dump("main-loaded", host)
        try host.capture(named: "diag-main-launch")
        model.scope = .rough
        host.settle()
        model.scope = .all
        host.settle()
        dump("main-rescoped", host)
        host.close()
    }

    @Test func diagMainWindowLoadedFromStart() throws {
        let model = ShellFixtures.model(openNote: "n2")
        let host = HostWindow(MainWindow().environment(model))
        dump("main-control", host)
        host.close()
    }

    @Test func diagMainWindowStepwise() throws {
        let model = launchingModel()
        model.isConnected = false
        let host = HostWindow(MainWindow().environment(model))
        model.isConnected = true
        host.settle(0.2)
        dump("step-connected", host)
        model.navigation = ShellFixtures.navigation
        host.settle()
        dump("step-navigation", host)
        model.hasLoadedNavigation = true
        model.open(noteId: "n2")
        host.settle()
        dump("step-open", host)
        host.close()
    }

    private func mainLoaded(_ label: String, poke: (HostWindow) -> Void) {
        let model = launchingModel()
        let host = HostWindow(MainWindow().environment(model))
        load(model)
        host.settle()
        dump("\(label)-before", host)
        poke(host)
        host.settle()
        dump("\(label)-after", host)
        host.close()
    }

    @Test func diagPokeNoteHeights() {
        mainLoaded("poke-noteheights") { host in
            let table = host.tableViews[0]
            table.noteHeightOfRows(withIndexesChanged: IndexSet(0..<table.numberOfRows))
        }
    }

    @Test func diagPokeLayout() {
        mainLoaded("poke-layout") { host in
            host.tableViews[0].enclosingScrollView?.needsLayout = true
            host.tableViews[0].enclosingScrollView?.tile()
            host.window.contentView?.layoutSubtreeIfNeeded()
        }
    }

    @Test func diagPokeResize() {
        mainLoaded("poke-resize") { host in
            host.window.setContentSize(CGSize(width: 1001, height: 660))
        }
    }

    @Test func diagNonEmptyFirst() {
        let model = launchingModel()
        var one = ShellFixtures.navigation
        one.items = one.items.filter { if case .document = $0 { true } else { false } }.prefix(1).map { $0 }
        model.navigation = one
        let host = HostWindow(MainWindow().environment(model))
        dump("nonempty-first", host)
        load(model)
        host.settle()
        dump("nonempty-loaded", host)
        host.close()
    }

    @Test func diagPlainSplitView() {
        let model = launchingModel()
        let host = HostWindow(
            NavigationSplitView {
                Text("Sidebar")
            } content: {
                NoteListView()
            } detail: {
                Text("Detail")
            }
            .environment(model).fortressTheme())
        load(model)
        host.settle()
        dump("plainsplit", host)
        host.close()
    }

    @Test func diagGenericSplitList() {
        let items = DiagItems()
        let host = HostWindow(DiagSplitList(items: items))
        items.ids = Array(0..<6)
        host.settle()
        dump("generic-split", host)
        host.close()
    }

    @Test func diagGenericStackList() {
        let items = DiagItems()
        let host = HostWindow(NavigationStack { DiagList(items: items) }, size: CGSize(width: 300, height: 660))
        items.ids = Array(0..<6)
        host.settle()
        dump("generic-stack", host)
        host.close()
    }

    @Test func diagListAloneNarrow() {
        let model = launchingModel()
        let host = HostWindow(NavigationStack { NoteListView() }.environment(model).fortressTheme(), size: CGSize(width: 291, height: 660))
        load(model)
        host.settle()
        dump("alone-narrow", host)
        host.close()
    }

    private func variant<Row: View>(_ label: String, sections: Bool = false, @ViewBuilder row: @escaping (NavigationDocument) -> Row) {
        let items = DiagNotes()
        let host = HostWindow(
            NavigationSplitView { Text("Sidebar") } content: {
                DiagVariantList(items: items, sections: sections, row: row)
            } detail: { Text("Detail") })
        items.notes = Array(ShellFixtures.notes.prefix(5))
        host.settle()
        dump("variant-\(label)", host)
        host.close()
    }

    @Test func diagVariants() {
        variant("noterow") { note in NoteRow(note: note, folderName: nil) }
        variant("noterow-sections", sections: true) { note in NoteRow(note: note, folderName: nil) }
        variant("fixed-sections", sections: true) { note in Text(note.title).frame(height: 80) }
        variant("reserves") { note in Text(note.snippet).lineLimit(2, reservesSpace: true) }
        variant("linelimit") { note in Text(note.snippet).lineLimit(2) }
        variant("wrapping") { note in Text(note.snippet) }
        variant("two-texts") { note in VStack(alignment: .leading) { Text(note.title).font(.headline); Text("one\ntwo") } }
        variant("fixed") { note in Text(note.title).frame(height: 80) }
    }

    @Test func selectedRow() throws {
        for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            let model = ShellFixtures.model(openNote: "n5")
            let host = HostWindow(MainWindow().environment(model), appearance: appearance)
            try host.capture(named: "diag-selected-unfocused-\(name)")
            host.window.makeFirstResponder(host.tableViews[0])
            host.settle()
            try host.capture(named: "diag-selected-focused-\(name)")
            host.close()
        }
    }
}

@Observable @MainActor final class DiagItems {
    var ids: [Int] = []
}

struct DiagList: View {
    let items: DiagItems
    var body: some View {
        List(selection: .constant(Int?.none)) {
            Section("Header") {
                ForEach(items.ids, id: \.self) { id in
                    Text("Row \(id)").frame(height: 80)
                }
            }
        }
        .listStyle(.inset)
    }
}

struct DiagSplitList: View {
    let items: DiagItems
    var body: some View {
        NavigationSplitView {
            Text("Sidebar")
        } content: {
            DiagList(items: items)
        } detail: {
            Text("Detail")
        }
    }
}

@Observable @MainActor final class DiagNotes {
    var notes: [NavigationDocument] = []
}

struct DiagVariantList<Row: View>: View {
    let items: DiagNotes
    let sections: Bool
    let row: (NavigationDocument) -> Row

    var body: some View {
        List(selection: .constant(String?.none)) {
            if sections {
                ForEach(Array(items.notes.enumerated()), id: \.element.id) { index, note in
                    Section("Section \(index)") { row(note) }
                }
            } else {
                Section("Header") {
                    ForEach(items.notes) { note in row(note) }
                }
            }
        }
        .listStyle(.inset)
    }
}
