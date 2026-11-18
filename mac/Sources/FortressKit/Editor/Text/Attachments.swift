import AppKit
import SwiftUI

/// What attachments need from the editor: where media lives, loaded images, live block
/// reference text, and a way to ask for relayout when something finishes loading.
@MainActor
@Observable
final class AttachmentContext {
    let baseURL: URL
    var images: [URL: NSImage] = [:]
    @ObservationIgnored private var loading: Set<URL> = []
    /// Current text of referenced blocks (block references are living).
    var referenceText: [String: String] = [:]
    @ObservationIgnored private var resolving: Set<String> = []
    @ObservationIgnored var resolveReference: ((String) async -> String?)?
    @ObservationIgnored var relayout: (() -> Void)?
    @ObservationIgnored var openURL: ((URL) -> Void)?
    /// Opens a note (a `[[link]]` clicked in a table cell).
    @ObservationIgnored var openNote: ((String) -> Void)?
    @ObservationIgnored var theme = EditorTheme()
    /// A table cell or a column's block changed (the editor saves).
    @ObservationIgnored var contentDidChange: (() -> Void)?
    /// A table or columns block changed height.
    @ObservationIgnored var attachmentDidResize: ((BlockAttachment) -> Void)?
    /// Leave a table or columns block for the text after it (or before it).
    @ObservationIgnored var leaveAttachment: ((BlockAttachment, _ forward: Bool) -> Void)?
    /// Sets up the editor of a column like the editor that holds it.
    @ObservationIgnored var configureNested: ((BlockEditorController) -> Void)?

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    /// Block references show the text the server resolved for them (living links).
    func adoptResolvedLinks(_ links: ResolvedLinks?) {
        guard let links else { return }
        for (id, block) in links.blocks { referenceText[id] = block.text }
    }

    func url(for string: String) -> URL? {
        if string.hasPrefix("/") { return URL(string: string, relativeTo: baseURL)?.absoluteURL }
        return URL(string: string)
    }

    func image(for url: URL) -> NSImage? {
        if let image = images[url] { return image }
        guard !loading.contains(url) else { return nil }
        loading.insert(url)
        if url.isFileURL {
            let image = NSImage(contentsOf: url)
            images[url] = image
            return image
        }
        Task {
            if let (data, _) = try? await APIClient.sharedSession.data(from: url), let image = NSImage(data: data) {
                images[url] = image
                relayout?()
            }
        }
        return nil
    }

    func referenceLabel(blockId: String, fallback: String) -> String {
        if let text = referenceText[blockId] { return text }
        if !resolving.contains(blockId), let resolveReference {
            resolving.insert(blockId)
            Task {
                if let text = await resolveReference(blockId) {
                    referenceText[blockId] = text
                    relayout?()
                }
            }
        }
        return fallback
    }
}

// MARK: Block attachments (image, file, divider, embed)

/// The editing state of an atomic block that is edited inside its attachment (a table's
/// cells, the blocks of each column). The attachment owns it, so it outlives recycled
/// views and comes back with undo.
@MainActor
protocol LiveBlock: AnyObject {
    var view: NSView { get }
    func height(forWidth width: CGFloat) -> CGFloat
    /// Writes the current state into a block read back from the text.
    func write(into block: inout EditorBlock)
    /// Puts the caret in the first (or last) editable place.
    func focus(atEnd: Bool)
}

/// The one character of an atomic block. Draws through a hosted SwiftUI view, or the
/// live view of a table or columns block.
final class BlockAttachment: NSTextAttachment {
    let info: BlockInfo
    nonisolated(unsafe) weak var context: AttachmentContext?
    nonisolated(unsafe) var live: (any LiveBlock)?

