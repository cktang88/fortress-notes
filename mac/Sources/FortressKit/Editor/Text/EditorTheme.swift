import AppKit

/// Editor typography (DESIGN a.2, base M = 16) and the mapping from semantic attributes
/// to fonts, colors and paragraph styles.
struct EditorTheme: Sendable {
    var baseSize: CGFloat = 16
    /// Table cells: no space between paragraphs.
    var isCompact = false
    /// Table cells: the cell's alignment.
    var alignment: NSTextAlignment = .natural
    /// Table header cells: semibold text.
    var emphasized = false
    /// Table cells: the cell's text colour (a name or hex), under any run colour.
    var textColorName: String?

    var scale: CGFloat { baseSize / 16 }
    /// One nesting level.
    var indentStep: CGFloat { 24 * scale }
    /// Room for a bullet, number or checkbox before list text.
    var markerWidth: CGFloat { 26 * scale }
    var quoteInset: CGFloat { 18 * scale }
    var codeInset: CGFloat { 16 * scale }
    /// Vertical padding drawn around a code block's background.
    var codePadding: CGFloat { 10 * scale }

    // MARK: Fonts

    func bodyFont() -> NSFont { .systemFont(ofSize: baseSize) }

    func baseFont(for kind: BlockKind) -> NSFont {
        switch kind {
        case .heading1: .systemFont(ofSize: 22 * scale, weight: .semibold)
        case .heading2: .systemFont(ofSize: 18 * scale, weight: .semibold)
        case .heading3: .systemFont(ofSize: 16 * scale, weight: .semibold)
        case .heading4: .systemFont(ofSize: 15 * scale, weight: .semibold)
        case .heading5, .heading6: .systemFont(ofSize: 14 * scale, weight: .semibold)
        case .code: .monospacedSystemFont(ofSize: 14 * scale, weight: .regular)
        default: .systemFont(ofSize: baseSize)
        }
    }

    func lineHeight(for kind: BlockKind) -> CGFloat {
        switch kind {
        case .heading1: 28 * scale
        case .heading2: 24 * scale
        case .heading3, .heading4: 22 * scale
        case .heading5, .heading6: 21 * scale
        case .code: 21 * scale
        default: 26 * scale
        }
    }

    /// Space before / after a block.
    func spacing(for kind: BlockKind) -> (before: CGFloat, after: CGFloat) {
        if isCompact { return (0, 0) }
        return switch kind {
        case .heading1: (24 * scale, 8 * scale)
        case .heading2: (20 * scale, 6 * scale)
        case .heading3, .heading4, .heading5, .heading6: (16 * scale, 4 * scale)
        case .bullet, .numbered, .check: (0, 4 * scale)
        case .code: ((8 + 10) * scale, (12 + 10) * scale)
        case .image, .embed, .table, .columns: (14 * scale, 18 * scale)
        case .math: (6 * scale, 14 * scale)
        case .file: (6 * scale, 12 * scale)
        case .divider: (6 * scale, 10 * scale)
        default: (0, 10 * scale)
        }
    }

    /// Where a block's text starts.
    func textIndent(kind: BlockKind, depth: Int) -> CGFloat {
        let nesting = CGFloat(depth) * indentStep
        switch kind {
        case .bullet, .numbered, .check: return nesting + markerWidth
        case .quote: return nesting + quoteInset
        case .code: return nesting + codeInset
        default: return nesting
        }
    }

