import AppKit
import Foundation
import Testing

@testable import FortressKit

/// Colour maths, the contrast rules, the default palettes, and the Base16/Base24 and
/// VS Code theme readers.
struct ThemeTests {
    private func hex(_ string: String) -> RGBColor { RGBColor(hex: string)! }

    // MARK: Colour maths

    @Test func hexAndContrast() {
        #expect(hex("#FBF8F1").hex == "#FBF8F1")
        #expect(hex("abc").hex == "#AABBCC")
        #expect(hex("#11223380").hex == "#112233")
        #expect(RGBColor(hex: "#12") == nil)
        #expect(abs(hex("#000000").contrast(with: hex("#FFFFFF")) - 21) < 0.01)
        // WCAG's own example pair: #777 on white is just under 4.5.
        #expect(abs(hex("#777777").contrast(with: .white) - 4.48) < 0.01)
    }

    @Test func oklchRoundTrip() {
        for string in ["#B4532A", "#13151C", "#FBF8F1", "#268BD2", "#859900"] {
            let color = hex(string)
            let back = RGBColor(oklch: color.oklch)
            #expect(back.hex == color.hex, "\(string) → \(back.hex)")
        }
        // Out-of-gamut chroma is reduced, not clipped per channel: the hue survives.
        let vivid = RGBColor(oklch: 0.7, 0.4, 145)
        #expect(abs(vivid.oklch.hue - 145) < 3)
    }

    // MARK: Contrast rule

    @Test func contrastRuleKeepsLegibleColoursAndHue() {
        let navy = hex("#1F3A93")
        // Already fine on paper: unchanged.
        #expect(Palette.paper.legibleText(navy) == navy)
        // On Night it is lifted to 4.5:1 with its hue kept.
        let lifted = Palette.night.legibleText(navy)
        #expect(lifted.contrast(with: Palette.night.background) >= 4.5)
        #expect(abs(lifted.oklch.hue - navy.oklch.hue) < 6)
        // A pale yellow chosen for dark paper is darkened on cream.
        let pale = hex("#FFF2A8")
        #expect(Palette.paper.legibleText(pale).contrast(with: Palette.paper.background) >= 4.5)
    }

    @Test func fillRuleKeepsTextLegible() {
        for palette in [Palette.paper, Palette.night] + ThemeLibrary.builtIn.map(\.palette) {
            for fill in ["#FFFF00", "#00B0F0", "#C00000", "#000000", "#FFFFFF", "#92D050"] {
                let tint = palette.legibleFill(hex(fill))
                #expect(palette.text.contrast(with: tint) >= 4.5, "\(fill) fill")
            }
            for hue in MarkHue.allCases {
                #expect(palette.text.contrast(with: palette.markHighlight(hue)) >= 4.5, "\(hue) highlight")
            }
        }
        // On a light page a light fill is kept exactly.
        #expect(Palette.paper.legibleFill(hex("#FFF2CC")) == hex("#FFF2CC"))
    }

    // MARK: Palettes

    /// Every rule `legible()` promises, checked from the outside.
    private func expectLegible(_ p: Palette, _ name: String, minimums: Palette.Minimums = .standard) {
        #expect(p.text.contrast(with: p.background) >= minimums.text - 0.01, "\(name) text")
        for bg in [p.background, p.sunken, p.surface] {
            #expect(p.text.contrast(with: bg) >= minimums.secondaryText - 0.01, "\(name) text on surfaces")
            #expect(p.secondaryText.contrast(with: bg) >= minimums.secondaryText - 0.01, "\(name) secondary")
            #expect(p.tertiaryText.contrast(with: bg) >= minimums.tertiaryText - 0.01, "\(name) tertiary")
            #expect(p.accent.contrast(with: bg) >= minimums.secondaryText - 0.01, "\(name) accent")
            #expect(p.link.contrast(with: bg) >= minimums.secondaryText - 0.01, "\(name) link")
            #expect(p.codeText.contrast(with: bg) >= minimums.secondaryText - 0.01, "\(name) code")
            for (hue, color) in p.marks {
                #expect(color.contrast(with: bg) >= minimums.secondaryText - 0.01, "\(name) \(hue)")
            }
        }
        #expect(p.text.contrast(with: p.selection) >= minimums.secondaryText - 0.01, "\(name) selection")
        #expect(p.text.contrast(with: p.highlight) >= minimums.secondaryText - 0.01, "\(name) highlight")
        // Citation chip: accent on accent at 15% over the page.
        let b = p.background, a = p.accent
        let chip = RGBColor(red: b.red + (a.red - b.red) * 0.15, green: b.green + (a.green - b.green) * 0.15,
                            blue: b.blue + (a.blue - b.blue) * 0.15)
        #expect(a.contrast(with: chip) >= minimums.secondaryText, "\(name) chip")
    }