    @MainActor init(info: BlockInfo, context: AttachmentContext, block: EditorBlock? = nil) {
        self.info = info
        self.context = context
        super.init(data: nil, ofType: nil)
        allowsTextAttachmentView = true
        switch info.kind {
        case .table:
            let table = block?.table ?? TableContent(blockNote: info.origin?.origin.content["blocknote"]) ?? .blank()
            live = TableBlockController(table: table, attrs: info.origin?.origin.attrs ?? [:], context: context, attachment: self)
        case .columns:
            live = ColumnsBlockController(columns: block?.columns ?? [], context: context, attachment: self)
        default:
            break
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func viewProvider(for parentView: NSView?, location: any NSTextLocation, textContainer: NSTextContainer?) -> NSTextAttachmentViewProvider? {
        let provider = BlockAttachmentViewProvider(textAttachment: self, parentView: parentView, textLayoutManager: textContainer?.textLayoutManager, location: location)
        provider.tracksTextAttachmentViewBounds = true
        return provider
    }

    /// Height for the available width.
    @MainActor func height(forWidth width: CGFloat) -> CGFloat {
        let theme = context?.theme ?? EditorTheme()
        let origin = info.origin?.origin
        if let live { return live.height(forWidth: width) }
        switch info.kind {
        case .divider:
            return 22 * theme.scale
        case .file:
            return 40 * theme.scale
        case .image:
            let size = ImageLayout.size(attrs: origin?.attrs ?? [:], image: imageNow(origin), width: width, theme: theme)
            return size.height + ImageLayout.captionHeight(attrs: origin?.attrs ?? [:], width: width, theme: theme)
        default:
            let view = NSHostingView(rootView: BlockEmbedView(info: info, context: context, width: width))
            view.frame.size.width = width
            return max(36, view.fittingSize.height)
        }
    }

    @MainActor private func imageNow(_ origin: BlockOrigin?) -> NSImage? {
        guard let context, let string = origin?.attrs["url"]?.stringValue, let url = context.url(for: string) else { return nil }
        return context.image(for: url)
    }
}

/// Image sizing: the stored width and height (px) when there are some, so the layout
/// doesn't jump when the picture arrives; otherwise the picture's own size. Never wider
/// than the column, never taller than 480.
enum ImageLayout {
    @MainActor static func size(attrs: [String: JSONValue], image: NSImage?, width: CGFloat, theme: EditorTheme) -> CGSize {
        func number(_ key: String) -> CGFloat? {
            if case .number(let value)? = attrs[key], value > 0 { return CGFloat(value) }
            return nil
        }
        let natural: CGSize? = {
            if let w = number("width"), let h = number("height") { return CGSize(width: w, height: h) }
            if let image, image.size.width > 0 {
                if let w = number("width") { return CGSize(width: w, height: w * image.size.height / image.size.width) }
                return image.size
            }
            return nil
        }()
        guard let natural, natural.width > 0 else { return CGSize(width: width, height: 220 * theme.scale) }
        let preview = number("previewWidth") ?? natural.width
        var shown = min(width, preview, natural.width)
        var height = shown * natural.height / natural.width
        if height > 480 {
            height = 480
            shown = height * natural.width / natural.height
        }
        return CGSize(width: shown, height: height)
    }

    @MainActor static func captionHeight(attrs: [String: JSONValue], width: CGFloat, theme: EditorTheme) -> CGFloat {
        guard let caption = attrs["caption"]?.stringValue, !caption.isEmpty else { return 0 }
        let font = NSFont.systemFont(ofSize: 12 * theme.scale)
        let bounds = (caption as NSString).boundingRect(
            with: CGSize(width: width, height: 200), options: [.usesLineFragmentOrigin],
            attributes: [.font: font, .paragraphStyle: captionStyle(theme)])
        return 6 * theme.scale + ceil(bounds.height)
    }

    static func captionStyle(_ theme: EditorTheme) -> NSParagraphStyle {
        let style = NSMutableParagraphStyle()
        style.minimumLineHeight = 16 * theme.scale
        style.maximumLineHeight = 16 * theme.scale
        return style
    }
}

final class BlockAttachmentViewProvider: NSTextAttachmentViewProvider {
    override func loadView() {
        nonisolated(unsafe) let provider = self
        MainActor.assumeIsolated {
            guard let attachment = provider.textAttachment as? BlockAttachment else { return }
            if let live = attachment.live {
                provider.view = live.view
                return
            }
            let hosting = NSHostingView(rootView: BlockEmbedView(info: attachment.info, context: attachment.context, width: nil))
            hosting.sizingOptions = []
            provider.view = hosting
        }
    }

    override func attachmentBounds(
        for attributes: [NSAttributedString.Key: Any], location: any NSTextLocation,
        textContainer: NSTextContainer?, proposedLineFragment: CGRect, position: CGPoint
    ) -> CGRect {
        nonisolated(unsafe) let attachment = textAttachment as? BlockAttachment
        return MainActor.assumeIsolated {
            guard let attachment else { return .zero }
            let width = max(40, proposedLineFragment.width - position.x - 2)
            return CGRect(x: 0, y: 0, width: width, height: attachment.height(forWidth: width))
        }
    }
}

/// The SwiftUI view inside a block attachment.
struct BlockEmbedView: View {
    let info: BlockInfo
    let context: AttachmentContext?
    let width: CGFloat?

    private var origin: BlockOrigin? { info.origin?.origin }
    private var attrs: [String: JSONValue] { origin?.attrs ?? [:] }

    var body: some View {
        Group {
            switch info.kind {
            case .divider:
                Rectangle().fill(Color(nsColor: .separatorColor)).frame(height: 1)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .image:
                ImageBlockView(url: attrs["url"]?.stringValue.flatMap { context?.url(for: $0) },
                               attrs: attrs, context: context, width: width)
            case .file:
                FileChip(name: fileName, url: attrs["url"]?.stringValue.flatMap { context?.url(for: $0) },
                         size: fileSize, context: context)
            case .math:
                MathBlockView(latex: origin?.content["latex"]?.stringValue ?? origin?.text ?? "")
            default:
                EmbedSummary(origin: origin)
            }
        }
        .frame(width: width, alignment: .leading)
    }

    private var fileSize: String? {
        guard case .number(let size)? = attrs["size"], size > 0 else { return nil }
        return ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file)
    }

    private var fileName: String {
        if let name = attrs["name"]?.stringValue, !name.isEmpty { return name }
        if let url = attrs["url"]?.stringValue { return (url as NSString).lastPathComponent }
        return "File"
    }
}

/// An image at its stored size (never wider than the column), its caption below, and
/// its alt text (OCR or a description) as the hover help and accessibility label.
private struct ImageBlockView: View {
    let url: URL?
    let attrs: [String: JSONValue]
    let context: AttachmentContext?
    let width: CGFloat?

