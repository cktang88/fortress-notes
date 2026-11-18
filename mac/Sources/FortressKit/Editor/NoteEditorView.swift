import AppKit
import SwiftUI

/// Title, meta line and the block editor for one note.
struct NoteEditorView: View {
    let documentId: String
    let focusBlockId: String?
    @Environment(AppModel.self) private var model
    @State private var session: EditorSession?
    @State private var showProgress = false

    init(documentId: String, focusBlockId: String?) {
        self.documentId = documentId
        self.focusBlockId = focusBlockId
    }

    /// A note that is already loaded (previews and snapshots).
    init(session: EditorSession, focusBlockId: String? = nil) {
        self.documentId = session.documentId
        self.focusBlockId = focusBlockId
        _session = State(initialValue: session)
    }

    var body: some View {
        ZStack(alignment: .top) {
            Color(nsColor: .textBackgroundColor)
            if let session, session.document != nil {
                BlockTextEditor(session: session, model: model, focusBlockId: focusBlockId,
                                focusRequest: model.editorFocusRequest)
                SaveBanner(session: session)
            } else if let error = session?.loadError {
                ContentUnavailableView {
                    Label("Couldn't Open This Note", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(error)
                } actions: {
                    Button("Try Again") { Task { await session?.load() } }
                }
            } else if showProgress {
                ProgressView().controlSize(.small).padding(.top, 120)
            }
        }
        .task(id: documentId) {
            guard session?.document == nil else { return }
            let session = self.session ?? EditorSession(documentId: documentId, api: model.api)
            self.session = session
            Task { try? await Task.sleep(for: .milliseconds(300)); showProgress = true }
            await session.load()
        }
        .onDisappear {
            guard let session else { return }
            model.focusedBlockId = nil
            Task {
                await session.flush()
                await session.commitTitle()
                session.detach()
            }
        }
    }
}

/// A calm banner for a conflict or lost connection, over the top of the note.
private struct SaveBanner: View {
    let session: EditorSession

    var body: some View {
        Group {
            switch session.saveState {
            case .conflict:
                banner(symbol: "arrow.triangle.2.circlepath", text: "This note changed elsewhere.") {
                    Button("Load Latest") { Task { await session.loadLatest() } }
                        .controlSize(.small)
                }
            case .offline:
                banner(symbol: "wifi.slash", text: "Can't reach Fortress. Your edits are kept and will save when it's back.") {
                    EmptyView()
                }
            default:
                EmptyView()
            }
        }
        .animation(.smooth(duration: 0.25), value: session.saveState)
    }

    private func banner<Actions: View>(symbol: String, text: String, @ViewBuilder actions: () -> Actions) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).foregroundStyle(.secondary)
            Text(text).font(.callout).foregroundStyle(.secondary)
            actions()
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5))
        .padding(.top, 12)
        .transition(.opacity)
    }
}

/// The TextKit 2 body with the header (title, meta line) hosted above the text.
struct BlockTextEditor: NSViewRepresentable {
    let session: EditorSession
    let model: AppModel
    let focusBlockId: String?
    var focusRequest = 0

    final class Coordinator {
        let controller: BlockEditorController
        var focusedRequest: String?
        var focusRequest = 0
        init(controller: BlockEditorController) { self.controller = controller }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(controller: BlockEditorController(baseURL: session.api.baseURL))
    }

    func makeNSView(context: Context) -> NSScrollView {
        let controller = context.coordinator.controller
        let session = session
        let model = model
        let api = session.api

        let header = NSHostingView(rootView: EditorHeader(
            session: session, model: model,
            onSubmitTitle: { [weak controller] in controller?.focusStart() },
            onHeightChange: { [weak controller] height in controller?.textView.headerDidChange(height: height) }))
        header.sizingOptions = []
        controller.textView.header = header

        context.coordinator.focusRequest = focusRequest
        controller.context.adoptResolvedLinks(session.document?.resolvedLinks)
        controller.load(session.initialBlocks)
        session.resetBase(to: controller.currentBlocks())
        session.currentBlocks = { controller.currentBlocks() }
        session.showBlocks = { blocks in
            controller.context.adoptResolvedLinks(session.document?.resolvedLinks)
            controller.load(blocks)
        }
        session.onDocumentChange = { document in model.noteDidChange(document) }
        controller.onChange = {
            session.textDidChange()
            session.updateAutoTitle(from: controller.leadingTexts())
        }
        controller.onFocusBlock = { id in model.focusedBlockId = id }
        controller.onOpenNote = { id, block in model.open(noteId: id, blockId: block) }
        let documentId = session.documentId
        controller.searchNotes = { query in
            await Self.suggestions(for: query, excluding: documentId, model: model)
        }
        controller.context.resolveReference = { blockId in
            let targets = try? await api.linkTargets(query: blockId, limit: 5)
            return targets?.first { $0.blockId == blockId }?.text
        }
        controller.uploadFile = { data, name, type in try await EditorUpload.upload(data, name: name, type: type, baseURL: api.baseURL) }

        if let focusBlockId {
            context.coordinator.focusedRequest = focusBlockId
            Self.later { controller.focus(blockId: focusBlockId) }
        } else if session.document?.children.allSatisfy({ $0.text.isEmpty && $0.children.isEmpty }) ?? false {
            // A new, empty note: start typing.
            Self.later { controller.focusStart() }
        }
        return controller.scrollView
    }

