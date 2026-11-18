import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// "Import from OneNote": how to export, the file and destination (step 1), progress
/// while the backend converts and imports (step 2), and the report (step 3).
struct OneNoteImportSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let importer: OneNoteImporter

    var body: some View {
        Group {
            switch importer.phase {
            case .choosing:
                OneNoteChooseStep(importer: importer, close: close)
            case .uploading, .running:
                OneNoteProgressStep(importer: importer, close: close)
            case .finished(let job):
                OneNoteReportStep(importer: importer, job: job, close: close)
            case .failed(let message, let converterMissing):
                OneNoteFailedStep(importer: importer, message: message, converterMissing: converterMissing, close: close)
            }
        }
        .frame(width: 560)
        .animation(reduceMotion ? .easeInOut(duration: 0.15) : .smooth(duration: 0.25), value: stepKey)
    }

    private var stepKey: Int {
        switch importer.phase {
        case .choosing: 0
        case .uploading, .running: 1
        case .finished: 2
        case .failed: 3
        }
    }

    private func close() {
        // A finished import's sheet starts fresh next time; a running one keeps going.
        if case .finished = importer.phase { importer.reset() }
        model.isOneNoteImportShown = false
        dismiss()
    }
}

// MARK: Step 1

/// The exact wording of the export how-to.
enum OneNoteExportHelp {
    static let steps = "Go to onenote.com/notebooks, right-click a notebook, choose Export notebook. If you don't see it, on onedrive.com select the notebook and click Download."
    static let locked = "Password-protected sections must be unlocked in OneNote before you export, or they're skipped."
}

private struct OneNoteChooseStep: View {
    @Environment(AppModel.self) private var model
    @Bindable var importer: OneNoteImporter
    let close: () -> Void
    @State private var isTargeted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SheetHeader(
                symbol: "square.and.arrow.down", title: "Import from OneNote",
                subtitle: "Notebooks become folders and pages become notes. Tables, columns, images and attachments come along.")

            VStack(alignment: .leading, spacing: 10) {
                Text("Export your notebook")
                    .font(.headline)
                Text(OneNoteExportHelp.steps)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                Label {
                    Text(OneNoteExportHelp.locked)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "lock")
                }
                .font(.callout)
                .foregroundStyle(.secondary)
            }

            dropZone

            if let problem = importer.fileProblem {
                Label(problem, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 10) {
                Text("Put notebooks in")
                    .foregroundStyle(.secondary)
                Picker("Put notebooks in", selection: $importer.folderId) {
                    Label("Top level", systemImage: "note.text").tag(String?.none)
                    Divider()
                    ForEach(NoteListing.folders(in: model.navigation.items), id: \.folder.id) { entry in
                        Text(String(repeating: "    ", count: entry.depth) + entry.folder.name)
                            .tag(Optional(entry.folder.id))
                    }
                }
                .labelsHidden()
                .fixedSize()
                Spacer(minLength: 0)
            }
            .font(.callout)

            HStack {
                Text("Each notebook gets its own folder.")
                    .font(.callout)
                    .foregroundStyle(.tertiary)
                Spacer()
                Button("Cancel", action: close)
                    .keyboardShortcut(.cancelAction)
                Button("Import") { importer.start(api: model.api) }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(importer.file == nil)
            }
        }
        .padding(24)
    }

    private var dropZone: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(isTargeted ? Color.accentColor.opacity(0.08) : Color(nsColor: .quaternarySystemFill).opacity(0.5))
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(isTargeted ? Color.accentColor : Color(nsColor: .separatorColor),
                              style: StrokeStyle(lineWidth: 1, dash: importer.file == nil ? [5, 4] : []))
            if let file = importer.file {
                HStack(spacing: 12) {
                    Image(nsImage: NSWorkspace.shared.icon(for: UTType(filenameExtension: file.pathExtension) ?? .data))
                        .resizable()
                        .frame(width: 36, height: 36)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(file.lastPathComponent)
                            .font(.body.weight(.medium))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        if let size = importer.fileSize {
                            Text(ByteCountFormatter.string(fromByteCount: size, countStyle: .file))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                    }
                    Spacer(minLength: 12)
                    Button("Choose Another…", action: chooseFile)
                }
                .padding(.horizontal, 18)
            } else {
                VStack(spacing: 6) {
                    Image(systemName: "tray.and.arrow.down")
                        .font(.system(size: 26, weight: .light))
                        .foregroundStyle(.tertiary)
                        .padding(.bottom, 2)
                    Text("Drop the exported file here")
                        .font(.body.weight(.medium))
                    Text(".zip, .onepkg or .one")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Choose File…", action: chooseFile)
                        .padding(.top, 6)
                }
            }
        }
        .frame(height: importer.file == nil ? 156 : 76)
        .dropDestination(for: URL.self) { urls, _ in
            guard let url = urls.first else { return false }
            return importer.choose(url)
        } isTargeted: { isTargeted = $0 }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("OneNote export file")
    }

    private func chooseFile() {
        let panel = NSOpenPanel()
        panel.title = "Choose a OneNote Export"
        panel.prompt = "Choose"
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = OneNoteImport.fileExtensions.compactMap { UTType(filenameExtension: $0) }
        if panel.runModal() == .OK, let url = panel.url {
            importer.choose(url)
        }
    }
}

