import SwiftUI

/// The inspector for the open note: Info (where it came from, dates, size, links),
/// "Seen before" for the block being written, related notes and backlinks.
struct InspectorView: View {
    @Environment(AppModel.self) private var model
    @State private var inspector = InspectorModel()

    private var info: NoteInfo { inspector.info }
    private var seenBefore: [RelatedResult] { inspector.seenBefore }
    private var related: [RelatedResult] { inspector.related }
    private var backlinks: [Backlink] { inspector.backlinks }

    var body: some View {
        let noteId = model.openNoteId
        // Cheap per render: one walk of the sidebar tree that stops at the note.
        let revision = noteId.flatMap { NoteInfo.updatedAt(of: $0, in: model.navigation) }
        let focus = FocusKey(blockId: model.focusedBlockId,
                             version: model.focusedBlockId.flatMap { model.savedBlockVersions[$0] })
        Group {
            if noteId != nil {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        InfoSection(info: info)
                        if !info.pictures.isEmpty {
                            PictureTextSection(
                                pictures: info.pictures, reading: model.pictureText.reading,
                                remaining: readingRemaining,
                                onShow: { model.open(noteId: noteId ?? "", blockId: $0.blockId) },
                                onCopy: { PictureText.copy($0, api: model.api) })
                        }
                        if !seenBefore.isEmpty {
                            seenBeforeSection.transition(.opacity)
                        }
                        relatedSection
                        backlinksSection
                        if info.pictures.isEmpty, let remaining = readingRemaining, remaining > 0 {
                            PictureReadingProgress(remaining: remaining)
                        }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .animation(.smooth(duration: 0.25), value: seenBefore)
                }
            } else {
                ContentUnavailableView(
                    "No Note Selected", systemImage: "sidebar.trailing",
                    description: Text("Open a note to see where it came from and what it connects to."))
                    .controlSize(.small)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .themedContent(.sunken)
        .onChange(of: NoteKey(noteId: noteId, revision: revision), initial: true) { _, key in
            let navigation = model.navigation
            inspector.show(noteId: key.noteId, revision: key.revision,
                           fallback: key.noteId.map { NoteInfo.from(navigation: navigation, documentId: $0) } ?? NoteInfo(),
                           api: model.api)
            inspector.focus(blockId: model.focusedBlockId, version: focus.version, api: model.api)
        }
        .onChange(of: focus, initial: true) { _, focus in
            inspector.focus(blockId: focus.blockId, version: focus.version, api: model.api)
        }
        .onChange(of: info.isReadingPictures, initial: true) { _, waiting in
            // A picture of this note is still to be read (just pasted): look for it now.
            if waiting { model.pictureText.wake() }
        }
        .onChange(of: model.pictureText.storedCount) {
            // This Mac just read a picture: if it is one of this note's, show its text.
            if let hash = model.pictureText.lastStored, info.pictures.contains(where: { $0.contentHash == hash }) {
                inspector.reloadInfo(api: model.api)
            }
        }
        .task(id: model.api.baseURL) {
            guard model.connectsOnAppear else { return }  // previews, snapshots, tests
            await inspector.watchPictureReading(api: model.api)
        }
    }

    /// Pictures and PDFs in the workspace still to be read: this Mac's own count while
    /// it reads (live), else the backend's.
    private var readingRemaining: Int? {
        if model.pictureText.isWorking, let remaining = model.pictureText.remaining { return remaining }
        return inspector.ocrStatus?.remaining
    }

    private struct NoteKey: Equatable {
        var noteId: String?
        var revision: String?
    }

    private struct FocusKey: Equatable {
        var blockId: String?
        var version: Int?
    }

    // MARK: Sections

    private var seenBeforeSection: some View {
        InspectorSection(title: "Seen before", symbol: "eye") {
            Text("You’ve written something like this")
                .font(.subheadline)
                .foregroundStyle(.themeTertiary)
            ForEach(seenBefore) { item in
                PassageCard(title: item.note.title, text: item.matchedBlockText, number: nil) {
                    model.open(noteId: item.note.id, blockId: item.matchedBlockId)
                }
            }
        }
    }

    private var relatedSection: some View {
        InspectorSection(title: "Related notes", symbol: "point.3.connected.trianglepath.dotted") {
            if related.isEmpty, let error = inspector.loadError {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.themeTertiary)
            } else if related.isEmpty {
                Text("Notes on the same subject show up here as you write.")
                    .font(.callout)
                    .foregroundStyle(.themeTertiary)
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
                    .foregroundStyle(.themeTertiary)
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
                .foregroundStyle(.themeTertiary)
            configuration.title
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.themeSecondary)
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
                    .foregroundStyle(.themeSecondary)
                    .lineLimit(2)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(.rect)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(isHovered ? AnyShapeStyle(ThemeStyle(.surface)) : AnyShapeStyle(.clear)))
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

/// Finder "Get Info"-style facts about the note: how it came in, dates, size and links.
struct InfoSection: View {
    let info: NoteInfo

    var body: some View {
        InspectorSection(title: "Info", symbol: "info.circle") {
            if let source = info.sourceLabel {
                Label(source, systemImage: "square.and.arrow.down")
                    .font(.callout.weight(.medium))
                    .padding(.bottom, 4)
            }
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 10, verticalSpacing: 5) {
                if !info.folderPath.isEmpty { row("Folder", info.folderPath.joined(separator: " › ")) }
                if let created = info.createdAt { row("Created", Self.format(created)) }
                if let updated = info.updatedAt { row("Edited", Self.format(updated)) }
                if let opened = info.openedAt { row("Opened", Self.format(opened)) }
                if let words = info.words {
                    row("Length", Self.length(words: words, blocks: info.blocks))
                }
                if info.linksOut != nil || info.backlinksIn != nil {
                    row("Links", "\(info.linksOut ?? 0) out · \(info.backlinksIn ?? 0) in")
                }
            }
        }
    }

    static func length(words: Int, blocks: Int?) -> String {
        var text = "\(words.formatted()) \(words == 1 ? "word" : "words")"
        if let blocks { text += " · \(blocks) \(blocks == 1 ? "block" : "blocks")" }
        return text
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.themeTertiary)
                .gridColumnAlignment(.trailing)
            Text(value)
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.themeSecondary)
                .lineLimit(2)
                .textSelection(.enabled)
        }
    }

    static func format(_ date: Date) -> String {
        if Calendar.current.isDateInToday(date) {
            let time = date.formatted(date: .omitted, time: .shortened)
            return String(localized: "Today, \(time)", comment: "Inspector date; the argument is a time")
        }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}