    private var alt: String { attrs["alt"]?.stringValue ?? "" }
    private var caption: String { attrs["caption"]?.stringValue ?? "" }
    private var name: String { attrs["name"]?.stringValue ?? "" }
    private var label: String {
        let text = [alt, caption, name].first { !$0.isEmpty } ?? ""
        return text.isEmpty ? "Image" : text
    }

    var body: some View {
        GeometryReader { proxy in
            content(width: width ?? proxy.size.width)
        }
    }

    @ViewBuilder private func content(width: CGFloat) -> some View {
        let theme = context?.theme ?? EditorTheme()
        let image = url.flatMap { context?.images[$0] }
        let size: CGSize? = ImageLayout.size(attrs: attrs, image: image, width: width, theme: theme)
        VStack(alignment: .leading, spacing: 6 * theme.scale) {
            Group {
                if let image {
                    Image(nsImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .contextMenu {
                            if let url { Button("Open Image") { context?.openURL?(url) } }
                            if !alt.isEmpty {
                                Button("Copy Text in Image") {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(alt, forType: .string)
                                }
                            }
                        }
                } else {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color(nsColor: .quaternarySystemFill))
                        .overlay {
                            Image(systemName: "photo").font(.title2).foregroundStyle(.tertiary)
                        }
                }
            }
            .frame(width: size?.width, height: size?.height)
            .help(alt.isEmpty ? name : alt)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(label)
            .accessibilityAddTraits(.isImage)
            if !caption.isEmpty {
                Text(caption)
                    .font(.system(size: 12 * theme.scale))
                    .lineSpacing(2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: size?.width ?? .infinity, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// A math block: the LaTeX set in a math style (symbols, raised and lowered parts),
/// with the source on hover. Saved untouched.
struct MathBlockView: View {
    let latex: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "function")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.tertiary)
            Text(MathText.attributed(latex, size: 19))
                .textSelection(.enabled)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color(nsColor: .quaternarySystemFill).opacity(0.6)))
        .help(latex)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Math: " + MathFormatter.plain(MathFormatter.format(latex)))
        .frame(maxHeight: .infinity, alignment: .center)
    }
}

enum MathText {
    /// The formula in New York italic with raised and lowered pieces.
    static func attributed(_ latex: String, size: CGFloat) -> AttributedString {
        var output = AttributedString()
        func append(_ pieces: [MathFormatter.Piece], size: CGFloat, offset: CGFloat) {
            for piece in pieces {
                switch piece {
                case .text(let text):
                    var run = AttributedString(text)
                    run.swiftUI.font = .system(size: size, design: .serif).italic()
                    run.swiftUI.baselineOffset = offset
                    output += run
                case .sup(let inner):
                    append(inner, size: size * 0.72, offset: offset + size * 0.38)
                case .sub(let inner):
                    append(inner, size: size * 0.72, offset: offset - size * 0.2)
                }
            }
        }
        append(MathFormatter.format(latex), size: size, offset: 0)
        return output
    }
}

/// A calm chip for a file: icon, name, opens on click.
private struct FileChip: View {
    let name: String
    let url: URL?
    var size: String?
    let context: AttachmentContext?
    @State private var hovering = false

