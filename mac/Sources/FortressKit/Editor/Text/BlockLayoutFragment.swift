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
        let string = text.nsString
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

    /// The fragment's origin sits at the paragraph's head indent.
    private var leading: CGFloat {
        guard let info else { return 0 }
        return theme.textIndent(kind: info.kind, depth: info.depth)
    }

    /// Vertical center of the first line's lowercase letters (where markers sit).
    private var markerCenterY: CGFloat {
        guard let info, let first = textLineFragments.first else { return lineRect.midY }
        let font = theme.baseFont(for: info.kind)
        let baseline = first.typographicBounds.minY + first.glyphOrigin.y - theme.baselineOffset(font: font, kind: info.kind)
        return baseline - font.xHeight / 2
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
            bounds = bounds.union(CGRect(x: -theme.markerWidth - 8, y: lineRect.minY - 4, width: theme.markerWidth + 8, height: lineRect.height + 8))
        default: break
        }
        return bounds
    }

    /// The code background / quote bar area, in fragment coordinates.
    private func decorationRect(_ info: BlockInfo) -> CGRect {
        let content = contentRect.isNull ? CGRect(x: 0, y: 0, width: 0, height: theme.lineHeight(for: info.kind)) : contentRect
        let nesting = CGFloat(info.depth) * theme.indentStep - leading
        let columnWidth = textLayoutManager?.textContainer?.size.width ?? (layoutFragmentFrame.maxX)
        let width = columnWidth - CGFloat(info.depth) * theme.indentStep
        switch info.kind {
        case .code:
            return CGRect(x: nesting, y: content.minY - theme.codePadding, width: max(0, width),
                          height: content.height + 2 * theme.codePadding)
        default:
            return CGRect(x: nesting + 1, y: content.minY + 1, width: 3, height: max(0, content.height - 2))
        }
    }

    /// Where the checkbox is, in fragment coordinates (for hit testing too).
    func checkboxRect() -> CGRect? {
        guard let info, info.kind == .check else { return nil }
        let size = 17 * theme.scale
        return CGRect(x: -theme.markerWidth, y: markerCenterY - size / 2, width: size, height: size)
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

        let centerY = point.y + markerCenterY
        switch info.kind {
        case .code:
            let rect = decorationRect(info).offsetBy(dx: point.x, dy: point.y)
            NSColor.themeSurface.setFill()
            NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8).fill()
        case .quote:
            let rect = decorationRect(info).offsetBy(dx: point.x, dy: point.y)
            NSColor.themeQuoteBar.setFill()
            NSBezierPath(roundedRect: rect, xRadius: 1.5, yRadius: 1.5).fill()
        case .bullet:
            let size: CGFloat = 5.5 * theme.scale
            let center = CGPoint(x: point.x - theme.markerWidth + 7 * theme.scale, y: centerY)
            let dot = CGRect(x: center.x - size / 2, y: center.y - size / 2, width: size, height: size)
            NSColor.themeSecondaryText.set()
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
            let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.themeSecondaryText]
            let size = label.size(withAttributes: attributes)
            let x = point.x - 6 * theme.scale - size.width
            let baseline = centerY + font.xHeight / 2
            label.draw(at: CGPoint(x: x, y: baseline - font.ascender), withAttributes: attributes)
        case .check:
            guard let box = checkboxRect()?.offsetBy(dx: point.x, dy: point.y) else { break }
            let name = info.checked ? "checkmark.circle.fill" : "circle"
            let color: NSColor = info.checked ? .themeAccent : .themeTertiaryText
            let configuration = NSImage.SymbolConfiguration(pointSize: box.height, weight: .regular)
                .applying(.init(paletteColors: [info.checked ? .themeBackground : color, color]))
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
