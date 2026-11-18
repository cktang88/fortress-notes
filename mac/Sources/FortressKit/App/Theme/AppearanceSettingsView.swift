import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Settings ▸ Appearance: System/Light/Dark, a theme for light and one for dark (the
/// system appearance picks between them), importing Base16/Base24 or VS Code themes,
/// and the bundled schemes' licences.
public struct AppearanceSettingsView: View {
    @State private var store = ThemeStore.shared
    @State private var isImporting = false
    @State private var importError: String?
    @State private var showsLicenses = false

    public init() {}

    static let importTypes: [UTType] = ["yaml", "yml", "json", "jsonc"].compactMap { UTType(filenameExtension: $0) }

    public var body: some View {
        @Bindable var store = store
        Form {
            Section {
                Picker("Appearance", selection: $store.appearance) {
                    ForEach(AppearanceMode.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
            } footer: {
                Text("System follows the setting in System Settings ▸ Appearance, switching between the light and dark themes below.")
                    .foregroundStyle(.secondary)
            }

            Section("Light Theme") {
                ThemeGrid(themes: store.lightThemes, selection: $store.lightThemeID, onRemove: { store.remove($0) })
            }
            Section("Dark Theme") {
                ThemeGrid(themes: store.darkThemes, selection: $store.darkThemeID, onRemove: { store.remove($0) })
            }

            Section {
                HStack {
                    Button("Import Theme…") { isImporting = true }
                    Button("Show Themes Folder") {
                        try? FileManager.default.createDirectory(at: ThemeLibrary.importDirectory, withIntermediateDirectories: true)
                        NSWorkspace.shared.open(ThemeLibrary.importDirectory)
                    }
                    Spacer()
                    Button("Restore Defaults") { store.resetToDefaults() }
                }
                if let importError {
                    Label(importError, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.secondary)
                }
            } footer: {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Import a Base16 or Base24 scheme (.yaml, such as any of the hundreds at github.com/tinted-theming/schemes) or a VS Code color theme (.json). Themes color your notes; the sidebar and toolbar stay native and take the theme's accent. Text stays readable: colors that would be too faint on a theme's background are adjusted.")
                    Button(showsLicenses ? "Hide Acknowledgements" : "Acknowledgements") { showsLicenses.toggle() }
                        .buttonStyle(.link)
                    if showsLicenses {
                        Text(ThemeLibrary.licenseNotices)
                            .font(.caption)
                            .textSelection(.enabled)
                    }
                }
                .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .fileImporter(isPresented: $isImporting, allowedContentTypes: Self.importTypes, allowsMultipleSelection: true) { result in
            importError = nil
            guard case .success(let urls) = result else { return }
            for url in urls {
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                do {
                    try store.importTheme(from: url)
                } catch {
                    importError = error.localizedDescription
                }
            }
        }
    }
}

/// Theme cards: a small page in the theme's own colours, its name and author.
struct ThemeGrid: View {
    let themes: [Theme]
    @Binding var selection: String
    var onRemove: @MainActor (Theme) -> Void

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 112, maximum: 140), spacing: 12)], alignment: .leading, spacing: 12) {
            ForEach(themes) { theme in
                Button {
                    selection = theme.id
                } label: {
                    ThemeSwatch(theme: theme, isSelected: theme.id == selection)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(theme.name)
                .accessibilityAddTraits(theme.id == selection ? .isSelected : [])
                .help(theme.author.isEmpty ? theme.name : "\(theme.name) — \(theme.author)")
                .contextMenu {
                    if case .imported = theme.source {
                        Button("Remove Theme", role: .destructive) { onRemove(theme) }
                    }
                }
            }
        }
        .padding(.vertical, 4)
    }
}

/// A live preview of a palette: page, sunken list strip, title, body and secondary text
/// lines, a link, a highlight, and the theme's mark colours.
struct ThemeSwatch: View {
    let theme: Theme
    var isSelected: Bool

    var body: some View {
        let p = theme.palette
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 0) {
                Rectangle().fill(p.sunken.color).frame(width: 18)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Aa").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.text.color)
                    Capsule().fill(p.text.color).frame(width: 52, height: 3)
                    HStack(spacing: 3) {
                        Capsule().fill(p.secondaryText.color).frame(width: 22, height: 3)
                        Capsule().fill(p.link.color).frame(width: 18, height: 3)
                    }
                    Capsule().fill(p.highlight.color).frame(width: 30, height: 5)
                    HStack(spacing: 3) {
                        ForEach([MarkHue.red, .orange, .yellow, .green, .blue, .purple], id: \.self) { hue in
                            Circle().fill(p.markText(hue).color).frame(width: 6, height: 6)
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                Spacer(minLength: 0)
            }
            .frame(height: 64)
            .background(p.background.color)
            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .strokeBorder(isSelected ? AnyShapeStyle(.tint) : AnyShapeStyle(Color.primary.opacity(0.12)),
                                  lineWidth: isSelected ? 2.5 : 1)
            }
            Text(theme.name)
                .font(.caption)
                .lineLimit(1)
                .foregroundStyle(isSelected ? .primary : .secondary)
        }
        .contentShape(Rectangle())
    }
}