    var body: some View {
        HStack {
            Button {
                if let url { context?.openURL?(url) }
            } label: {
                HStack(spacing: 8) {
                    Image(nsImage: NSWorkspace.shared.icon(for: .init(filenameExtension: (name as NSString).pathExtension) ?? .data))
                        .resizable().frame(width: 20, height: 20)
                    Text(name).font(.callout).lineLimit(1).truncationMode(.middle)
                        .foregroundStyle(.primary)
                    if let size {
                        Text(size).font(.callout).foregroundStyle(.secondary).monospacedDigit()
                            .fixedSize()
                    }
                    Image(systemName: "arrow.up.forward").font(.caption2.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .opacity(hovering ? 1 : 0)
                }
                .padding(.horizontal, 10)
                .frame(height: 32)
                .background(Capsule().fill(Color(nsColor: hovering ? .tertiarySystemFill : .quaternarySystemFill)))
            }
            .buttonStyle(.plain)
            .onHover { hovering = $0 }
            .help(url?.absoluteString ?? name)
            Spacer(minLength: 0)
        }
        .frame(maxHeight: .infinity)
    }
}

/// Read-only view of a block the editor can't edit: a table grid when it is one,
/// otherwise its type and text. Saved back untouched.
struct EmbedSummary: View {
    let origin: BlockOrigin?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let rows = tableRows, !rows.isEmpty {
                Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                        GridRow {
                            ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                                Text(cell)
                                    .font(.system(size: 14, weight: index == 0 ? .semibold : .regular))
                                    .padding(.horizontal, 10).padding(.vertical, 6)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .overlay(Rectangle().stroke(Color(nsColor: .separatorColor), lineWidth: 0.5))
                            }
                        }
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            } else {
                HStack(spacing: 6) {
                    Image(systemName: symbol).foregroundStyle(.secondary)
                    Text(label).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                }
                if !summary.isEmpty {
                    Text(summary).font(.system(size: 14)).foregroundStyle(.primary).lineLimit(8)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color(nsColor: .quaternarySystemFill)))
        .help("Shown as is — edit this block on the web for now")
    }

    private var type: String { origin?.type ?? "block" }

    private var symbol: String {
        switch type {
        case "table", "raw": "tablecells"
        case "columns", "column", "columnList": "rectangle.split.2x1"
        case "mathPlaceholder", "math": "function"
        default: "square.dashed"
        }
    }

