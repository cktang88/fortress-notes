# Fortress Notes for macOS: native UI design and technical spec

Research date: 18 November 2026. This spec covers the native SwiftUI app in `mac/`, the only Fortress Notes client (an earlier web UI was removed); the backend it talks to is described in `../spec.md` and `../docs/block-model.md`. Sections (a) and (b) describe the app as built; ideas from the original research that were dropped (pinned notes, saved searches, tags, a words/meaning search toggle, AI review, a menu-bar extra) are no longer listed. Where a number comes from a source, the source is cited inline. Where it is my decision, it says so. Third-party "design teardowns" (blakecrosley.com and similar) are reconstructions, not vendor specs, and are marked that way.

> **Correction to the brief:** macOS 26 Tahoe is no longer the newest macOS. **macOS 27 "Golden Gate" shipped on 14 September 2026** ([MacRumors](https://www.macrumors.com/2026/09/14/apple-releases-macos-golden-gate/), [Eclectic Light](https://eclecticlight.co/2026/09/14/apple-has-released-macos-golden-gate-and-security-updates-to-tahoe-26-7-sequoia-15-8/)). It keeps Liquid Glass and refines it. Apple tuned the glass to diffuse busy backgrounds more and added toolbar visibility priority, overflow, and pinned-trailing placement ([WWDC26 "What's new in SwiftUI"](https://developer.apple.com/videos/play/wwdc2026/269), [Swift with Majid](https://swiftwithmajid.com/2026/06/08/what-is-new-in-swiftui-after-wwdc26/)). **Decision:** keep the deployment target at **macOS 26** (as `mac/Package.swift` has it) and build with Xcode 26 on the `macos-26` runner. Every API below exists on macOS 26.

---

## 0. Design principles (the short version)

1. **The note is the hero.** Chrome recedes, as in Linear's 2026 refresh: "parts central to the user's task should stay in focus, ones that support orientation and navigation should recede." Linear dimmed its sidebar "a few notches" and softened its separators ([Linear, Mar 2026](https://linear.app/now/behind-the-latest-design-refresh)).
2. **Standard components first, glass only on the navigation layer.** Apple says "Don't use Liquid Glass in the content layer" and "use Liquid Glass effects sparingly" ([HIG: Materials](https://developer.apple.com/design/human-interface-guidelines/materials)). Standard bars, sidebars, sheets and popovers adopt glass on their own. Custom backgrounds on them should be removed ([Adopting Liquid Glass](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass)).
3. **One accent, used for state only.** No color for decoration. Status (rough or polished) is shown by glyph shape, not by hue.
4. **Keyboard-complete.** Every action is in the menu bar (frequent ones with a shortcut), in the palette (Open Quickly…, ⇧⌘O), and in context menus. Raycast's rule: "every action has a keyboard shortcut, and every shortcut has visual confirmation" ([Raycast teardown, third-party](https://blakecrosley.com/guides/design/raycast)).
5. **Instant and local.** No spinners for local work. Bear's approach: "no spinners, skeleton screens, or toasts… sync runs in the background" ([Bear teardown, third-party](https://blakecrosley.com/guides/design/bear)). Progress UI appears only for network or LLM work, and only after 300 ms.
6. **Calm motion.** Critically damped springs, no bounce, everything under 300 ms. Reduce Motion is respected everywhere.
7. **Least surprise: the Mac's keys and menus are never overridden.** ⌘C/⌘V/⌘Z, ⌘F/⌘G/⌘E, ⌘B/⌘I/⌘U, ⌘K (Add Link), ⌘T, ⌥⌘T, ⌥⌘C/⌥⌘V, ⌃⌘Space and the rest of the HIG's standard table keep their system meaning. App shortcuts live in one table (`Shell/Shortcuts.swift`) that `UXAuditTests` checks against the system's; the full map is in b.12.

---

## 1. What the admired apps actually do (extracted, copyable decisions)

| App | Layout | Typography / measure | Color | Keyboard / capture | What we copy |
|---|---|---|---|---|---|
| **Apple Notes (macOS 26/27)** | 3 columns: folders sidebar, list (or gallery), editor. Translucent glass sidebar and floating toolbar ([macOS Tahoe Notes guide, third-party](https://macos-tahoe.com/blog/macos-tahoe-notes-app-complete-guide-troubleshooting-tips-2025/), [MacStories Tahoe review](https://www.macstories.net/stories/macos-26-tahoe-the-macstories-review/2/)) | System font. Title / Heading / Subheading / Body / Monospaced paragraph styles. Checklists in a single text view | System accent plus yellow find highlights | ⌘K inserts a link; Quick Note via hot corner | The column structure, a single NSTextView-style editor, and system find highlights. Notes is our baseline for "native". |
| **Bear 2** | Sidebar (tags) / note list / editor. Focus mode hides panes | Custom **Bear Sans** (customized from Clarika, larger x-height, true italics) ([Bear blog](https://blog.bear.app/2023/08/learn-about-our-new-custom-font-bear-sans/)). Body around **16pt / 1.6 line height**, paragraph spacing 12, max line width around **680pt (~75 chars)**, with narrow, medium and wide presets (third-party reconstruction: [blakecrosley](https://blakecrosley.com/guides/design/bear)) | Default theme is near-white with a single red accent for cursor and tags ([same](https://blakecrosley.com/guides/design/bear)) | Markdown syntax hides once styled. `[[` wiki links | Body size, line height, measure presets, live Markdown that hides its syntax, and one accent applied to the cursor and links. |
| **Things 3** (3.22 updated for OS 26, [lavx](https://news.lavx.hu/article/things-3-22-overhaul-aligns-with-apple-os-26-aesthetic-refinement-meets-ipad-productivity-boost)) | Sidebar of lists; a single content pane | Large bold list title (~34pt claimed, [third-party](https://www.oppadu.com/tools/design-systems-site/brand/things.html)), generous white space ([MacStories](https://www.macstories.net/reviews/things-3-beauty-and-delight-in-a-task-manager/)) | About 95% neutral. Color carries meaning only (yellow = Today, red = deadline) | **Quick Entry** global panel (⌃Space), about 500pt wide, auto-focused, ↩ saves to Inbox, esc dismisses ([third-party](https://blakecrosley.com/guides/design/things)). ⌘1–6 jump to lists | Quick Capture panel behavior, a big quiet title, color only for meaning, ⌘1…9 sidebar jumps. |
| **Craft** | Sidebar, then a page with a centered column | Body 16pt, H1 28 semibold, H2 22, H3 18 medium, page column max **720pt**, margins 40pt (third-party: [blakecrosley](https://blakecrosley.com/guides/design/craft)) | Per-page accent and paper backgrounds | `/` block menu. Cards. Native Swift rendering, Mac App of the Year 2021 ([MacStories](https://www.macstories.net/reviews/craft-review-a-powerful-native-notes-and-collaboration-app/)) | Heading scale and block-level (not document-level) structure. We skip cards and colored pages, which are too decorative for us. |
| **Linear (desktop)** | Dimmed sidebar, dense list, detail pane | Compact tabs, smaller icon and text sizing | Warmer, less saturated grays. LCH-generated themes ([Linear 2024](https://linear.app/blog/how-we-redesigned-the-linear-ui), [2026](https://linear.app/now/behind-the-latest-design-refresh)) | ⌘K palette for everything. Single-key shortcuts | Recessive sidebar, fewer and softer dividers, ⌘K that exposes every command with its shortcut. |
| **Raycast** | One floating panel: search field, sectioned list, action footer | Rows about 13pt. Shortcut badges in caption/tertiary color. Row padding 12h / 8v (third-party: [blakecrosley](https://blakecrosley.com/guides/design/raycast)) | Window background plus system accent | Arrow keys move, ↩ runs the primary action, ⌘K opens the secondary **Actions** panel, ⌘1–9 pick results. Target under 50ms | Command palette anatomy, the secondary action panel, and the empty-results copy ("No results for '…'" plus a suggestion). |
| **iA Writer** | A single editor | Measure is selectable at **64 / 72 / 80 characters** ([iA settings](https://ia.net/writer/support/basics/settings/settings-mac)). Type size tuned for perceived size at reading distance | Monochrome with blue cursor | Focus mode, syntax highlight. ADA 2025 Inclusivity finalist ([MacStories](https://www.macstories.net/news/2025-apple-design-awards-winners-and-finalists-announced/)) | A measure expressed in **characters**, not pixels, and a focus mode. |
| **Ulysses** | Library / sheets / editor | Typewriter mode that keeps the current line fixed and can dim others ([help](https://help.ulysses.app/en_US/introduction/details-and-tips)) | Neutral | Markup-based | Optional typewriter scrolling (settings). |
| **Tot** | 7 colored dots, one sheet each | Plain text | Seven carefully tuned light and dark colors ([MacStories](https://www.macstories.net/reviews/tot-review-collect-and-edit-bits-of-text/)) | Menu-bar popover | Proof that a capture surface can be tiny and still feel premium. Our Quick Capture is the same spirit. |
| **Reeder (2024)** | Unified timeline, no unread counts ([9to5Mac](https://9to5mac.com/2024/09/05/reeder-launches-new-app-that-unites-rss-with-video-audio-and-social-feeds)) | Large readable cards | Calm, no badges | – | **No counts and no badges** in our sidebar by default. Counts appear on hover or in the folder inspector only. |
| **Arc / Dia sidebar** | A single vertical sidebar that holds everything ([Arc analysis, third-party](https://blakecrosley.com/guides/design/arc)) | – | Theme tint behind the sidebar | ⌘T command bar | Little: we do **not** copy the custom tinted chrome, which fights Liquid Glass, and we keep no pinned items. |
| **Obsidian (contrast)** | Panes, tabs, graph | Theme-dependent. Web-rendered (Electron) | Theme-dependent | Command palette | Mostly what to avoid: tabs-inside-panes, graph-as-hero, and a non-native text system (IME, services, and spelling feel off). |
| **Day One** | Sidebar of journals, timeline list, editor | Serif option for entries, large date headers | One journal color | Quick entry via menu bar | A date-led list for "Recent". |

Common traits of the "calm, premium" apps above:
- System or near-system type at 15–16pt with a line height of about 1.5–1.6.
- A capped measure of 64–80 characters.
- One accent.
- No dividers where spacing alone can do the job.
- Every command reachable from the keyboard, with shortcuts shown inline.
- Instant local operations.
- A global quick-capture panel.

---

## 2. Liquid Glass and the macOS 26/27 platform rules we design against

Facts (sources inline):
- **Two layers.** Liquid Glass "forms a distinct functional layer for controls and navigation elements… that floats above the content layer". The content layer uses **standard materials**, not glass ([HIG Materials](https://developer.apple.com/design/human-interface-guidelines/materials)).
- **Sidebars and inspectors.** In `NavigationSplitView` the sidebar floats on glass. The inspector gets a blurred background but does not float ([WWDC25-323 notes](https://wwdcnotes.com/documentation/wwdc25-323-build-a-swiftui-app-with-the-new-design/)). `backgroundExtensionEffect()` mirrors adjacent content under the sidebar ([HIG Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars)). For us the editor is plain text, so we **do not** use background extension. The sidebar sits over the window background.
- **Toolbars.** Items are grouped on shared glass capsules. Use `ToolbarSpacer(.fixed)` to split groups and `.sharedBackgroundVisibility(.hidden)` to drop the capsule. "Don't mix text and icons across items that share a background." Hide the whole `ToolbarItem`, not the view inside it ([Adopting Liquid Glass](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass)).
- **Scroll edge effect.** Remove custom bar backgrounds and let the system blur content under the toolbar. Use `.scrollEdgeEffectStyle(.hard, for: .top)` for a crisper edge ([WWDC25-323](https://wwdcnotes.com/documentation/wwdc25-323-build-a-swiftui-app-with-the-new-design/)).
- **Search.** `.searchable` on a `NavigationSplitView` places the field **top-trailing in the toolbar on Mac** ([same](https://wwdcnotes.com/documentation/wwdc25-323-build-a-swiftui-app-with-the-new-design/)).
- **Controls.** Bordered buttons become capsules. On macOS, mini, small and medium sizes stay rounded rectangles, and controls are slightly taller. There is a new `.extraLarge` size. Use `.buttonStyle(.glass)` and `.glassProminent` ([same](https://wwdcnotes.com/documentation/wwdc25-323-build-a-swiftui-app-with-the-new-design/)).
- **Custom glass.** Use `.glassEffect(.regular | .clear, in: shape)`, `.tint()`, `.interactive()`, `GlassEffectContainer` (do not nest containers), and `glassEffectID(_:in:)` for morphing ([WWDC25-323](https://wwdcnotes.com/documentation/wwdc25-323-build-a-swiftui-app-with-the-new-design/), [Landmarks sample](https://developer.apple.com/tutorials/data/documentation/swiftui/landmarks-building-an-app-with-liquid-glass.md)). Use `.regular` for anything with text. `.clear` is only for media backgrounds ([HIG Materials](https://developer.apple.com/design/human-interface-guidelines/materials)).
- **Concentric corners.** Shapes should share their container's corner center. Use `.rect(corner: .containerConcentric)` / `ConcentricRectangle` ([WWDC25-323](https://wwdcnotes.com/documentation/wwdc25-323-build-a-swiftui-app-with-the-new-design/)).
- **Sidebar icons follow the user's accent color.** Fixed colors are allowed only "sparingly" with clear meaning (updated 8 June 2026). Sidebar row height comes from the user's Sidebar icon size setting. Put nothing critical at the bottom of the sidebar. Show at most two levels of hierarchy ([HIG Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars)).
- **macOS text styles** (HIG Typography, size/line height in pt): Large Title 26/32, Title1 22/26, Title2 17/22, Title3 15/20, Headline 13/16 bold, **Body 13/16**, Callout 12/15, Subheadline 11/14, Footnote 10/13, Caption1 10/13, Caption2 10/13 medium ([HIG Typography](https://developer.apple.com/design/human-interface-guidelines/typography)).
- **Accessibility settings** (Reduce Transparency, Increase Contrast, the user's Liquid Glass "look" preference) change glass automatically. Custom glass must be tested under each of them ([Adopting Liquid Glass](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass)).
- **Known bug:** SwiftUI buttons placed *behind* the toolbar area were not clickable in macOS 26 betas and still were not in 26.0.1 ([Apple forums](https://developer.apple.com/forums/thread/788928)). Do not put interactive content under the toolbar region.

---

## (a) Visual design system

### a.1 Window and columns

`NavigationSplitView(columnVisibility:) { Sidebar } content: { NoteList } detail: { Editor }` plus `.inspector(isPresented:)` on the detail column.

| Region | min | ideal | max | Notes |
|---|---|---|---|---|
| Window | 900 × 560 | 1280 × 820 (first launch, centered) | – | `.defaultSize(width: 1280, height: 820)`, `.windowResizability(.contentMinSize)` |
| Sidebar | 180 | **220** | 300 | `.navigationSplitViewColumnWidth(min:180, ideal:220, max:300)`, `List(selection:)` with `.listStyle(.sidebar)` |
| Note list (content) | 260 | **300** | 420 | `.navigationSplitViewColumnWidth(min:260, ideal:300, max:420)` |
| Editor (detail) | 480 | flexible | – | The text column is centered and capped by the measure (a.3) |
| Inspector | 240 | **280** | 360 | `.inspectorColumnWidth(min:240, ideal:280, max:360)`, hidden by default, ⌥⌘I |

Column visibility modes: `.all` (default); `.doubleColumn` (list + editor), toggled with ⌃⌘S (View ▸ Show/Hide Sidebar) or the sidebar button; `.detailOnly` = **Focus mode** (⌃⌘↩).

### a.2 Typography scale

All type is **SF Pro** (system). Monospace is **SF Mono**. Use semantic `Font.TextStyle` wherever the size equals a HIG style, so Dynamic Type and accessibility work. Editor sizes are custom (larger than the 13pt UI body, as Bear, Craft and iA Writer all do). The values below are for a 16pt base. (A text-size setting, S/M/L/XL = 14/16/18/20, was planned and is not built.)

**UI chrome:**

| Role | Font | Size / line height | Weight | Color |
|---|---|---|---|---|
| Sidebar row | `.body` | 13/16 (system sidebar may enlarge per user setting) | Regular | `.primary`. Glyph uses accent |
| Sidebar section header | `.subheadline` | 11/14 | Semibold | `.secondary` |
| List row title | `.headline` | 13/16 | Semibold (bold per HIG Headline. I use `.headline.weight(.semibold)`) | `.primary` |
| List row snippet | `.callout` | 12/15, 2 lines max | Regular | `.secondary` |
| List row meta (date · folder) | `.subheadline` | 11/14, `.monospacedDigit()` | Regular | `.tertiary` |
| Toolbar title (note title in window title) | system | – | – | system |
| Inspector section header | `.subheadline` | 11/14 | Semibold, sentence case (no uppercase) | `.secondary` |
| Inspector body | `.callout` | 12/15 | Regular | `.primary` / `.secondary` |
| Palette / capture input | system 20 | 20/24 | Regular | `.primary` |
| Palette rows | `.body` | 13/16 (row height 36) | Regular, match highlight Semibold | `.primary` |
| Shortcut hints | `.caption` monospaced digits | 10/13 | Medium | `.tertiary` |

**Editor (base M = 16):**

| Block | Size / line height | Weight | Space before / after | Color |
|---|---|---|---|---|
| Document title (first line, its own field) | **28 / 34** | Bold | 0 / 16 | `.primary` |
| Heading 1 | **22 / 28** | Semibold | 24 / 8 | `.primary` |
| Heading 2 | **18 / 24** | Semibold | 20 / 6 | `.primary` |
| Heading 3 | **16 / 22** | Semibold | 16 / 4 | `.secondary` |
| Body paragraph | **16 / 26** (×1.625) | Regular | 0 / **10** | `.primary` (`NSColor.textColor`) |
| List item (bullet, number, check) | 16 / 26 | Regular | 0 / 4 (tighter within a list) | `.primary`. Checked items `.tertiary` + strikethrough `.tertiary` |
| Quote | 16 / 26 | Regular | 0 / 10 | `.secondary`, 3pt leading bar `separatorColor`, 14pt inset |
| Code block | SF Mono **14 / 21** | Regular | 8 / 12 | `.primary` on `quaternarySystemFill` |
| Inline code | SF Mono 0.875em | Regular | – | `.primary` on `quaternarySystemFill`, 4pt radius |
| Link / `[[Note]]` | inherit | Medium | – | **Accent**. Unresolved `[[…]]` uses `.secondary` + dotted underline |
| Block reference `((…))` | inherit | Regular | – | `.primary` with 2pt accent-tinted underline (accent at 35%) |
| Captions under images | 12 / 16 | Regular | 4 / 12 | `.secondary` |

The body numbers follow the consensus of Bear (16/1.6) and Craft (16, +8 line spacing). Heading sizes follow Craft's 28/22/18 (third-party, cited above). Implement line height via `NSParagraphStyle.minimumLineHeight = maximumLineHeight = 26` with `baselineOffset` centering, not `lineSpacing`, so that the caret and selection heights stay even.

### a.3 Editor measure

- The text column width is **`min(available − 2×48, measure)`**, where measure = **68 characters** × the average advance of SF Pro at the current size. At 16pt this is about **640pt**. Do not hard-code it. Compute it as `68 × ("abcdefghijklmnopqrstuvwxyz " average advance)` from `NSFont`.
- A line-width setting (Narrow 60 / **Comfortable 68** / Wide 80 / Full) was planned and is not built. The options follow iA's 64/72/80 options ([iA](https://ia.net/writer/support/basics/settings/settings-mac)) and the general 45–90 guidance ([Practical Typography](https://practicaltypography.com/line-length.html)).
- Images, code blocks and tables may break out to **min(available, measure + 160)**, like Craft's "wide" content.
- Minimum side gutter: 48pt. Top padding above the title: 40pt below the toolbar's scroll-edge area. Bottom padding: 50% of viewport height, so that the last line can be scrolled to eye level (the iA/Ulysses feel).

### a.4 Color

Content colours come from **one semantic palette** (`App/Theme/Palette.swift`); chrome (sidebar, toolbar, sheets, popovers, glass) stays native and takes only the palette's accent through `.tint`. Never hex grays in views: use the palette roles.

- **Roles:** `background` (editor, Ask), `sunken` (note list, search results, inspector, and the window behind the sidebar glass via `containerBackground(_:for: .window)`, so the material picks up the tone instead of being replaced), `surface` (code blocks, inline code, cards, table headers), `border`, `text`, `secondaryText`, `tertiaryText`, `accent`, `link`, `selection`, `highlight` (find matches and the yellow highlighter), `codeText`, `quoteBar`, and ten named mark hues (red … gray) for user-chosen text colours and highlighters.
- **Access:** AppKit draws with stable dynamic colours (`NSColor.themeText`, …) that resolve per appearance at draw time, so cached text attributes survive a theme change; SwiftUI uses `ThemeStyle` (`.foregroundStyle(.themeSecondary)`, `.background(.themeBackground)`), which resolves from the environment (`.fortressTheme()` at each window root, `.themedContent()` on content columns).
- **Defaults** (designed in OKLCH; see `Palette.paper` / `Palette.night`):
  - *Paper* (light): warm cream page `#FBF8F1`, list `#F6F1E7`, surface `#F0EADF`, border `#DCD5C9`, text warm near-black `#221D18` (15.8:1), secondary `#5B544C` (7.0:1), tertiary `#6F6962` (5.1:1).
  - *Night* (dark, Linear-style blue-grey rather than black): page `#13151C`, list `#0D0F14`, surface `#1C1E26`, border `#2D3039`, text `#E8E9EE` (15:1), secondary `#B1B4BC` (8.8:1), tertiary `#888C96` (5.4:1).
- **Accent:** "Fortress Ember", light `#AA4C23`, dark `#F09159`, used when the system accent is Multicolor; otherwise the user's accent colour from System Settings (made legible on the page). Themes bring their own accent (Base16 `base0D`). The accent is used **only** for: the insertion point, links and `[[ ]]`, checked checkboxes, the selected-row glyph in the sidebar, the prominent button in sheets, the "Ask" send button, and the citation chips. It is never used as a fill behind text blocks.
- **Contrast rules** (`Palette.legible`, checked in `ThemeTests`): text ≥ 7:1 on the page; every other text colour (secondary, tertiary, accent, link, code, marks) ≥ 4.5:1 on page, list and surface; text ≥ 4.5:1 on selection, highlights and citation chips. Under Increase Contrast: 10:1 / 7:1 and borders ≥ 3:1. A failing colour keeps its OKLCH hue and chroma and only moves in lightness.
- **User colours** (OneNote text and cell colours): named colours map onto the theme's hues; hex colours keep their hue and are moved in lightness until they read at 4.5:1 on the page (`legibleText`); fills become a tint the text stays legible on (`legibleFill`, as is on light pages when already legible).
- **Themes:** Settings ▸ Appearance picks System/Light/Dark, a light theme and a dark theme. Themes are tinted-theming Base16/Base24 YAML schemes (nine MIT-licensed ones bundled, licences shown in Settings) or VS Code colour theme JSON, imported as files into `~/Library/Application Support/Fortress Notes/Themes`. There is no Fortress theme format.
- **Separators:** used rarely. No separators between list rows. The list uses `.listRowSeparator(.hidden)` and 8pt row spacing, as in Linear's "structure should be felt, not seen".
- **Status:**
  - *Rough* uses the glyph `circle.dashed` in `.tertiary`.
  - *Polished* uses `checkmark.seal` in `.secondary`.
  - No colors.
- **Destructive:** `.red` only in confirmation buttons (`role: .destructive`).
- **Dark mode** follows the system appearance (or the Appearance setting) through the palette pair. Verify contrast with Increase Contrast on.

### a.5 Spacing scale (4pt base)

`2 · 4 · 8 · 12 · 16 · 24 · 32 · 48`. Named: `hairline 2`, `xxs 4`, `xs 8`, `s 12`, `m 16`, `l 24`, `xl 32`, `xxl 48`.

- List row: 10 vertical / 12 horizontal padding inside the system selection inset. Title-to-snippet 2, snippet-to-meta 4.
- Inspector: 16 outer padding, 12 between cards, 8 inside a card.
- Panels: 16 outer padding, 12 between input and results, row height 36 (single line) / 52 (two lines).
- Prefer system `Form` and `List` metrics over custom padding (HIG: "Prefer to use standard spacing metrics").

### a.6 Corner radii

- **Prefer concentric shapes** over numbers: inner elements in panels use `.rect(corner: .containerConcentric)`.
- Fixed values where there is no container:
  - Inline code: 4.
  - Code block, image, passage card: 8.
  - Search-result passage highlight: 3.
  - Chips/tokens: capsule.
  - Palette and capture panels: **22**. This is an approximation of Tahoe's large window radius. Use `ConcentricRectangle` for the inner list so it nests.
- Buttons: system capsule or rounded-rect by control size. Do not override.

### a.7 Materials and Liquid Glass usage (exhaustive list)

| Surface | Material |
|---|---|
| Sidebar, toolbar, sheets, popovers, menus, alerts | **System glass, automatic.** No `.background()` on them |
| Note list, editor, inspector content | Content layer: `textBackgroundColor` / system. **No glass** |
| Command palette panel | `.glassEffect(.regular, in: .rect(cornerRadius: 22))` in a `GlassEffectContainer`. It is a transient navigation surface floating over content |
| Quick Capture panel | Same as the palette (it floats over other apps' windows) |
| "Ask" composer at the bottom of the answer view | `GlassEffectContainer { TextField… Button(.glassProminent) }`, floating 16pt above the bottom edge, like Notes' floating toolbar |
| Undo toast ("Moved to Trash · Undo") | `.glassEffect(.regular.interactive())` capsule, bottom center, auto-hides after 5s (the timer waits while the pointer is on it; its Undo is also Edit ▸ Undo) |

All four custom surfaces go through `floatingGlass(in:)` (`Shell/FloatingGlass.swift`): under Reduce Transparency they become an opaque panel in the theme's surface color with a theme-border hairline, and under Increase Contrast the glass gets that border.
| Editor floating format bar | **None.** Formatting lives in keyboard shortcuts, Markdown shortcuts and the editor's context menu (*Paragraph Style*). The `[[` note picker is a system popover (glass automatically) |

Under Reduce Transparency, glass becomes opaque automatically. Our custom glass must still read correctly (test it).

### a.8 Iconography (SF Symbols, `.symbolRenderingMode(.hierarchical)` in chrome, `.monochrome` in content)

| Use | Symbol |
|---|---|
| All Notes | `note.text` |
| Rough | `circle.dashed` |
| Polished | `checkmark.seal` |
| Recent | `clock` |
| Folder | `folder` (selected: `folder.fill`) |
| New folder | `plus.circle` (sidebar footer), `folder.badge.plus` (palette) |
| Trash | `trash` |
| New note | `square.and.pencil` |
| Search | `magnifyingglass` |
| Ask your notes | `sparkle.magnifyingglass` |
| Inspector toggle | `sidebar.trailing` |
| Related notes | `point.3.connected.trianglepath.dotted` |
| Seen before | `eye` |
| Backlinks | `arrow.uturn.backward` |
| Info | `info.circle` |
| Backups | `clock.arrow.circlepath` |
| Import | `square.and.arrow.down` |
| Export | `square.and.arrow.up` |
| Sort and group | `line.3.horizontal.decrease` |
| Quick capture | `bolt` |
| More actions | `ellipsis` |

Toolbar rule: icons only, no text labels, with an `accessibilityLabel` on each ([Adopting Liquid Glass](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass)).

### a.9 Motion

- Default: `.smooth(duration: 0.25)` (no bounce). Panels: `.snappy(duration: 0.18)`.
- Reduce Motion: replace every move or scale with `.opacity` over 0.15s (`@Environment(\.accessibilityReduceMotion)`).
- Specific moves:
  - **Palette and capture appear** with scale 0.98 → 1 plus opacity 0 → 1 over 0.18s, anchored top. They disappear with opacity only over 0.12s.
  - **Note selection:** no animated transition in the editor (instant swap). The list selection highlight is the system one.
  - **New note:** the row inserts at the top with `.move(edge:.top).combined(with:.opacity)`, and the caret lands in the title.
  - **Trash:** the row collapses (height → 0) over 0.2s, then the undo toast slides up 12pt.
  - **Checkbox toggle:** symbol replace `.symbolEffect(.replace)` `circle` → `checkmark.circle.fill`, and the text fades to tertiary over 0.2s. No sound. This is Things' delight, but quieter.
  - **Ask streaming:** tokens fade in per sentence chunk (opacity 0 → 1, 0.12s). Citation chips pop with scale 0.9 → 1.
- No skeletons. Lists from SQLite render synchronously. Remote (LLM) work shows `ProgressView().controlSize(.small)` only after 300 ms.

---

## (b) Screen-by-screen

### b.1 Main window

```
┌──────────── sidebar (glass, 220) ─┬──── note list (300) ──────┬──── editor (flex) ─────────────────┬─ inspector (280, hidden) ┐
│ ● ● ●                 [sidebar⇆]  │ Kyoto trip   [≡] [✎ new]  │        [⇪][⋯]   [ⓘ]  [🔍 Search notes      ]│
│                                   │                           │                                    │                          │
│ Library                           │ Today                     │      My note title (28 bold)       │ 👁 Seen before            │
│  􀉆 All Notes                      │ ┌───────────────────────┐ │      rough · edited 2h ago         │ ┌──────────────────────┐ │
│  􀓞 Rough                          │ │Title (13 semibold)    │ │                                    │ │“…passage…”            │ │
│  􀇻 Polished                       │ │Snippet two lines…(12) │ │      Body text 16/26 capped at     │ │Note B                 │ │
│  􀐫 Recent                         │ │8:24 AM · Travel   ◌   │ │      68ch, centered …              │ └──────────────────────┘ │
│ Folders                           │ └───────────────────────┘ │                                    │ Related notes            │
│  􀈕 Inbox                          │ Yesterday                 │                                    │ Backlinks                │
│ ›􀈕 Work                           │  …                        │                                    │ Info                     │
│  􀈕 Travel                         │ Previous 7 Days           │                                    │ Text in pictures         │
│  􀈑 Trash                          │  …                        │                                    │                          │
│ ⊕ New Folder                      │                           │                                    │                          │
└───────────────────────────────────┴───────────────────────────┴────────────────────────────────────┴──────────────────────────┘
```

**Sidebar** (`List(selection:)`, `.listStyle(.sidebar)`):
- Sections, in order:
  - **Library**: All Notes, Rough, Polished, Recent (⌘1–⌘4).
  - **Folders**: nested, as an `OutlineGroup` (disclosure triangles, collapsed by default), then **Trash** as the last row (HIG: nothing critical at the bottom). *HIG decision:* the HIG suggests at most two levels in a sidebar; OneNote imports bring notebook → section → page trees, so deeper folders are allowed but stay collapsed, so the sidebar shows two levels unless the person opens more. Folders drag into folders (never into themselves or their own subfolders); notes drag onto folders and the Trash; Markdown files drop onto folders. Every drop has a menu alternative (Note ▸ Move To ▸) and is undoable.
- A quiet **New Folder** button (⌥⌘N) in the sidebar's bottom inset.
- No counts and no badges.
- Drag notes onto a folder, onto All Notes (top level) or onto Trash; drag folders into folders. Drop `.md` files onto a folder to import them there.
- Folder context menu: New Note, New Folder Inside, Rename, Import Markdown…, Delete Folder… (with confirmation).

**Note list** (`List(selection:)` with `.listStyle(.inset)`, `.listRowSeparator(.hidden)`):
- Grouped by date sections like Apple Notes (Today / Yesterday / Previous 7 Days / Previous 30 Days / month names). Section headers are 11pt semibold secondary.
- Row anatomy (top to bottom): title (1 line), snippet (2 lines, first non-title text), meta line (`time or date · folder`, plus a trailing `circle.dashed` glyph when rough).
- The column's toolbar has the **Sort and Group** menu (`line.3.horizontal.decrease`) and **New Note** (`square.and.pencil`, ⌘N). The window title shows the open note's title with its folder as subtitle.
- Context menu: Open, Open in New Window, Move To ▸, Export as Markdown…, Move to Trash. ⌘⌫ moves the selected note to the Trash (only while the list has focus) with the undo toast. Library edits (Move to Trash, Restore, Move Note, Move Folder, New Folder, Rename Folder) are undoable with ⌘Z on the window's undo stack (Edit ▸ Undo Move to Trash); the note's text has its own undo stack, so ⌘Z in the editor undoes typing and ⌘Z in the list undoes list actions.

**Editor** (detail):
- The title is a separate single-line field (28 bold). ↩ in the title moves to the body. A new note's title follows its first line until the person types one.
- A meta line under the title (12 secondary) with the Rough/Polished status menu and "edited …".
- The body is the TextKit 2 editor (section c).
- Detail toolbar: **Export as Markdown** (`square.and.arrow.up`, ⇧⌘E) and the **⋯** menu (Move To ▸ folders, Export as Markdown…, Move to Trash), then `ToolbarSpacer(.fixed)` and the **Inspector** toggle (⌥⌘I). The system search field sits top trailing.

### b.2 Search (one box, unified results)

- Placement: `.searchable(text:placement: .toolbar, prompt: "Search notes")` on the split view (top trailing). In the editor, ⌘F is in-note find.
- **One box, no modes.** The backend fuses exact words (FTS5), meaning (ColBERT) and titles into one ranked list (`GET /api/unified-search`); there is no Words/Meaning toggle, no tokens and no saved searches.
- Results replace the note-list column while the query is non-empty. Searching is debounced; results always say which query they answer, so ↩ never opens a stale result.
  1. The first row is always **Ask your notes: "…"** (`sparkle.magnifyingglass`, ⌘↩).
  2. Then one row per matching passage: note title above the passage with the matched words highlighted (`findHighlightColor`), and why it matched.
- Opening a passage opens the note at that block.
- Keyboard: ↓/↑ move through the rows from the field, ↩ opens the highlighted row, ⌘↩ asks, esc clears.
- No results: "No matches for "…"" with "Try other words, or ask your notes with ⌘↩."

### b.3 Ask your notes (streaming, cited answer)

- Entry points: the Ask row in search results, ⌘↩ in the search field, `?` in the palette, and **Ask Your Notes…** (⇧⌘A; with an empty search field it opens the palette with `? `).
- Presentation: **the detail column shows an Answer page** in place of the editor (the window title reads "Ask"). It is not a modal, so the user can click citations and come back.
- Layout (same measure as the editor):
  - **Question**: 22/28 semibold, `.primary`.
  - A trace line under it (11 tertiary): what Ask did and how long it took, e.g. "Searched for "ryokan|hotel" · Read "Kyoto trip" · 0.8 s", pulsing while it works.
  - **Answer**: 16/26 body, streamed sentence by sentence. Each sentence ends with numbered citation **chips** (capsule, accent foreground on accent at 12%). Clicking a chip opens the note at that block.
  - **Sources** section under the answer: numbered rows (number, note title, the cited passage) in the passage-card style.
  - **Follow-up composer**: a floating glass capsule at the bottom with "Ask a follow-up" and a send button (`arrow.up`). Follow-ups stack below the first answer; esc stops a running answer.
- Failure, no key configured, or nothing found: an inline `.secondary` message (with the closest passages when Ask timed out). Never an alert.

### b.4 Command palette (Open Quickly…, ⇧⌘O)

- *Why not ⌘K:* in Notes, Pages, Mail and TextEdit ⌘K is **Add Link**, and the editor keeps it. The palette takes ⇧⌘O, Xcode's Open Quickly, the Mac's own jump-anywhere key.

- A window-anchored glass overlay, **560 wide**, near the top of the window. The rest of the window is not dimmed, matching Raycast and Spotlight.
- Input: placeholder "Search notes and commands", `magnifyingglass` leading.
  - Prefix `>` limits to commands, `?` asks your notes, `/` searches inside notes.
- Result sections: **Notes** (fuzzy title match ranked locally from the sidebar tree, matched characters semibold; recent notes when empty), **Commands** (the menu bar's commands: New Note, New Folder, New Window, Quick Capture, Search Notes, Ask Your Notes, Show All Notes / Rough / Polished / Recent, Show Trash, Backups, Back Up Now, Toggle Inspector, Toggle Focus Mode, Reload Notes, Import Markdown Files…, Import from OneNote…, Export as Markdown…, Move Note to Trash, Open Note in New Window; each also matches synonyms such as "jot" or "migrate"; formatting stays in the Format menu, since it acts on the text selection).
- Row: leading symbol (`.secondary`), title (13), trailing shortcut hint where one exists.
- Keys: ↑/↓, ↩ runs the selected row, ⌘↩ asks your notes, esc closes.

### b.5 Quick Capture (⇧⌘Space by default, global, configurable)

- A global hot key registered by the app itself with Carbon `RegisterEventHotKey` (no Accessibility permission, no third-party package). The person changes it in Settings ▸ General ▸ Keyboard Shortcut (click, press keys; esc cancels, delete turns it off). Combinations macOS or the app already use (⌘Space Spotlight, ⌥⌘Space Finder search, ⌃Space / ⌃⌥Space input sources, ⌃⌘Space emoji, screenshots, the app's menu keys) are refused with the reason; ⇧⌘Space is none of these and not Raycast's or Alfred's default (⌥Space). It is re-registered after sleep; if another app owns it, Settings says so next to it, a notice says so once, the menu item (View ▸ Quick Capture) still works, and registration is retried when the app becomes active.
- A borderless, non-activating floating panel (`NSPanel`, `.nonactivatingPanel`, `canJoinAllSpaces`, `fullScreenAuxiliary`) hosting SwiftUI, so the app in front does not lose its place, as in Things' Quick Entry. **560 wide**, glass, placeholder "Capture a thought…".
- Behavior:
  - ↩ saves to the **Uncategorized** folder (`POST /api/capture`; the first line becomes the title). ⇧↩ inserts a newline. ⌘↩ saves and opens the note. esc puts the panel away.
  - Nothing typed is lost: esc (or quitting) keeps the draft until it is saved, and a capture made while the backend is unreachable waits in an on-disk outbox and is sent when it answers again.
  - After saving, the footer briefly confirms "Saved to <folder> · <title>".

### b.6 Inspector

- `.inspector(isPresented:)`, toggled with ⌥⌘I or the toolbar button. One scrolling column of sections:
  - **Seen before** (`eye`): "You've written something like this" — passage cards from other notes like the block under the caret (`GET …/related?block_id=`). It follows the caret once it settles on a block (400 ms) and is cached per block text.
  - **Related notes** (`point.3.connected.trianglepath.dotted`): notes like this one, each with a line of the matching passage. Click opens. Empty: "Notes on the same subject show up here as you write."
  - **Backlinks** (`arrow.uturn.backward`): notes linking here, each with the linking sentence. Empty: "No notes link here yet."
  - **Info** (`info.circle`): where the note came from (provenance), Created / Edited / Opened, Length, Links (out · in), and re-import conflicts with the source.
  - **Text in pictures**: the text read from each picture and PDF in the note (Apple Vision or the backend's OCR), with Copy Text and Show in Note.
- Info, related notes and backlinks load in parallel when a note opens and are cached per note revision.

### b.7 Trash

- A sidebar item. Selecting it shows trashed notes in the list, with the meta line "Deleted 3 days ago". Empty: "Deleted notes stay here until you empty the Trash."
- A trashed note opens **read-only** with a banner: "This note is in the Trash." followed by [Restore] and [Delete…] (destructive).
- List toolbar shows "Empty Trash…", confirmed with a `.confirmationDialog` ("This can't be undone.") and a destructive button.
- Context menu: Restore, Delete Permanently.
- Undo toast after any trash: a bottom-center glass capsule ""<title>" moved to Trash" with an Undo action that restores the note.

### b.8 Backups

- **Settings → Backups** tab, plus **File → Restore from Backup…** (the same view as a sheet) and **File → Back Up Now**.
- A list of snapshots (date, kind: daily / manual / pre-restore) with [Back Up Now] and Restore per snapshot. The caption explains: "Fortress backs up your notes once a day. Restoring saves your current notes as a backup first, so nothing is lost." Daily backups are a backend setting (`AUTO_BACKUP_ENABLED`); the backend keeps 7 daily, 10 manual and 5 pre-restore snapshots.
- Restore asks for confirmation ("Your current notes will be saved as a backup first.") and then reloads everything.

### b.9 Import and export

- **File → Import → Markdown Files…** (⇧⌘I) picks `.md` files, then a sheet: destination folder ("Into"), "Each file becomes a note named after the file. Front matter (title, status) is kept.", [Cancel] [Import], then the files that couldn't be imported with the reason.
- Dropping `.md` files on a sidebar folder imports them into it.
- **File → Import → From OneNote…** (also the palette's "Import from OneNote…" and Settings ▸ General). One sheet, 560 wide, three steps:
  1. The export how-to (onenote.com/notebooks → Export notebook, or Download on onedrive.com; unlock password-protected sections first), a dashed drop zone with [Choose File…] (.zip, .onepkg, .one), and "Put notebooks in" (top level or a folder).
  2. A step list (Uploading → Converting → Importing pages → Linking pages: done steps `checkmark.circle.fill` secondary, the current one a small spinner, later ones `.quaternary`), one linear progress bar (upload fraction, then pages_done/pages_total), and the current "Notebook › Section › Page" in secondary, truncated in the middle. Closing the sheet doesn't stop the import; a toast says when it's done.
  3. The report: headline + "147 pages · 36 images · 9 files · 12 tables · in 42 s", one card per notebook (`quaternarySystemFill` at 60%, radius 12) with created/updated/unchanged/skipped and images/files/tables/with-columns counts (zeros in `.tertiary`), failed sections with reason and hint, converter notes in a disclosure, unresolved links in a disclosure, [Show in Sidebar] per notebook, and the re-import promise ("Importing the same export again updates these notes; your edits are kept.").
  - If the backend reports the `fortress-onenote` converter missing, the sheet explains how to build it (`cargo build --release` in tools/fortress-onenote). The app passes `FORTRESS_ONENOTE_BIN` to a backend it starts when the binary is built in the repository (or bundled).
- **Export**: per note, File → Export as Markdown… (⇧⌘E), the toolbar button, or the note's context menu. There is no folder or whole-workspace export (every note is already mirrored as a `.md` file in the notes folder).

### b.10 Settings (`Settings` scene, `TabView`, each tab a grouped `Form`)

- **General**:
  - **Backend**: status, address (default `http://127.0.0.1:8000`), the Fortress folder (the repository or its `backend/`, found automatically when the app runs from the repository) with [Choose…], "Start the backend automatically" (on by default), the command it runs, [Start Backend] / [Stop Backend] and [Show Log] (`~/Library/Logs/Fortress Notes/backend.log`).
  - **Text in Pictures**: "Read text in pictures and PDFs on this Mac" (Apple Vision).
  - **Keyboard Shortcut**: the global Quick Capture shortcut (b.5).
  - **Importing from OneNote**: whether the `fortress-onenote` converter is built (how to build it if not) and [Import from OneNote…] (opens in the main window last in front).
- **Backups**: as in b.8.
- Settings reopens on the pane last viewed.

### b.11 Empty states (all `ContentUnavailableView`)

| Where | Symbol | Title | Description | Action |
|---|---|---|---|---|
| No notes at all (first launch) | `square.and.pencil` | "Write Your First Note" | – | [New Note] [Import Markdown…] |
| Folder empty | `folder` | "No Notes in Ideas" | – | [New Note in Ideas] |
| No selection | none (blank editor background with a centered `.tertiary` 13pt "No note selected", like Notes) | | | |
| Search no results | – | "No matches for "…"" | "Try other words, or ask your notes with ⌘↩." | |
| Trash empty | `trash` | "Trash is empty" | "Deleted notes stay here until you empty the Trash." | – |
| Inspector related empty | see b.6 | | | |
| Ask: backend unreachable | `bolt.horizontal.circle` | "Ask isn't available" | "The local server isn't responding." | [Try Again] |

### b.12 Menu bar and the full shortcut map (`ShellCommands`, `FindingCommands`, `FormatCommands`, `Shell/Shortcuts.swift`)

Menus are in the standard order: App, File, Edit, Format, View, Note, Go, Window, Help. Every command acts on the **window in front** (each window publishes its own `AppModel` with `focusedSceneValue(\.windowModel)`), and is disabled, never hidden, when it doesn't apply. The system's command sets are included: `SidebarCommands`, `TextEditingCommands` (Find, Spelling and Grammar, Substitutions, Transformations, Speech), `ToolbarCommands` (Show/Hide Toolbar ⌥⌘T, Customize Toolbar…). `TextFormattingCommands` is left out on purpose: its Font and Text items set fonts behind the editor's marks; our Format menu sends the editor's own actions instead.

| Menu | Command | Shortcut |
|---|---|---|
| App | Settings… | ⌘, |
| File | New Note | ⌘N (with no window open, opens one and makes the note there) |
| File | New Folder | ⌥⌘N |
| File | New Window | – |
| File | Import ▸ Markdown Files… / From OneNote… | ⇧⌘I / – |
| File | Export as Markdown… | ⇧⌘E |
| File | Back Up Now, Restore from Backup… | – |
| File | Close Window | ⌘W (system) |
| Edit | Undo / Redo (list actions in the list, typing in the note) | ⌘Z / ⇧⌘Z |
| Edit | Cut / Copy / Paste / Paste and Match Style / Select All | ⌘X / ⌘C / ⌘V / ⌥⇧⌘V / ⌘A (system) |
| Edit | Find ▸ Find… / Find Next / Find Previous / Use Selection for Find / Jump to Selection | ⌘F / ⌘G / ⇧⌘G / ⌘E / ⌘J (system, in the note) |
| Edit | Search All Notes (puts the caret in the window's search field) | ⌥⌘F |
| Edit | Spelling and Grammar, Substitutions, Transformations, Speech, Writing Tools, Emoji & Symbols | system (⌘; ⌘: ⌃⌘Space) |
| Format | Bold / Italic / Underline | ⌘B / ⌘I / ⌘U |
| Format | Strikethrough | ⇧⌘X |
| Format | Inline Code | ⌥⌘E (⌘E stays Use Selection for Find) |
| Format | Highlight | ⇧⌘H |
| Format | Superscript / Subscript | ⌃⌘= / ⌃⌘- |
| Format | Body, Heading 1/2/3 | ⌥⌘0, ⌥⌘1/2/3 |
| Format | Bulleted List / Numbered List / Checklist | ⇧⌘7 / ⇧⌘8 / ⇧⌘L |
| Format | Quote | ⌥⌘' |
| Format | Code Block | ⌃⌥⌘C (⌥⌘C is Copy Style) |
| Format | Mark as Checked | ⇧⌘U (as in Notes) |
| Format | Indent / Outdent | ⌘] / ⌘[ (and Tab / ⇧Tab in lists) |
| Format | Move Up / Move Down | ⌃⌘↑ / ⌃⌘↓ |
| Format | Add Link… | ⌘K (as in Notes, Pages, Mail, TextEdit) |
| Format | Insert Table | – (⌥⌘T is Show/Hide Toolbar) |
| View | Show/Hide Toolbar, Customize Toolbar… | ⌥⌘T (system) |
| View | Show/Hide Sidebar | ⌃⌘S |
| View | Show/Hide Inspector | ⌥⌘I |
| View | Enter/Exit Focus Mode | ⌃⌘↩ |
| View | Open Quickly… (palette) | ⇧⌘O |
| View | Ask Your Notes… | ⇧⌘A |
| View | Quick Capture | ⇧⌘Space (global, configurable) |
| View | Reload Notes | ⌘R |
| View | Enter Full Screen | ⌃⌘F (system) |
| Note | Move To ▸, Open in New Window | – |
| Note | Move to Trash (only while the note list has focus; in the note ⌘⌫ deletes to the line start) | ⌘⌫ |
| Go | All Notes / Rough / Polished / Recent / Trash | ⌘1 / ⌘2 / ⌘3 / ⌘4 / – |
| Window | Minimize, Zoom, Tile, Merge All Windows, Cycle Windows | ⌘M, ⌘` (system) |
| Help | Search | ⇧⌘/ (system) |

Inside surfaces: in the palette and the search field ↑/↓ move, ↩ opens, ⌘↩ asks, esc closes or clears; in Quick Capture ↩ saves, ⇧↩ adds a line, ⌘↩ saves and opens, esc puts it away; in a table cell Tab/⇧Tab move between cells. Text navigation and selection (⌥/⌘ arrows, ⇧ to extend, Emacs keys ⌃A/⌃E/⌃K/⌃Y…), Services, Look Up (⌃⌘D or force click), dictation, text replacements and smart quotes/dashes (from System Settings) are NSTextView's own and untouched.

**Pasteboard.** Copying from a note puts the note's blocks (for pasting between notes with kinds and marks), Markdown (`net.daringfireball.markdown`, for Markdown editors and code tools), RTF and plain text on the pasteboard. Pasting takes the same: blocks, rich text (RTF/HTML from Safari, Pages, Word, Notes; marks the editor knows are kept, and the source page is recorded), plain text, and images (a screenshot copied with ⌃⇧⌘4, a PNG/TIFF) or files (a dragged screenshot thumbnail, PDFs, any file), which upload and become picture or file blocks. Paste and Match Style (⌥⇧⌘V) pastes plain text.

---

## (c) Editor: recommended approach

### Decision

Use **one AppKit `NSTextView` on TextKit 2** (`NSTextLayoutManager` + `NSTextContentStorage`), wrapped in `NSViewRepresentable`, per note. Blocks are represented as **paragraphs carrying custom attributes**. The package has no third-party dependencies: Markdown import and export are the backend's job (`block_markdown.py`), and Ask answers and passages are plain SwiftUI `Text`. SwiftUI text inputs are used only for small inputs (Quick Capture, Ask composer).

### Why not the alternatives

- **SwiftUI `TextEditor` + `AttributedString` (macOS 26).** It supports bold, italic, underline, strikethrough, fonts, colors, line height, alignment, custom attributes, `AttributedTextFormattingDefinition` / `AttributedTextValueConstraint`, and `AttributedTextSelection` with `transform(updating:)` ([WWDC25-280 transcript](https://nonstrict.eu/wwdcindex/wwdc2025/280/), [docs](https://developer.apple.com/documentation/swiftui/attributedtextselection)). It falls short for this editor in several ways:
  - The session mentions **no lists, no attachments or images** (only Genmoji) and no per-paragraph custom drawing such as checkbox glyphs, quote bars or code backgrounds.
  - It has no `shouldChangeText` hook for Markdown shortcuts, and "any mutation… invalidates all of its indices".
  - It is still a first-generation API ([Create with Swift](https://www.createwithswift.com/using-rich-text-in-the-texteditor-with-swiftui/)).
  - It is great for the capture field, but not for the main editor.
- **One SwiftUI/AppKit text view per block (Notion/Craft-style).** Cross-block selection, drag-select, IME composition across blocks, Find, Services, spell check and a single undo stack all break or need reimplementation. Long notes mean thousands of views. Rejected.
- **STTextView** (2.4.1). It is an excellent TextKit 2 NSTextView replacement aimed at code editing (line numbers, plugins). It is **GPLv3 or a commercial license** ([LICENSE](https://github.com/krzyzanowskim/STTextView/blob/main/LICENSE.md), [Swift Package Registry](https://swiftpackageregistry.com/krzyzanowskim/STTextView)). It is code-oriented, and the license is a burden unless we buy the commercial license. Keep it as a fallback if our NSTextView subclass hits TextKit 2 viewport bugs it has already solved.
- **Runestone** (0.5.2) is iOS/UITextView-only. Not applicable.
- **MarkdownUI** is in maintenance mode, superseded by **Textual** ([README](https://github.com/gonzalezreal/textual)). Textual is render-only, so it is not an editor.
- **A WebView editor.** Fastest to ship, but it is exactly the "not native" feel the user is escaping (IME, Services, text system, look). Rejected.

### Architecture

```
SQLite (backend: blocks with id, parent_id, order key, type, attrs, inline content, payload)
   ⇅  POST /api/block-documents/{id}/transactions: insert / update / move / delete
      (placement by after_id / before_id / parent_id; docs/block-model.md §4)
EditorSession (@Observable) + Model/: BlockCodec (inline nodes ⇄ runs), BlockDiff
   (synced tree → operations), PendingEdits, TableModel, BlockSchema
   ⇅
BlockEditorController + FortressTextView : NSTextView (TextKit 2)  ← NSViewRepresentable
   ├─ NSTextContentStorage  (paragraph = block; custom attributes carry the block id, kind, indent, checked)
   ├─ BlockLayoutFragment (bullets, numbers, checkboxes, quote bars, code backgrounds, headings)
   ├─ Attachments: images (resizable), files, dividers, math, tables (TableBlock), columns (ColumnsBlock)
   ├─ Markdown shortcuts (after the edit, IME-safe)
   └─ LinkCompletion: `[[` note picker
```

- **Block identity.** Each paragraph carries its block id. Typing ↩ makes a new paragraph with a fresh id, while the original keeps its id. Joining two paragraphs (⌫ at the start of a block) keeps the upper id. Nesting is an indent level mapped to `parent_id` on save. These are editor gestures; on the wire they become ordinary `insert`/`update`/`move`/`delete` operations (the API has no split or merge).
- **Persistence.** Edits mark blocks dirty; after a short pause `BlockDiff` turns the difference between the last synced tree and the editor's tree into one revision-checked transaction (inserts and moves placed after their previous sibling, updates with only what changed, attrs as a patch). Never re-serialize the whole note. A 409 conflict offers "Load Latest".
- **Markdown shortcuts (live).** At line start: `#`…`######` + space produce headings; `- `/`* `/`+ ` a bullet; `1. ` / `1) ` numbered; `[] `/`[ ] `/`[x] ` a checkbox; `> ` a quote. Implementation details:
  - Shortcuts run *after* the edit and **only when `!hasMarkedText()`**. This makes them IME-safe: never transform during Japanese, Chinese or Korean composition.
  - Each transform is one undo group, so ⌘Z restores the literal characters, as Notes and Bear do.
- **Links.**
  - Note links are `docLink` inline nodes; their label is the target's current title (from `resolved_links`), in the accent colour. Typing `[[` opens the note picker. Renames show up everywhere without rewriting stored text.
  - Block references are `blockRef` nodes showing the referenced block's current text, with a subtle underline.
- **Images and files.** Paste or drop uploads through `POST /api/files`; the image is inserted as an attachment with a view provider (stored `width`/`height`, drag handle for width). The text in pictures comes from OCR (backend and Apple Vision) and is shown in the inspector, never in the note.
- **Undo.** NSTextView's `undoManager` with `allowsUndo = true`; structural operations (indent, move block ⌃⌘↑/↓, checkbox toggle, table edits) register as text-system undo, all in one stack per note (`FortressTextView.noteUndo`, shared by columns and table cells), separate from the window's stack where list actions are undone.
- **Performance.**
  - TextKit 2 lays out only the viewport.
  - Never touch `textView.layoutManager`. Accessing it silently drops the view back to TextKit 1.
  - Keep attribute runs minimal.
  - The budget is 50k words opening in under 150ms and typing latency under 8ms; `EditorPerformanceTests` measures it on CI.
- **Free system behavior.** Find bar (`usesFindBar`, `isIncrementalSearchingEnabled`), spelling and grammar, Writing Tools, Services, dictation, drag and drop, accessibility.

### Tables, columns, math (rich imported content)

- **Tables** stay one atomic paragraph in the note (one attachment character) whose view is a live grid: one small TextKit 2 text view per cell (rich marks, links, ⌘B/⌘I/⌘U/⌥⌘E/⇧⌘X, ⌃⌘= superscript, ⌃⌘− subscript). Tab/⇧Tab move between cells (Tab in the last cell adds a row), ↑/↓ at a cell's edge move to the cell above/below or out of the table, Esc leaves it, ↩ is a line break inside the cell. The context menu inserts/deletes rows and columns and toggles the header row; each is one undo step. Look: 14pt cell text, 10/7 padding, radius 6 outline, `separatorColor` grid when `attrs.borders` (borderless tables show faint dashed guides only while a cell is edited), header cells semibold on a 4.5% label tint, imported cell colours as is in light mode and at 22% in dark. Stored column widths (CSS px ≈ pt) are honoured; automatic columns share the rest; everything shrinks to the measure. Nested cell blocks (OneNote lists/pictures in cells) show read-only under the cell text and are kept untouched. A table is saved as ordinary child blocks (`table` → `table_row` → `table_cell` → paragraphs; docs/block-model.md §2): a cell edit is an `update` of that cell's paragraph, a new row or column is inserts, and `text`/Markdown are derived by the server.
- **Columns** stay inside the measure (two columns of ~300pt read well; widening past the measure would break the calm single column). Each column hosts its own block editor (the same controller as the note, embedded, no scrolling), with a 32pt gutter and a hairline. Trade-offs: no selection across columns or out of a column, Find works per column, ↑/↓ leave a column at its first/last line. Saving diffs the column blocks as ordinary children (`column` parents), so edits are inserts/updates/moves the backend accepts; columns are never created or removed from the native editor.
- **Images** use stored width/height (no jump when the picture loads), never wider than the column or taller than 480; caption 12/16 secondary below; alt text as hover help and accessibility label. **Files** are chips with icon, name and size. **Math** is set from the LaTeX by a small reader (Greek, operators, ^/_ raised/lowered, \frac, \sqrt) in New York italic on a quiet fill, source on hover; saved untouched.

### Risks and mitigations

1. **TextKit 2 viewport glitches.** These include jumpy scrolling with estimated heights and fragment-height inconsistencies. They are documented as still "unresolved" by STTextView's author in Aug 2025 ([TextKit 2 – the promised land](https://blog.krzyzanowskim.com/2025/08/14/textkit-2-the-promised-land/)). Mitigations: keep fragments simple; avoid huge attachments (cap image height at 480 and lazy-load); add a settings-hidden "TextKit 1 compatibility" fallback (a custom `NSLayoutManager` drawing for bullets and quotes) if real users hit it; and keep STTextView's commercial license (from €5 per its LICENSE/README) as plan B.
2. **Custom fragments plus hit-testing for checkboxes.** Handle `mouseDown` and map it to the fragment via `textLayoutManager.textLayoutFragment(for:)`.
3. **Block ID stability across paste and undo.** Pasted content always gets new IDs, and undo restores old IDs from the attribute snapshot. Unit-test the mapper with property tests (random edit sequences → IDs unique, order preserved).
4. **IME.** Integration tests type through `insertText(_:replacementRange:)` with `setMarkedText` sequences for kana, pinyin and Hangul, and assert that no transform fires mid-composition.

---

## (d) Build, test and screenshot on GitHub Actions

Verified 18 Nov 2026 against [actions/runner-images](https://github.com/actions/runner-images):

| Label | Arch | OS | Xcode | Status |
|---|---|---|---|---|
| **`macos-26`** (= `macos-latest`, `macos-26-xlarge`) | arm64 | macOS **26.7.2** (25G332), image **20261102.0473.1** | **26.6 (17F113) default**; also 26.5, 26.4.1, 26.3, 26.2, 26.1.1, 26.0.1 | GA ([readme](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md)) |
| `macos-26-intel` / `macos-26-large` | x64 | macOS 26 | 26.x | GA |
| `macos-15` | arm64 | macOS 15 | – | GA (not needed) |
| `macos-14` | – | – | – | **Retired 2 Nov 2026** |
| **`xcode-27`** / `xcode-27-xlarge` | arm64 | **macOS 27** base | Xcode 27.0 (beta at the issue's last edit; commenters report GA around 15 Oct 2026) | Listed as "preview" in the README ([issue #14404](https://github.com/actions/runner-images/issues/14404)) |

- **Swift version.** Xcode 26.4 and later ship **Swift 6.3**, so 26.6 is Swift 6.3.x ([Xcode 26.4 coverage](https://dev.to/arshtechpro/xcode-264-here-is-what-actually-matters-for-devs-2hke), [Xcode 26.5 notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-26_5-release-notes.md)). The image README doesn't print a Swift version, so log `swift --version` in CI. Xcode 27 points to Swift 6.4 (unverified, from a third-party commit title).
- **Preinstalled tools.** `xcbeautify 3.2.1` and Homebrew 7.0.11 are installed. **XcodeGen, Tuist and SwiftLint are not**, so `brew install xcodegen` (about 10s) if used.
- **Screen recording is pre-granted.** The image's TCC setup grants `kTCCServiceScreenCapture` to `/bin/bash` and the hosted compute agent ([configure-tccdb-macos.sh](https://github.com/actions/runner-images/blob/main/images/macos/scripts/build/configure-tccdb-macos.sh)). The `screencapture -l <windowID>` approach in `mac/Tests/FortressKitTests/Snapshot.swift` therefore works on hosted runners (the screenshots in `docs/screenshots/` come from it). Runner VMs render at **1x**, unlike Retina laptops ([dev.to](https://dev.to/d4g4/our-swiftui-snapshot-tests-passed-locally-but-failed-on-ci-heres-the-actual-fix-5fhd)).

### Project structure

- **The SwiftPM package is the whole app** (`mac/Package.swift`, swift-tools 6.2, `.macOS(.v26)`, no dependencies): the `FortressKit` library holds the models, API client and every view; the `FortressNotes` executable is only the entry point; `FortressKitTests` holds the tests. `swift run FortressNotes` starts it as a regular foreground app. It is fast in CI and needs no project files in git.
- Not done yet: an Xcode/XcodeGen app target for a shippable, signed `.app` (Info.plist, asset-catalog accent colour and icon, bundled `fortress-onenote`). `BackendLaunch` already looks for a bundled converter.

### Tests and screenshots

1. **Screenshot gallery (not diffed).** Tests put real windows in front of `screencapture` and capture the app's windows and panels (`ShellGallerySnapshotTests`, `FindingGallery`) when `SNAPSHOT_DIR` is set. This is the only way to capture **Liquid Glass, materials, `List` and NSTextView** (`ImageRenderer` cannot render views backed by native platform views). CI uploads them as the `snapshots` artifact for humans to review.
2. **Unit and behaviour tests** for the editor model and text system (`Editor*Tests`, `TableModelTests`), the data layer and caches, finding, Quick Capture reliability, picture text and the OneNote import flow.
3. **Performance**: `EditorPerformanceTests` (opening and typing budgets) and `FindingPerfTests` (`FINDING_PERF=1`) run in their own CI steps and print `PERF` lines to the job summary.
4. **Integration**: `IntegrationTests` run against a real backend seeded with a few hundred notes (`mac/Scripts/seed_backend.py`) when `FORTRESS_INTEGRATION_URL` is set.

Pixel-diff regression tests (e.g. swift-snapshot-testing) are not used.

### Workflow (`.github/workflows/mac.yml`)

Runs on pushes that touch `mac/**`, `backend/app/**` (the integration job uses the backend) or the workflow, and on demand:

- **build** (`macos-26`): `swift build`; `SNAPSHOT_DIR="$PWD/snapshots" swift test --skip EditorPerformanceTests`; then the editor performance suite alone (its `PERF` lines go to the step summary) and `FINDING_PERF=1 swift test --filter FindingPerf`; uploads `mac/snapshots`.
- **integration** (`macos-26`): makes a random `FORTRESS_TOKEN`, starts the backend with `uv` (`EMBEDDINGS_ENABLED=false VISION_ENABLED=false`, a temporary `NOTES_DIR`), checks that it refuses requests without the token, seeds 300 notes and runs `swift test --filter IntegrationTests` with `FORTRESS_INTEGRATION_URL`; `[perf]` lines go to the step summary.

Code signing is not needed: SwiftPM builds are unsigned (ad-hoc on arm64 automatically).

---

## (e) "mwufi": what it is

- **mwufi** is a real GitHub user ([github.com/mwufi](https://github.com/mwufi)): San Francisco, bio "hi, future.", site zentomorrow.com, about 237 repositories, mostly TypeScript and Python (agents, chat UIs, Next.js starters).
- **Only one Swift repository exists: [`mwufi/ios-bangers`](https://github.com/mwufi/ios-bangers)**. It is "A collection of stunning iOS UI components and effects built with SwiftUI":
  - Neon glow rings (`NeonGlowView`, angular-gradient stroke plus stacked shadows), animated **MeshGradient** backgrounds, and Metal ripple shaders (`RippleEffect.metal`, `Shaders.metal`, used via a `RippleEffectModifier`).
  - These are hosted in an iOS demo app called `chatapp` that also has a work-session/project tracker (models, view models, services, about 2,900 lines of Swift).
  - Targets iOS 16+, Xcode 15, Swift 5.9. **No macOS target.** 31 commits, 0 stars, last commit 25 Dec 2024.
  - The README says **MIT**, but the repo has **no LICENSE file**. Ask the author, or add attribution, before copying code.
- **mwufi/mac-editor ("Wordy")** is related but not native: "an open-source notes app, for writers" built with **Tauri + Next.js + Tailwind/shadcn + TipTap + SQLite**, with Zen Mode, AI writing help and dark mode. It has 13 stars and no license shown. It is essentially the same web-stack approach Fortress Notes is moving away from.
- **Can we reuse anything?** Very little.
  - `ios-bangers` is iOS-only visual-effects candy. Neon glows and mesh gradients are the opposite of the calm, content-first Liquid Glass aesthetic we want, and the HIG asks for restraint.
  - At most, its `MeshGradient` and Metal-shader patterns could inform a **subtle** first-launch or empty-state illustration. I recommend against even that.
  - `mac-editor` offers product ideas (Zen Mode = our Focus mode) but no native code.
- **Closest real native SwiftUI notes references** if the user was thinking of something else:
  - [TakeNote](https://awesome.ecosyste.ms/projects/github.com%2Fadamrdrew%2Ftakenote) (Markdown notes for Mac/iPhone/iPad/Vision Pro, iCloud).
  - [Linearama](https://awesome.ecosyste.ms/projects/github.com%2Flinearama%2Flinearama) (local-first SwiftUI notes).
  - Apple's [Landmarks: Building an app with Liquid Glass](https://developer.apple.com/tutorials/data/documentation/swiftui/landmarks-building-an-app-with-liquid-glass.md) sample, which is the best official reference for `NavigationSplitView`, inspector, glass and toolbars.
  - [conorluddy/LiquidGlassReference](https://github.com/conorluddy/LiquidGlassReference).

---

## Appendix: the app's scene (`mac/Sources/FortressNotes/FortressNotesApp.swift`)

```swift
@main
struct FortressNotesApp: App {
    @NSApplicationDelegateAdaptor(FortressAppDelegate.self) private var delegate

    var body: some Scene {
        // Each window: its own AppModel (selection, open note, sheets) over the shared
        // Library; a String value opens a note in a new window.
        WindowGroup(id: MainWindowScene.id, for: String.self) { $noteId in
            MainWindowScene(library: delegate.library, noteId: noteId)
                .findingSetup(delegate.model)  // global Quick Capture shortcut
        }
        .defaultSize(width: 1280, height: 820)
        .windowResizability(.contentMinSize)
        .commands {
            SidebarCommands()
            TextEditingCommands()  // Find ⌘F/⌘G/⌘E, Spelling, Substitutions, Speech
            ToolbarCommands()
            ShellCommands(library: delegate.library)  // File, Edit, Format, View, Note, Go
            FindingCommands()  // Open Quickly… (palette), Ask, Quick Capture
        }

        Settings {
            SettingsView()
                .environment(delegate.model)
        }
    }
}
```

The command palette is a custom glass panel on the navigation layer only:
`GlassEffectContainer { … }.glassEffect(.regular, in: .rect(cornerRadius: 22))`, 560 wide.
