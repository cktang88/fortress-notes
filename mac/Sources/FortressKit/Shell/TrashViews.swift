import SwiftUI

/// Trashed notes, newest first, with Restore and Delete Permanently.
struct TrashListView: View {
    @Environment(AppModel.self) private var model
    @State private var isConfirmingEmpty = false

    var body: some View {
        @Bindable var model = model
        List(selection: $model.selectedTrashId) {
            ForEach(model.trash) { note in
                TrashRow(note: note)
                    .tag(note.id)
                    .listRowSeparator(.hidden)
                    .contextMenu {
                        Button("Restore") { Task { await model.restore(trashedId: note.id) } }
                        Divider()
                        Button("Delete Permanently", role: .destructive) {
                            Task { await model.deleteForever(trashedId: note.id) }
                        }
                    }
            }
        }
        .listStyle(.inset)
        .scrollContentBackground(.hidden)
        .background(Color(nsColor: .textBackgroundColor))
        .animation(.smooth(duration: 0.2), value: model.trash.map(\.id))
        .overlay {
            if model.trash.isEmpty && model.isConnected {
                ContentUnavailableView(
                    "Trash Is Empty", systemImage: "trash",
                    description: Text("Deleted notes stay here until you empty the Trash."))
            }
        }
        .onDeleteCommand {
            if let id = model.selectedTrashId { Task { await model.deleteForever(trashedId: id) } }
        }
        .toolbar {
            ToolbarItem {
                Button("Empty Trash…") { isConfirmingEmpty = true }
                    .fixedSize()
                    .disabled(model.trash.isEmpty)
                    .help("Permanently delete every note in the Trash")
            }
        }
        .confirmationDialog(emptyTitle, isPresented: $isConfirmingEmpty) {
            Button("Empty Trash", role: .destructive) { Task { await model.emptyTrash() } }
        } message: {
            Text("This can't be undone.")
        }
    }

    private var emptyTitle: String {
        model.trash.count == 1 ? "Permanently delete 1 note?" : "Permanently delete \(model.trash.count) notes?"
    }
}

struct TrashRow: View {
    let note: TrashedDocument
    var now = Date()

    var body: some View {
        let preview = NoteListing.preview(title: note.title, snippet: note.snippet)
        VStack(alignment: .leading, spacing: 2) {
            Text(NoteListing.displayTitle(note.title))
                .font(.headline)
                .lineLimit(1)
            Text(preview.isEmpty ? "No additional text" : preview)
                .font(.callout)
                .foregroundStyle(preview.isEmpty ? .tertiary : .secondary)
                .lineLimit(2, reservesSpace: true)
            Text(NoteListing.deletedLabel(note.deletedAt, now: now))
                .font(.subheadline)
                .foregroundStyle(.tertiary)
                .padding(.top, 2)
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 4)
        .accessibilityElement(children: .combine)
    }
}

/// The detail column while the Trash is shown: a read-only look at the selected note.
struct TrashDetailView: View {
    @Environment(AppModel.self) private var model
    @State private var isConfirmingDelete = false

    var body: some View {
        if let note = model.trash.first(where: { $0.id == model.selectedTrashId }) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    HStack(spacing: 12) {
                        Image(systemName: "trash")
                            .foregroundStyle(.secondary)
                        Text("This note is in the Trash.")
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 8)
                        Button("Restore") { Task { await model.restore(trashedId: note.id) } }
                            .fixedSize()
                        Button("Delete…", role: .destructive) { isConfirmingDelete = true }
                            .buttonStyle(.plain)
                            .foregroundStyle(.red)
                            .fixedSize()
                            .help("Delete Permanently")
                    }
                    .font(.body)
                    .padding(12)
                    .background(Color(nsColor: .quaternarySystemFill), in: .rect(cornerRadius: 8))

                    Text(NoteListing.displayTitle(note.title))
                        .font(.system(size: 28, weight: .bold))
                        .textSelection(.enabled)
                    Text(NoteListing.deletedLabel(note.deletedAt, now: Date()))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    if !note.snippet.isEmpty {
                        Text(note.snippet)
                            .font(.system(size: 16))
                            .lineSpacing(10)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
                .frame(maxWidth: 640, alignment: .leading)
                .padding(.horizontal, 48)
                .padding(.top, 32)
                .frame(maxWidth: .infinity)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .confirmationDialog("Delete “\(NoteListing.displayTitle(note.title))” permanently?", isPresented: $isConfirmingDelete) {
                Button("Delete Permanently", role: .destructive) { Task { await model.deleteForever(trashedId: note.id) } }
            } message: {
                Text("This can't be undone.")
            }
        } else {
            NoSelectionView()
        }
    }
}