    func updateNSView(_ scrollView: NSScrollView, context: Context) {
        if focusRequest != context.coordinator.focusRequest {
            context.coordinator.focusRequest = focusRequest
            let controller = context.coordinator.controller
            Self.later { controller.focusCaret() }
        }
        if let focusBlockId, focusBlockId != context.coordinator.focusedRequest {
            context.coordinator.focusedRequest = focusBlockId
            let controller = context.coordinator.controller
            Self.later { controller.focus(blockId: focusBlockId) }
        }
    }

    /// After the current layout pass (a run loop turn, which also runs while the main
    /// queue is busy, unlike a GCD hop).
    static func later(_ body: @escaping @MainActor () -> Void) {
        RunLoop.main.perform(inModes: [.common]) { MainActor.assumeIsolated { body() } }
    }

    static func dismantleNSView(_ scrollView: NSScrollView, coordinator: Coordinator) {
        coordinator.controller.onChange = nil
        coordinator.controller.onFocusBlock = nil
    }

    /// Notes for `[[` completion: recent notes for an empty query, then title and text
    /// matches from the server, one row per note.
    @MainActor
    static func suggestions(for query: String, excluding documentId: String, model: AppModel) async -> [LinkSuggestion] {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        var output: [LinkSuggestion] = []
        var seen: Set<String> = [documentId]
        if trimmed.isEmpty {
            for note in model.navigation.recent where !seen.contains(note.id) {
                seen.insert(note.id)
                output.append(LinkSuggestion(documentId: note.id, title: note.title, detail: note.snippet))
            }
            return output
        }
        let targets = (try? await model.api.linkTargets(query: trimmed, limit: 40)) ?? []
        let lowered = trimmed.lowercased()
        let titled = targets.filter { $0.documentTitle.lowercased().contains(lowered) }
        for target in titled + targets where !seen.contains(target.documentId) {
            seen.insert(target.documentId)
            let detail = target.documentTitle.lowercased().contains(lowered) ? "" : target.text
            output.append(LinkSuggestion(documentId: target.documentId, title: target.documentTitle, detail: detail))
        }
        return output
    }
}

/// Title (28 bold; ↩ moves into the body) and the meta line: status, edited, folder.
struct EditorHeader: View {
    @Bindable var session: EditorSession
    let model: AppModel
    var onSubmitTitle: () -> Void
    var onHeightChange: (CGFloat) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("Untitled", text: Binding(get: { session.title }, set: { session.titleDidChange($0) }), axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 28, weight: .bold))
                .lineLimit(1...4)
                .onSubmit {
                    Task { await session.commitTitle() }
                    onSubmitTitle()
                }
                .accessibilityLabel("Title")
            MetaLine(session: session, folder: model.folderName(forNote: session.documentId))
        }
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { onHeightChange($0) }
        .frame(maxHeight: .infinity, alignment: .top)
    }
}

private struct MetaLine: View {
    let session: EditorSession
    let folder: String?

    var body: some View {
        HStack(spacing: 6) {
            Menu {
                ForEach(NoteStatus.allCases, id: \.self) { status in
                    Button {
                        Task { await session.setStatus(status) }
                    } label: {
                        Label(status.label, systemImage: status.symbol)
                    }
                }
            } label: {
                HStack(spacing: 3) {
                    Image(systemName: session.status.symbol)
                    Text(session.status.label)
                }
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .menuIndicator(.visible)
            .fixedSize()
            .help("Status")
            if let updatedAt = session.updatedAt {
                Text("·").foregroundStyle(.quaternary)
                TimelineView(.periodic(from: .now, by: 60)) { context in
                    Text("Edited \(Self.relative(updatedAt, now: context.date))")
                }
            }
            if let folder {
                Text("·").foregroundStyle(.quaternary)
                Label(folder, systemImage: "folder").labelStyle(.titleAndIcon)
            }
        }
        .font(.system(size: 12))
        .foregroundStyle(.secondary)
        .monospacedDigit()
    }

    static func relative(_ date: Date, now: Date) -> String {
        if now.timeIntervalSince(date) < 60 { return "just now" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: now)
    }
}

extension NoteStatus {
    var label: String {
        switch self {
        case .rough: "Rough"
        case .polished: "Polished"
        }
    }

    var symbol: String {
        switch self {
        case .rough: "circle.dashed"
        case .polished: "checkmark.seal"
        }
    }
}

// MARK: Editor helpers on the shared model

extension AppModel {
    /// The folder a note is filed in, from the sidebar tree.
    func folderName(forNote id: String) -> String? {
        func search(_ nodes: [NavigationNode], folder: String?) -> String?? {
            for node in nodes {
                switch node {
                case .document(let document) where document.id == id:
                    return .some(folder)
                case .folder(let child):
                    if let found = search(child.children, folder: child.name) { return found }
                default:
                    continue
                }
            }
            return nil
        }
        return search(navigation.items, folder: nil) ?? nil
    }
}

/// Uploads a pasted or dropped file (`POST /api/files`).
enum EditorUpload {
    struct Response: Decodable {
        struct Props: Decodable {
            var url: String
            var name: String?
        }
        var props: Props
    }

    static func upload(_ data: Data, name: String, type: String, baseURL: URL) async throws -> (url: String, name: String) {
        let boundary = "fortress-\(UUID().uuidString)"
        var request = URLRequest(url: baseURL.appending(path: "api/files"))
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        let safeName = name.replacingOccurrences(of: "\"", with: "'")
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\nContent-Type: \(type)\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        request.httpBody = body
        let (responseData, response) = try await APIClient.sharedSession.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError(status: http.statusCode, message: HTTPURLResponse.localizedString(forStatusCode: http.statusCode))
        }
        let decoded = try JSONDecoder().decode(Response.self, from: responseData)
        return (decoded.props.url, decoded.props.name ?? name)
    }
}