    func font(for kind: BlockKind, marks: InlineMarks) -> NSFont {
        var base = baseFont(for: kind)
        if emphasized, kind == .paragraph { base = .systemFont(ofSize: base.pointSize, weight: .semibold) }
        if marks.sup || marks.sub {
            // Raised and lowered text is three quarters size (the offset is in `attributes`).
            let smaller = InlineMarks(bold: marks.bold, italic: marks.italic, code: marks.code, link: marks.link)
            let font = self.font(for: kind, marks: smaller)
            return NSFont(descriptor: font.fontDescriptor, size: font.pointSize * 0.75) ?? font
        }
        if marks.code && kind != .code {
            return .monospacedSystemFont(ofSize: base.pointSize * 0.875, weight: marks.bold ? .semibold : .regular)
        }
        var font = base
        if marks.bold {
            let weight: NSFont.Weight = kind.headingLevel != nil ? .heavy : .bold
            font = kind == .code ? .monospacedSystemFont(ofSize: base.pointSize, weight: .bold) : .systemFont(ofSize: base.pointSize, weight: weight)
        } else if marks.link != nil, kind != .code {
            font = .systemFont(ofSize: base.pointSize, weight: kind.headingLevel != nil ? .semibold : .medium)
        }
        if marks.italic {
            let descriptor = font.fontDescriptor.withSymbolicTraits(font.fontDescriptor.symbolicTraits.union(.italic))
            font = NSFont(descriptor: descriptor, size: font.pointSize) ?? font
        }
        return font
    }

    // MARK: Paragraphs

    /// `role`: a soft paragraph of a long block (`SoftBreaks`) has no spacing on the sides
    /// where its block goes on.
    func paragraphStyle(for info: BlockInfo, role: SoftBreaks.Role = []) -> NSParagraphStyle {
        let style = NSMutableParagraphStyle()
        style.alignment = alignment
        let indent = textIndent(kind: info.kind, depth: info.depth)
        style.firstLineHeadIndent = indent
        style.headIndent = indent
        let spacing = spacing(for: info.kind)
        style.paragraphSpacingBefore = role.contains(.continuation) ? 0 : spacing.before
        style.paragraphSpacing = role.contains(.continued) ? 0 : spacing.after
        if info.kind.hasInlineText {
            let height = lineHeight(for: info.kind)
            style.minimumLineHeight = height
            style.maximumLineHeight = height
        }
        if info.kind == .code {
            style.tailIndent = -codeInset
            style.lineBreakMode = .byCharWrapping
        }
        return style
    }

    /// Centers glyphs in the fixed line height (DESIGN a.2: min = max line height).
    func baselineOffset(font: NSFont, kind: BlockKind) -> CGFloat {
        let natural = font.ascender - font.descender
        return max(0, (lineHeight(for: kind) - natural) / 2) / 2
    }

    // MARK: Colors

    // Every colour here comes from the theme palette (App/Theme): stable dynamic colours
    // that resolve per appearance, so cached attributes survive theme changes.

    private static let markTextColors = Dictionary(uniqueKeysWithValues: MarkHue.allCases.map { ($0, ThemeRuntime.markText($0)) })
    private static let markHighlightColors = Dictionary(uniqueKeysWithValues: MarkHue.allCases.map { ($0, ThemeRuntime.markHighlight($0)) })

    /// A text colour by name ("red", the theme's red) or hex (made legible on the page).
    static func color(named name: String) -> NSColor? {
        if let hue = MarkHue(name: name) { return markTextColors[hue] }
        return hexColor(name)
    }

    /// A highlighter by name (the theme's hue as a tint) or hex (made quiet enough for text).
    static func highlightColor(named name: String) -> NSColor? {
        if let hue = MarkHue(name: name) { return markHighlightColors[hue] }
        return PaletteColor(hex: name).map(ThemeRuntime.foreignFill)
    }

    /// A hex text colour picked in another app, under the contrast rule (`Palette.legibleText`).
    static func hexColor(_ string: String) -> NSColor? {
        guard string.trimmingCharacters(in: .whitespaces).hasPrefix("#"), let color = PaletteColor(hex: string) else { return nil }
        return ThemeRuntime.foreignText(color)
    }

    /// A cell or block background picked in another app (`Palette.legibleFill`).
    static func cellBackground(named name: String) -> NSColor? {
        if let hue = MarkHue(name: name) { return markHighlightColors[hue] }
        guard name.trimmingCharacters(in: .whitespaces).hasPrefix("#"), let color = PaletteColor(hex: name) else { return nil }
        return ThemeRuntime.foreignFill(color)
    }

    func textColor(for info: BlockInfo) -> NSColor {
        if info.kind == .check && info.checked { return .themeTertiaryText }
        if let name = textColorName, let color = Self.color(named: name) { return color }
        switch info.kind {
        case .heading3, .heading5, .heading6, .quote: return .themeSecondaryText
        case .code: return .themeCodeText
        default: return .themeText
        }
    }

