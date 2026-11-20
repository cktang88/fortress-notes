import AppKit
import SwiftUI

/// Shows `AppModel.toast` (or else the latest notice) as a glass capsule at the bottom
/// center for five seconds.
struct ToastHost: View {
    @Environment(AppModel.self) private var model
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The timer waits while the pointer is on the toast (HIG: no time-boxed UI people
    /// can't finish acting on). Its Undo is also Edit ▸ Undo, after it's gone too.
    @State private var isHovering = false

    var body: some View {
        let toast = model.toast ?? (model.isConnected ? model.notice.map { Toast(id: $0.id, message: $0.message) } : nil)
        ZStack {
            if let toast {
                ToastCapsule(toast: toast)
                    .id(toast.id)
                    .onHover { isHovering = $0 }
                    .transition(reduceMotion ? .opacity : .move(edge: .bottom).combined(with: .opacity))
                    .onAppear { AccessibilityNotification.Announcement(AttributedString(toast.message)).post() }
                    .task(id: ToastTimer(id: toast.id, isPaused: isHovering)) {
                        guard !isHovering else { return }
                        try? await Task.sleep(for: .seconds(5))
                        guard !Task.isCancelled else { return }
                        if model.toast?.id == toast.id { model.toast = nil }
                        if model.notice?.message == toast.message { model.notice = nil }
                    }
            }
        }
        .padding(.bottom, 20)
        .animation(reduceMotion ? .easeOut(duration: 0.15) : .smooth(duration: 0.25), value: toast?.id)
    }
}

private struct ToastTimer: Hashable {
    var id: UUID
    var isPaused: Bool
}

struct ToastCapsule: View {
    @Environment(AppModel.self) private var model
    let toast: Toast

    var body: some View {
        HStack(spacing: 12) {
            Text(toast.message)
                .lineLimit(2)
            if let action = toast.action, let label = toast.actionLabel {
                Divider().frame(height: 14)
                Button(label) { Task { await model.perform(action) } }
                    .buttonStyle(.plain)
                    .fontWeight(.semibold)
                    .foregroundStyle(.tint)
            }
            Button {
                model.toast = nil
                model.notice = nil
            } label: {
                Image(systemName: "xmark")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.themeSecondary)
                    .frame(width: 20, height: 20)
                    .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .font(.callout)
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .fixedSize()
        .floatingGlass(in: Capsule(), interactive: true)
    }
}

/// A slim strip over the note list while the backend is unreachable but notes are showing.
struct OfflineBanner: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "bolt.horizontal.circle")
            Text("Can't reach Fortress")
                .lineLimit(1)
            Spacer(minLength: 4)
            Button("Retry") { Task { await model.retryConnection() } }
                .controlSize(.small)
        }
        .font(.callout)
        .foregroundStyle(.themeSecondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(ThemeStyle(.surface), in: .rect(cornerRadius: 8))
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }
}

/// Fills the window when the backend can't be reached and nothing has loaded yet.
struct ConnectionUnavailableView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        let backend = model.backend
        Group {
            if backend.state == .starting {
                VStack(spacing: 12) {
                    ProgressView().controlSize(.small)
                    Text("Starting Fortress…")
                        .font(.headline)
                    Text("The first start can take a little while.")
                        .font(.callout)
                        .foregroundStyle(.themeSecondary)
                }
            } else {
                ContentUnavailableView {
                    Label("Can't Reach Fortress", systemImage: "bolt.horizontal.circle")
                } description: {
                    Text(description)
                } actions: {
                    HStack(spacing: 8) {
                        if backend.canStart {
                            Button("Start Backend") { Task { await model.startBackend() } }
                                .buttonStyle(.borderedProminent)
                        }
                        Button("Retry") { Task { await model.retryConnection() } }
                    }
                    HStack(spacing: 12) {
                        Button("Open Settings…") { openSettings() }
                        if case .failed = backend.state, FileManager.default.fileExists(atPath: backend.logURL.path) {
                            Button("Show Log") { NSWorkspace.shared.open(backend.logURL) }
                        }
                    }
                    .buttonStyle(.link)
                    .font(.callout)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .themedContent()
    }

    private var description: String {
        let backend = model.backend
        let address = backend.serverURL.host().map { host in backend.serverURL.port.map { "\(host):\($0)" } ?? host } ?? backend.urlString
        if case .failed(let reason) = backend.state { return reason }
        if backend.canStart { return "Nothing is answering at \(address). Start the backend here, or run it yourself and retry." }
        if backend.isLocal { return "Nothing is answering at \(address). Start the backend, or choose its folder in Settings so Fortress can start it for you." }
        return "Nothing is answering at \(address)."
    }
}

/// Sheets, file panels and dialogs the Shell presents over the main window.
struct ShellPresentations: ViewModifier {
    @Environment(AppModel.self) private var model

    func body(content: Content) -> some View {
        @Bindable var model = model
        content
            .sheet(isPresented: $model.isBackupsShown) {
                BackupsView(isSheet: true)
                    .frame(width: 560, height: 420)
            }
            .sheet(item: $model.pendingImport) { request in
                ImportSheet(request: request)
            }
            .sheet(isPresented: $model.isOneNoteImportShown) {
                OneNoteImportSheet(importer: model.oneNoteImport)
            }
            .fileImporter(
                isPresented: $model.isImporterShown,
                allowedContentTypes: [.markdownText, .plainText, .folder],
                allowsMultipleSelection: true
            ) { result in
                guard case .success(let urls) = result else { return }
                let folder: String? = if case .folder(let id) = model.scope { id } else { nil }
                model.beginImport(urls, into: folder)
            }
            .fileExporter(
                isPresented: Binding(get: { model.pendingExport != nil }, set: { if !$0 { model.pendingExport = nil } }),
                document: model.pendingExport,
                contentType: .markdownText,
                defaultFilename: model.pendingExport?.filename
            ) { result in
                model.pendingExport = nil
                if case .failure(let error) = result { model.report(error) }
            }
    }
}
