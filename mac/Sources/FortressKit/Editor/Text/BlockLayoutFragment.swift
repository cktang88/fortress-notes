import AppKit

/// Creates layout fragments that draw block decorations: bullets, numbers, checkboxes,
/// the quote bar and the code block background (DESIGN c, "custom NSTextLayoutFragment").
final class BlockFragmentFactory: NSObject, NSTextLayoutManagerDelegate {
    var theme = EditorTheme()

    func textLayoutManager(
        _ textLayoutManager: NSTextLayoutManager, textLayoutFragmentFor location: any NSTextLocation,
        in textElement: NSTextElement
    ) -> NSTextLayoutFragment {
        let fragment = BlockLayoutFragment(textElement: textElement, range: textElement.elementRange)
        guard let paragraph = textElement as? NSTextParagraph, paragraph.attributedString.length > 0,
              let info = paragraph.attributedString.blockInfo(at: 0)
        else { return fragment }
        fragment.info = info
        fragment.theme = theme
        if info.kind == .numbered,
           let storage = (textLayoutManager.textContentManager as? NSTextContentStorage)?.textStorage,
           let contentManager = textLayoutManager.textContentManager {
            let offset = contentManager.offset(from: contentManager.documentRange.location, to: location)
            fragment.ordinal = Self.ordinal(in: storage, at: offset, depth: info.depth)
        }
        return fragment
    }

    /// 1-based number of a numbered item among its consecutive siblings.
    static func ordinal(in text: NSAttributedString, at offset: Int, depth: Int) -> Int {
        let string = text.string as NSString
        var count = 1
        var location = string.paragraphRange(at: offset).location
        while location > 0 {
            let previous = string.paragraphRange(at: location - 1)
            guard let info = text.blockInfo(at: previous.location) else { break }
            if info.depth > depth { location = previous.location; continue }
            guard info.depth == depth, info.kind == .numbered else { break }
            count += 1
            location = previous.location
        }
        return count
    }
}

final class BlockLayoutFragment: NSTextLayoutFragment {
    var info: BlockInfo?
    var theme = EditorTheme()
    var ordinal = 1

    private var lineRect: CGRect {
        guard let first = textLineFragments.first else { return .zero }
        return first.typographicBounds
    }

    private var contentRect: CGRect {
        textLineFragments.reduce(CGRect.null) { $0.union($1.typographicBounds) }
    }

    override var renderingSurfaceBounds: CGRect {
        guard let info else { return super.renderingSurfaceBounds }
        var bounds = super.renderingSurfaceBounds
        switch info.kind {
        case .code, .quote:
            bounds = bounds.union(decorationRect(info).insetBy(dx: -1, dy: -1))
        case .bullet, .numbered, .check:
            let indent = theme.textIndent(kind: info.kind, depth: info.depth)
            bounds = bounds.union(CGRect(x: indent - theme.markerWidth - 4, y: lineRect.minY, width: theme.markerWidth + 4, height: lineRect.height))
        default: break
        }
        return bounds
    }

    /// The code background / quote bar area, in fragment coordinates.
    private func decorationRect(_ info: BlockInfo) -> CGRect {
        let content = contentRect.isNull ? CGRect(x: 0, y: 0, width: 0, height: theme.lineHeight(for: info.kind)) : contentRect
        let nesting = CGFloat(info.depth) * theme.indentStep
        let width = layoutFragmentFrame.width - nesting
        switch info.kind {
        case .code:
            return CGRect(x: nesting, y: content.minY - theme.codePadding, width: max(0, width),
                          height: content.height + 2 * theme.codePadding)
        default:
            return CGRect(x: nesting + 1, y: content.minY + 3, width: 3, height: max(0, content.height - 6))
        }
    }

    /// Where the checkbox is, in fragment coordinates (for hit testing too).
    func checkboxRect() -> CGRect? {
        guard let info, info.kind == .check else { return nil }
        let size = 17 * theme.scale
        let indent = theme.textIndent(kind: info.kind, depth: info.depth)
        let line = lineRect
        return CGRect(x: indent - theme.markerWidth, y: line.midY - size / 2, width: size, height: size)
    }

    override func draw(at point: CGPoint, in context: CGContext) {
        if let info { drawDecoration(info, at: point, in: context) }
        super.draw(at: point, in: context)
    }

    private func drawDecoration(_ info: BlockInfo, at point: CGPoint, in context: CGContext) {
        context.saveGState()
        defer { context.restoreGState() }
        let graphics = NSGraphicsContext(cgContext: context, flipped: true)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = graphics
        defer { NSGraphicsContext.restoreGraphicsState() }

        let indent = theme.textIndent(kind: info.kind, depth: info.depth)
        let line = lineRect.offsetBy(dx: point.x, dy: point.y)
        switch info.kind {
        case .code:
            let rect = decorationRect(info).offsetBy(dx: point.x, dy: point.y)
            NSColor.quaternarySystemFill.setFill()
            NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8).fill()
        case .quote:
            let rect = decorationRect(info).offsetBy(dx: point.x, dy: point.y)
            NSColor.separatorColor.setFill()
            NSBezierPath(roundedRect: rect, xRadius: 1.5, yRadius: 1.5).fill()
        case .bullet:
            let size: CGFloat = 5.5 * theme.scale
            let center = CGPoint(x: point.x + indent - theme.markerWidth + 7 * theme.scale, y: line.midY)
            let dot = CGRect(x: center.x - size / 2, y: center.y - size / 2, width: size, height: size)
            NSColor.secondaryLabelColor.set()
            let path = NSBezierPath(ovalIn: dot)
            if info.depth % 2 == 1 {
                path.lineWidth = 1.2
                NSBezierPath(ovalIn: dot.insetBy(dx: 0.6, dy: 0.6)).stroke()
            } else {
                path.fill()
            }
        case .numbered:
            let label = "\(ordinal)." as NSString
            let font = NSFont.monospacedDigitSystemFont(ofSize: theme.baseSize, weight: .regular)
            let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.secondaryLabelColor]
            let size = label.size(withAttributes: attributes)
            let x = point.x + indent - 6 * theme.scale - size.width
            label.draw(at: CGPoint(x: x, y: line.midY - size.height / 2), withAttributes: attributes)
        case .check:
            guard let box = checkboxRect()?.offsetBy(dx: point.x, dy: point.y) else { break }
            let name = info.checked ? "checkmark.circle.fill" : "circle"
            let color: NSColor = info.checked ? .controlAccentColor : .tertiaryLabelColor
            let configuration = NSImage.SymbolConfiguration(pointSize: box.height, weight: .regular)
                .applying(.init(paletteColors: [info.checked ? .white : color, color]))
            if let image = NSImage(systemSymbolName: name, accessibilityDescription: info.checked ? "Checked" : "Unchecked")?
                .withSymbolConfiguration(configuration) {
                let fitted = CGRect(x: box.minX, y: box.midY - image.size.height / 2, width: image.size.width, height: image.size.height)
                image.draw(in: fitted, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
            }
        default:
            break
        }
    }
}
