# Fortress Notes for macOS: native UI design and technical spec

Research date: 18 November 2026. This spec covers the native SwiftUI rebuild of the React/Tailwind/BlockNote UI described in `spec.md` and `BLOCK_CENTRIC_REBUILD.md`. Where a number comes from a source, the source is cited inline. Where it is my decision, it says so. Third-party "design teardowns" (blakecrosley.com and similar) are reconstructions, not vendor specs, and are marked that way.

> **Correction to the brief:** macOS 26 Tahoe is no longer the newest macOS. **macOS 27 "Golden Gate" shipped on 14 September 2026** ([MacRumors](https://www.macrumors.com/2026/09/14/apple-releases-macos-golden-gate/), [Eclectic Light](https://eclecticlight.co/2026/09/14/apple-has-released-macos-golden-gate-and-security-updates-to-tahoe-26-7-sequoia-15-8/)). It keeps Liquid Glass and refines it. Apple tuned the glass to diffuse busy backgrounds more and added toolbar visibility priority, overflow, and pinned-trailing placement ([WWDC26 "What's new in SwiftUI"](https://developer.apple.com/videos/play/wwdc2026/269), [Swift with Majid](https://swiftwithmajid.com/2026/06/08/what-is-new-in-swiftui-after-wwdc26/)). **Decision:** keep the deployment target at **macOS 26** (as `mac/Package.swift` already has it), build with Xcode 26.6, and add a forward-compatibility CI job on Xcode 27. Every API below exists on macOS 26.

---

## 0. Design principles (the short version)

1. **The note is the hero.** Chrome recedes, as in Linear's 2026 refresh: "parts central to the user's task should stay in focus, ones that support orientation and navigation should recede." Linear dimmed its sidebar "a few notches" and softened its separators ([Linear, Mar 2026](https://linear.app/now/behind-the-latest-design-refresh)).
2. **Standard components first, glass only on the navigation layer.** Apple says "Don't use Liquid Glass in the content layer" and "use Liquid Glass effects sparingly" ([HIG: Materials](https://developer.apple.com/design/human-interface-guidelines/materials)). Standard bars, sidebars, sheets and popovers adopt glass on their own. Custom backgrounds on them should be removed ([Adopting Liquid Glass](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass)).
3. **One accent, used for state only.** No color for decoration. Status (rough or polished) is shown by glyph shape, not by hue.
4. **Keyboard-complete.** Every action is in the menu bar with a shortcut, in the ⌘K palette, and in context menus. Raycast's rule: "every action has a keyboard shortcut, and every shortcut has visual confirmation" ([Raycast teardown, third-party](https://blakecrosley.com/guides/design/raycast)).
5. **Instant and local.** No spinners for local work. Bear's approach: "no spinners, skeleton screens, or toasts… sync runs in the background" ([Bear teardown, third-party](https://blakecrosley.com/guides/design/bear)). Progress UI appears only for network or LLM work, and only after 300 ms.
6. **Calm motion.** Critically damped springs, no bounce, everything under 300 ms. Reduce Motion is respected everywhere.

---

## 1. What the admired apps actually do (extracted, copyable decisions)

| App | Layout | Typography / measure | Color | Keyboard / capture | What we copy |
|---|---|---|---|---|---|
| **Apple Notes (macOS 26/27)** | 3 columns: folders sidebar, list (or gallery), editor. Translucent glass sidebar and floating toolbar ([macOS Tahoe Notes guide, third-party](https://macos-tahoe.com/blog/macos-tahoe-notes-app-complete-guide-troubleshooting-tips-2025/), [MacStories Tahoe review](https://www.macstories.net/stories/macos-26-tahoe-the-macstories-review/2/)) | System font. Title / Heading / Subheading / Body / Monospaced paragraph styles. Checklists in a single text view | System accent plus yellow find highlights | ⌘K inserts a link; Quick Note via hot corner | The column structure, a single NSTextView-style editor, and system find highlights. Notes is our baseline for "native". |
| **Bear 2** | Sidebar (tags) / note list / editor. Focus mode hides panes | Custom **Bear Sans** (customized from Clarika, larger x-height, true italics) ([Bear blog](https://blog.bear.app/2023/08/learn-about-our-new-custom-font-bear-sans/)). Body around **16pt / 1.6 line height**, paragraph spacing 12, max line width around **680pt (~75 chars)**, with narrow, medium and wide presets (third-party reconstruction: [blakecrosley](https://blakecrosley.com/guides/design/bear)) | Default theme is near-white with a single red accent for cursor and tags ([same](https://blakecrosley.com/guides/design/bear)) | Markdown syntax hides once styled. `[[` wiki links | Body size, line height, measure presets, live Markdown that hides its syntax, and one accent applied to the cursor and links. |
| **Things 3** (3.22 updated for OS 26, [lavx](https://news.lavx.hu/article/things-3-22-overhaul-aligns-with-apple-os-26-aesthetic-refinement-meets-ipad-productivity-boost)) | Sidebar of lists; a single content pane | Large bold list title (~34pt claimed, [third-party](https://www.oppadu.com/tools/design-systems-site/brand/things.html)), generous white space ([MacStories](https://www.macstories.net/reviews/things-3-beauty-and-delight-in-a-task-manager/)) | About 95% neutral. Color carries meaning only (yellow = Today, red = deadline) | **Quick Entry** global panel (⌃Space), about 500pt wide, auto-focused, ↩ saves to Inbox, esc dismisses ([third-party](https://blakecrosley.com/guides/design/things)). ⌘1–6 jump to lists | Quick Capture panel behavior, a big quiet title, color only for meaning, ⌘1…9 sidebar jumps. |
| **Craft** | Sidebar, then a page with a centered column | Body 16pt, H1 28 semibold, H2 22, H3 18 medium, page column max **720pt**, margins 40pt (third-party: [blakecrosley](https://blakecrosley.com/guides/design/craft)) | Per-page accent and paper backgrounds | `/` block menu. Cards. Native Swift rendering, Mac App of the Year 2021 ([MacStories](https://www.macstories.net/reviews/craft-review-a-powerful-native-notes-and-collaboration-app/)) | Heading scale, `/` slash menu, and block-level (not document-level) structure. We skip cards and colored pages, which are too decorative for us. |
| **Linear (desktop)** | Dimmed sidebar, dense list, detail pane | Compact tabs, smaller icon and text sizing | Warmer, less saturated grays. LCH-generated themes ([Linear 2024](https://linear.app/blog/how-we-redesigned-the-linear-ui), [2026](https://linear.app/now/behind-the-latest-design-refresh)) | ⌘K palette for everything. Single-key shortcuts | Recessive sidebar, fewer and softer dividers, ⌘K that exposes every command with its shortcut. |
| **Raycast** | One floating panel: search field, sectioned list, action footer | Rows about 13pt. Shortcut badges in caption/tertiary color. Row padding 12h / 8v (third-party: [blakecrosley](https://blakecrosley.com/guides/design/raycast)) | Window background plus system accent | Arrow keys move, ↩ runs the primary action, ⌘K opens the secondary **Actions** panel, ⌘1–9 pick results. Target under 50ms | Command palette anatomy, the secondary action panel, and the empty-results copy ("No results for '…'" plus a suggestion). |
| **iA Writer** | A single editor | Measure is selectable at **64 / 72 / 80 characters** ([iA settings](https://ia.net/writer/support/basics/settings/settings-mac)). Type size tuned for perceived size at reading distance | Monochrome with blue cursor | Focus mode, syntax highlight. ADA 2025 Inclusivity finalist ([MacStories](https://www.macstories.net/news/2025-apple-design-awards-winners-and-finalists-announced/)) | A measure expressed in **characters**, not pixels, and a focus mode. |
| **Ulysses** | Library / sheets / editor | Typewriter mode that keeps the current line fixed and can dim others ([help](https://help.ulysses.app/en_US/introduction/details-and-tips)) | Neutral | Markup-based | Optional typewriter scrolling (settings). |
| **Tot** | 7 colored dots, one sheet each | Plain text | Seven carefully tuned light and dark colors ([MacStories](https://www.macstories.net/reviews/tot-review-collect-and-edit-bits-of-text/)) | Menu-bar popover | Proof that a capture surface can be tiny and still feel premium. Our Quick Capture is the same spirit. |
| **Reeder (2024)** | Unified timeline, no unread counts ([9to5Mac](https://9to5mac.com/2024/09/05/reeder-launches-new-app-that-unites-rss-with-video-audio-and-social-feeds)) | Large readable cards | Calm, no badges | – | **No counts and no badges** in our sidebar by default. Counts appear on hover or in the folder inspector only. |
| **Arc / Dia sidebar** | A single vertical sidebar that holds everything ([Arc analysis, third-party](https://blakecrosley.com/guides/design/arc)) | – | Theme tint behind the sidebar | ⌘T command bar | A sidebar as a "place": pinned notes at the top. We do **not** copy the custom tinted chrome, which fights Liquid Glass. |
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

Column visibility modes: `.all` (default); `.doubleColumn` (list + editor, i.e. the original two-pane web layout), toggled with ⌥⌘S / the sidebar button; `.detailOnly` = **Focus** (⌃⌘F also enters full screen optional). Persist with `@SceneStorage`.

### a.2 Typography scale

All type is **SF Pro** (system). Monospace is **SF Mono**. Use semantic `Font.TextStyle` wherever the size equals a HIG style, so Dynamic Type and accessibility work. Editor sizes are custom (larger than the 13pt UI body, as Bear, Craft and iA Writer all do). The editor scales with a user setting (S/M/L/XL = 14/16/18/20 base). All editor values below are for **M (16)** and scale proportionally.

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
- Settings: Narrow 60 / **Comfortable 68 (default)** / Wide 80 / Full. These follow iA's 64/72/80 options ([iA](https://ia.net/writer/support/basics/settings/settings-mac)) and the general 45–90 guidance ([Practical Typography](https://practicaltypography.com/line-length.html)).
- Images, code blocks and tables may break out to **min(available, measure + 160)**, like Craft's "wide" content.
- Minimum side gutter: 48pt. Top padding above the title: 40pt below the toolbar's scroll-edge area. Bottom padding: 50% of viewport height, so that the last line can be scrolled to eye level (the iA/Ulysses feel).

### a.4 Color

Use only system semantic colors plus **one accent**:

- **Accent:** an `AccentColor` asset named "Fortress Ember". Light `#B4532A` (sRGB 180,83,42), dark `#F0915A`, plus increased-contrast variants `#8F3D1A` and `#FFB085`. It is applied through `.tint` at the app root.
  - When the user picks a non-Multicolor system accent, the system overrides ours. That is correct per the HIG.
  - The accent is used **only** for: the insertion point, text selection (system behavior), links and `[[ ]]`, the selected-row glyph in the sidebar, the prominent button in sheets, the "Ask" send button, and the citation chips.
  - It is never used as a fill behind text blocks.
- **Backgrounds:**
  - Editor and list: `Color(nsColor: .textBackgroundColor)` (white / near-black).
  - Window: system.
  - Sidebar: system glass.
  - Inspector: system.
  - Panels (palette, capture): `.glassEffect(.regular)`.
- **Text:** `.primary`, `.secondary`, `.tertiary`, `.quaternary`. **Never hex grays.**
- **Fills:** `quaternarySystemFill` for code and chip backgrounds. `.selection`-style row selection comes from the system List.
- **Separators:** `Color(nsColor: .separatorColor)`, used rarely. No separators between list rows. The list uses `.listRowSeparator(.hidden)` and 8pt row spacing, as in Linear's "structure should be felt, not seen".
- **Search highlight:** `NSColor.findHighlightColor` (the system's yellow) at 100% under the matched characters, with matching text staying `.primary`. This is the same as Safari and Notes find.
- **Status:**
  - *Rough* uses the glyph `circle.dashed` in `.tertiary`.
  - *Polished* uses `checkmark.seal` in `.secondary`.
  - No colors.
- **Destructive:** `.red` only in confirmation buttons (`role: .destructive`).
- **Dark mode** is automatic through the semantics above. Verify contrast with Increase Contrast on.

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
| Undo toast ("Moved to Trash · Undo") | `.glassEffect(.regular.interactive())` capsule, bottom center, auto-hides after 5s |
| Editor floating format bar | **None.** Formatting lives in the Format menu, Markdown shortcuts and ⌘B/⌘I/⌘U. The `/` menu is a system popover (glass automatically) |

Under Reduce Transparency, glass becomes opaque automatically. Our custom glass must still read correctly (test it).

### a.8 Iconography (SF Symbols, `.symbolRenderingMode(.hierarchical)` in chrome, `.monochrome` in content)

| Use | Symbol |
|---|---|
| All Notes | `note.text` |
| Rough | `circle.dashed` |
| Polished | `checkmark.seal` |
| Pinned | `pin` |
| Recent | `clock` |
| Folder | `folder` (selected: `folder.fill`) |
| New folder | `folder.badge.plus` |
| Saved search | `magnifyingglass` (row) / `bookmark` (save action) |
| Trash | `trash` |
| New note | `square.and.pencil` |
| Search | `magnifyingglass` |
| Ask your notes | `sparkle.magnifyingglass` (fallback `text.bubble`) |
| Command palette | `command` |
| Inspector toggle | `sidebar.trailing` |
| Related notes | `point.3.connected.trianglepath.dotted` |
| Seen before | `eye` |
| Backlinks | `arrow.uturn.backward` (or `link`) |
| Outline | `list.bullet.indent` |
| Promote to polished | `checkmark.seal` |
| Demote | `circle.dashed` |
| AI review | `text.badge.checkmark` |
| Link check failed | `exclamationmark.triangle` in `.secondary` |
| Backups | `clock.arrow.circlepath` |
| Restore | `arrow.counterclockwise` |
| Import | `square.and.arrow.down` |
| Export | `square.and.arrow.up` |
| Quick capture | `bolt` (menu bar extra icon: `square.and.pencil`) |
| Checklist | `checklist` |
| Code | `chevron.left.forwardslash.chevron.right` |
| Quote | `text.quote` |
| Image | `photo` |
| Heading | `textformat.size` |
| Divider | `minus` |

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
  - **Glass morph:** when the Ask composer collapses into the toolbar button, use `glassEffectID` in a shared `@Namespace`.
- No skeletons. Lists from SQLite render synchronously. Remote (LLM) work shows `ProgressView().controlSize(.small)` only after 300 ms.

---

## (b) Screen-by-screen

### b.1 Main window

```
┌──────────── sidebar (glass, 220) ─┬──── note list (300) ──────┬──── editor (flex) ─────────────────┬─ inspector (280, hidden) ┐
│ ● ● ●                 [sidebar⇆]  │ [All Notes ▾]   [✎ new]   │  ‹title in window›  [seal][ⓘ][⋯] [🔍 Search      ]│
│                                   │                           │                                    │                          │
│ Library                           │ Pinned                    │      My note title (28 bold)       │ Related · Backlinks · ≡   │
│  􀉆 All Notes                      │ ┌───────────────────────┐ │      rough · edited 2h ago · Ideas  │                          │
│  􀓞 Rough                          │ │Title (13 semibold)    │ │                                    │ Seen before              │
│  􀇻 Polished                       │ │Snippet two lines…(12) │ │      Body text 16/26 capped at     │ ┌──────────────────────┐ │
│  􀐫 Recent                         │ │2h · Ideas (11 tert.)  │ │      68ch, centered …              │ │“…passage…”            │ │
│ Folders                           │ └───────────────────────┘ │                                    │ │Note B · Mar 3 · 0.82  │ │
│  􀈕 Ideas                          │ Today                     │                                    │ └──────────────────────┘ │
│  􀈕 Work                           │  …                        │                                    │ Related notes            │
│ Saved Searches                    │ Previous 7 Days           │                                    │  …                       │
│  􀊫 "colbert"                      │  …                        │                                    │                          │
│  􀈑 Trash                          │                           │                                    │                          │
└───────────────────────────────────┴───────────────────────────┴────────────────────────────────────┴──────────────────────────┘
```

**Sidebar** (`List(selection:)`, `.listStyle(.sidebar)`):
- Sections, in order:
  - **Library**: All Notes, Rough, Polished, Recent.
  - **Pinned** (only if any): pinned notes as rows (Arc idea).
  - **Folders**: flat. Nested folders are a non-goal per spec.md.
  - **Saved Searches**.
  - **Trash**: the last row of the Folders group, not a bottom bar (HIG: nothing critical at the bottom).
- No counts by default. A per-folder count shows as `.badge()` only while hovering.
- Folder context menu: Rename, New Note in Folder, Export Folder…, Delete Folder… (notes move to All Notes).
- Drag notes onto folders. Drop `.md` files onto a folder to import.
- ⌘1–⌘4 select Library items. ⌘⌥N creates a new folder (inline rename field).

**Note list** (`List(selection:)` with `.listStyle(.inset)`, `.listRowSeparator(.hidden)`):
- Grouped by date sections like Apple Notes (Pinned / Today / Yesterday / Previous 7 Days / Previous 30 Days / month names). Section headers are 11pt semibold secondary.
- Row anatomy (top to bottom): title (1 line), snippet (2 lines, first non-title text, Markdown stripped), meta line (`relative date · folder`, plus a trailing status glyph when rough).
- Typical row height is about 72pt.
- The toolbar for this column has a sort/filter menu (`line.3.horizontal.decrease`): Sort by Edited / Created / Title, plus "Group by Date" on/off. The New Note button `square.and.pencil` (⌘N) sits here too.
- Keyboard: ↑/↓ moves, ↩ or → focuses the editor, ⌘⌫ moves to Trash (with the undo toast), Space toggles the inspector preview (optional), type-to-select.

**Editor** (detail):
- The title is a separate single-line field (28 bold). ↩ in the title moves to the body.
- A meta line under the title (12 secondary): status menu button ("Rough ▾" / "Polished ▾"), "edited 2h ago", folder name, link-check warning glyph if any.
- The body is the TextKit 2 editor (section c).
- Detail toolbar items, from right to left:
  - the system search field;
  - `ToolbarSpacer(.fixed)`;
  - group 1 [Inspector toggle];
  - group 2 [Promote/Status `checkmark.seal`, AI Review `text.badge.checkmark`, Share/Export `square.and.arrow.up`];
  - group 3 [`⋯` overflow: Move to Folder, Pin, Duplicate, Export Markdown, Show Markdown Mirror in Finder, Move to Trash].
- **The window title is the note title** (`.navigationTitle`), and `.navigationSubtitle` is the folder.

### b.2 Search (unified results, highlighted passages)

- Placement: `.searchable(text:tokens:placement: .toolbar, prompt: "Search notes")` on the split view (top trailing). ⌘F focuses it when the list has focus. In the editor, ⌘F is in-note find (NSTextView `usesFindBar = true`). **⇧⌘F** always opens global search.
- **Scopes**: `.searchScopes($mode) { Text("Words").tag(.fullText); Text("Meaning").tag(.embedding) }`. These map to the spec's text/embedding modes. The default is **Words**, falling back to Meaning automatically when Words returns 0 results, with an inline note "No exact matches · showing results by meaning".
- **Tokens**: typing `in:Ideas`, `status:rough` or `#tag` converts to a capsule token (`.searchable(text:tokens:)` with `SearchToken`). Suggestions via `.searchSuggestions`, with recent searches and saved searches.
- **Results replace the note-list column** while the query is non-empty. Sections:
  1. **Ask** row (only when the query looks like a question: ends with "?" or starts with who/what/why/how/when). Shows `sparkle.magnifyingglass` and "Ask your notes: '…'" with a `⌘↩` hint.
  2. **Notes** (title matches): standard note rows with the title match highlighted.
  3. **Passages**: one row per matching block. Note title (11 semibold secondary) above a **3-line passage** (13 regular). Matched words get the `findHighlightColor` background (radius 3) and Semibold weight. The meta line shows the date, and the relevance score only in Meaning mode (`0.82`, monospaced, tertiary).
- Selecting a passage opens the note, scrolls the block into the center, and flashes the block background (accent at 12% fading out over 1.2s). The editor's find bar is prefilled with the query, so ⌘G walks to the next match.
- Bottom of the list: "Save Search" (`bookmark`) adds it to the sidebar.
- Keyboard: ↓ from the field goes into results, ↩ opens, esc clears the field and restores the previous list selection.

### b.3 Ask your notes (streaming, cited answer)

- Entry points: the Ask row in search results, ⌘↩ in the search field, the palette command "Ask Your Notes…", and the toolbar `sparkle.magnifyingglass` (⇧⌘A).
- Presentation: **the detail column shows an Answer page** in place of the editor (selection in the list is cleared, and the window title reads "Ask"). It is not a modal, so the user can click citations and come back with ⌘[.
- Layout (same measure as the editor):
  - **Question**: 22/28 semibold, `.primary`.
  - **Answer**: 16/26 body, rendered from streamed Markdown with **Textual `StructuredText`** (section c).
  - Citations appear inline as superscript-sized **chips** `[1]`: capsule, `.caption` medium, accent foreground on accent at 12%, 18pt tall. Hovering a chip shows a popover with the cited passage (3–6 lines, highlighted), the note title and the date. Clicking opens the note at that block.
  - **Sources** section under the answer: numbered rows (number, note title, 2-line passage) in the passage-card style (8pt radius, `quaternarySystemFill`, 12pt padding).
  - While streaming: a 6pt accent caret dot pulses at the end of the text (opacity 0.4 ↔ 1, 0.8s). A "Stop" button (`stop.circle`, esc) replaces Send.
  - **Follow-up composer**: a floating glass capsule at the bottom (min height 36, grows to 5 lines) with "Ask a follow-up" and a `.glassProminent` send button (`arrow.up`, accent).
  - Footer meta (11 tertiary): "Answered from 6 passages in 4 notes · model name".
- Failure: an inline `.secondary` message plus "Try Again". Never an alert.
- "No sources found": the answer area reads "I couldn't find anything in your notes about that." followed by a "Search instead" button.

### b.4 Command palette (⌘K)

- A window-anchored overlay, **560 wide**, top edge at 18% of the window height. Max 8 visible rows, then it scrolls. Glass panel radius 22, 12pt inner padding.
- The rest of the window is not dimmed, matching Raycast and Spotlight.
- Opening the palette **always** goes to the palette. **Add Link** is ⇧⌘K. This intentionally deviates from Notes and Bear, where ⌘K inserts a link. Note this in Settings → Shortcuts so users can swap them.
- Input: 20pt, placeholder "Search notes and commands", `magnifyingglass` leading.
  - Prefix `>` limits to commands.
  - `#` limits to folders.
  - `?` turns the query into an Ask.
- Result sections:
  - With an empty query: **Recent notes** (5), then **Suggested commands** (New Note, Quick Capture, Ask, Toggle Inspector, Import, Back Up Now).
  - While typing: **Notes** (fuzzy title match, matched characters semibold), **Commands** (all menu commands, generated from one `AppCommand` registry that also builds the menu bar so the two never drift), **Folders**.
- Row: 36pt tall. Leading 16pt symbol (`.secondary`), title (13), trailing shortcut hint (10 mono tertiary, e.g. `⇧⌘F`). The selected row has a system selection fill and a concentric radius.
- Keys: ↑/↓, ↩ runs the default action, ⌘↩ the alternate (for a note: open in new window), **→ or Tab opens the Actions sub-list** for the selected note (Open, Move to…, Pin, Promote, Export, Trash), as in Raycast's ⌘K action panel. esc closes. ⌘1–⌘9 run the nth result.
- Empty: "No results for '<query>'". Secondary row: "Search inside notes for '<query>'" (↩), then "Ask your notes" (⌘↩).

### b.5 Quick Capture (⌘⇧Space, global)

- Global hotkey via **KeyboardShortcuts** (sindresorhus, MIT, v1.10.0), user-rebindable. ⌘⇧Space is not a macOS default, but some input-method setups use it, so offer rebinding in onboarding.
- An **NSPanel** subclass (`.nonactivatingPanel`, `.fullSizeContentView`, `isFloatingPanel = true`, `level = .floating`, `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`, `canBecomeKey = true`) hosting SwiftUI. It is non-activating so the user's current app does not lose its place, as in Things' Quick Entry. Position: centered horizontally, top at 22% of the active screen.
- Size: **560 wide**. Height grows from 112 to a maximum of 360, then scrolls.
- Content:
  - A multi-line `TextEditor` (AttributedString, macOS 26) at 15/22 with placeholder "Capture a thought…". The first line becomes the title.
  - Footer row (11pt): a folder chip (`folder` + name, a menu), a status chip ("Rough", default), and on the right `↩ Save   ⌘↩ Save & Open   esc`.
  - When Safari or another browser is frontmost, an optional "Include link to <page title>" toggle (off by default; needs Apple Events permission, so ship it later).
- Behavior:
  - ↩ saves. ⇧↩ inserts a newline (Things semantics). Empty input does nothing. esc discards, but if there is text, esc keeps a draft for 10 minutes and the next open restores it.
  - On save, the panel morphs out (opacity + 0.98 scale) and a menu-bar extra icon briefly shows `checkmark`.
- Also available from `MenuBarExtra` (optional, Settings → General → "Show in menu bar").

### b.6 Inspector: Related / Seen before / Backlinks / Outline

- `.inspector(isPresented:)`, toggled with ⌥⌘I. At the top, a segmented `Picker` (`.pickerStyle(.segmented)`, `.controlSize(.small)`) with icons + labels: **Related**, **Links**, **Outline**.
- **Related** tab:
  - **Seen before** (only when the focused block has strong matches, score ≥ threshold): "You've written something like this" (11 semibold secondary). Up to 3 passage cards, each with the quote (3 lines, 12/16), note title (11 semibold) and date (11 tertiary). The cards are draggable into the editor, which inserts a block reference `((id))`. They refresh on caret movement with a 400ms debounce, and the cards cross-fade.
  - **Related notes**: up to 8 rows with title (13), a 1-line snippet (11 secondary) and a score (11 mono tertiary). Click opens. ⌘-click opens in a new window.
  - In the editor, blocks with a "seen before" match get a **4pt dot in the left gutter** (`.tertiary`, accent on hover). Clicking it opens the inspector scrolled to the match. This is the quietest possible "you wrote this before" cue.
- **Links** tab: Backlinks (notes linking to this note, each with the linking sentence and the `[[Title]]` highlighted). Below that, "Unlinked mentions" (notes mentioning the title as plain text) with a "Link" button per row.
- **Outline** tab: the heading tree. Clicking scrolls the editor. The current section shows an accent leading bar.
- Empty states: "No related notes yet. Notes appear here as you write." (`point.3.connected.trianglepath.dotted`, `ContentUnavailableView`, small).

### b.7 Trash

- A sidebar item. Selecting it shows trashed notes in the list, with the meta line "Deleted 3 days ago".
- The editor is **read-only** with a banner at the top of the content: 13pt, `.secondary`, `quaternarySystemFill`, radius 8. It reads "This note is in the Trash." followed by [Restore] (bordered) and [Delete Permanently…] (destructive, plain).
- List toolbar shows "Empty Trash…". It asks for confirmation with `.confirmationDialog`: "Permanently delete 12 notes? This can't be undone." with a destructive button.
- Context menu: Restore, Delete Permanently.
- Undo toast after any trash: a bottom-center glass capsule "Moved to Trash · Undo (⌘Z)". It lives 5s. ⌘Z also works through the window undo manager.

### b.8 Backups

- **Settings → Backups** tab, plus menu **File → Restore from Backup…** which opens the same view as a sheet.
- A `Table` of snapshots: Date (relative + absolute on hover), Kind (Daily / Manual / Pre-restore), Notes count, Size. Selection plus [Restore…].
- Above the table: the "Automatic daily backups" toggle (bound to `AUTO_BACKUP_ENABLED`), the "Keep 7 daily · 10 manual · 5 pre-restore" caption (secondary), [Back Up Now] and [Show in Finder].
- Restore confirmation sheet (500 wide):
  - Title "Restore backup from Mar 3, 2026, 09:14?"
  - Body: "Your current notes will be saved as a pre-restore backup first. Notes not in this backup will be moved aside, not deleted."
  - Buttons: [Cancel] [Restore] (`.borderedProminent`).
  - Progress: inline `ProgressView` (determinate) in the sheet, then a success state "Restored 214 notes" with [Done].

### b.9 Import and export

- **File → Import Markdown…** (⇧⌘I) uses `.fileImporter(allowedContentTypes: [.markdown, .folder], allowsMultipleSelection: true)`.
- Then a sheet (520 wide):
  - Header "Import 42 files".
  - Destination folder `Picker`, "Mark imported notes as" Rough/Polished.
  - A list preview (filename → title) with warnings (`exclamationmark.triangle` secondary, e.g. "Duplicate title, will be renamed").
  - [Cancel] [Import].
  - Then a determinate progress bar, then a summary: "Imported 40 · Skipped 2" with a disclosure listing the reasons.
- Dropping `.md` files on the window or a sidebar folder skips the file panel and goes straight to this sheet.
- **Export**: per note, File → Export as Markdown… (`.fileExporter`). Per folder, a context menu entry that exports a folder of `.md` files.

### b.10 Settings (`Settings` scene, `TabView`, each tab a `Form` with `.formStyle(.grouped)`, window width 560)

- **General**:
  - Default folder for new notes.
  - New notes start as Rough/Polished.
  - Show in menu bar.
  - Quick Capture shortcut (`KeyboardShortcuts.Recorder`).
  - Notes folder location (read-only path + "Show in Finder").
- **Editor**:
  - Text size (S/M/L/XL).
  - Line width (Narrow 60 / Comfortable 68 / Wide 80 / Full).
  - Font (System / New York serif / SF Mono).
  - Typewriter scrolling.
  - Hide Markdown syntax (on).
  - Spell check / grammar (system).
  - Smart quotes (system).
  - A live preview paragraph at the bottom.
- **Search & AI**:
  - Default search mode (Words / Meaning).
  - "Seen before" sensitivity slider (`Slider` with `step` ticks; Low / Medium / High).
  - Ask model picker, API endpoint status.
  - Image OCR + captions toggle.
  - Rebuild index button.
- **Backups**: as in b.8.
- **Shortcuts**: a read-only table of all shortcuts from the `AppCommand` registry, plus the ⌘K / ⇧⌘K swap toggle.

### b.11 Empty states (all `ContentUnavailableView`)

| Where | Symbol | Title | Description | Action |
|---|---|---|---|---|
| No notes at all (first launch) | `square.and.pencil` | "Write your first note" | "Notes are saved on this Mac as you type." | [New Note ⌘N] [Import Markdown…] |
| Folder empty | `folder` | "No notes in Ideas" | – | [New Note in Ideas] |
| No selection | none (blank editor background with a centered `.tertiary` 13pt "No note selected", like Notes) | | | |
| Search no results | `.search` (built-in `ContentUnavailableView.search(text:)`) | system | "Try Meaning search" link if in Words mode | |
| Trash empty | `trash` | "Trash is empty" | "Deleted notes stay here until you empty the Trash." | – |
| Inspector related empty | see b.6 | | | |
| Ask: backend unreachable | `bolt.horizontal.circle` | "Ask isn't available" | "The local server isn't responding." | [Try Again] |

### b.12 Menu bar (single source of truth: `AppCommand` registry → `.commands {}` and palette)

- **File**: New Note ⌘N, New Folder ⌥⌘N, Quick Capture ⇧⌘Space (shows the global binding), Open Note… ⌘O (= palette notes-only), Import Markdown… ⇧⌘I, Export as Markdown… ⇧⌘E, Restore from Backup…, Back Up Now.
- **Edit**: standard, plus Find in Note ⌘F, Search All Notes ⇧⌘F.
- **Format**: Heading 1/2/3 ⌥⌘1/2/3, Body ⌥⌘0, Bulleted List ⇧⌘7, Numbered List ⇧⌘8, Checklist ⇧⌘L, Toggle Checkbox ⌘↩ (in a list item), Quote ⌥⌘', Code Block ⌥⌘C, Bold ⌘B, Italic ⌘I, Underline ⌘U, Strikethrough ⇧⌘X, Inline Code ⌘E, Add Link ⇧⌘K, Indent ⌘] / Tab, Outdent ⌘[ / ⇧Tab, Move Block Up/Down ⌃⌘↑/↓.
- **Note**: Promote to Polished ⇧⌘P, Pin/Unpin ⌥⌘P, AI Review ⇧⌘R, Move to Folder… ⇧⌘M, Move to Trash ⌘⌫.
- **View**: Show/Hide Sidebar ⌃⌘S, Show/Hide Inspector ⌥⌘I, Focus Mode ⌃⌘F (detailOnly), Command Palette ⌘K, Ask ⇧⌘A, Text Size larger/smaller ⌘+ / ⌘−.
- **Go**: Back ⌘[ (when not in text), Forward ⌘], All Notes ⌘1, Rough ⌘2, Polished ⌘3, Recent ⌘4.

---

## (c) Editor: recommended approach

### Decision

Use **one AppKit `NSTextView` on TextKit 2** (`NSTextLayoutManager` + `NSTextContentStorage`), wrapped in `NSViewRepresentable`, per note. Blocks are represented as **paragraphs carrying custom attributes**. Use **swift-markdown** (Apache-2.0, 0.9.0) for Markdown import, export and paste, and **Textual** (MIT, 0.5.0) for read-only rendering (Ask answers, passage previews). SwiftUI `TextEditor(AttributedString)` is used only for small inputs (Quick Capture, Ask composer).

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
- **WebView + BlockNote (reuse the web editor).** Fastest to ship, but it is exactly the "not native" feel the user is escaping (IME, Services, text system, look). Rejected for the main editor. It could serve as a temporary bridge only.

### Architecture

```
SQLite (backend, blocks table: id, parent_id, position, type, attrs, inline content)
   ⇅  BlockTransaction (insert/update/move/delete/set-attrs)   ← existing API contract
DocumentModel (Swift, @Observable): [Block] tree, stable ULIDs
   ⇅  BlockAttributedMapper  (Block ⇄ NSAttributedString paragraph)
FortressTextView : NSTextView (TextKit 2)  ← NSViewRepresentable
   ├─ NSTextContentStorage  (paragraph = block; attrs: .fnBlockID, .fnBlockKind, .fnIndent, .fnChecked, .fnListOrdinal)
   ├─ NSTextLayoutManager delegate → custom NSTextLayoutFragment subclasses:
   │     BulletFragment, NumberFragment, CheckboxFragment (hit-testable), QuoteFragment (bar), CodeBlockFragment (bg + mono), HeadingFragment
   ├─ NSTextAttachment + NSTextAttachmentViewProvider: images (resizable), dividers, embedded block refs
   ├─ MarkdownInputRules (after-change, IME-safe)
   └─ Completion popovers: `[[` note picker, `((` block picker, `/` slash menu (NSPopover anchored at caret rect from textLayoutManager)
```

- **Block identity.** Each paragraph carries `.fnBlockID` (ULID). Typing ↩ splits a paragraph. The new paragraph gets a fresh ID in `textStorage(_:didProcessEditing:)`, while the original keeps its ID. Merging (⌫ at the start of a block) keeps the upper ID. Nested blocks use `.fnIndent` (0…6), mapped to `parent_id` on save. This is Bear's and Apple Notes' model (flat paragraphs with indentation) and maps to the BlockNote-style tree in the backend.
- **Persistence.** On an edit, mark dirty block IDs. A 400ms debounce diffs dirty paragraphs into `BlockTransaction`s, which are sent through the existing API. Never re-serialize the whole note.
- **Markdown shortcuts (live).** At line start: `# `, `## `, `### ` produce headings; `- `/`* ` a bullet; `1. ` numbered; `[] `/`[ ] ` a checkbox; `> ` a quote; ```` ``` ```` + ↩ a code block; `---` + ↩ a divider. Inline: `**x**`, `_x_`, `` `x` ``, `~~x~~`, `[[` opens the note picker, `((` opens the block picker. Implementation details:
  - Shortcuts run in `textDidChange` *after* the edit and **only when `!hasMarkedText()`**. This makes them IME-safe: never transform during Japanese, Chinese or Korean composition.
  - Each transform is one undo group, so ⌘Z restores the literal characters, as Notes and Bear do.
  - Syntax characters are removed and represented by attributes ("hide Markdown syntax" on). The Markdown mirror is regenerated by the backend.
- **Links.**
  - `[[Title]]` is stored as an attribute `.fnNoteLink = noteID` on the title text. It renders with the accent color. ⌘-click or a plain click with the pointing-hand cursor opens it. Unresolved links get a dotted underline. Renames update the attribute targets server-side.
  - `((blockID))` renders as an inline attachment showing the referenced text (live), with a subtle underline.
- **Images.** Paste or drop goes through the existing `/api/images` endpoint. The image is inserted as an `NSTextAttachment` with a view provider (async thumbnail, drag handle for width, stored as `width` attr). Captions and OCR come from the backend.
- **Undo.** Use NSTextView's built-in `undoManager` with `allowsUndo = true`. Structural operations (indent, move block ⌃⌘↑/↓, checkbox toggle) use `shouldChangeText(in:replacementString:)` + `didChangeText()` wrappers so they register as text-system undo, all in one stack.
- **Performance.**
  - TextKit 2 lays out only the viewport.
  - Never touch `textView.layoutManager`. Accessing it silently drops the view back to TextKit 1.
  - Keep attribute runs minimal.
  - The budget is 50k words opening in under 150ms and typing latency under 8ms. Add a perf test with a generated 50k-word note.
- **Free system behavior.** Find bar (`usesFindBar`, `isIncrementalSearchingEnabled`), spelling and grammar, Writing Tools (`writingToolsBehavior = .complete` on macOS 15+), Services, dictation, drag and drop, accessibility.

### Risks and mitigations

1. **TextKit 2 viewport glitches.** These include jumpy scrolling with estimated heights and fragment-height inconsistencies. They are documented as still "unresolved" by STTextView's author in Aug 2025 ([TextKit 2 – the promised land](https://blog.krzyzanowskim.com/2025/08/14/textkit-2-the-promised-land/)). Mitigations: keep fragments simple; avoid huge attachments (cap image height at 480 and lazy-load); add a settings-hidden "TextKit 1 compatibility" fallback (a custom `NSLayoutManager` drawing for bullets and quotes) if real users hit it; and keep STTextView's commercial license (from €5 per its LICENSE/README) as plan B.
2. **Custom fragments plus hit-testing for checkboxes.** Handle `mouseDown` and map it to the fragment via `textLayoutManager.textLayoutFragment(for:)`.
3. **Block ID stability across paste and undo.** Pasted content always gets new IDs, and undo restores old IDs from the attribute snapshot. Unit-test the mapper with property tests (random edit sequences → IDs unique, order preserved).
4. **IME.** Integration tests type through `insertText(_:replacementRange:)` with `setMarkedText` sequences for kana, pinyin and Hangul, and assert that no transform fires mid-composition.
5. **Effort.** This is the biggest piece of the rebuild, estimated at 3–5 weeks for a strong v1. Ship order: paragraphs + headings + lists + checkboxes → links → code/quote → images → block refs.

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
- **Screen recording is pre-granted.** The image's TCC setup grants `kTCCServiceScreenCapture` to `/bin/bash` and the hosted compute agent ([configure-tccdb-macos.sh](https://github.com/actions/runner-images/blob/main/images/macos/scripts/build/configure-tccdb-macos.sh)). The existing `screencapture -l <windowID>` approach in `mac/Tests/FortressKitTests/Snapshot.swift` therefore works on hosted runners. Runner VMs render at **1x**, unlike Retina laptops ([dev.to](https://dev.to/d4g4/our-swiftui-snapshot-tests-passed-locally-but-failed-on-ci-heres-the-actual-fix-5fhd)).

### Project structure decision

- **Keep the SwiftPM package** (`mac/Package.swift`, swift-tools 6.2, `.macOS(.v26)`) as the home of all code (`FortressKit`) and tests. It is fast in CI and needs no project files in git.
- **Add XcodeGen (2.47.0)** only for the shippable `.app`: a `project.yml` declaring an app target that depends on the local package. The app target holds Info.plist, `AccentColor`/AppIcon asset catalog, entitlements and the `Settings`/`MenuBarExtra` scenes. A plain SwiftPM executable has no bundle, so it can't carry an asset-catalog accent color, app icon or proper bundle ID, and global hotkeys and Login Items want a real bundle. Commit `project.yml` and gitignore the `.xcodeproj`.
- Tuist (4.218) is more than a single-app project needs.

### Two tiers of image tests

1. **Screenshot gallery (not diffed).** The current approach puts real windows in front of `screencapture` and captures the whole main window, palette, capture panel, Ask, Trash and Settings in **light and dark**. This is the only way to capture **Liquid Glass, materials, `List`, `TextEditor` and NSTextView**. `ImageRenderer` cannot capture them: Apple documents that it does not render views backed by native platform views such as web views and some controls. Glass and AppKit-backed lists are in that category. Upload as an artifact. Humans review it in PRs.
   - Improve determinism: set `NSApp.appearance`, fix the window size (1280×820 fits the runner's display), disable caret blink (`NSTextInsertionIndicator` hidden or `insertionPointColor = .clear`), freeze dates through an injected clock, and use fixture notes.
2. **Pixel-diff regression tests** with **swift-snapshot-testing 1.19.7** (`.image` on `NSView`) for **content-layer components only** (list rows, passage cards, editor fragments with fixed text).
   - Render a standalone `NSHostingView` at a fixed size and 1x, using `cacheDisplay(in:to:)` with **no window**. That avoids the title-bar offset bug reported on CI ([dev.to](https://dev.to/d4g4/our-swiftui-snapshot-tests-passed-locally-but-failed-on-ci-heres-the-actual-fix-5fhd)).
   - Use `precision: 0.99, perceptualPrecision: 0.98`.
   - **Record references on CI** (a `workflow_dispatch` with `SNAPSHOT_RECORD=1` that uploads them), never on laptops.

### Workflow (drop-in replacement for `.github/workflows/mac.yml`)

```yaml
name: macOS app
on:
  push: { branches: [main], paths: ["mac/**", ".github/workflows/mac.yml"] }
  pull_request: { paths: ["mac/**", ".github/workflows/mac.yml"] }
  workflow_dispatch:
    inputs: { record: { type: boolean, default: false } }

jobs:
  build-test:
    runs-on: macos-26            # arm64, macOS 26.7.x, Xcode 26.6 default (image 20261102)
    timeout-minutes: 30
    defaults: { run: { working-directory: mac } }
    steps:
      - uses: actions/checkout@v5          # v7.0.1 is the newest tag; v5+ fine
      - run: sudo xcode-select -s /Applications/Xcode_26.6.app && swift --version && xcodebuild -version
      - uses: actions/cache@v4
        with: { path: mac/.build, key: spm-${{ runner.os }}-${{ hashFiles('mac/Package.resolved') }} }
      - run: swift build -c debug
      - name: Tests + screenshots
        run: SNAPSHOT_DIR="$PWD/snapshots" SNAPSHOT_RECORD=${{ inputs.record && '1' || '' }} swift test 2>&1 | xcbeautify
      - uses: actions/upload-artifact@v4   # v7.0.2 newest; v4+ fine
        if: always()
        with: { name: screenshots-xcode26, path: mac/snapshots, if-no-files-found: ignore }

  app-bundle:                       # proves the real .app builds; no signing
    runs-on: macos-26
    needs: build-test
    steps:
      - uses: actions/checkout@v5
      - run: brew install xcodegen && cd mac && xcodegen generate
      - run: >
          xcodebuild -project mac/FortressNotes.xcodeproj -scheme FortressNotes
          -destination 'platform=macOS' -configuration Debug
          CODE_SIGNING_ALLOWED=NO CODE_SIGN_IDENTITY="" build | xcbeautify

  forward-compat:                   # Xcode 27 / macOS 27 (Golden Gate); non-blocking
    runs-on: xcode-27
    continue-on-error: true
    defaults: { run: { working-directory: mac } }
    steps:
      - uses: actions/checkout@v5
      - run: swift --version && swift build && SNAPSHOT_DIR="$PWD/snapshots" swift test
      - uses: actions/upload-artifact@v4
        if: always()
        with: { name: screenshots-xcode27, path: mac/snapshots, if-no-files-found: ignore }
```

- Code signing is not needed. SwiftPM builds are unsigned (ad-hoc on arm64 automatically). The xcodebuild job uses `CODE_SIGNING_ALLOWED=NO`.
- Swift Testing can now attach `CGImage`/`NSImage` directly to test results (since Xcode 26.4) ([coverage](https://dev.to/arshtechpro/xcode-264-here-is-what-actually-matters-for-devs-2hke)). This is useful for attaching diffs to failing tests.

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

## Appendix: key SwiftUI skeleton

```swift
@main struct FortressNotesApp: App {
  var body: some Scene {
    WindowGroup { MainView() }
      .defaultSize(width: 1280, height: 820)
      .windowResizability(.contentMinSize)
      .commands { AppCommands() }               // generated from AppCommand registry
    Settings { SettingsView() }
    MenuBarExtra("Fortress", systemImage: "square.and.pencil", isInserted: $showMenuBarExtra) { QuickCaptureMenu() }
  }
}

struct MainView: View {
  @State private var visibility: NavigationSplitViewVisibility = .all
  @State private var inspector = false
  var body: some View {
    NavigationSplitView(columnVisibility: $visibility) {
      Sidebar().navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 300)
    } content: {
      NoteListOrResults().navigationSplitViewColumnWidth(min: 260, ideal: 300, max: 420)
    } detail: {
      EditorOrAnswer()
        .inspector(isPresented: $inspector) { InspectorView().inspectorColumnWidth(min: 240, ideal: 280, max: 360) }
    }
    .searchable(text: $query, tokens: $tokens, placement: .toolbar, prompt: "Search notes") { $0.label }
    .searchScopes($mode) { Text("Words").tag(SearchMode.words); Text("Meaning").tag(SearchMode.meaning) }
    .overlay(alignment: .top) { if paletteShown { CommandPalette().padding(.top, 140) } }
    .tint(.accentColor)
  }
}

struct CommandPalette: View {               // custom glass: navigation layer only
  var body: some View {
    GlassEffectContainer {
      VStack(spacing: 12) { PaletteField(); PaletteResults() }
        .padding(12)
        .frame(width: 560)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
  }
}
```
