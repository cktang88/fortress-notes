import AppKit
import SwiftUI
import Testing

@testable import FortressKit

/// The editor, note list and Ask in two bundled Base16 themes (artifact "snapshots").
/// The default Paper/Night look is in the other galleries. Theme choice is app-wide,
/// so CI runs this suite on its own (`--filter ThemeGallerySnapshotTests`); everything
/// here is synchronous on the main actor and restores the defaults at the end.
@MainActor
@Suite(.serialized) struct ThemeGallerySnapshotTests {
    private func withThemes(light: String, dark: String, _ body: () throws -> Void) rethrows {
        let store = ThemeStore.shared
        let saved = (store.lightThemeID, store.darkThemeID)
        store.lightThemeID = light
        store.darkThemeID = dark
        defer {
            store.lightThemeID = saved.0
            store.darkThemeID = saved.1
        }
        try body()
    }

    private func shots(_ slug: String, appearance: NSAppearance.Name) throws {
        let page = try RichPage.load()
        let model = AppModel()
        model.connectsOnAppear = false
        model.navigation = NavigationResponse(items: [.folder(NavigationFolder(
            id: "lab", parentId: nil, name: "Lab", position: 0,
            children: [.document(NavigationDocument(id: page.id, title: page.title,
                                                    updatedAt: page.updatedAt, createdAt: page.createdAt,
                                                    snippet: "", folderId: "lab", position: 0))]))], recent: [])
        try snapshot(NoteEditorView(session: EditorSession(document: page)).environment(model).fortressTheme(),
                     named: "theme-\(slug)-editor-onenote", appearance: appearance)

        let rich = try richDocument()
        try snapshot(NoteEditorView(session: EditorSession(document: rich)).environment(model).fortressTheme(),
                     named: "theme-\(slug)-editor", appearance: appearance)

        try snapshot(MainWindow().environment(ShellFixtures.model()), named: "theme-\(slug)-library", appearance: appearance)

        var state = AskState()
        FindingFixtures.steps.forEach { state.apply(.step($0)) }
        state.apply(.done(AskResponse(status: .answered, answer: FindingFixtures.sentences,
                                      steps: FindingFixtures.steps, elapsedMs: 1840)))
        let turn = AskTurn(question: FindingFixtures.question, state: state)
        try snapshot(AskView(question: FindingFixtures.question, turns: [turn])
                        .environment(FindingFixtures.model()).fortressTheme(),
                     named: "theme-\(slug)-ask", size: CGSize(width: 760, height: 660), appearance: appearance)
    }

    @Test func solarizedLightAndCatppuccinMocha() throws {
        try withThemes(light: "base16.solarized-light", dark: "base16.catppuccin-mocha") {
            try shots("solarized-light", appearance: .aqua)
            try shots("catppuccin-mocha", appearance: .darkAqua)
        }
    }

    /// Settings ▸ Appearance with the theme pickers.
    @Test func appearanceSettings() throws {
        try snapshot(AppearanceSettingsView().fortressTheme(), named: "theme-settings-light",
                     size: CGSize(width: 560, height: 720))
        try snapshot(AppearanceSettingsView().fortressTheme(), named: "theme-settings-dark",
                     size: CGSize(width: 560, height: 720), appearance: .darkAqua)
    }
}