/// Symbol, title and a line of explanation at the top of each step.
private struct SheetHeader: View {
    let symbol: String
    let title: String
    var subtitle: String?

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: symbol)
                .font(.system(size: 22, weight: .regular))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
                .frame(width: 28, height: 28)
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.title2.weight(.semibold))
                if let subtitle {
                    Text(subtitle)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

// MARK: Step 2

private struct OneNoteProgressStep: View {
    let importer: OneNoteImporter
    let close: () -> Void

    private var job: OneNoteImportJob? {
        if case .running(let job) = importer.phase { return job }
        return nil
    }

    private var step: OneNoteImport.Step {
        job.map { OneNoteImport.step(for: $0.status) } ?? .upload
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SheetHeader(symbol: "square.and.arrow.down", title: "Importing “\(importer.fileName)”",
                        subtitle: "Large notebooks take a few minutes. You can close this window; the import keeps going.")

            VStack(alignment: .leading, spacing: 12) {
                ForEach(OneNoteImport.Step.allCases, id: \.self) { item in
                    StepRow(title: item.title, state: item < step ? .done : item == step ? .current : .pending,
                            detail: detail(for: item))
                }
            }
            .padding(.leading, 42)

            VStack(alignment: .leading, spacing: 8) {
                if let fraction {
                    ProgressView(value: fraction)
                } else {
                    ProgressView()
                        .progressViewStyle(.linear)
                }
                Text(caption)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.leading, 42)

            HStack {
                Spacer()
                Button("Close", action: close)
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(24)
    }

    private var fraction: Double? {
        switch importer.phase {
        case .uploading(let value): return value
        case .running(let job) where job.status == .importing: return job.progress.fraction
        default: return nil
        }
    }

    private var caption: String {
        switch importer.phase {
        case .uploading: return "Sending the export to Fortress…"
        case .running(let job):
            switch job.status {
            case .queued: return "Waiting to start…"
            case .converting: return "Reading sections and pages…"
            case .importing: return job.progress.current ?? "Creating notes…"
            default: return "Connecting links between pages…"
            }
        default: return ""
        }
    }

    private func detail(for item: OneNoteImport.Step) -> String? {
        switch (item, importer.phase) {
        case (.upload, .uploading(let value)):
            return value > 0 ? "\(Int((value * 100).rounded()))%" : nil
        case (.importPages, .running(let job)):
            return OneNoteImport.pageCount(job.progress)
        default:
            return nil
        }
    }
}

private struct StepRow: View {
    enum Phase { case done, current, pending }
    let title: String
    let state: Phase
    let detail: String?

    var body: some View {
        HStack(spacing: 10) {
            Group {
                switch state {
                case .done:
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.secondary)
                case .current:
                    ProgressView().controlSize(.small)
                case .pending:
                    Image(systemName: "circle")
                        .foregroundStyle(.quaternary)
                }
            }
            .frame(width: 18, height: 18)
            Text(title)
                .foregroundStyle(state == .pending ? .tertiary : .primary)
                .fontWeight(state == .current ? .medium : .regular)
            Spacer()
            if let detail {
                Text(detail)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
        .font(.body)
        .accessibilityElement(children: .combine)
    }
}

// MARK: Step 3

private struct OneNoteReportStep: View {
    @Environment(AppModel.self) private var model
    let importer: OneNoteImporter
    let job: OneNoteImportJob
    let close: () -> Void

    private var result: OneNoteImportResult { job.result ?? OneNoteImportResult(notebooks: []) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SheetHeader(symbol: result.problemCount > 0 ? "exclamationmark.circle" : "checkmark.circle",
                        title: OneNoteImport.headline(result), subtitle: OneNoteImport.summary(result))
                .padding([.horizontal, .top], 24)
                .padding(.bottom, 16)

            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(result.notebooks) { notebook in
                        NotebookCard(notebook: notebook) {
                            if let id = notebook.folderId { Task { await model.revealImportedFolder(id) } }
                        }
                    }
                    if !result.links.unresolved.isEmpty || result.links.rewritten > 0 {
                        LinksCard(links: result.links)
                    }
                    if let log = result.errorsTxt?.trimmingCharacters(in: .whitespacesAndNewlines), !log.isEmpty {
                        DisclosureGroup("Converter log") {
                            Text(log)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.top, 4)
                        }
                        .font(.callout)
                        .padding(.horizontal, 4)
                    }
                    Label {
                        Text("Importing the same export again updates these notes; your edits are kept.")
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "arrow.triangle.2.circlepath")
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 4)
                    .padding(.top, 4)
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 8)
            }
            .frame(maxHeight: 440)
            .fixedSize(horizontal: false, vertical: true)
            .scrollBounceBehavior(.basedOnSize)

            Divider()
            HStack {
                Button("Import Another…") { importer.reset() }
                Spacer()
                if result.notebooks.count == 1, let id = result.notebooks[0].folderId {
                    Button("Show in Sidebar") { Task { await model.revealImportedFolder(id) } }
                }
                Button("Done", action: close)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 16)
        }
    }
}