    @Test func defaultPalettesAreWarmPaperAndBlueGreyNight() {
        // The designed values (OKLCH) land on the intended hex.
        #expect(Palette.paper.background.hex == "#FBF8F1")
        #expect(Palette.paper.sunken.hex == "#F6F1E7")
        #expect(Palette.night.background.hex == "#13151C")
        #expect(Palette.night.sunken.hex == "#0D0F14")
        #expect(!Palette.paper.isDark && Palette.night.isDark)
        // Not stark: neither pure white/black pages nor pure black/white text.
        #expect(Palette.paper.background != .white && Palette.paper.text != .black)
        #expect(Palette.night.background != .black && Palette.night.text != .white)
        // Night is blue-grey: its hue sits in the blues with a little chroma.
        let lch = Palette.night.background.oklch
        #expect(lch.hue > 240 && lch.hue < 290 && lch.chroma > 0.008)
        // Warm paper: hue in the yellows.
        #expect(Palette.paper.background.oklch.hue > 60 && Palette.paper.background.oklch.hue < 100)
        expectLegible(Palette.paper, "paper")
        expectLegible(Palette.night, "night")
    }

    @Test func increasedContrastAndSystemAccent() {
        let active = ActivePalettes(light: .paper, dark: .night, systemAccent: hex("#007AFF"))
        expectLegible(active.lightIncreased, "paper+", minimums: .increased)
        expectLegible(active.darkIncreased, "night+", minimums: .increased)
        // The user's blue replaces Ember, darkened just enough for text on cream.
        #expect(abs(active.light.accent.oklch.hue - hex("#007AFF").oklch.hue) < 8)
        expectLegible(active.light, "paper blue")
        expectLegible(active.dark, "night blue")
        // Multicolor (nil) keeps Ember; themes keep their own accent.
        #expect(ActivePalettes(light: .paper, dark: .night).light.accent == Palette.paper.accent)
        let solarized = ThemeLibrary.builtIn.first { $0.id == "base16.solarized-light" }!.palette
        #expect(ActivePalettes(light: solarized, dark: .night, systemAccent: hex("#FF0000")).light.accent == solarized.accent)
    }

    @Test func appearanceSelectsPalette() {
        let active = ActivePalettes.default
        #expect(active.palette(for: NSAppearance(named: .darkAqua)!) == active.dark)
        #expect(active.palette(for: NSAppearance(named: .aqua)!) == active.light)
        #expect(active.palette(for: NSAppearance(named: .accessibilityHighContrastDarkAqua)!) == active.darkIncreased)
    }

    // MARK: Base16 / Base24

