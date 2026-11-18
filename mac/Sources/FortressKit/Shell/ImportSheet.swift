import SwiftUI

/// "Import 3 Files": pick the destination folder, import, then a summary.
struct ImportSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let request: MarkdownImportRequest
    @State private var folderId: String?
    @State private var isImporting = false
    @State private var result: MarkdownImportResult?

    init(request: MarkdownImportRequest, result: MarkdownImportResult? = nil) {
        self.request = request
        _folderId = State(initialValue: request.folderId)
        _result = State(initialValue: result)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let result {
                summary(result)
            } else {
                form
            }
        }
        .padding(20)
        .frame(width: 480)
    }

    private var title: String {
        request.files.count == 1 ? "Import 1 File" : "Import \(request.files.count) Files"
    }

    @ViewBuilder private var form: some View {
        Text(title)
            .font(.title2.weight(.semibold))
        Form {
            Picker("Into", selection: $folderId) {
                Label("All Notes", systemImage: "note.text").tag(String?.none)
                Divider()
                ForEach(NoteListing.folders(in: model.navigation.items), id: \.folder.id) { entry in
                    Text(String(repeating: "    ", count: entry.depth) + entry.folder.name)
                        .tag(Optional(entry.folder.id))
                }
            }
        }
        .formStyle(.columns)

        List(request.files, id: \.self) { url in
            Label {
                Text(url.deletingPathExtension().lastPathComponent)
                    .lineLimit(1)
                    .truncationMode(.middle)
            } icon: {
                Image(systemName: "doc.text")
                    .foregroundStyle(.secondary)
            }
        }
        .listStyle(.bordered)
        .alternatingRowBackgrounds(.disabled)
        .frame(height: min(CGFloat(request.files.count) * 28 + 12, 196))

        Text("Each file becomes a note named after the file. Front matter (title, status) is kept.")
            .font(.callout)
            .foregroundStyle(.secondary)

        HStack {
            if isImporting { ProgressView().controlSize(.small) }
            Spacer()
            Button("Cancel") { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button("Import") {
                Task {
                    isImporting = true
                    result = await model.importMarkdown(request, into: folderId)
                    isImporting = false
                    if result == nil { dismiss() }
                }
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
            .disabled(isImporting)
        }
    }

    @ViewBuilder private func summary(_ result: MarkdownImportResult) -> some View {
        Label {
            Text(result.imported.count == 1 ? "Imported 1 note" : "Imported \(result.imported.count) notes")
                .font(.title2.weight(.semibold))
        } icon: {
            Image(systemName: result.errors.isEmpty ? "checkmark.circle" : "exclamationmark.triangle")
                .foregroundStyle(.secondary)
        }
        if !result.errors.isEmpty {
            DisclosureGroup(result.errors.count == 1 ? "1 file was skipped" : "\(result.errors.count) files were skipped") {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(result.errors, id: \.self) { error in
                        Text("\(error.name): \(error.detail)")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 4)
            }
        }
        HStack {
            Spacer()
            Button("Done") { dismiss() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
    }
}
