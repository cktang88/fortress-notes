import AppKit
import Observation

/// System, Light or Dark. System is the default (HIG: respect the system setting).
public enum AppearanceMode: String, CaseIterable, Identifiable, Sendable {
    case system, light, dark

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .system: "System"
        case .light: "Light"
        case .dark: "Dark"
        }
    }
}

/// The user's appearance and theme choices (a light theme and a dark theme; the system
/// appearance picks between them), persisted in user defaults, plus the themes on offer.
@MainActor @Observable
public final class ThemeStore {
    public static let shared = ThemeStore()

    public static let appearanceKey = "theme.appearance"
    public static let lightThemeKey = "theme.light"
    public static let darkThemeKey = "theme.dark"

    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private let importDirectory: URL
    @ObservationIgnored private var observers: [NSObjectProtocol] = []
    @ObservationIgnored private let runtime: ThemeRuntime

    public var appearance: AppearanceMode {
        didSet {
            defaults.set(appearance.rawValue, forKey: Self.appearanceKey)
            applyAppearance()
        }
    }

    public var lightThemeID: String {
        didSet {
            defaults.set(lightThemeID, forKey: Self.lightThemeKey)
            apply()
        }
    }

    public var darkThemeID: String {
        didSet {
            defaults.set(darkThemeID, forKey: Self.darkThemeKey)
            apply()
        }
    }

    public private(set) var themes: [Theme]
    public private(set) var active: ActivePalettes = .default

    /// `runtime` is what AppKit drawing reads; tests pass their own so they don't
    /// recolour other tests' windows.
    public init(defaults: UserDefaults = .standard, importDirectory: URL = ThemeLibrary.importDirectory,
                runtime: ThemeRuntime = .shared) {
        self.defaults = defaults
        self.importDirectory = importDirectory
        self.runtime = runtime
        appearance = AppearanceMode(rawValue: defaults.string(forKey: Self.appearanceKey) ?? "") ?? .system
        lightThemeID = defaults.string(forKey: Self.lightThemeKey) ?? Theme.paper.id
        darkThemeID = defaults.string(forKey: Self.darkThemeKey) ?? Theme.night.id
        themes = ThemeLibrary.builtIn + ThemeLibrary.importedThemes(in: importDirectory)
        apply()
        if appearance != .system, runtime === ThemeRuntime.shared { applyAppearance() }
        // The default theme follows the system accent colour: pick up changes to it.
        observers.append(NotificationCenter.default.addObserver(
            forName: NSColor.systemColorsDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.apply() }
        })
    }

    public var lightThemes: [Theme] { themes.filter { !$0.isDark } }
    public var darkThemes: [Theme] { themes.filter(\.isDark) }

    public var lightTheme: Theme { themes.first { $0.id == lightThemeID && !$0.isDark } ?? .paper }
    public var darkTheme: Theme { themes.first { $0.id == darkThemeID && $0.isDark } ?? .night }

    /// Adds a Base16/Base24 or VS Code theme file and selects it for its appearance.
    @discardableResult
    public func importTheme(from url: URL) throws -> Theme {
        let theme = try ThemeLibrary.importTheme(from: url, into: importDirectory)
        reloadImported()
        if theme.isDark { darkThemeID = theme.id } else { lightThemeID = theme.id }
        return theme
    }

    public func remove(_ theme: Theme) {
        ThemeLibrary.removeImported(theme)
        reloadImported()
        if lightThemeID == theme.id { lightThemeID = Theme.paper.id }
        if darkThemeID == theme.id { darkThemeID = Theme.night.id }
    }

    public func reloadImported() {
        themes = ThemeLibrary.builtIn + ThemeLibrary.importedThemes(in: importDirectory)
        apply()
    }

    /// Back to Paper and Night, following the system.
    public func resetToDefaults() {
        appearance = .system
        lightThemeID = Theme.paper.id
        darkThemeID = Theme.night.id
    }

    /// The user's accent colour, or nil for Multicolor (no explicit choice), when the
    /// app's own accent (Fortress Ember) applies.
    public static var systemAccent: PaletteColor? {
        guard UserDefaults.standard.object(forKey: "AppleAccentColor") != nil else { return nil }
        return PaletteColor(NSColor.controlAccentColor)
    }

    func apply() {
        let next = ActivePalettes(light: lightTheme.palette, dark: darkTheme.palette, systemAccent: Self.systemAccent)
        guard next != runtime.active || next != active else { return }
        active = next
        runtime.update(next)
        if runtime === ThemeRuntime.shared { Self.redrawWindows() }
    }

    /// AppKit views hold dynamic colours that resolve when drawn: redraw them, and lay
    /// out text again (TextKit keeps resolved colours in its line fragments).
    static func redrawWindows() {
        guard let app = NSApp else { return }
        func visit(_ view: NSView) {
            view.needsDisplay = true
            if let textView = view as? NSTextView, let manager = textView.textLayoutManager {
                manager.invalidateLayout(for: manager.documentRange)
                manager.textViewportLayoutController.layoutViewport()
            }
            view.subviews.forEach(visit)
        }
        for window in app.windows {
            if let content = window.contentView?.superview ?? window.contentView { visit(content) }
            window.invalidateShadow()
        }
    }

    private func applyAppearance() {
        guard runtime === ThemeRuntime.shared, let app = NSApp else { return }
        switch appearance {
        case .system: app.appearance = nil
        case .light: app.appearance = NSAppearance(named: .aqua)
        case .dark: app.appearance = NSAppearance(named: .darkAqua)
        }
    }
}
