import Foundation

/// The colours a user can give text or a highlighter (OneNote and Markdown imports use
/// these names; hex colours go through the same contrast rule, see `Palette.legibleText`).
/// They map onto Base16's accent slots, so a theme recolours them too.
public enum MarkHue: String, CaseIterable, Sendable, Codable {
    case red, orange, yellow, green, cyan, blue, purple, pink, brown, gray

    public init?(name: String) {
        switch name.lowercased() {
        case "grey": self = .gray
        case "teal", "aqua": self = .cyan
        case "magenta", "violet": self = .purple
        default: self.init(rawValue: name.lowercased())
        }
    }
}

/// Every colour the content layer (editor, note list, search results, Ask, inspector)
/// draws with, for one appearance. Chrome (sidebar, toolbar, sheets, glass) stays native
/// and takes only `accent`.
///
/// Not a file format: palettes are made from Base16/Base24 schemes or VS Code colour
/// themes (`Palette(base16:)`, `Palette(vsCode:)`), or are the built-in defaults.
public struct Palette: Hashable, Sendable {
    public var isDark: Bool
    /// Editor and Ask page.
    public var background: PaletteColor
    /// One step below the page: note list, window background behind the sidebar glass.
    public var sunken: PaletteColor
    /// One step above the page: code blocks, cards, table headers, chips.
    public var surface: PaletteColor
    /// Table grids, quote bars when the theme has none, dividers.
    public var border: PaletteColor
    public var text: PaletteColor
    public var secondaryText: PaletteColor
    public var tertiaryText: PaletteColor
    public var accent: PaletteColor
    public var link: PaletteColor
    /// Selected text background.
    public var selection: PaletteColor
    /// Find matches and the yellow highlighter.
    public var highlight: PaletteColor
    public var codeText: PaletteColor
    public var quoteBar: PaletteColor
    public var marks: [MarkHue: PaletteColor]
    /// The default palette uses the user's accent colour from System Settings unless it
    /// is Multicolor (then Fortress Ember). Themes bring their own accent.
    public var followsSystemAccent = false

    public init(
        isDark: Bool, background: PaletteColor, sunken: PaletteColor, surface: PaletteColor, border: PaletteColor,
        text: PaletteColor, secondaryText: PaletteColor, tertiaryText: PaletteColor, accent: PaletteColor, link: PaletteColor,
        selection: PaletteColor, highlight: PaletteColor, codeText: PaletteColor, quoteBar: PaletteColor,
        marks: [MarkHue: PaletteColor], followsSystemAccent: Bool = false
    ) {
        self.isDark = isDark
        self.background = background
        self.sunken = sunken
        self.surface = surface
        self.border = border
        self.text = text
        self.secondaryText = secondaryText
        self.tertiaryText = tertiaryText
        self.accent = accent
        self.link = link
        self.selection = selection
        self.highlight = highlight
        self.codeText = codeText
        self.quoteBar = quoteBar
        self.marks = marks
        self.followsSystemAccent = followsSystemAccent
    }

    // MARK: Contrast rules

    /// Minimum contrast ratios, WCAG 2.2 AA (1.4.3 text 4.5:1, 1.4.11 non-text 3:1),
    /// and AAA-level 7:1 under Increase Contrast.
    public struct Minimums: Sendable {
        public var text: Double
        public var secondaryText: Double
        public var tertiaryText: Double
        public var border: Double

        public static let standard = Minimums(text: 7, secondaryText: 4.5, tertiaryText: 4.5, border: 1.25)
        public static let increased = Minimums(text: 10, secondaryText: 7, tertiaryText: 7, border: 3)
    }

