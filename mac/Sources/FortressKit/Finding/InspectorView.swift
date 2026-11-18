import SwiftUI

/// The inspector for the open note: Info (where it came from, dates, size, links),
/// "Seen before" for the block being written, related notes and backlinks.
struct InspectorView: View {
    @Environment(AppModel.self) private var model
    /// Provenance to show until the backend reports it (previews and snapshots).
    var provenance: NoteProvenance?

    @State private var info = NoteInfo()
    @State private var seenBefore: [RelatedResult] = []
    @State private var related: [RelatedResult] = []
    @State private var backlinks: [Backlink] = []
    @State private var loadedNoteId: String?

    init(provenance: NoteProvenance? = nil) {
        self.provenance = provenance
    }

    var body: some View {
        Group {
            if let noteId = model.openNoteId {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        InfoSection(info: info, provenance: provenance ?? info.provenance)
                        if !seenBefore.isEmpty {
                            seenBeforeSection.transition(.opacity)
                        }
                        relatedSection
                        backlinksSection
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .animation(.smooth(duration: 0.25), value: seenBefore)
                }
                .task(id: noteId) { await loadNote(noteId) }
                .task(id: SeenKey(noteId: noteId, blockId: model.focusedBlockId)) {
                    await loadSeenBefore(noteId: noteId, blockId: model.focusedBlockId)
                }
            } else {
                ContentUnavailableView(
                    "No Note Selected", systemImage: "sidebar.trailing",
                    description: Text("Open a note to see where it came from and what it connects to."))
                    .controlSize(.small)
            }
        }
    }

    private struct SeenKey: Hashable {
        var noteId: String
        var blockId: String?
    }

    // MARK: Loading

    private func loadNote(_ noteId: String) async {
        if loadedNoteId != noteId {
            info = NoteInfo.from(navigation: model.navigation, documentId: noteId)
            related = []
            backlinks = []
            seenBefore = []
            loadedNoteId = noteId
        }
        let api = model.api
        async let document = try? api.document(id: noteId)
        async let relatedNotes = try? api.related(documentId: noteId, count: 8)
        async let links = try? api.backlinks(documentId: noteId)
        let (loadedDocument, loadedRelated, loadedLinks) = await (document, relatedNotes, links)
        guard !Task.isCancelled else { return }
        if let loadedDocument { info.add(loadedDocument) }
        related = loadedRelated ?? []
        backlinks = loadedLinks ?? []
        info.backlinksIn = loadedLinks.map { Set($0.map(\.sourceDocumentId)).count }
    }

    /// Passages like the block being written; refreshed 400 ms after the caret settles.
    private func loadSeenBefore(noteId: String, blockId: String?) async {
        guard let blockId else {
            seenBefore = []
            return
        }
        try? await Task.sleep(for: .milliseconds(400))
        guard !Task.isCancelled else { return }
        let results = (try? await model.api.related(documentId: noteId, blockId: blockId, count: 3)) ?? []
        guard !Task.isCancelled else { return }
        seenBefore = results.filter { $0.matchedBlockId != nil && !$0.matchedBlockText.isEmpty }
    }

    // MARK: Sections

    private var seenBeforeSection: some View {
        InspectorSection(title: "Seen before", symbol: "eye") {
            Text("You’ve written something like this")
                .font(.subheadline)
                .foregroundStyle(.tertiary)
            ForEach(seenBefore) { item in
                PassageCard(title: item.note.title, text: item.matchedBlockText, number: nil) {
                    model.open(noteId: item.note.id, blockId: item.matchedBlockId)
                }
            }
        }
    }

    private var relatedSection: some View {
        InspectorSection(title: "Related notes", symbol: "point.3.connected.trianglepath.dotted") {
            if related.isEmpty {
                Text("Notes on the same subject show up here as you write.")
                    .font(.callout)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(related) { item in
                        InspectorRow(
                            title: item.note.title,
                            detail: item.matchedBlockText.isEmpty ? item.note.snippet : item.matchedBlockText
                        ) {
                            model.open(noteId: item.note.id, blockId: item.matchedBlockId)
                        }
                    }
                }
                .padding(.horizontal, -8)
            }
        }
    }

    private var backlinksSection: some View {
        InspectorSection(title: "Backlinks", symbol: "arrow.uturn.backward") {
            if backlinks.isEmpty {
                Text("No notes link here yet.")
                    .font(.callout)
                    .foregroundStyle(.tertiary)
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(backlinks) { link in
                        InspectorRow(
                            title: link.sourceDocumentTitle,
                            detail: link.sourceText,
                            emphasis: link.label
                        ) {
                            model.open(noteId: link.sourceDocumentId, blockId: link.sourceBlockId)
                        }
                    }
                }
                .padding(.horizontal, -8)
            }
        }
    }
}

