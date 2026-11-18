import AppKit
import SwiftUI

/// The inspector's "Text in pictures": what was read from each picture and PDF of the
/// open note (searchable, but not part of the note), with where it stands while it is
/// being read, and how much of the workspace is still to be read.
struct PictureTextSection: View {
    let pictures: [PictureText]
    /// Content hashes this Mac is reading right now.
    var reading: Set<String> = []
    /// Pictures and PDFs in the whole workspace still to be read (nil or 0: none).
    var remaining: Int?
    var onShow: (PictureText) -> Void = { _ in }
    var onCopy: (PictureText) -> Void = { _ in }

    var body: some View {
        InspectorSection(title: "Text in pictures", symbol: "text.viewfinder") {
            if let remaining, remaining > 0 {
                PictureReadingProgress(remaining: remaining)
            }
            ForEach(Array(numbered.enumerated()), id: \.element.picture.id) { _, entry in
                PictureTextCard(
                    picture: entry.picture, title: entry.title,
                    isReading: entry.picture.contentHash.map(reading.contains) ?? false,
                    onShow: { onShow(entry.picture) }, onCopy: { onCopy(entry.picture) })
            }
        }
    }

    /// "Picture", or "Picture 2" when the note has several; "PDF" likewise.
    private var numbered: [(picture: PictureText, title: String)] {
        let pdfs = pictures.filter(\.isPDF).count
        let images = pictures.count - pdfs
        var seen = (pdfs: 0, images: 0)
        return pictures.map { picture in
            if picture.isPDF {
                seen.pdfs += 1
                return (picture, pdfs > 1 ? "PDF \(seen.pdfs)" : "PDF")
            }
            seen.images += 1
            return (picture, images > 1 ? "Picture \(seen.images)" : "Picture")
        }
    }
}

/// "Reading text in pictures · 1,204 left": quiet, while the workspace's backlog runs.
struct PictureReadingProgress: View {
    let remaining: Int

    var body: some View {
        HStack(spacing: 6) {
            ProgressView()
                .controlSize(.mini)
            Text("Reading text in pictures · \(remaining.formatted()) left")
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.tertiary)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .help("Fortress reads the words in pictures and PDFs in the background so search and Ask can find them.")
    }
}

/// One picture or PDF: its state and the text read from it, selectable.
struct PictureTextCard: View {
    let picture: PictureText
    let title: String
    var isReading = false
    var onShow: () -> Void = {}
    var onCopy: () -> Void = {}
    @State private var isExpanded = false

    /// Lines shown before "Show More".
    static let collapsedLines = 6

    private var state: ReadState {
        if isReading || picture.status == "running" { return .reading }
        switch picture.status {
        case "done": return picture.text.isEmpty ? .empty : .done
        case "failed": return .failed
        default: return .queued
        }
    }