    /// The palette with every text colour moved (lightness only, hue kept) until it
    /// passes `minimums` on each background it is drawn on. Colours that pass are kept.
    public func legible(_ minimums: Minimums = .standard) -> Palette {
        var p = self
        // Text sits on the page, the list and surfaces: check against the worst of them.
        // Body text gets the full `text` ratio on the page and AA on the other surfaces.
        func onAll(_ color: PaletteColor, _ ratio: Double, page: Double? = nil) -> PaletteColor {
            var c = color
            for _ in 0..<3 {
                c = c.ensuringContrast(page ?? ratio, against: p.background)
                for bg in [p.sunken, p.surface] { c = c.ensuringContrast(ratio, against: bg) }
            }
            return c.ensuringContrast(page ?? ratio, against: p.background)
        }
        p.text = onAll(text, minimums.secondaryText, page: minimums.text)
        p.secondaryText = onAll(secondaryText, minimums.secondaryText)
        p.tertiaryText = onAll(tertiaryText, minimums.tertiaryText)
        p.accent = Self.chipLegible(onAll(accent, minimums.secondaryText), on: background, minimums.secondaryText)
        p.link = onAll(link, minimums.secondaryText)
        p.codeText = onAll(codeText, minimums.secondaryText)
        p.border = border.ensuringContrast(minimums.border, against: background)
        p.quoteBar = quoteBar.ensuringContrast(max(minimums.border, 1.5), against: background)
        p.selection = selection.tint(over: background, strength: 1, keeping: p.text, ratio: minimums.secondaryText)
        p.highlight = highlight.tint(over: background, strength: 1, keeping: p.text, ratio: minimums.secondaryText)
        for (hue, color) in marks { p.marks[hue] = onAll(color, minimums.secondaryText) }
        return p
    }

    /// Citation chips and selected rows draw accent text on the accent at 15% over the
    /// page (sRGB alpha blending): keep that legible too, with a little margin.
    static func chipLegible(_ accent: PaletteColor, on background: PaletteColor, _ minimum: Double) -> PaletteColor {
        var accent = accent
        for _ in 0..<4 {
            let chip = PaletteColor(red: background.red + (accent.red - background.red) * 0.15,
                                green: background.green + (accent.green - background.green) * 0.15,
                                blue: background.blue + (accent.blue - background.blue) * 0.15)
            if accent.contrast(with: chip) >= minimum + 0.1 { break }
            accent = accent.ensuringContrast(minimum + 0.15, against: chip)
        }
        return accent
    }

    /// Increase Contrast: stronger text and borders, the same hues.
    public func increasedContrast() -> Palette { legible(.increased) }

    /// The accent, or the system accent for the default palette (`systemAccent` is nil
    /// when the user chose Multicolor), made legible as text.
    public func withSystemAccent(_ systemAccent: PaletteColor?, minimum: Double = 4.5) -> Palette {
        guard followsSystemAccent, let systemAccent else { return self }
        var p = self
        var accent = systemAccent
        for bg in [background, sunken, surface] { accent = accent.ensuringContrast(minimum, against: bg) }
        accent = Self.chipLegible(accent, on: background, minimum)
        p.accent = accent
        p.link = accent
        p.selection = background.mixed(with: systemAccent, isDark ? 0.32 : 0.24)
            .tint(over: background, strength: 1, keeping: text)
        return p
    }

    // MARK: User colours (OneNote, pasted)

    /// A named text colour in this theme.
    public func markText(_ hue: MarkHue) -> PaletteColor {
        marks[hue] ?? text
    }

    /// A named highlighter: the theme's hue as a tint the text stays legible on.
    public func markHighlight(_ hue: MarkHue) -> PaletteColor {
        if hue == .yellow { return highlight }
        let color = marks[hue] ?? highlight
        return color.tint(over: background, strength: isDark ? 0.34 : 0.26, keeping: text)
    }

    /// The contrast rule for colours picked in another app (OneNote's hex text
    /// colours): keep hue and chroma, move lightness until it reads at 4.5:1 here.
    /// Dark blue on white stays as it is; on a dark theme it becomes a light blue.
    public func legibleText(_ color: PaletteColor) -> PaletteColor {
        color.ensuringContrast(4.5, against: background)
    }

    /// A cell or block background picked in another app: as is on light pages when the
    /// text stays legible on it, otherwise (and on dark pages) a tint of it.
    public func legibleFill(_ color: PaletteColor) -> PaletteColor {
        color.tint(over: background, strength: isDark ? 0.28 : 1, keeping: text)
    }
}