/// A titled inspector group: small semibold secondary header, sentence case.
struct InspectorSection<Content: View>: View {
    let title: String
    let symbol: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: symbol)
                .labelStyle(InspectorHeaderLabelStyle())
            content
        }
    }
}

private struct InspectorHeaderLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.tertiary)
            configuration.title
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
        }
    }
}

/// A clickable note row: title and one line of detail (with an emphasized phrase, e.g. the link label).
private struct InspectorRow: View {
    let title: String
    let detail: String
    var emphasis: String? = nil
    let action: () -> Void
    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title.isEmpty ? "Untitled" : title)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                detailText
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(.rect)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(isHovered ? AnyShapeStyle(.quaternary.opacity(0.7)) : AnyShapeStyle(.clear)))
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
    }

    private var detailText: Text {
        guard let emphasis, !emphasis.isEmpty, let range = detail.range(of: emphasis) else {
            return Text(verbatim: detail)
        }
        return Text.joined([
            Text(verbatim: String(detail[..<range.lowerBound])),
            Text(verbatim: String(detail[range])).foregroundStyle(.tint).fontWeight(.medium),
            Text(verbatim: String(detail[range.upperBound...])),
        ])
    }
}

/// Finder "Get Info"-style facts about the note: source, dates, size and links.
struct InfoSection: View {
    let info: NoteInfo
    let provenance: NoteProvenance?

    var body: some View {
        InspectorSection(title: "Info", symbol: "info.circle") {
            if let provenance { source(provenance) }
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 6) {
                if let folder = info.folder { row("Folder", folder) }
                if let created = info.createdAt { row("Created", Self.format(created)) }
                if let updated = info.updatedAt { row("Edited", Self.format(updated)) }
                if let words = info.words, let blocks = info.blocks {
                    row("Length", "\(words.formatted()) \(words == 1 ? "word" : "words") · \(blocks) \(blocks == 1 ? "block" : "blocks")")
                }
                if info.linksOut != nil || info.backlinksIn != nil {
                    row("Links", "\(info.linksOut ?? 0) out · \(info.backlinksIn ?? 0) in")
                }
            }
        }
    }

    private func source(_ provenance: NoteProvenance) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label {
                Text(provenance.label).font(.callout.weight(.medium))
            } icon: {
                Image(systemName: provenance.symbol)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            if !provenance.path.isEmpty {
                Text(provenance.path.joined(separator: " › "))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .textSelection(.enabled)
            }
            if let url = provenance.url {
                Link(url.host() ?? url.absoluteString, destination: url)
                    .font(.subheadline)
                    .lineLimit(1)
            }
            if let original = provenance.originalCreatedAt {
                Text("Originally written \(original.formatted(date: .abbreviated, time: .omitted))"
                     + (provenance.importedAt.map { " · imported \($0.formatted(date: .abbreviated, time: .omitted))" } ?? ""))
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .quaternarySystemFill), in: .rect(cornerRadius: 8, style: .continuous))
        .padding(.bottom, 4)
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.tertiary)
                .gridColumnAlignment(.trailing)
            Text(value)
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    static func format(_ date: Date) -> String {
        date.formatted(date: .abbreviated, time: .shortened)
    }
}
