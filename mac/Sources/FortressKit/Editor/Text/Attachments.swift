import AppKit
import SwiftUI

/// What attachments need from the editor: where media lives, loaded images, live block
/// reference text, and a way to ask for relayout when something finishes loading.
@MainActor
@Observable
final class AttachmentContext {
    let baseURL: URL
    /// The app's session (APIClient's), so pictures share its URL cache and configuration.
    @ObservationIgnored var session: URLSession = MediaCache.shared.session
    @ObservationIgnored var pipeline = ImagePipeline.shared
    @ObservationIgnored private var slots: [URL: ImageSlot] = [:]
    /// A picture arrived (blocks without a stored size lay out again).
    @ObservationIgnored var imageDidLoad: ((URL) -> Void)?
    /// Current text of referenced blocks (block references are living).
    var referenceText: [String: String] = [:]
    /// Current titles of linked notes (note links show them; labels are never stored).
    var documentTitles: [String: String] = [:]
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
        for (id, document) in links.documents { documentTitles[id] = document.title }
    }

    func url(for string: String) -> URL? {
        if string.hasPrefix("/") { return URL(string: string, relativeTo: baseURL)?.absoluteURL }
        return URL(string: string)
    }

    /// The picture's slot; asking starts loading it (off the main thread, downsampled).
    func slot(for url: URL) -> ImageSlot {
        if let slot = slots[url] { return slot }
        let slot = ImageSlot(image: pipeline.cached(url))
        slots[url] = slot
        if slot.image == nil {
            let session = session
            let pipeline = pipeline
            Task { [weak self, weak slot] in
                let image = await pipeline.image(for: url, session: session)
                guard let image, let slot else { return }
                slot.fill(image)
                self?.imageDidLoad?(url)
            }
        }
        return slot
    }

    /// The picture if it has arrived (and starts loading it if not).
    func image(for url: URL) -> NSImage? {
        slot(for: url).image
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
///
/// NSTextAttachment isn't main-actor isolated (TextKit may ask it for bounds from a
/// layout pass), so the mutable state below is `nonisolated(unsafe)`: it is only ever
/// read or written on the main thread, because the editor's text views lay out on the
/// main thread (TextKit 2 viewport layout in NSTextView), and every access that can
/// arrive through a nonisolated TextKit callback goes through `MainActor.assumeIsolated`,
/// which traps instead of racing if that ever changes.
final class BlockAttachment: NSTextAttachment {
    let info: BlockInfo
    nonisolated(unsafe) weak var context: AttachmentContext?
    nonisolated(unsafe) var live: (any LiveBlock)?
    /// Last measured (width, height) of a hosted embed or math view (measuring hosts a view).
    nonisolated(unsafe) private var measured: (width: CGFloat, height: CGFloat)?
    /// See `blockView`.
    nonisolated(unsafe) private var shownView: NSView?

    @MainActor init(info: BlockInfo, context: AttachmentContext, block: EditorBlock? = nil) {
        self.info = info
        self.context = context
        super.init(data: nil, ofType: nil)
        allowsTextAttachmentView = true
        switch info.kind {
        case .table:
            let table = block?.table ?? .blank()
            live = TableBlockController(table: table, attrs: table.attrs, context: context, attachment: self)
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

    /// The view this block shows: made the first time TextKit asks, then the same view
    /// for as long as the attachment lives. TextKit asks again whenever it lays the
    /// paragraph out and re-adds the view to the viewport on every layout pass (each
    /// keystroke); a fresh view each time re-rendered it each time. Pictures and dividers
    /// are drawn by AppKit, because a hosting view re-renders its SwiftUI whenever it
    /// moves to a window (and TextKit's re-adding is such a move); the rarer kinds
    /// (files, math, embeds) keep one hosting view each.
    @MainActor var blockView: NSView {
        if let live { return live.view }
        if let shownView { return shownView }
        let view: NSView
        switch info.kind {
        case .image:
            view = ImageAttachmentView(attrs: info.origin?.origin.attrs ?? [:], context: context)
        case .divider:
            view = DividerAttachmentView()
        default:
            let hosting = NSHostingView(rootView: BlockEmbedView(info: info, context: context, width: nil))
            hosting.sizingOptions = []
            view = hosting
        }
        shownView = view
        return view
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
            if let measured, abs(measured.width - width) < 0.5 { return measured.height }
            let view = NSHostingView(rootView: BlockEmbedView(info: info, context: context, width: width))
            view.frame.size.width = width
            let height = max(36, view.fittingSize.height)
            measured = (width, height)
            return height
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
    /// Width and height stored with the block: the space is known before the picture arrives.
    static func hasStoredSize(_ attrs: [String: JSONValue]) -> Bool {
        guard case .number(let width)? = attrs["width"], case .number(let height)? = attrs["height"] else { return false }
        return width > 0 && height > 0
    }

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
        var shown = min(width, natural.width)
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

/// A picture block, drawn by AppKit: the image at its stored size (never wider than the
/// column), rounded, its caption below, its alt text as hover help and accessibility
/// label. Not SwiftUI on purpose: TextKit re-adds attachment views to the viewport on
/// every layout pass (each keystroke), and a hosting view re-renders its SwiftUI each
/// time it moves to a window; this view only redraws when its picture arrives.
final class ImageAttachmentView: NSView {
    private let attrs: [String: JSONValue]
    private let url: URL?
    private weak var context: AttachmentContext?
    private(set) var image: NSImage?

    init(attrs: [String: JSONValue], context: AttachmentContext?) {
        self.attrs = attrs
        self.context = context
        url = attrs["url"]?.stringValue.flatMap { context?.url(for: $0) }
        super.init(frame: .zero)
        toolTip = alt.isEmpty ? name : alt
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        setAccessibilityLabel(label)
        if let url, let context {
            let slot = context.slot(for: url)
            image = slot.image
            if image == nil { slot.whenLoaded { [weak self] image in self?.show(image) } }
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var isFlipped: Bool { true }

    private var alt: String { attrs["alt"]?.stringValue ?? "" }
    private var caption: String { attrs["caption"]?.stringValue ?? "" }
    private var name: String { attrs["name"]?.stringValue ?? "" }
    private var label: String {
        let text = [alt, caption, name].first { !$0.isEmpty } ?? ""
        return text.isEmpty ? String(localized: "Image") : text
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }

    private func show(_ image: NSImage) {
        guard image !== self.image else { return }
        self.image = image
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let theme = context?.theme ?? EditorTheme()
        let size = ImageLayout.size(attrs: attrs, image: image, width: bounds.width, theme: theme)
        let frame = CGRect(origin: .zero, size: size)
        if let image, image.size.width > 0, image.size.height > 0 {
            // Fit (the stored size can differ a little from the picture's own).
            let scale = min(size.width / image.size.width, size.height / image.size.height)
            let fitted = CGSize(width: image.size.width * scale, height: image.size.height * scale)
            let rect = CGRect(x: 0, y: (size.height - fitted.height) / 2, width: fitted.width, height: fitted.height)
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8).addClip()
            image.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true,
                       hints: [.interpolation: NSImageInterpolation.high.rawValue])
            NSGraphicsContext.restoreGraphicsState()
        } else {
            NSColor.themeSurface.setFill()
            NSBezierPath(roundedRect: frame, xRadius: 8, yRadius: 8).fill()
            let configuration = NSImage.SymbolConfiguration(pointSize: 22, weight: .regular)
                .applying(.init(paletteColors: [.themeTertiaryText]))
            if let symbol = NSImage(systemSymbolName: "photo", accessibilityDescription: nil)?.withSymbolConfiguration(configuration) {
                let rect = CGRect(x: frame.midX - symbol.size.width / 2, y: frame.midY - symbol.size.height / 2,
                                  width: symbol.size.width, height: symbol.size.height)
                symbol.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
            }
        }
        if !caption.isEmpty {
            let attributes: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 12 * theme.scale),
                .foregroundColor: NSColor.themeSecondaryText,
                .paragraphStyle: ImageLayout.captionStyle(theme),
            ]
            let rect = CGRect(x: 0, y: size.height + 6 * theme.scale, width: size.width,
                              height: max(0, bounds.height - size.height - 6 * theme.scale))
            (caption as NSString).draw(with: rect, options: [.usesLineFragmentOrigin], attributes: attributes)
        }
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        guard image != nil else { return nil }
        let menu = NSMenu()
        if url != nil {
            menu.addItem(withTitle: String(localized: "Open Image"), action: #selector(openImage), keyEquivalent: "").target = self
        }
        if !alt.isEmpty {
            menu.addItem(withTitle: String(localized: "Copy Text in Image"), action: #selector(copyText), keyEquivalent: "").target = self
        }
        return menu.items.isEmpty ? nil : menu
    }

    @objc private func openImage() {
        if let url { context?.openURL?(url) }
    }

    @objc private func copyText() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(alt, forType: .string)
    }
}

/// A divider block: a one-point rule across the column, centered.
final class DividerAttachmentView: NSView {
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.themeBorder.setFill()
        CGRect(x: 0, y: (bounds.height / 2).rounded(.down), width: bounds.width, height: 1).fill()
    }
}

/// TextKit calls these from its (main-thread) layout of an NSTextView; the overrides are
/// nonisolated in the SDK, so each hops into the main actor with `assumeIsolated` (a
/// checked assertion, not a hop). `nonisolated(unsafe)` lets the non-Sendable provider
/// and attachment cross into that closure: they never leave the main thread.
final class BlockAttachmentViewProvider: NSTextAttachmentViewProvider {
    override func loadView() {
        nonisolated(unsafe) let provider = self
        MainActor.assumeIsolated {
            guard let attachment = provider.textAttachment as? BlockAttachment else { return }
            provider.view = attachment.blockView
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

/// The SwiftUI view inside a file, math or embed block's attachment (pictures and
/// dividers are AppKit views: `BlockAttachment.blockView`).
struct BlockEmbedView: View {
    let info: BlockInfo
    let context: AttachmentContext?
    let width: CGFloat?

    private var origin: BlockOrigin? { info.origin?.origin }
    private var attrs: [String: JSONValue] { origin?.attrs ?? [:] }

    // Hosted in its own NSHostingView inside the text view: theme it here.
    var body: some View {
        themedBody.fortressTheme().foregroundStyle(.themeText)
    }

    @ViewBuilder private var themedBody: some View {
        Group {
            switch info.kind {
            case .file:
                FileChip(name: fileName, url: attrs["url"]?.stringValue.flatMap { context?.url(for: $0) },
                         size: fileSize, context: context)
            case .math:
                MathBlockView(latex: origin?.payload?["latex"]?.stringValue ?? origin?.text ?? "")
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

/// A math block: the LaTeX set in a math style (symbols, raised and lowered parts),
/// with the source on hover. Saved untouched.
struct MathBlockView: View {
    let latex: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "function")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.themeTertiary)
            Text(MathText.attributed(latex, size: 19))
                .textSelection(.enabled)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(ThemeStyle(.surface)))
        .help(latex)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Math: \(MathFormatter.plain(MathFormatter.format(latex)))")
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
                        Text(size).font(.callout).foregroundStyle(.themeSecondary).monospacedDigit()
                            .fixedSize()
                    }
                    Image(systemName: "arrow.up.forward").font(.caption2.weight(.semibold))
                        .foregroundStyle(.themeTertiary)
                        .opacity(hovering ? 1 : 0)
                }
                .padding(.horizontal, 10)
                .frame(height: 32)
                .background(Capsule().fill(ThemeStyle(hovering ? .border : .surface)))
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
                                    .overlay(Rectangle().stroke(ThemeStyle(.border), lineWidth: 0.5))
                            }
                        }
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            } else {
                HStack(spacing: 6) {
                    Image(systemName: symbol).foregroundStyle(.themeSecondary)
                    Text(label).font(.caption.weight(.medium)).foregroundStyle(.themeSecondary)
                }
                if !summary.isEmpty {
                    Text(summary).font(.system(size: 14)).foregroundStyle(.primary).lineLimit(8)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(ThemeStyle(.surface)))
        .help("Shown as is — edit this block on the web for now")
    }

    private var type: String { origin?.type ?? "block" }

    private var symbol: String {
        switch type {
        case "table": "tablecells"
        case "html": "chevron.left.forwardslash.chevron.right"
        case "quote", "callout": "text.quote"
        case "columns", "column": "rectangle.split.2x1"
        case "math": "function"
        default: "square.dashed"
        }
    }

    private var label: String {
        switch type {
        case "html": "HTML"
        case "columns": "Columns"
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

    /// A table kept as it came: rows of cells' text.
    private var tableRows: [[String]]? {
        guard let origin, origin.type == "table" else { return nil }
        let rows = origin.children.filter { $0.type == "table_row" }
        guard !rows.isEmpty else { return nil }
        return rows.map { row in row.children.map(TableContent.nestedText) }
    }
}