/// One notebook's numbers, warnings and failed sections.
private struct NotebookCard: View {
    let notebook: OneNoteImportResult.Notebook
    let reveal: () -> Void
    @State private var showsWarnings = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "folder")
                    .foregroundStyle(.secondary)
                Text(notebook.name)
                    .font(.headline)
                Text(OneNoteImport.plural(notebook.sections, "section"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
                if notebook.folderId != nil {
                    Button("Show in Sidebar", action: reveal)
                        .buttonStyle(.link)
                        .font(.callout)
                }
            }

            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 10) {
                GridRow {
                    Stat(value: notebook.pagesCreated, label: "created")
                    Stat(value: notebook.pagesUpdated, label: "updated")
                    Stat(value: notebook.pagesUnchanged, label: "unchanged")
                    Stat(value: notebook.pagesSkipped, label: "skipped")
                }
                GridRow {
                    Stat(value: notebook.images, label: notebook.images == 1 ? "image" : "images")
                    Stat(value: notebook.files, label: notebook.files == 1 ? "file" : "files")
                    Stat(value: notebook.tables, label: notebook.tables == 1 ? "table" : "tables")
                    Stat(value: notebook.columnsPages, label: "with columns")
                }
            }

            if !notebook.failedSections.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(notebook.failedSections, id: \.self) { section in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle")
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("\(section.path) wasn't imported")
                                    .font(.callout.weight(.medium))
                                if !section.reason.isEmpty {
                                    Text(section.reason)
                                        .font(.callout)
                                        .foregroundStyle(.secondary)
                                }
                                if let hint = section.hint, !hint.isEmpty {
                                    Text(hint)
                                        .font(.callout)
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }

            if !notebook.warnings.isEmpty {
                DisclosureGroup(isExpanded: $showsWarnings) {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(notebook.warnings.enumerated()), id: \.offset) { _, warning in
                            Text(warning)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 4)
                } label: {
                    Text("\(OneNoteImport.plural(notebook.warnings.count, "note")) from the converter")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color(nsColor: .quaternarySystemFill).opacity(0.6)))
    }
}

private struct Stat: View {
    let value: Int
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value.formatted())
                .font(.system(size: 17, weight: .semibold))
                .monospacedDigit()
                .foregroundStyle(value == 0 ? .tertiary : .primary)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(width: 120, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

private struct LinksCard: View {
    let links: OneNoteImportResult.Links
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "link")
                    .foregroundStyle(.secondary)
                Text("Links between pages")
                    .font(.headline)
                Spacer()
            }
            Text(linksLine)
                .font(.callout)
                .foregroundStyle(.secondary)
            if !links.unresolved.isEmpty {
                DisclosureGroup(isExpanded: $isExpanded) {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(links.unresolved, id: \.self) { link in
                            VStack(alignment: .leading, spacing: 1) {
                                Text(link.page)
                                    .font(.callout)
                                Text(link.target)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.tertiary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 4)
                } label: {
                    Text("Show links that point outside this export")
                        .font(.callout)
                }
            }
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color(nsColor: .quaternarySystemFill).opacity(0.6)))
    }

    private var linksLine: String {
        var parts: [String] = []
        if links.rewritten > 0 { parts.append(String(localized: "\(OneNoteImport.plural(links.rewritten, "link")) now open the imported note.")) }
        if !links.unresolved.isEmpty {
            let count = links.unresolved.count
            parts.append(count == 1 ? String(localized: "1 link points to a page that wasn't in this export; it was kept as is.")
                                    : String(localized: "\(count) links point to pages that weren't in this export; they were kept as is."))
        }
        return parts.joined(separator: " ")
    }
}

// MARK: Failure

private struct OneNoteFailedStep: View {
    let importer: OneNoteImporter
    let message: String
    let converterMissing: Bool
    let close: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if converterMissing {
                SheetHeader(symbol: "wrench.and.screwdriver", title: "The OneNote converter isn't built yet",
                            subtitle: "Fortress reads OneNote files with a small helper, fortress-onenote, that lives in the Fortress folder.")
                VStack(alignment: .leading, spacing: 8) {
                    Text("Build it once with `cargo build --release` in tools/fortress-onenote, then restart the backend from Settings and import again.")
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.leading, 42)
            } else {
                SheetHeader(symbol: "exclamationmark.triangle", title: "The import didn't finish", subtitle: message)
                Text("Nothing was lost: notes imported before the problem are kept, and importing the same export again picks up where it left off.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 42)
            }
            HStack {
                Spacer()
                Button("Close", action: close)
                    .keyboardShortcut(.cancelAction)
                Button("Try Again") { importer.reset(keepFile: true) }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
    }
}
