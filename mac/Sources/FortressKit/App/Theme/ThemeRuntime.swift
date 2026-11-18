import AppKit
import SwiftUI

extension Palette {
    /// A palette colour by name, for dynamic colours and styles.
    public enum Role: String, CaseIterable, Sendable {
        case background, sunken, surface, border, text, secondaryText, tertiaryText
        case accent, link, selection, highlight, codeText, quoteBar
    }

    public subscript(role: Role) -> PaletteColor {
        switch role {
        case .background: background
        case .sunken: sunken
        case .surface: surface
        case .border: border
        case .text: text
        case .secondaryText: secondaryText
        case .tertiaryText: tertiaryText
        case .accent: accent
        case .link: link
        case .selection: selection
        case .highlight: highlight
        case .codeText: codeText
        case .quoteBar: quoteBar
        }
    }
}

/// The palettes in use for each appearance, with the system accent and the Increase
/// Contrast variants worked out once.
public struct ActivePalettes: Hashable, Sendable {
    public var light: Palette
    public var dark: Palette
    public var lightIncreased: Palette
    public var darkIncreased: Palette

    /// `systemAccent` is nil when the user's accent colour is Multicolor.
    public init(light: Palette, dark: Palette, systemAccent: PaletteColor? = nil) {
        self.light = light.withSystemAccent(systemAccent)
        self.dark = dark.withSystemAccent(systemAccent)
        lightIncreased = light.withSystemAccent(systemAccent, minimum: 7).increasedContrast()
        darkIncreased = dark.withSystemAccent(systemAccent, minimum: 7).increasedContrast()
    }

    public static let `default` = ActivePalettes(light: .paper, dark: .night)

    public func palette(dark isDark: Bool, increasedContrast: Bool) -> Palette {
        switch (isDark, increasedContrast) {
        case (false, false): light
        case (true, false): dark
        case (false, true): lightIncreased
        case (true, true): darkIncreased
        }
    }

    public func palette(for appearance: NSAppearance) -> Palette {
        let match = appearance.bestMatch(from: [.aqua, .darkAqua, .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua])
        // A window's appearance carries Increase Contrast as the high-contrast names; an
        // appearance made by name doesn't, so ask the workspace as well.
        let increased = match == .accessibilityHighContrastAqua || match == .accessibilityHighContrastDarkAqua
            || NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast
        return palette(dark: match == .darkAqua || match == .accessibilityHighContrastDarkAqua, increasedContrast: increased)
    }
}

/// The palettes AppKit drawing resolves against (the editor, tables, layout fragments).
/// Its colours are stable dynamic `NSColor`s, so cached text attributes stay valid; a
/// theme change posts `themeDidChange` and views redraw.
public final class ThemeRuntime: @unchecked Sendable {
    public static let shared = ThemeRuntime()
    public static let themeDidChange = Notification.Name("FortressThemeDidChange")

    public init() {}

    private let lock = NSLock()
    private var _active = ActivePalettes.default
    private var derived: [DerivedKey: PaletteColor] = [:]

    private struct DerivedKey: Hashable {
        enum Kind: Hashable { case text, fill, markText, markHighlight }
        var kind: Kind
        var color: PaletteColor
        var hue: MarkHue?
        var palette: Palette
    }

    public var active: ActivePalettes {
        lock.lock()
        defer { lock.unlock() }
        return _active
    }

    public func update(_ active: ActivePalettes) {
        lock.lock()
        let changed = _active != active
        _active = active
        if changed { derived.removeAll() }
        lock.unlock()
        if changed {
            if Thread.isMainThread {
                NotificationCenter.default.post(name: Self.themeDidChange, object: nil)
            } else {
                DispatchQueue.main.async { NotificationCenter.default.post(name: Self.themeDidChange, object: nil) }
            }
        }
    }

    func palette(for appearance: NSAppearance) -> Palette { active.palette(for: appearance) }

    private func derive(_ key: DerivedKey, _ make: () -> PaletteColor) -> PaletteColor {
        lock.lock()
        if let found = derived[key] { lock.unlock(); return found }
        lock.unlock()
        let made = make()
        lock.lock()
        if derived.count > 2048 { derived.removeAll() }
        derived[key] = made
        lock.unlock()
        return made
    }

    // MARK: Dynamic colours

    /// A palette role as a dynamic colour (resolved per appearance at draw time).
    public static func color(_ role: Palette.Role) -> NSColor {
        NSColor(name: NSColor.Name("fortress.\(role.rawValue)")) { appearance in
            ThemeRuntime.shared.palette(for: appearance)[role].nsColor
        }
    }

    /// A named text colour (OneNote's "red", "blue", …) in the current theme.
    public static func markText(_ hue: MarkHue) -> NSColor {
        NSColor(name: nil) { appearance in
            let runtime = ThemeRuntime.shared
            let palette = runtime.palette(for: appearance)
            return palette.markText(hue).nsColor
        }
    }

    /// A named highlighter in the current theme.
    public static func markHighlight(_ hue: MarkHue) -> NSColor {
        NSColor(name: nil) { appearance in
            let runtime = ThemeRuntime.shared
            let palette = runtime.palette(for: appearance)
            return runtime.derive(DerivedKey(kind: .markHighlight, color: palette.background, hue: hue, palette: palette)) {
                palette.markHighlight(hue)
            }.nsColor
        }
    }

