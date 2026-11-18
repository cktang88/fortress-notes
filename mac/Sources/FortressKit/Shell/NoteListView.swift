import SwiftUI
import UniformTypeIdentifiers

/// Notes in the selected scope, grouped by date like Notes.
struct NoteListView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("noteSort") private var sort: NoteSort = .edited
    @AppStorage("groupByDate") private var groupsByDate = true

    var body: some View {
        let notes = NoteListing.notes(in: model.scope, navigation: model.navigation)
        let sections = NoteListing.sections(notes, sort: sort, grouped: groupsByDate)
        let folderNames = NoteListing.folderNames(model.navigation.items)
        let showsFolder = !isFolderScope
        let selection = Binding<String?>(
            get: { model.openNoteId },
            set: { id in
                if let id { model.open(noteId: id) } else if model.openNoteId != nil { model.route = .empty }
            })

        List(selection: selection) {
            ForEach(sections) { section in
                Section {
                    ForEach(section.notes) { note in
                        NoteRow(note: note, folderName: showsFolder ? note.folderId.flatMap { folderNames[$0] } : nil)
                            .tag(note.id)
                            .listRowSeparator(.hidden)
                            .draggable(NoteDrag.payload(for: note.id)) {
                                Label(NoteListing.displayTitle(note.title), systemImage: "note.text")
                                    .padding(8)
                            }
                            .contextMenu { NoteContextMenu(note: note) }
                            .accessibilityAction(named: "Move to Trash") { Task { await model.trashNote(id: note.id) } }
                            .accessibilityAction(named: "Open in New Window") { model.library.openWindow?(note.id) }
                            .onHover { inside in model.prefetchOnHover(noteId: inside ? note.id : nil) }
                    }
                } header: {
                    if !section.title.isEmpty {
                        Text(section.title)
                    }
                }
            }
        }
        .listStyle(.inset)
        .scrollContentBackground(.hidden)
        .themedContent(.sunken)
        .task(id: model.openNoteId) {
            // Load the notes above and below the open one, so ↑/↓ open instantly. A new
            // selection cancels this before it sends anything.
            guard let id = model.openNoteId else { return }
            let neighbours = NoteListing.neighbours(of: id, in: sections.flatMap(\.notes).map(\.id))
            try? await Task.sleep(for: .milliseconds(150))
            guard !Task.isCancelled else { return }
            await model.prefetch(noteIds: neighbours)
        }
        .overlay {
            if notes.isEmpty && model.isConnected && model.hasLoadedNavigation {
                NoteListEmptyView()
            }
        }
        .onKeyPress(.return) {
            guard model.openNoteId != nil else { return .ignored }
            model.requestEditorFocus()
            return .handled
        }
        .onCopyCommand {
            // Edit ▸ Copy on a selected note: its Markdown (loaded when pasted), as Notes copies a note.
            guard let id = model.openNoteId else { return [] }
            return [NoteCopy.provider(noteId: id, title: model.openNote.map { NoteListing.displayTitle($0.title) } ?? "", api: model.api)]
        }
        .onDeleteCommand {
            if let id = model.openNoteId { Task { await model.trashNote(id: id) } }
        }
        .focusedValue(\.noteListTrash, model.openNoteId.map { id in TrashAction(id: id) })
        .toolbar {
            ToolbarItemGroup {
                Menu {
                    Picker("Sort By", selection: $sort) {
                        ForEach(NoteSort.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.inline)
                    Divider()
                    Toggle("Group by Date", isOn: $groupsByDate)
                        .disabled(sort == .title)
                } label: {
                    Label("Sort and Group", systemImage: "line.3.horizontal.decrease")
                }
                .menuIndicator(.hidden)
                .help("Sort and group notes")

                Button {
                    Task { await model.newNote() }
                } label: {
                    Label("New Note", systemImage: "square.and.pencil")
                }
                .help("New Note (⌘N)")
                .disabled(!model.isConnected)
            }
        }
    }

    private var isFolderScope: Bool {
        if case .folder = model.scope { true } else { false }
    }
}

