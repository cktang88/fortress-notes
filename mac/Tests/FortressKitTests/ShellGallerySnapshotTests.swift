import AppKit
import SwiftUI
import Testing

@testable import FortressKit

/// Shell screens with fixture data, in light and dark (artifact "snapshots").
@MainActor
@Suite(.serialized) struct ShellGallerySnapshotTests {
    private func both<V: View>(_ name: String, size: CGSize = CGSize(width: 1000, height: 660), _ view: () -> V) throws {
        try snapshot(view(), named: "shell-\(name)-light", size: size)
        try snapshot(view(), named: "shell-\(name)-dark", size: size, appearance: .darkAqua)
    }

    @Test func library() throws {
        let model = ShellFixtures.model()
        try both("library") { MainWindow().environment(model) }
    }

    @Test func folderWithoutSelection() throws {
        let model = ShellFixtures.model(scope: .folder(id: "work"), openNote: nil)
        try snapshot(MainWindow().environment(model), named: "shell-folder-light")
    }

    @Test func emptyFolder() throws {
        let model = ShellFixtures.model(scope: .folder(id: "meetings"), openNote: nil)
        try snapshot(MainWindow().environment(model), named: "shell-empty-folder-light")
    }

    @Test func trash() throws {
        let model = ShellFixtures.model(scope: .trash, openNote: nil)
        model.selectedTrashId = "t2"
        try both("trash") { MainWindow().environment(model) }
    }

    @Test func undoToast() throws {
        let model = ShellFixtures.model(openNote: "n2")
        model.toast = Toast(message: "“Packing list” moved to Trash", action: .restore(documentId: "n5"))
        try both("toast") { MainWindow().environment(model) }
    }

    @Test func disconnected() throws {
        let model = AppModel(api: APIClient(baseURL: URL(string: "http://127.0.0.1:9")!))
        model.connectsOnAppear = false
        model.isConnected = false
        try both("disconnected") { MainWindow().environment(model) }
    }

    @Test func offlineWithNotes() throws {
        let model = ShellFixtures.model(scope: .recent)
        model.isConnected = false
        try snapshot(MainWindow().environment(model), named: "shell-offline-light")
    }

    @Test func backupsSheet() throws {
        let model = ShellFixtures.model()
        try both("backups", size: CGSize(width: 560, height: 420)) {
            BackupsView(isSheet: true).environment(model).frame(width: 560, height: 420)
        }
    }

    @Test func importSheet() throws {
        let model = ShellFixtures.model()
        let files = ["Kyoto itinerary.md", "Reading list.md", "Recipes/Sourdough.md"].map { URL(fileURLWithPath: "/tmp/\($0)") }
        let request = MarkdownImportRequest(files: files, folderId: "travel")
        try both("import", size: CGSize(width: 480, height: 330)) {
            ImportSheet(request: request).environment(model)
        }
        let result = MarkdownImportResult(
            imported: [.init(name: "a.md", id: "x", title: "A"), .init(name: "b.md", id: "y", title: "B")],
            errors: [.init(name: "Recipes/Sourdough.md", detail: "file is not UTF-8 text")])
        try snapshot(
            ImportSheet(request: request, result: result).environment(model),
            named: "shell-import-done-light", size: CGSize(width: 480, height: 170))
    }

    @Test func settings() throws {
        let model = ShellFixtures.model()
        try both("settings", size: CGSize(width: 560, height: 440)) {
            BackendSettingsView().environment(model).padding(.top, 8)
        }
    }

    /// HIG audit (UX_AUDIT.md B4, F9): the toast under Reduce Transparency, and the Quick Capture shortcut recorder in Settings.
    @Test func accessibilityFallbacks() throws {
        let model = ShellFixtures.model(openNote: "n2")
        model.toast = Toast(message: "“Packing list” moved to Trash", action: .restore(documentId: "n5"))
        try both("toast-reduce-transparency") {
            MainWindow().environment(model).environment(\.accessibilityReduceTransparency, true)
        }
        try both("shortcut-recorder", size: CGSize(width: 560, height: 160)) {
            Form {
                Section("Keyboard Shortcut") { QuickCaptureShortcutRecorder(state: QuickCaptureState()) }
            }
            .formStyle(.grouped)
        }
    }
}