    enum ReadState { case queued, reading, done, empty, failed }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: picture.isPDF ? "doc.richtext" : "photo")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Text(title)
                    .font(.callout.weight(.medium))
                Spacer(minLength: 4)
                status
            }
            content
            if let source = sourceLine {
                Text(source)
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.tertiary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 12) {
                if !picture.text.isEmpty {
                    Button("Copy Text", action: onCopy)
                }
                Button("Show in Note", action: onShow)
            }
            .buttonStyle(.link)
            .font(.subheadline)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .quaternarySystemFill), in: .rect(cornerRadius: 8, style: .continuous))
        .contextMenu {
            Button("Copy Text", action: onCopy).disabled(picture.text.isEmpty)
            Button("Show in Note", action: onShow)
        }
    }

    @ViewBuilder private var status: some View {
        switch state {
        case .reading:
            HStack(spacing: 4) {
                ProgressView().controlSize(.mini)
                Text("Reading…")
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        case .queued:
            Label("Waiting", systemImage: "clock")
                .labelStyle(StatusLabelStyle())
        case .failed:
            Label("Couldn’t read", systemImage: "exclamationmark.triangle")
                .labelStyle(StatusLabelStyle())
        case .done, .empty:
            EmptyView()
        }
    }

    @ViewBuilder private var content: some View {
        if !picture.text.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text(verbatim: picture.text)
                    .font(.callout)
                    // Text read earlier stays while a newer reader works on it: dimmed.
                    .foregroundStyle(state == .done ? .primary : .secondary)
                    .lineLimit(isExpanded ? nil : Self.collapsedLines)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if isLong {
                    Button(isExpanded ? "Show Less" : "Show More") {
                        withAnimation(.smooth(duration: 0.2)) { isExpanded.toggle() }
                    }
                    .buttonStyle(.link)
                    .font(.subheadline)
                }
            }
        } else {
            Text(placeholder)
                .font(.callout)
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }

    /// Long enough that the collapsed text may hide some of it.
    private var isLong: Bool {
        picture.isTruncated || picture.text.count > 280
            || picture.text.split(separator: "\n", omittingEmptySubsequences: false).count > Self.collapsedLines
    }

    private var placeholder: String {
        switch state {
        case .queued: "Its text will be read in the background."
        case .reading: "Reading the text…"
        case .empty: picture.isPDF ? "No text in this PDF." : "No text in this picture."
        case .failed: picture.error.map(Self.reason) ?? "This file couldn’t be read."
        case .done: ""
        }
    }

    /// "Apple Vision · 3 pages · 1,204 characters"
    private var sourceLine: String? {
        var parts: [String] = []
        switch state {
        case .done, .empty:
            parts.append(Self.readerName(model: picture.model, method: picture.method))
        case .failed:
            if !picture.text.isEmpty, let error = picture.error { parts.append(Self.reason(error)) }
        case .queued, .reading:
            break
        }
        if let pages = picture.pages, pages > 0 { parts.append("\(pages.formatted()) \(pages == 1 ? "page" : "pages")") }
        if let characters = picture.textChars, characters > 0, state == .done {
            parts.append("\(characters.formatted()) \(characters == 1 ? "character" : "characters")")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Who read the text, in words.
    nonisolated static func readerName(model: String?, method: String?) -> String {
        let reader = (model ?? "").hasPrefix("apple-vision/") ? "Apple Vision" : "Fortress OCR"
        switch method {
        case "pdf-text": return "PDF text"
        case "pdf-text+ocr": return "PDF text + \(reader)"
        default: return reader
        }
    }

    /// A backend error ("FileNotFoundError: x.png") as a sentence.
    nonisolated static func reason(_ error: String) -> String {
        if error.hasPrefix("FileNotFoundError") { return "The file is missing." }
        if let colon = error.firstIndex(of: ":"), error[..<colon].hasSuffix("Error") {
            let rest = error[error.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            return rest.isEmpty ? error : rest.prefix(1).uppercased() + rest.dropFirst()
        }
        return error
    }
}

private struct StatusLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            configuration.icon.font(.system(size: 10))
            configuration.title
        }
        .font(.subheadline)
        .foregroundStyle(.tertiary)
    }
}

extension PictureText {
    /// Copies the whole text (fetching it when the info carried only its start).
    @MainActor
    static func copy(_ picture: PictureText, api: APIClient) {
        Task {
            var text = picture.text
            if picture.isTruncated, let hash = picture.contentHash,
               let reading = try? await api.ocrReading(contentHash: hash) {
                text = reading.text
            }
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
    }
}

/// Runs the app's picture reader while the backend is connected and Settings allow it.
struct PictureTextReading: ViewModifier {
    @Environment(AppModel.self) private var model
    @AppStorage(PictureTextReader.enabledKey) private var isEnabled = true

    private struct Key: Equatable {
        var runs: Bool
        var backend: String
    }

    func body(content: Content) -> some View {
        content.onChange(of: Key(runs: model.connectsOnAppear && model.isConnected && isEnabled,
                                 backend: model.api.baseURL.absoluteString), initial: true) { _, key in
            if key.runs {
                model.pictureText.start(backend: model.api, key: key.backend)
            } else {
                model.pictureText.stop()
            }
        }
    }
}
