import Foundation

/// A tinted-theming Base16 or Base24 scheme (github.com/tinted-theming/schemes), read
/// from its YAML file as published: the current format (`system`, `name`, `author`,
/// `variant`, `palette:` with `base00`…) and the legacy one (`scheme`, `author`, slots at
/// the top level, hex without `#`).
public struct Base16Scheme: Hashable, Sendable {
    public var system: String
    public var name: String
    public var author: String
    /// "light" or "dark" when the file says; otherwise from base00's lightness.
    public var variant: String?
    /// "base00"… to colours (16 slots for Base16, 24 for Base24).
    public var slots: [String: RGBColor]

    public enum ParseError: Error, Equatable {
        case missingSlot(String)
    }

    public var isDark: Bool { variant.map { $0 == "dark" } ?? (slots["base00"]?.isDark ?? false) }

    public init(yaml: String) throws {
        var fields: [String: String] = [:]
        var slots: [String: RGBColor] = [:]
        for rawLine in yaml.components(separatedBy: .newlines) {
            let line = Self.stripComment(rawLine).trimmingCharacters(in: .whitespaces)
            guard let colon = line.firstIndex(of: ":") else { continue }
            let key = line[..<colon].trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            var value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            if value.count >= 2, let first = value.first, first == "\"" || first == "'", value.last == first {
                value = String(value.dropFirst().dropLast())
            }
            let lower = key.lowercased()
            if lower.hasPrefix("base"), lower.count == 6, let color = RGBColor(hex: value) {
                slots[lower] = color
            } else if !value.isEmpty {
                fields[lower] = value
            }
        }
        for index in 0..<16 {
            let slot = String(format: "base%02X", index).lowercased()
            guard slots[slot] != nil else { throw ParseError.missingSlot(String(format: "base%02X", index)) }
        }
        let isBase24 = (0x10...0x17).allSatisfy { slots[String(format: "base%02X", $0).lowercased()] != nil }
        self.system = fields["system"] ?? (isBase24 ? "base24" : "base16")
        self.name = fields["name"] ?? fields["scheme"] ?? "Untitled"
        self.author = fields["author"] ?? ""
        self.variant = fields["variant"]?.lowercased()
        self.slots = slots
    }

    /// `# comment` outside quotes.
    private static func stripComment(_ line: String) -> String {
        var quote: Character?
        for (offset, character) in line.enumerated() {
            if let open = quote {
                if character == open { quote = nil }
            } else if character == "\"" || character == "'" {
                quote = character
            } else if character == "#", offset == 0 || line[line.index(line.startIndex, offsetBy: offset - 1)] == " " {
                // A hex value like `base00: fbf1c7` has no `#`; `"#fbf1c7"` is quoted.
                return String(line.prefix(offset))
            }
        }
        return line
    }

    public subscript(_ slot: Int) -> RGBColor? { slots[String(format: "base%02x", slot)] }
}

extension Palette {
    /// A palette from a Base16/Base24 scheme, following the tinted-theming styling
    /// guidelines: base00 background, base01 lighter background, base02 selection,
    /// base05 foreground, base08–base0F accents (base0A "search text background",
    /// base0B "markup code", base0C "markup quotes", base0D "headings" → our accent).
    /// Base24's base10/base11 (darker backgrounds) and base17 (bright magenta) are used
    /// when present. Secondary and tertiary text are mixed from base05 toward base00
    /// rather than read from base03/base04, whose use varies too much between schemes;
    /// `legible()` then enforces contrast.
    public init(base16 scheme: Base16Scheme) {
        let s = { (i: Int) in scheme[i]! }
        let background = s(0x00)
        let text = s(0x05)
        let dark = scheme.isDark
        var lowered = background.oklch
        lowered.lightness += dark ? -0.03 : -0.022
        let sunken = scheme[0x10] ?? RGBColor(oklch: lowered)
        var surface = s(0x01)
        if surface.contrast(with: background) < 1.04 { surface = background.mixed(with: text, 0.06) }
        let red = s(0x08), purple = s(0x0E)
        let marks: [MarkHue: RGBColor] = [
            .red: red, .orange: s(0x09), .yellow: s(0x0A), .green: s(0x0B), .cyan: s(0x0C),
            .blue: s(0x0D), .purple: purple, .brown: s(0x0F),
            .pink: scheme[0x17] ?? purple.mixed(with: red, 0.5),
            .gray: text.mixed(with: background, 0.45),
        ]
        self.init(
            isDark: dark, background: background, sunken: sunken, surface: surface,
            border: background.mixed(with: text, 0.16),
            text: text,
            secondaryText: text.mixed(with: background, 0.28),
            tertiaryText: text.mixed(with: background, 0.45),
            accent: s(0x0D), link: s(0x0D),
            selection: s(0x02),
            highlight: s(0x0A).tint(over: background, strength: dark ? 0.4 : 0.45, keeping: text),
            codeText: s(0x0B), quoteBar: s(0x0C),
            marks: marks)
        self = legible()
    }
}
