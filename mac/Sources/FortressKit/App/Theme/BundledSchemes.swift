// Base16 schemes bundled with the app, verbatim from github.com/tinted-theming/schemes
// (base16/*.yaml, MIT). The original themes are MIT-licensed too; notices are in
// `ThemeLibrary.licenseNotices` and shown in Settings ▸ Appearance.

enum BundledSchemes {
    static let all: [(id: String, yaml: String)] = [
        ("solarized-light", #"""
            system: "base16"
            name: "Solarized Light"
            author: "Ethan Schoonover (modified by aramisgithub)"
            variant: "light"
            palette:
              base00: "#fdf6e3"
              base01: "#eee8d5"
              base02: "#93a1a1"
              base03: "#839496"
              base04: "#657b83"
              base05: "#586e75"
              base06: "#073642"
              base07: "#002b36"
              base08: "#dc322f"
              base09: "#cb4b16"
              base0A: "#b58900"
              base0B: "#859900"
              base0C: "#2aa198"
              base0D: "#268bd2"
              base0E: "#6c71c4"
              base0F: "#d33682"
            """#),
        ("solarized-dark", #"""
            system: "base16"
            name: "Solarized Dark"
            author: "Ethan Schoonover (modified by aramisgithub)"
            variant: "dark"
            palette:
              base00: "#002b36"
              base01: "#073642"
              base02: "#586e75"
              base03: "#657b83"
              base04: "#839496"
              base05: "#93a1a1"
              base06: "#eee8d5"
              base07: "#fdf6e3"
              base08: "#dc322f"
              base09: "#cb4b16"
              base0A: "#b58900"
              base0B: "#859900"
              base0C: "#2aa198"
              base0D: "#268bd2"
              base0E: "#6c71c4"
              base0F: "#d33682"
            """#),
        ("gruvbox-light-medium", #"""
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
            """#),
        ("gruvbox-dark-medium", #"""
            system: "base16"
            name: "Gruvbox dark, medium"
            author: "Dawid Kurek (dawikur@gmail.com), morhetz (https://github.com/morhetz/gruvbox)"
            variant: "dark"
            palette:
              base00: "#282828" # ----
              base01: "#3c3836" # ---
              base02: "#504945" # --
              base03: "#665c54" # -
              base04: "#bdae93" # +
              base05: "#d5c4a1" # ++
              base06: "#ebdbb2" # +++
              base07: "#fbf1c7" # ++++
              base08: "#fb4934" # red
              base09: "#fe8019" # orange
              base0A: "#fabd2f" # yellow
              base0B: "#b8bb26" # green
              base0C: "#8ec07c" # aqua/cyan
              base0D: "#83a598" # blue
              base0E: "#d3869b" # purple
              base0F: "#d65d0e" # brown
            """#),
        ("catppuccin-latte", #"""
            system: "base16"
            name: "Catppuccin Latte"
            author: "https://github.com/catppuccin/catppuccin"
            variant: "light"
            palette:
              base00: "#eff1f5" # base
              base01: "#e6e9ef" # mantle
              base02: "#ccd0da" # surface0
              base03: "#bcc0cc" # surface1
              base04: "#acb0be" # surface2
              base05: "#4c4f69" # text
              base06: "#dc8a78" # rosewater
              base07: "#7287fd" # lavender
              base08: "#d20f39" # red
              base09: "#fe640b" # peach
              base0A: "#df8e1d" # yellow
              base0B: "#40a02b" # green
              base0C: "#179299" # teal
              base0D: "#1e66f5" # blue
              base0E: "#8839ef" # mauve
              base0F: "#dd7878" # flamingo
            """#),
        ("catppuccin-mocha", #"""
            system: "base16"
            name: "Catppuccin Mocha"
            author: "https://github.com/catppuccin/catppuccin"
            variant: "dark"
            palette:
              base00: "#1e1e2e" # base
              base01: "#181825" # mantle
              base02: "#313244" # surface0
              base03: "#45475a" # surface1
              base04: "#585b70" # surface2
              base05: "#cdd6f4" # text
              base06: "#f5e0dc" # rosewater
              base07: "#b4befe" # lavender
              base08: "#f38ba8" # red
              base09: "#fab387" # peach
              base0A: "#f9e2af" # yellow
              base0B: "#a6e3a1" # green
              base0C: "#94e2d5" # teal
              base0D: "#89b4fa" # blue
              base0E: "#cba6f7" # mauve
              base0F: "#f2cdcd" # flamingo
            """#),
        ("rose-pine-dawn", #"""
            system: "base16"
            name: "Rosé Pine Dawn"
            author: "Emilia Dunfelt <edun@dunfelt.se>"
            slug: "rose-pine-dawn"
            variant: "light"
            palette:
              base00: "#faf4ed"
              base01: "#fffaf3"
              base02: "#f2e9de"
              base03: "#9893a5"
              base04: "#797593"
              base05: "#575279"
              base06: "#575279"
              base07: "#cecacd"
              base08: "#b4637a"
              base09: "#ea9d34"
              base0A: "#d7827e"
              base0B: "#286983"
              base0C: "#56949f"
              base0D: "#907aa9"
              base0E: "#ea9d34"
              base0F: "#cecacd"
            """#),
        ("rose-pine-moon", #"""
            system: "base16"
            name: "Rosé Pine Moon"
            author: "Emilia Dunfelt <edun@dunfelt.se>"
            slug: "rose-pine-moon"
            variant: "dark"
            palette:
              base00: "#232136"
              base01: "#2a273f"
              base02: "#393552"
              base03: "#6e6a86"
              base04: "#908caa"
              base05: "#e0def4"
              base06: "#e0def4"
              base07: "#56526e"
              base08: "#eb6f92"
              base09: "#f6c177"
              base0A: "#ea9a97"
              base0B: "#3e8fb0"
              base0C: "#9ccfd8"
              base0D: "#c4a7e7"
              base0E: "#f6c177"
              base0F: "#56526e"
            """#),
        ("nord", #"""
            system: "base16"
            name: "Nord"
            author: "arcticicestudio"
            variant: "dark"
            palette:
              base00: "#2E3440"
              base01: "#3B4252"
              base02: "#434C5E"
              base03: "#4C566A"
              base04: "#D8DEE9"
              base05: "#E5E9F0"
              base06: "#ECEFF4"
              base07: "#8FBCBB"
              base08: "#BF616A"
              base09: "#D08770"
              base0A: "#EBCB8B"
              base0B: "#A3BE8C"
              base0C: "#88C0D0"
              base0D: "#81A1C1"
              base0E: "#B48EAD"
              base0F: "#5E81AC"
            """#),
    ]
}