    // MARK: Attributes

    /// Every presentation attribute for a run.
    func attributes(info: BlockInfo, marks: InlineMarks, paragraphStyle: NSParagraphStyle) -> [NSAttributedString.Key: Any] {
        let font = font(for: info.kind, marks: marks)
        var attributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .paragraphStyle: paragraphStyle,
            .foregroundColor: marks.textColor.flatMap(Self.color(named:))
                ?? (marks.code && textColorName == nil && !(info.kind == .check && info.checked) ? NSColor.themeCodeText : textColor(for: info)),
            .fnStyled: true,
        ]
        if info.kind.hasInlineText {
            var offset = baselineOffset(font: baseFont(for: info.kind), kind: info.kind)
            if marks.sup { offset += baseFont(for: info.kind).pointSize * 0.33 }
            if marks.sub { offset -= baseFont(for: info.kind).pointSize * 0.18 }
            attributes[.baselineOffset] = offset
        }
        if let highlight = marks.highlight.flatMap(Self.highlightColor(named:)) {
            attributes[.backgroundColor] = highlight
        } else if marks.code && info.kind != .code {
            attributes[.backgroundColor] = NSColor.themeSurface
        }
        if marks.underline { attributes[.underlineStyle] = NSUnderlineStyle.single.rawValue }
        if marks.strike || (info.kind == .check && info.checked) {
            attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
            attributes[.strikethroughColor] = NSColor.themeTertiaryText
        }
        if let link = marks.link, info.kind != .code {
            attributes[.link] = link
            attributes[.toolTip] = link
        }
        return attributes
    }

    /// What decides a run's presentation: the theme, the block's kind, depth and check
    /// state, and the marks. Fonts, paragraph styles and colours are made once per
    /// combination instead of once per run (opening a long note restyles ~10,000 runs).
    private struct StyleKey: Hashable {
        var kind: BlockKind
        var depth: Int
        var checked: Bool
        var marks: InlineMarks
        var baseSize: CGFloat
        var isCompact: Bool
        var alignment: Int
        var emphasized: Bool
        var textColorName: String?
        var role: SoftBreaks.Role
    }

    /// `@unchecked Sendable`: every access to `attributes` holds `lock`. (Not a `Mutex`:
    /// the values, fonts, colors and paragraph styles, aren't `Sendable`, so a `Mutex`
    /// can't hand them out; they are immutable once made, which is what makes sharing
    /// them between the main thread and the decoding task safe.)
    private final class StyleCache: @unchecked Sendable {
        let lock = NSLock()
        var attributes: [StyleKey: [NSAttributedString.Key: Any]] = [:]
    }

    private static let styleCache = StyleCache()

    func cachedAttributes(info: BlockInfo, marks: InlineMarks, role: SoftBreaks.Role = []) -> [NSAttributedString.Key: Any] {
        let key = StyleKey(kind: info.kind, depth: info.depth, checked: info.checked, marks: marks, baseSize: baseSize,
                           isCompact: isCompact, alignment: alignment.rawValue, emphasized: emphasized, textColorName: textColorName,
                           role: role)
        let cache = Self.styleCache
        if let found = cache.lock.withLock({ cache.attributes[key] }) { return found }
        // Made outside the lock (it's the slow part); two threads may both make one.
        let made = attributes(info: info, marks: marks, paragraphStyle: paragraphStyle(for: info, role: role))
        cache.lock.withLock {
            if cache.attributes.count > 4096 { cache.attributes.removeAll() }
            cache.attributes[key] = made
        }
        return made
    }

    static let presentationKeys: [NSAttributedString.Key] = [
        .font, .paragraphStyle, .foregroundColor, .backgroundColor, .underlineStyle,
        .strikethroughStyle, .strikethroughColor, .link, .toolTip, .baselineOffset, .fnStyled,
    ]

    /// Re-derives presentation for the paragraphs touching `range`. Also gives every
    /// paragraph one block (the one on its first character) and turns pasted rich text
    /// into marks.
    func restyle(_ storage: NSMutableAttributedString, range: NSRange) {
        let string = storage.nsString
        guard string.length > 0 else { return }
        var location = string.paragraphRange(at: range.location).location
        let end = min(string.length, NSMaxRange(range))
        repeat {
            let paragraph = string.paragraphRange(at: location)
            // The block's info is the one on its first character, for all its soft paragraphs.
            let info = storage.blockInfo(at: paragraph.location) ?? BlockInfo()
            for piece in SoftBreaks.pieces(of: paragraph, in: string) {
                restyleParagraph(storage, piece.range, info: info, role: piece.role)
            }
            location = NSMaxRange(paragraph)
        } while location < end && location < string.length
    }

    private func restyleParagraph(_ storage: NSMutableAttributedString, _ paragraph: NSRange, info: BlockInfo, role: SoftBreaks.Role) {
        storage.enumerateAttributes(in: paragraph, options: []) { attributes, run, _ in
            var marks = (attributes[.fnMarks] as? MarksBox)?.marks ?? InlineMarks()
            let foreign = attributes[.fnStyled] == nil
            if foreign && attributes[.fnMarks] == nil {
                marks = Self.marks(fromForeign: attributes)
            }
            var updated = self.cachedAttributes(info: info, marks: marks, role: role)
            updated[.fnBlock] = info
            if let soft = attributes[.fnSoft] { updated[.fnSoft] = soft }
            if !marks.isPlain { updated[.fnMarks] = MarksBox(marks) }
            if let node = attributes[.fnNode] {
                updated[.fnNode] = node
                // A note link keeps the URL that opens it.
                if let box = node as? NodeBox, let id = NoteLinks.documentTarget(box.node), let url = NoteLinks.documentURL(id: id) {
                    updated[.link] = url
                    updated[.toolTip] = String(localized: "Open note")
                }
            }
            if let attachment = attributes[.attachment] { updated[.attachment] = attachment }
            if foreign {
                storage.setAttributes(updated, range: run)
            } else if !Self.isStyled(attributes, as: updated) {
                for key in Self.presentationKeys where updated[key] == nil && attributes[key] != nil {
                    storage.removeAttribute(key, range: run)
                }
                if marks.isPlain && attributes[.fnMarks] != nil { storage.removeAttribute(.fnMarks, range: run) }
                storage.addAttributes(updated, range: run)
            }
        }
    }

    /// Whether a run already has the attributes restyling would give it. Then it is left
    /// alone: setting equal attributes still marks the whole paragraph as edited, and
    /// TextKit 2 then put the caret after the paragraph (typing "X" in the middle of
    /// one moved the caret to the next line, and a Markdown shortcut typed before text
    /// was never seen). Only what changed is edited.
    private static func isStyled(_ current: [NSAttributedString.Key: Any], as updated: [NSAttributedString.Key: Any]) -> Bool {
        // The paragraph's one block info, by identity (the editor tracks blocks by it).
        guard (current[.fnBlock] as AnyObject?) === (updated[.fnBlock] as AnyObject?) else { return false }
        for key in presentationKeys + [.fnMarks, .fnNode, .attachment] {
            switch (current[key], updated[key]) {
            case (nil, nil): continue
            case (let found?, let wanted?):
                if (found as AnyObject) !== (wanted as AnyObject) && !(found as AnyObject).isEqual(wanted) { return false }
            default: return false
            }
        }
        return true
    }

    /// Marks from text pasted in from elsewhere (a web page, another app).
    static func marks(fromForeign attributes: [NSAttributedString.Key: Any]) -> InlineMarks {
        var marks = InlineMarks()
        if let font = attributes[.font] as? NSFont {
            let traits = font.fontDescriptor.symbolicTraits
            marks.bold = traits.contains(.bold)
            marks.italic = traits.contains(.italic)
            marks.code = traits.contains(.monoSpace)
        }
        if let underline = attributes[.underlineStyle] as? Int, underline != 0, attributes[.link] == nil {
            marks.underline = true
        }
        if let strike = attributes[.strikethroughStyle] as? Int, strike != 0 { marks.strike = true }
        if let link = attributes[.link] {
            marks.link = (link as? URL)?.absoluteString ?? (link as? String)
        }
        return marks
    }
}