    /// A text colour picked in another app, made legible (`Palette.legibleText`).
    public static func foreignText(_ color: PaletteColor) -> NSColor {
        NSColor(name: nil) { appearance in
            let runtime = ThemeRuntime.shared
            let palette = runtime.palette(for: appearance)
            return runtime.derive(DerivedKey(kind: .text, color: color, palette: palette)) { palette.legibleText(color) }.nsColor
        }
    }

    /// A cell or block fill picked in another app, made quiet enough for text (`Palette.legibleFill`).
    public static func foreignFill(_ color: PaletteColor) -> NSColor {
        NSColor(name: nil) { appearance in
            let runtime = ThemeRuntime.shared
            let palette = runtime.palette(for: appearance)
            return runtime.derive(DerivedKey(kind: .fill, color: color, palette: palette)) { palette.legibleFill(color) }.nsColor
        }
    }
}

extension NSColor {
    nonisolated(unsafe) public static let themeBackground = ThemeRuntime.color(.background)
    nonisolated(unsafe) public static let themeSunken = ThemeRuntime.color(.sunken)
    nonisolated(unsafe) public static let themeSurface = ThemeRuntime.color(.surface)
    nonisolated(unsafe) public static let themeBorder = ThemeRuntime.color(.border)
    nonisolated(unsafe) public static let themeText = ThemeRuntime.color(.text)
    nonisolated(unsafe) public static let themeSecondaryText = ThemeRuntime.color(.secondaryText)
    nonisolated(unsafe) public static let themeTertiaryText = ThemeRuntime.color(.tertiaryText)
    nonisolated(unsafe) public static let themeAccent = ThemeRuntime.color(.accent)
    nonisolated(unsafe) public static let themeLink = ThemeRuntime.color(.link)
    nonisolated(unsafe) public static let themeSelection = ThemeRuntime.color(.selection)
    nonisolated(unsafe) public static let themeHighlight = ThemeRuntime.color(.highlight)
    nonisolated(unsafe) public static let themeCodeText = ThemeRuntime.color(.codeText)
    nonisolated(unsafe) public static let themeQuoteBar = ThemeRuntime.color(.quoteBar)
}

// MARK: SwiftUI

private struct ThemePalettesKey: EnvironmentKey {
    static var defaultValue: ActivePalettes { ThemeRuntime.shared.active }
}

extension EnvironmentValues {
    /// The active palettes; set at each window's root by `.fortressTheme()`.
    public var themePalettes: ActivePalettes {
        get { self[ThemePalettesKey.self] }
        set { self[ThemePalettesKey.self] = newValue }
    }

    /// The palette for this view's appearance and contrast setting.
    public var themePalette: Palette {
        themePalettes.palette(dark: colorScheme == .dark, increasedContrast: colorSchemeContrast == .increased)
    }
}

extension PaletteColor {
    public var color: Color { Color(.sRGB, red: red, green: green, blue: blue) }
}

/// A palette role as a SwiftUI style; it resolves against the window's appearance,
/// Increase Contrast and the active theme.
public struct ThemeStyle: ShapeStyle {
    public var role: Palette.Role

    public init(_ role: Palette.Role) { self.role = role }

    public func resolve(in environment: EnvironmentValues) -> Color {
        environment.themePalette[role].color
    }
}

extension ShapeStyle where Self == ThemeStyle {
    public static var themeBackground: ThemeStyle { ThemeStyle(.background) }
    public static var themeSunken: ThemeStyle { ThemeStyle(.sunken) }
    public static var themeSurface: ThemeStyle { ThemeStyle(.surface) }
    public static var themeBorder: ThemeStyle { ThemeStyle(.border) }
    public static var themeText: ThemeStyle { ThemeStyle(.text) }
    public static var themeSecondary: ThemeStyle { ThemeStyle(.secondaryText) }
    public static var themeTertiary: ThemeStyle { ThemeStyle(.tertiaryText) }
    public static var themeAccent: ThemeStyle { ThemeStyle(.accent) }
    public static var themeHighlight: ThemeStyle { ThemeStyle(.highlight) }
}

extension View {
    /// At a window's root: the active palettes, and the theme accent as the tint (chrome
    /// stays native and only takes this accent).
    public func fortressTheme() -> some View { modifier(ThemeRoot()) }

    /// On a content-layer column (editor, note list, Ask, search results, inspector):
    /// its background and the theme's text colour.
    public func themedContent(_ background: Palette.Role = .background) -> some View {
        self.foregroundStyle(.themeText).background(ThemeStyle(background))
    }
}

struct ThemeRoot: ViewModifier {
    @MainActor private var store: ThemeStore { ThemeStore.shared }

    func body(content: Content) -> some View {
        content
            .environment(\.themePalettes, store.active)
            .tint(ThemeStyle(.accent))
    }
}

extension Color {
    /// The theme accent ("Fortress Ember" in the default palettes, or the user's system
    /// accent colour); prefer `.fortressTheme()` at a window root, which sets the tint.
    public static let fortressEmber = Color(nsColor: .themeAccent)
}
