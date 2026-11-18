import AppKit
import SwiftUI

/// What attachments need from the editor: where media lives, loaded images, live block
/// reference text, and a way to ask for relayout when something finishes loading.
@MainActor
final class AttachmentContext {
    let baseURL: URL
    var images: [URL: NSImage] = [:]
    private var loading: Set<URL> = []
    /// Current text of referenced blocks (block references are living).
    var referenceText: [String: String] = [:]
    private var resolving: Set<String> = []
    var resolveReference: ((String) async -> String?)?
    var relayout: (() -> Void)?
    var openURL: ((URL) -> Void)?
    var theme = EditorTheme()

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
        Task {
            if let (data, _) = try? await URLSession.shared.data(from: url), let image = NSImage(data: data) {
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

/// The one character of an atomic block. Draws through a hosted SwiftUI view.
final class BlockAttachment: NSTextAttachment {
    let info: BlockInfo
    nonisolated(unsafe) weak var context: AttachmentContext?

    @MainActor init(info: BlockInfo, context: AttachmentContext) {
        self.info = info
        self.context = context
        super.init(data: nil, ofType: nil)
        allowsTextAttachmentView = true
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
        switch info.kind {
        case .divider:
            return 22 * theme.scale
        case .file:
            return 40 * theme.scale
        case .image:
            guard let context, let string = origin?.attrs["url"]?.stringValue, let url = context.url(for: string),
                  let image = context.image(for: url), image.size.width > 0
            else { return 220 * theme.scale }
            let preview = origin?.attrs["previewWidth"]?.intValue.map(CGFloat.init) ?? image.size.width
            let shown = min(width, preview, image.size.width)
            return min(480, shown * image.size.height / image.size.width)
        default:
            let view = NSHostingView(rootView: BlockEmbedView(info: info, context: context, width: width))
            view.frame.size.width = width
            return max(36, view.fittingSize.height)
        }
    }
}

final class BlockAttachmentViewProvider: NSTextAttachmentViewProvider {
    override func loadView() {
        nonisolated(unsafe) let provider = self
        MainActor.assumeIsolated {
            guard let attachment = provider.textAttachment as? BlockAttachment else { return }
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
                               name: attrs["name"]?.stringValue ?? attrs["caption"]?.stringValue ?? "",
                               context: context)
            case .file:
                FileChip(name: fileName, url: attrs["url"]?.stringValue.flatMap { context?.url(for: $0) }, context: context)
            default:
                EmbedSummary(origin: origin)
            }
        }
        .frame(width: width, alignment: .leading)
    }

    private var fileName: String {
        if let name = attrs["name"]?.stringValue, !name.isEmpty { return name }
        if let url = attrs["url"]?.stringValue { return (url as NSString).lastPathComponent }
        return "File"
    }
}

private struct ImageBlockView: View {
    let url: URL?
    let name: String
    let context: AttachmentContext?

    var body: some View {
        if let url, let image = context?.images[url] {
            Image(nsImage: image)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .accessibilityLabel(name.isEmpty ? "Image" : name)
                .contextMenu {
                    Button("Open Image") { context?.openURL?(url) }
                }
        } else {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color(nsColor: .quaternarySystemFill))
                .overlay {
                    Image(systemName: "photo").font(.title2).foregroundStyle(.tertiary)
                }
                .accessibilityLabel(name.isEmpty ? "Image" : name)
        }
    }
}

/// A calm chip for a file: icon, name, opens on click.
private struct FileChip: View {
    let name: String
    let url: URL?
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
