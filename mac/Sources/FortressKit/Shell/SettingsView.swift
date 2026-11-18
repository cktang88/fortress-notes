import AppKit
import SwiftUI

/// The Settings window: where the backend lives and how it starts, and backups.
public struct SettingsView: View {
    /// Settings reopens on the pane last viewed (HIG).
    @AppStorage("settingsPane") private var pane = "general"

    public init() {}

    public var body: some View {
        TabView(selection: $pane) {
            Tab("General", systemImage: "gearshape", value: "general") {
                BackendSettingsView()
            }
            Tab("Backups", systemImage: "clock.arrow.circlepath", value: "backups") {
                BackupsView()
                    .padding(20)
            }
        }
        .frame(width: 560, height: 520)
    }
}

/// Backend address, folder and automatic start.
struct BackendSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var isChoosingFolder = false
    @AppStorage(PictureTextReader.enabledKey) private var readsPictureText = true

    var body: some View {
        @Bindable var backend = model.backend
        Form {
            Section {
                TextField("Address", text: $backend.urlString, prompt: Text(BackendController.defaultURL.absoluteString))
                    .onSubmit { Task { await model.retryConnection() } }
                LabeledContent("Status") {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(statusColor)
                            .frame(width: 7, height: 7)
                        Text(statusText)
                            .foregroundStyle(.secondary)
                    }
                }
            } header: {
                Text("Backend")
            } footer: {
                Text("Fortress keeps your notes in a local server. Change the address only if you run it somewhere else.")
                    .foregroundStyle(.secondary)
            }

            Section {
                LabeledContent("Folder") {
                    HStack(spacing: 8) {
                        Text(backend.folderPath.isEmpty ? "Not set" : (backend.folderPath as NSString).abbreviatingWithTildeInPath)
                            .foregroundStyle(backend.folderPath.isEmpty ? .tertiary : .secondary)
                            .lineLimit(1)
                            .truncationMode(.head)
                        Button("Choose…") { isChoosingFolder = true }
                    }
                }
                Toggle("Start the backend automatically", isOn: $backend.startsAutomatically)
                if let launch = backend.launch {
                    LabeledContent("Command") {
                        Text(launch.displayCommand)
                            .font(.callout.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                } else if !backend.folderPath.isEmpty {
                    Label("No backend in this folder. Choose the Fortress folder or its backend folder.", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.secondary)
                }
                HStack {
                    if backend.isProcessRunning {
                        Button("Stop Backend") { Task { await backend.stop() } }
                    } else {
                        Button("Start Backend") { Task { await model.startBackend() } }
                            .disabled(!backend.canStart || model.isConnected)
                    }
                    Button("Show Log") { NSWorkspace.shared.open(backend.logURL) }
                        .disabled(!FileManager.default.fileExists(atPath: backend.logURL.path))
                }
            } header: {
                Text("Starting the Backend")
            } footer: {
                Text("When nothing answers at launch, Fortress starts the backend from this folder and stops it when you quit.")
                    .foregroundStyle(.secondary)
            }

            Section {
                QuickCaptureShortcutRecorder(state: QuickCapture.shared.state)
            } header: {
                Text("Keyboard Shortcut")
            } footer: {
                Text("Opens Quick Capture from any app. Click the shortcut, then press the keys you want; esc cancels, delete turns it off.")
                    .foregroundStyle(.secondary)
            }

            Section {
                Toggle("Read text in pictures and PDFs on this Mac", isOn: $readsPictureText)
            } header: {
                Text("Text in Pictures")
            } footer: {
                Text("Apple Vision reads the words in pasted pictures, scans and PDFs (handwriting and most scripts included) so search and Ask find them. It runs in the background and never changes your notes.")
                    .foregroundStyle(.secondary)
            }

            Section {
                LabeledContent("Converter") {
                    if let converter = backend.launch?.onenoteConverter {
                        Text((converter.path as NSString).abbreviatingWithTildeInPath)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.head)
                            .help(converter.path)
                    } else {
                        Text("Not built")
                            .foregroundStyle(.tertiary)
                    }
                }
                HStack {
                    // The sheet opens in the main window last in front.
                    Button("Import from OneNote…") { model.library.activeWindowModel?.showOneNoteImport() }
                        .disabled(!model.isConnected || model.library.activeWindowModel == nil)
                }
            } header: {
                Text("Importing from OneNote")
            } footer: {
                Group {
                    if backend.launch?.onenoteConverter == nil {
                        Text("Build the converter once with `cargo build --release` in tools/fortress-onenote; Fortress hands it to the backend it starts.")
                    } else {
                        Text("Use File ▸ Import ▸ From OneNote… to bring in exported notebooks. Importing again updates them and keeps your edits.")
                    }
                }
                .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .fileImporter(isPresented: $isChoosingFolder, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result { model.backend.folderPath = url.path }
        }
    }

    private var statusText: String {
        switch model.backend.state {
        case .starting: return "Starting…"
        case .failed(let reason): return model.isConnected ? "Connected" : reason
        case .running: return model.isConnected ? "Connected · started by Fortress" : "Not responding"
        case .idle: return model.isConnected ? "Connected" : "Not connected"
        }
    }

    private var statusColor: Color {
        if model.backend.state == .starting { return .orange }
        return model.isConnected ? .green : .secondary
    }
}