    @Test func base16CurrentFormat() throws {
        let scheme = try Base16Scheme(yaml: """
            system: "base16"
            name: "Gruvbox light, medium"
            author: "Dawid Kurek (dawikur@gmail.com), morhetz (https://github.com/morhetz/gruvbox)"
            variant: "light"
            palette:
              base00: "#fbf1c7" # ----
              base01: "#ebdbb2" # ---
              base02: "#d5c4a1" # --
              base03: "#bdae93" # -
              base04: "#665c54" # +
              base05: "#504945" # ++
              base06: "#3c3836" # +++
              base07: "#282828" # ++++
              base08: "#9d0006" # red
              base09: "#af3a03" # orange
              base0A: "#b57614" # yellow
              base0B: "#79740e" # green
              base0C: "#427b58" # aqua/cyan
              base0D: "#076678" # blue
              base0E: "#8f3f71" # purple
              base0F: "#d65d0e" # brown
            """)
        #expect(scheme.name == "Gruvbox light, medium")
        #expect(scheme.system == "base16")
        #expect(!scheme.isDark)
        #expect(scheme[0x0D] == hex("#076678"))
        let palette = Palette(base16: scheme)
        #expect(palette.background == hex("#fbf1c7"))
        #expect(palette.text == hex("#504945"))
        #expect(palette.accent == hex("#076678"))  // base0D: legible already, kept
        #expect(palette.marks[.red] == hex("#9d0006"))
        expectLegible(palette, "gruvbox")
    }

    @Test func base16LegacyFormatAndBase24() throws {
        let legacy = try Base16Scheme(yaml: """
            scheme: "Legacy Dark"
            author: "Someone"
            base00: "181818"
            base01: "282828"
            base02: "383838"
            base03: "585858"
            base04: "b8b8b8"
            base05: "d8d8d8"
            base06: "e8e8e8"
            base07: "f8f8f8"
            base08: "ab4642"
            base09: "dc9656"
            base0A: "f7ca88"
            base0B: "a1b56c"
            base0C: "86c1b9"
            base0D: "7cafc2"
            base0E: "ba8baf"
            base0F: "a16946"
            """)
        #expect(legacy.name == "Legacy Dark")
        #expect(legacy.isDark)  // no variant: from base00
        expectLegible(Palette(base16: legacy), "legacy")

        var base24 = """
            system: "base24"
            name: "One Dark"
            author: "FredHappyface"
            variant: "dark"
            palette:
            """
        let values = ["282c34", "3f4451", "4f5666", "545862", "9196a1", "abb2bf", "e6e6e6", "ffffff",
                      "e05561", "d18f52", "e6b965", "8cc265", "42b3c2", "4aa5f0", "c162de", "bf4034",
                      "21252b", "181a1f", "ff616e", "f0a45d", "a5e075", "4cd1e0", "4dc4ff", "de73ff"]
        for (index, value) in values.enumerated() { base24 += "\n  " + String(format: "base%02X", index) + ": \"#\(value)\"" }
        let scheme = try Base16Scheme(yaml: base24)
        #expect(scheme.system == "base24")
        let palette = Palette(base16: scheme)
        #expect(palette.sunken == hex("#21252b"))  // base10, darker background
        // base17, bright magenta (lightness may be raised for the lighter base01 surface).
        #expect(abs(palette.marks[.pink]!.oklch.hue - hex("#de73ff").oklch.hue) < 8)
        expectLegible(palette, "one dark")
    }

