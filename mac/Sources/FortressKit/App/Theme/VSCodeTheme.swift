import Foundation

/// A VS Code colour theme (`*-color-theme.json`, as found in a .vsix or on Open VSX):
/// `name`, `type` ("light", "dark", "hc-black", "hc-light"), workbench `colors`, and
/// `tokenColors` (TextMate scopes). The file is JSON with comments and trailing commas.
public struct VSCodeTheme: Sendable {
    public var name: String
    public var type: String?
    /// Workbench colour id → colour with alpha (e.g. "editor.background").
    public var colors: [String: (color: RGBColor, alpha: Double)]
    /// TextMate scope → foreground.
    public var tokenForegrounds: [String: RGBColor]

    public enum ParseError: Error, Equatable {
        case notATheme
    }

    public init(json data: Data, fallbackName: String = "Imported Theme") throws {
        guard let text = String(data: data, encoding: .utf8),
              let clean = Self.stripJSONC(text).data(using: .utf8),
              let root = try JSONSerialization.jsonObject(with: clean) as? [String: Any]
        else { throw ParseError.notATheme }
        name = root["name"] as? String ?? fallbackName
        type = (root["type"] as? String)?.lowercased()
        colors = [:]
        for (key, value) in root["colors"] as? [String: Any] ?? [:] {
            if let string = value as? String, let parsed = Self.color(string) { colors[key] = parsed }
        }
        tokenForegrounds = [:]
        for rule in root["tokenColors"] as? [[String: Any]] ?? [] {
            guard let settings = rule["settings"] as? [String: Any], let fg = settings["foreground"] as? String,
                  let color = Self.color(fg)?.color else { continue }
            let scopes: [String]
            if let list = rule["scope"] as? [String] {
                scopes = list
            } else if let string = rule["scope"] as? String {
                scopes = string.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            } else {
                continue
            }
            for scope in scopes where tokenForegrounds[scope] == nil { tokenForegrounds[scope] = color }
        }
        guard colors["editor.background"] != nil || colors["editor.foreground"] != nil || !tokenForegrounds.isEmpty
        else { throw ParseError.notATheme }
    }

    public var isDark: Bool {
        if let type { return type == "dark" || type == "hc-black" || type == "vs-dark" || type == "hc" }
        return colors["editor.background"]?.color.isDark ?? true
    }

    /// A token colour for the first scope (or a more specific one) that the theme sets.
    func token(_ scopes: String...) -> RGBColor? {
        for scope in scopes {
            if let exact = tokenForegrounds[scope] { return exact }
            if let match = tokenForegrounds.first(where: { $0.key.hasPrefix(scope + ".") }) { return match.value }
        }
        return nil
    }

    static func color(_ string: String) -> (color: RGBColor, alpha: Double)? {
        var hex = string.trimmingCharacters(in: .whitespaces)
        if hex.hasPrefix("#") { hex.removeFirst() }
        var alpha = 1.0
        if hex.count == 8, let a = UInt8(hex.suffix(2), radix: 16) { alpha = Double(a) / 255 }
        if hex.count == 4, let a = UInt8(String(repeating: hex.last!, count: 2), radix: 16) { alpha = Double(a) / 255 }
        guard let color = RGBColor(hex: hex) else { return nil }
        return (color, alpha)
    }

    /// JSON with `//` and `/* */` comments and trailing commas → plain JSON.
    static func stripJSONC(_ text: String) -> String {
        var out = ""
        let chars = Array(text)
        var i = 0
        var inString = false
        while i < chars.count {
            let c = chars[i]
            if inString {
                out.append(c)
                if c == "\\", i + 1 < chars.count { out.append(chars[i + 1]); i += 2; continue }
                if c == "\"" { inString = false }
                i += 1
            } else if c == "\"" {
                inString = true
                out.append(c)
                i += 1
            } else if c == "/", i + 1 < chars.count, chars[i + 1] == "/" {
                while i < chars.count, chars[i] != "\n" { i += 1 }
            } else if c == "/", i + 1 < chars.count, chars[i + 1] == "*" {
                i += 2
                while i + 1 < chars.count, !(chars[i] == "*" && chars[i + 1] == "/") { i += 1 }
                i += 2
            } else if c == "," {
                // Drop a comma that only whitespace separates from } or ].
                var j = i + 1
                while j < chars.count, chars[j].isWhitespace { j += 1 }
                if j < chars.count, chars[j] == "}" || chars[j] == "]" { i += 1; continue }
                out.append(c)
                i += 1
            } else {
                out.append(c)
                i += 1
            }
        }
        return out
    }
}