    private var label: String {
        switch type {
        case "raw": "HTML"
        case "columns", "columnList": "Columns"
        case "mathPlaceholder": "Math"
        default: type.prefix(1).uppercased() + type.dropFirst()
        }
    }

    private var summary: String {
        var parts: [String] = []
        if let text = origin?.text, !text.isEmpty { parts.append(text) }
        func collect(_ nodes: [BlockNode]) {
            for node in nodes {
                if !node.text.isEmpty { parts.append(node.text) }
                collect(node.children)
            }
        }
        collect(origin?.children ?? [])
        return parts.joined(separator: "\n")
    }

    /// BlockNote table content: rows of cells of inline content.
    private var tableRows: [[String]]? {
        guard let content = origin?.content["blocknote"], content["type"]?.stringValue == "tableContent",
              case .array(let rows)? = content["rows"] else { return nil }
        return rows.map { row in
            guard case .array(let cells)? = row["cells"] else { return [] }
            return cells.map { cell in
                if let inner = cell["content"] { return BlockCodec.inlineText(inner) }
                return BlockCodec.inlineText(cell)
            }
        }
    }
}

// MARK: Inline nodes (block references, mentions, …)

/// An atomic inline node drawn as a small piece of text. Block references show the
/// referenced block's current text.
final class InlineNodeAttachment: NSTextAttachment {
    let node: JSONValue
    nonisolated(unsafe) weak var context: AttachmentContext?

    @MainActor init(node: JSONValue, context: AttachmentContext) {
        self.node = node
        self.context = context
        super.init(data: nil, ofType: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    var href: String? { node["href"]?.stringValue }

    @MainActor var label: String {
        let fallback = BlockCodec.inlineText(node).trimmingCharacters(in: .whitespaces)
        if let href, let target = InlineMarkdown.blockReferenceTarget(href) {
            let text = context?.referenceLabel(blockId: target.id, fallback: fallback.isEmpty ? (target.label ?? "block") : fallback) ?? fallback
            let clean = text.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            return clean.count > 80 ? String(clean.prefix(79)) + "…" : clean
        }
        return fallback.isEmpty ? (node["type"]?.stringValue ?? "•") : fallback
    }

    @MainActor private func textAttributes(_ attributes: [NSAttributedString.Key: Any]) -> [NSAttributedString.Key: Any] {
        let font = (attributes[.font] as? NSFont) ?? (context?.theme ?? EditorTheme()).bodyFont()
        return [.font: font, .foregroundColor: NSColor.textColor]
    }

    override func attachmentBounds(
        for attributes: [NSAttributedString.Key: Any], location: any NSTextLocation,
        textContainer: NSTextContainer?, proposedLineFragment: CGRect, position: CGPoint
    ) -> CGRect {
        nonisolated(unsafe) let this = self
        nonisolated(unsafe) let attributes = attributes
        return MainActor.assumeIsolated {
            let attrs = this.textAttributes(attributes)
            let label = this.label
            let size = (label as NSString).size(withAttributes: attrs)
            let font = attrs[.font] as! NSFont
            return CGRect(x: 0, y: font.descender, width: ceil(size.width) + 2, height: ceil(font.ascender - font.descender))
        }
    }

    override func image(for bounds: CGRect, attributes: [NSAttributedString.Key: Any], location: any NSTextLocation, textContainer: NSTextContainer?) -> NSImage? {
        nonisolated(unsafe) let this = self
        nonisolated(unsafe) let attributes = attributes
        return MainActor.assumeIsolated {
            let font = this.textAttributes(attributes)[.font] as! NSFont
            let label = this.label
            return NSImage(size: bounds.size, flipped: false) { rect in
                (label as NSString).draw(at: CGPoint(x: 1, y: -font.descender), withAttributes: [.font: font, .foregroundColor: NSColor.textColor])
                NSColor.controlAccentColor.withAlphaComponent(0.35).setFill()
                NSRect(x: 0, y: 0, width: rect.width, height: 2).fill()
                return true
            }
        }
    }
}