/// One note: title, two lines of text, then "date · folder" and a rough glyph.
struct NoteRow: View {
    let note: NavigationDocument
    let folderName: String?
    var now = Date()

    var body: some View {
        let preview = NoteListing.preview(title: note.title, snippet: note.snippet)
        VStack(alignment: .leading, spacing: 2) {
            Text(NoteListing.displayTitle(note.title))
                .font(.headline)
                .lineLimit(1)
            Text(preview.isEmpty ? "No additional text" : preview)
                .font(.callout)
                .foregroundStyle(preview.isEmpty ? .themeTertiary : .themeSecondary)
                .lineLimit(2, reservesSpace: true)
            HStack(spacing: 4) {
                if let date = Timestamp.date(note.updatedAt) {
                    Text(NoteListing.listDate(date, now: now, calendar: .current))
                        .monospacedDigit()
                }
                if let folderName {
                    Text("·")
                    Image(systemName: "folder")
                        .imageScale(.small)
                    Text(folderName)
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                if note.status == .rough {
                    Image(systemName: "circle.dashed")
                        .help("Rough")
                        .accessibilityLabel("Rough")
                }
            }
            .font(.subheadline)
            .foregroundStyle(.themeTertiary)
            .padding(.top, 2)
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 4)
        .accessibilityElement(children: .combine)
    }
}

struct NoteContextMenu: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow
    let note: NavigationDocument

    var body: some View {
        Button("Open") { model.open(noteId: note.id) }
        Button("Open in New Window") { openWindow(id: MainWindowScene.id, value: note.id) }
        MoveToFolderMenu(noteId: note.id)
        Button("Export as Markdown…") {
            model.open(noteId: note.id)
            Task { await model.exportOpenNote() }
        }
        Divider()
        Button("Move to Trash", role: .destructive) { Task { await model.trashNote(id: note.id) } }
    }
}

/// What the list shows when the scope has no notes.
struct NoteListEmptyView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        switch model.scope {
        case .folder(let id):
            let name = NoteListing.folder(id: id, in: model.navigation.items)?.name ?? "this folder"
            ContentUnavailableView {
                Label("No Notes in \(name)", systemImage: "folder")
            } actions: {
                Button("New Note in \(name)") { Task { await model.newNote(in: id) } }
            }
        case .rough:
            ContentUnavailableView(
                "Nothing Rough", systemImage: "circle.dashed",
                description: Text("Notes you're still shaping show up here."))
        case .polished:
            ContentUnavailableView(
                "Nothing Polished Yet", systemImage: "checkmark.seal",
                description: Text("Promote a note when it's ready and it shows up here."))
        default:
            ContentUnavailableView {
                Label("Write Your First Note", systemImage: "square.and.pencil")
            } description: {
                Text("Notes are saved on this Mac as you type.")
            } actions: {
                Button("New Note") { Task { await model.newNote() } }
                    .buttonStyle(.borderedProminent)
                Button("Import Markdown…") { model.isImporterShown = true }
            }
        }
    }
}

/// A note copied from the list: its Markdown as plain text and Markdown, fetched when
/// something pastes it (the title if the server can't be reached).
enum NoteCopy {
    static func provider(noteId: String, title: String, api: APIClient) -> NSItemProvider {
        let provider = NSItemProvider()
        for type in [UTType.utf8PlainText.identifier, MarkdownCopy.typeIdentifier] {
            provider.registerDataRepresentation(forTypeIdentifier: type, visibility: .all) { @Sendable completion in
                nonisolated(unsafe) let completion = completion
                Task {
                    let text = (try? await api.markdown(documentId: noteId)) ?? title
                    completion(Data(text.utf8), nil)
                }
                return nil
            }
        }
        return provider
    }
}

/// Lets the menu bar's Move to Trash (⌘⌫) act only while the note list has focus, so the
/// editor keeps ⌘⌫ for deleting to the start of the line.
struct TrashAction: Equatable {
    var id: String
}

extension FocusedValues {
    @Entry var noteListTrash: TrashAction?
}