    @Test func base16MissingSlotFails() {
        #expect(throws: Base16Scheme.ParseError.missingSlot("base0F")) {
            try Base16Scheme(yaml: (0..<15).map { String(format: "base%02X: \"#000000\"", $0) }.joined(separator: "\n"))
        }
    }

    @Test func bundledThemesParseAndAreLegible() {
        let ids = ThemeLibrary.builtIn.map(\.id)
        #expect(ids.count == 2 + BundledSchemes.all.count)
        #expect(Set(ids).count == ids.count)
        #expect(ThemeLibrary.builtIn.contains { !$0.isDark && $0.source == .bundled })
        #expect(ThemeLibrary.builtIn.contains { $0.isDark && $0.source == .bundled })
        for theme in ThemeLibrary.builtIn { expectLegible(theme.palette, theme.name) }
    }

    // MARK: VS Code

    static let vsCodeJSON = """
        // A VS Code theme, with the comments and trailing commas VS Code allows.
        {
          "name": "Test Dark+",
          "type": "dark",
          "colors": {
            "editor.background": "#1E1E1E",
            "editor.foreground": "#D4D4D4",
            "textLink.foreground": "#3794FF",
            "editor.selectionBackground": "#264F78",
            "editor.findMatchHighlightBackground": "#EA5C0055", /* translucent */
            "sideBar.background": "#181818",
            "terminal.ansiRed": "#CD3131",
            "terminal.ansiGreen": "#0DBC79",
            "terminal.ansiYellow": "#E5E510",
            "terminal.ansiBlue": "#2472C8",
            "terminal.ansiMagenta": "#BC3FBC",
            "terminal.ansiCyan": "#11A8CD",
            "url.test": "https://example.com/#a//b",
          },
          "tokenColors": [
            { "scope": "comment", "settings": { "foreground": "#6A9955" } },
            { "scope": "string, string.quoted", "settings": { "foreground": "#CE9178" } },
            { "scope": ["constant.numeric"], "settings": { "foreground": "#B5CEA8" } },
          ],
        }
        """

    @Test func vsCodeThemeMapsWorkbenchColours() throws {
        let theme = try VSCodeTheme(json: Data(Self.vsCodeJSON.utf8))
        #expect(theme.name == "Test Dark+")
        #expect(theme.isDark)
        #expect(theme.tokenForegrounds["string.quoted"] == hex("#CE9178"))
        let palette = Palette(vsCode: theme)
        #expect(palette.background == hex("#1E1E1E"))
        #expect(palette.text == hex("#D4D4D4"))
        #expect(palette.sunken == hex("#181818"))
        #expect(palette.link == hex("#3794FF"))
        #expect(palette.marks[.green] == hex("#0DBC79"))
        // The translucent find colour is composited over the background, not used raw.
        #expect(palette.highlight != hex("#EA5C00"))
        expectLegible(palette, "vscode")
    }

    @Test func vsCodeRejectsOtherJSON() {
        #expect(throws: VSCodeTheme.ParseError.notATheme) { try VSCodeTheme(json: Data(#"{"hello": 1}"#.utf8)) }
        #expect(throws: VSCodeTheme.ParseError.notATheme) { try VSCodeTheme(json: Data("not json".utf8)) }
    }

    // MARK: Store

    @MainActor @Test func importSelectsAndPersists() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("themes-\(UUID().uuidString)")
        let suite = "theme-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer {
            defaults.removePersistentDomain(forName: suite)
            try? FileManager.default.removeItem(at: directory)
        }
        let runtime = ThemeRuntime()
        let store = ThemeStore(defaults: defaults, importDirectory: directory, runtime: runtime)
        #expect(store.lightTheme.id == Theme.paper.id && store.darkTheme.id == Theme.night.id)

        let file = FileManager.default.temporaryDirectory.appendingPathComponent("test-dark-color-theme.json")
        try Data(Self.vsCodeJSON.utf8).write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        let theme = try store.importTheme(from: file)
        #expect(theme.isDark)
        #expect(store.darkThemeID == theme.id)
        #expect(store.darkTheme.name == "Test Dark+")
        #expect(defaults.string(forKey: ThemeStore.darkThemeKey) == theme.id)
        #expect(runtime.active.dark.background == hex("#1E1E1E"))
        #expect(FileManager.default.fileExists(atPath: directory.appendingPathComponent(file.lastPathComponent).path))

        // A second store reads the choice back.
        let again = ThemeStore(defaults: defaults, importDirectory: directory, runtime: ThemeRuntime())
        #expect(again.darkTheme.name == "Test Dark+")

        store.remove(store.darkTheme)
        #expect(store.darkTheme.id == Theme.night.id)
        #expect(throws: ThemeLibrary.ImportError.self) {
            let bad = directory.appendingPathComponent("bad.yaml")
            try Data("name: nope".utf8).write(to: bad)
            try store.importTheme(from: bad)
        }
    }
}
