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
}