extension Palette {
    /// A palette from a VS Code theme's workbench colours (editor, sidebar, text*
    /// "markdown" colours, terminal ANSI colours for the named marks), falling back to
    /// token colours and then to mixes. Translucent colours are composited over the
    /// editor background.
    public init(vsCode theme: VSCodeTheme) {
        let dark = theme.isDark
        let background = theme.colors["editor.background"]?.color
            ?? RGBColor(hex: dark ? "#1E1E1E" : "#FFFFFF")!
        func solid(_ keys: String...) -> RGBColor? {
            for key in keys {
                if let entry = theme.colors[key] { return background.mixed(with: entry.color, entry.alpha) }
            }
            return nil
        }
        let text = solid("editor.foreground", "foreground") ?? theme.token("source", "text")
            ?? RGBColor(hex: dark ? "#D4D4D4" : "#1F1F1F")!
        let accent = solid("textLink.foreground", "button.background", "focusBorder", "activityBarBadge.background")
            ?? theme.token("entity.name.function") ?? text
        let red = solid("terminal.ansiRed") ?? theme.token("invalid", "variable.other.constant") ?? RGBColor(hex: "#E5484D")!
        let yellow = solid("terminal.ansiYellow") ?? theme.token("entity.name.type", "support.type") ?? RGBColor(hex: "#D9A400")!
        let green = solid("terminal.ansiGreen") ?? theme.token("string") ?? RGBColor(hex: "#3FA35A")!
        let cyan = solid("terminal.ansiCyan") ?? theme.token("support.function", "string.regexp") ?? RGBColor(hex: "#2AA3B8")!
        let blue = solid("terminal.ansiBlue") ?? theme.token("entity.name.function") ?? accent
        let purple = solid("terminal.ansiMagenta") ?? theme.token("keyword", "storage") ?? RGBColor(hex: "#9A5BD6")!
        let orange = theme.token("constant.numeric", "constant") ?? red.mixed(with: yellow, 0.5)
        var surface = solid("textCodeBlock.background", "editorWidget.background", "editor.lineHighlightBackground")
            ?? background.mixed(with: text, 0.06)
        if surface.contrast(with: background) < 1.04 { surface = background.mixed(with: text, 0.06) }
        self.init(
            isDark: dark, background: background,
            sunken: solid("sideBar.background", "panel.background") ?? background.mixed(with: .black, dark ? 0.2 : 0.03),
            surface: surface,
            border: solid("editorGroup.border", "panel.border", "sideBar.border") ?? background.mixed(with: text, 0.16),
            text: text,
            secondaryText: solid("descriptionForeground") ?? text.mixed(with: background, 0.28),
            tertiaryText: theme.token("comment") ?? text.mixed(with: background, 0.45),
            accent: accent, link: solid("textLink.foreground") ?? accent,
            selection: solid("editor.selectionBackground") ?? background.mixed(with: accent, 0.25),
            highlight: solid("editor.findMatchHighlightBackground", "editor.findMatchBackground")
                ?? yellow.tint(over: background, strength: 0.45, keeping: text),
            codeText: solid("textPreformat.foreground") ?? theme.token("markup.inline.raw", "string") ?? text,
            quoteBar: solid("textBlockQuote.border") ?? background.mixed(with: text, 0.25),
            marks: [
                .red: red, .orange: orange, .yellow: yellow, .green: green, .cyan: cyan, .blue: blue,
                .purple: purple, .pink: solid("terminal.ansiBrightMagenta") ?? purple.mixed(with: red, 0.5),
                .brown: orange.mixed(with: background, 0.3), .gray: text.mixed(with: background, 0.45),
            ])
        self = legible()
    }
}

extension RGBColor {
    static let black = RGBColor(red: 0, green: 0, blue: 0)
    static let white = RGBColor(red: 1, green: 1, blue: 1)
}
