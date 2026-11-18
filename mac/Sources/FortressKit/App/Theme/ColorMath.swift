import AppKit

/// An opaque sRGB colour with components in 0…1, plus the colour science the theme
/// system needs: WCAG 2 contrast, and OKLab/OKLCH (Björn Ottosson, 2020) for perceptual
/// mixing and for changing lightness without shifting hue.
public struct RGBColor: Hashable, Sendable, Codable {
    public var red: Double
    public var green: Double
    public var blue: Double

    public init(red: Double, green: Double, blue: Double) {
        self.red = min(1, max(0, red))
        self.green = min(1, max(0, green))
        self.blue = min(1, max(0, blue))
    }

    /// `#rgb`, `#rrggbb` or `#rrggbbaa` (alpha ignored), with or without `#`.
    public init?(hex string: String) {
        var hex = string.trimmingCharacters(in: .whitespacesAndNewlines)
        if hex.hasPrefix("#") { hex.removeFirst() }
        if hex.count == 3 || hex.count == 4 { hex = hex.prefix(3).map { "\($0)\($0)" }.joined() }
        if hex.count == 8 { hex = String(hex.prefix(6)) }
        guard hex.count == 6, let value = UInt32(hex, radix: 16) else { return nil }
        self.init(red: Double((value >> 16) & 0xFF) / 255, green: Double((value >> 8) & 0xFF) / 255,
                  blue: Double(value & 0xFF) / 255)
    }

    public var hex: String {
        String(format: "#%02X%02X%02X", Int((red * 255).rounded()), Int((green * 255).rounded()), Int((blue * 255).rounded()))
    }

    public var nsColor: NSColor { NSColor(srgbRed: red, green: green, blue: blue, alpha: 1) }

    public init?(_ color: NSColor) {
        guard let srgb = color.usingColorSpace(.sRGB) else { return nil }
        self.init(red: srgb.redComponent, green: srgb.greenComponent, blue: srgb.blueComponent)
    }

    // MARK: WCAG 2

    /// Relative luminance (WCAG 2.2, "relative luminance").
    public var luminance: Double {
        0.2126 * Self.linear(red) + 0.7152 * Self.linear(green) + 0.0722 * Self.linear(blue)
    }

