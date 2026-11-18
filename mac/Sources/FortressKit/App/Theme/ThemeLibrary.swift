import Foundation

/// A named palette and where it came from. There is no Fortress theme file: a theme is
/// one of the two built-in defaults, a bundled Base16 scheme, or a Base16/Base24 YAML or
/// VS Code JSON file the user imported (kept as is in `ThemeLibrary.importDirectory`).
public struct Theme: Identifiable, Hashable, Sendable {
    public enum Source: Hashable, Sendable {
        case builtIn
        case bundled
        case imported(URL)
    }

    public var id: String
    public var name: String
    public var author: String
    public var palette: Palette
    public var source: Source

    public var isDark: Bool { palette.isDark }

    public static let paper = Theme(id: "fortress.paper", name: "Paper", author: "Fortress", palette: .paper, source: .builtIn)
    public static let night = Theme(id: "fortress.night", name: "Night", author: "Fortress", palette: .night, source: .builtIn)
}

public enum ThemeLibrary {
    public enum ImportError: LocalizedError {
        case unreadable(String)

        public var errorDescription: String? {
            switch self {
            case .unreadable(let name):
                "“\(name)” isn’t a Base16/Base24 scheme (.yaml) or a VS Code color theme (.json)."
            }
        }
    }

    /// The defaults first, then the bundled schemes.
    public static let builtIn: [Theme] = [Theme.paper, Theme.night] + BundledSchemes.all.compactMap { entry in
        guard let scheme = try? Base16Scheme(yaml: entry.yaml) else { return nil }
        return Theme(id: "base16.\(entry.id)", name: scheme.name, author: scheme.author,
                     palette: Palette(base16: scheme), source: .bundled)
    }

    /// ~/Library/Application Support/Fortress Notes/Themes
    public static var importDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("Fortress Notes/Themes", isDirectory: true)
    }

    /// A theme from a file: Base16/Base24 YAML or VS Code colour theme JSON (by content,
    /// whatever the extension).
    public static func theme(at url: URL, id: String? = nil) throws -> Theme {
        let data = try Data(contentsOf: url)
        let fallbackName = url.deletingPathExtension().lastPathComponent
            .replacingOccurrences(of: "-color-theme", with: "")
        let themeID = id ?? "imported.\(url.lastPathComponent)"
        let isJSON = url.pathExtension.lowercased().hasPrefix("json")
            || String(decoding: data.prefix(64), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("{")
        if isJSON, let vsCode = try? VSCodeTheme(json: data, fallbackName: fallbackName) {
            return Theme(id: themeID, name: vsCode.name, author: "", palette: Palette(vsCode: vsCode), source: .imported(url))
        }
        if let yaml = String(data: data, encoding: .utf8), let scheme = try? Base16Scheme(yaml: yaml) {
            return Theme(id: themeID, name: scheme.name, author: scheme.author, palette: Palette(base16: scheme), source: .imported(url))
        }
        throw ImportError.unreadable(url.lastPathComponent)
    }

    /// Copies a theme file into the import directory (checked first) and returns it.
    public static func importTheme(from url: URL, into directory: URL = importDirectory) throws -> Theme {
        _ = try theme(at: url)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(url.lastPathComponent)
        if destination.standardizedFileURL != url.standardizedFileURL {
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.copyItem(at: url, to: destination)
        }
        return try theme(at: destination)
    }

    /// The readable theme files in the import directory, by name.
    public static func importedThemes(in directory: URL = importDirectory) -> [Theme] {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.filter { ["yaml", "yml", "json", "jsonc"].contains($0.pathExtension.lowercased()) }
            .compactMap { try? theme(at: $0) }
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    public static func removeImported(_ theme: Theme) {
        if case .imported(let url) = theme.source { try? FileManager.default.removeItem(at: url) }
    }

    /// Licences of the bundled schemes (all MIT), shown in Settings.
    public static let licenseNotices = """
        Bundled color schemes come from Tinted Theming's Base16 collection \
        (github.com/tinted-theming/schemes), MIT License, © 2022 Tinted Theming.

        Solarized © 2011 Ethan Schoonover, MIT License.
        Gruvbox © Pavel Pertsev (morhetz), MIT/X11 License.
        Catppuccin © 2021 Catppuccin, MIT License.
        Rosé Pine © 2023 Rosé Pine, MIT License.
        Nord © 2016-present Sven Greb, MIT License.

        Permission is hereby granted, free of charge, to any person obtaining a copy of \
        this software and associated documentation files (the "Software"), to deal in the \
        Software without restriction, including without limitation the rights to use, copy, \
        modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, \
        and to permit persons to whom the Software is furnished to do so, subject to the \
        following conditions: The above copyright notice and this permission notice shall \
        be included in all copies or substantial portions of the Software. THE SOFTWARE IS \
        PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT \
        LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND \
        NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY \
        CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR \
        OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR \
        OTHER DEALINGS IN THE SOFTWARE.
        """
}