// MARK: Defaults

extension Palette {
    /// Paper: warm cream, warm near-black text. Built in OKLCH (hue ~85, low chroma)
    /// so each step is an even perceptual change; contrasts are checked in tests.
    public static let paper = Palette(
        isDark: false,
        background: PaletteColor(oklch: 0.980, 0.010, 85),     // #FBF8F1
        sunken: PaletteColor(oklch: 0.958, 0.014, 84),         // #F6F1E7
        surface: PaletteColor(oklch: 0.938, 0.016, 82),        // #F0EADF
        border: PaletteColor(oklch: 0.875, 0.018, 80),         // #DCD5C9
        text: PaletteColor(oklch: 0.235, 0.012, 70),           // #221D18  15.8:1
        secondaryText: PaletteColor(oklch: 0.450, 0.016, 72),  // #5B544C   7.0:1
        tertiaryText: PaletteColor(oklch: 0.525, 0.014, 76),   // #6F6962   5.1:1
        accent: PaletteColor(oklch: 0.530, 0.135, 42),         // #AA4C23 Fortress Ember, 5.3:1
        link: PaletteColor(oklch: 0.530, 0.135, 42),
        selection: PaletteColor(oklch: 0.885, 0.050, 55),
        highlight: PaletteColor(oklch: 0.910, 0.105, 95),      // #F7E18F
        codeText: PaletteColor(oklch: 0.300, 0.020, 60),
        quoteBar: PaletteColor(oklch: 0.820, 0.030, 70),
        marks: Palette.markHues(lightness: 0.50, chroma: 0.14),
        followsSystemAccent: true).legible()

    /// Night: Linear-style blue-grey dark, not black (base ~#13151C, surfaces a few
    /// OKLCH lightness steps up, the list a step down; hue ~270, low chroma).
    public static let night = Palette(
        isDark: true,
        background: PaletteColor(oklch: 0.198, 0.013, 270),    // #13151C
        sunken: PaletteColor(oklch: 0.168, 0.011, 268),        // #0D0F14
        surface: PaletteColor(oklch: 0.238, 0.015, 272),       // #1C1E26
        border: PaletteColor(oklch: 0.310, 0.017, 272),        // #2D3039
        text: PaletteColor(oklch: 0.935, 0.006, 270),          // #E8E9EE  15.0:1
        secondaryText: PaletteColor(oklch: 0.770, 0.012, 270), // #B1B4BC   8.8:1
        tertiaryText: PaletteColor(oklch: 0.640, 0.015, 270),  // #888C96   5.4:1
        accent: PaletteColor(oklch: 0.745, 0.135, 50),         // #F09159 Ember (dark), 7.7:1
        link: PaletteColor(oklch: 0.745, 0.135, 50),
        selection: PaletteColor(oklch: 0.360, 0.060, 45),
        highlight: PaletteColor(oklch: 0.470, 0.085, 92),      // #6C5918
        codeText: PaletteColor(oklch: 0.870, 0.020, 250),
        quoteBar: PaletteColor(oklch: 0.400, 0.020, 272),
        marks: Palette.markHues(lightness: 0.76, chroma: 0.12),
        followsSystemAccent: true).legible()

    /// Evenly spaced OKLCH hues for the named text colours.
    static func markHues(lightness: Double, chroma: Double) -> [MarkHue: PaletteColor] {
        let hues: [(MarkHue, Double, Double)] = [
            (.red, 27, 1), (.orange, 50, 1), (.yellow, 85, 1), (.green, 145, 1), (.cyan, 200, 0.9),
            (.blue, 255, 1), (.purple, 305, 1), (.pink, 350, 1), (.brown, 55, 0.45), (.gray, 270, 0.05),
        ]
        var marks: [MarkHue: PaletteColor] = [:]
        for (hue, angle, chromaScale) in hues {
            // Yellow reads darker than its lightness suggests on paper: give it room.
            let l = hue == .yellow && lightness < 0.6 ? lightness - 0.02 : lightness
            marks[hue] = PaletteColor(oklch: l, chroma * chromaScale, angle)
        }
        return marks
    }
}