    /// Contrast ratio, 1…21 (WCAG 2.2: AA text needs 4.5, large text and UI parts 3).
    public func contrast(with other: RGBColor) -> Double {
        let a = luminance, b = other.luminance
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)
    }

    public var isDark: Bool { oklch.lightness < 0.6 }

    // MARK: OKLab / OKLCH

    public struct OKLCH: Hashable, Sendable {
        public var lightness: Double
        public var chroma: Double
        /// Degrees.
        public var hue: Double

        public init(_ lightness: Double, _ chroma: Double, _ hue: Double) {
            self.lightness = lightness
            self.chroma = chroma
            self.hue = hue
        }
    }

    private static func linear(_ c: Double) -> Double { c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
    private static func gamma(_ c: Double) -> Double { c <= 0.0031308 ? 12.92 * c : 1.055 * pow(c, 1 / 2.4) - 0.055 }

    var oklab: (l: Double, a: Double, b: Double) {
        let r = Self.linear(red), g = Self.linear(green), b = Self.linear(blue)
        let l = cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
        let m = cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
        let s = cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
        return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
                1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
                0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)
    }

    /// The colour for OKLab coordinates, or nil when it is outside sRGB.
    static func fromOKLab(_ L: Double, _ A: Double, _ B: Double, clamp: Bool) -> RGBColor? {
        let l = pow(L + 0.3963377774 * A + 0.2158037573 * B, 3)
        let m = pow(L - 0.1055613458 * A - 0.0638541728 * B, 3)
        let s = pow(L - 0.0894841775 * A - 1.2914855480 * B, 3)
        let r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
        let g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
        let b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
        let epsilon = 1e-4
        if !clamp, [r, g, b].contains(where: { $0 < -epsilon || $0 > 1 + epsilon }) { return nil }
        return RGBColor(red: gamma(max(0, r)), green: gamma(max(0, g)), blue: gamma(max(0, b)))
    }

    public var oklch: OKLCH {
        let lab = oklab
        let hue = atan2(lab.b, lab.a) * 180 / .pi
        return OKLCH(lab.l, hypot(lab.a, lab.b), hue < 0 ? hue + 360 : hue)
    }

    /// The colour for OKLCH coordinates; out-of-gamut chroma is reduced (hue and
    /// lightness kept), as CSS Color 4 gamut mapping does.
    public init(oklch: OKLCH) {
        let L = min(1, max(0, oklch.lightness))
        let h = oklch.hue * .pi / 180
        var low = 0.0, high = max(0, oklch.chroma)
        if let exact = Self.fromOKLab(L, high * cos(h), high * sin(h), clamp: false) {
            self = exact
            return
        }
        for _ in 0..<24 {
            let mid = (low + high) / 2
            if Self.fromOKLab(L, mid * cos(h), mid * sin(h), clamp: false) != nil { low = mid } else { high = mid }
        }
        self = Self.fromOKLab(L, low * cos(h), low * sin(h), clamp: true)!
    }

    public init(oklch lightness: Double, _ chroma: Double, _ hue: Double) {
        self.init(oklch: OKLCH(lightness, chroma, hue))
    }

    /// Perceptual mix in OKLab: 0 is self, 1 is `other`.
    public func mixed(with other: RGBColor, _ fraction: Double) -> RGBColor {
        if fraction <= 0 { return self }
        if fraction >= 1 { return other }
        let t = fraction
        let a = oklab, b = other.oklab
        return Self.fromOKLab(a.l + (b.l - a.l) * t, a.a + (b.a - a.a) * t, a.b + (b.b - a.b) * t, clamp: true)!
    }

    /// The same hue and chroma with the lightness moved, as little as possible, away
    /// from `background` until the contrast reaches `ratio`. Colours that already pass
    /// come back unchanged, so a theme author's choice survives whenever it is legible.
    public func ensuringContrast(_ ratio: Double, against background: RGBColor) -> RGBColor {
        if contrast(with: background) >= ratio { return self }
        let lch = oklch
        // Darker on light backgrounds, lighter on dark ones; the other way if that can't work.
        let towardDark = background.luminance > 0.18
        for darker in [towardDark, !towardDark] {
            let limit = darker ? 0.0 : 1.0
            let extreme = RGBColor(oklch: OKLCH(limit, lch.chroma, lch.hue))
            guard extreme.contrast(with: background) >= ratio else { continue }
            var near = lch.lightness, far = limit
            for _ in 0..<22 {
                let mid = (near + far) / 2
                if RGBColor(oklch: OKLCH(mid, lch.chroma, lch.hue)).contrast(with: background) >= ratio { far = mid } else { near = mid }
            }
            return RGBColor(oklch: OKLCH(far, lch.chroma, lch.hue))
        }
        // Not reachable at this chroma: the best neutral.
        let black = RGBColor(red: 0, green: 0, blue: 0), white = RGBColor(red: 1, green: 1, blue: 1)
        return black.contrast(with: background) > white.contrast(with: background) ? black : white
    }

    /// A background tint of `self` over `base` (fraction 0…`strength`), the strongest one
    /// on which `text` still has `ratio` contrast.
    public func tint(over base: RGBColor, strength: Double, keeping text: RGBColor, ratio: Double = 4.5) -> RGBColor {
        var t = min(1, max(0, strength))
        if text.contrast(with: base.mixed(with: self, t)) >= ratio { return base.mixed(with: self, t) }
        var low = 0.0
        for _ in 0..<18 {
            let mid = (low + t) / 2
            if text.contrast(with: base.mixed(with: self, mid)) >= ratio { low = mid } else { t = mid }
        }
        return base.mixed(with: self, low)
    }
}
