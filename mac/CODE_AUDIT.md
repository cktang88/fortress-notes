# Fortress Notes (mac/): code audit — platform, editor, performance, localization, distribution

Audited 18 Nov 2026 against the SwiftUI/macOS 26 checklist (sections A, I, L, N, O; the
UX/HIG sections B–H, J, K, M and the color theme are audited separately). Each item:
**pass**, **fail → fixed** (what changed, where), **fail → deferred** (why, and who/what it
needs), or **n.a.**. Line numbers are as of branch `mac-code-audit`.

Summary: 33 items. 16 pass, 13 failed and fixed (2 of them only in part), 4 deferred.
Strict concurrency is a build error now (`treatAllWarnings(as: .error)`, Swift 6 mode).

| | Pass | Fixed | Deferred | n.a. |
|---|---|---|---|---|
| A Platform, Swift 6 | A3, A4 | A1 | A2, A5 | |
| I Editor | I1, I2, I6 | I3 (see Perf), I4 (part), I5 | I7 | |
| L Observation, perf, concurrency | L1, L2, L4, L5, L6, L7, L9, L11 | L3, L8, L10 | | |
| N Localization | N4 | N1 (part), N2, N3, N5 | | |
| O Distribution, privacy | O3, O5 | O1, O2, O4 | | |

## A. Platform, toolchain, Swift 6

- **A1 [M] Swift 6 mode, complete checking, no suppressed warnings — fail → fixed.**
  `Package.swift` was tools 6.2 (Swift 6 mode by default) but CI printed ~400 concurrency
  warnings (four `nonisolated(unsafe)` on a Sendable `TableBlockController?`,
  `TableBlock.swift` accessibility element; one in `SpyProtocol.swift`; a captured `var`
  mutated after capture by a Sendable closure, `FindingFixesTests.swift`). Fixed all, and
  `Package.swift:6` now sets `.treatAllWarnings(as: .error)` on every target with
  `swiftLanguageModes: [.v6]`, so a new warning fails the build. Every remaining escape
  hatch is justified in a comment:
  - `BlockInfo`, `OriginBox`, `MarksBox`, `NodeBox` (`TextAttributes.swift:19–100`):
    were `@unchecked Sendable`; now checked `Sendable` (final NSObject subclasses with
    only `let` Sendable properties).
  - `PendingEdits.directory` (`PendingEdits.swift:22`): was a `nonisolated(unsafe) static
    var` read from the main actor and the write queue; now behind a `Mutex`.
  - `EditorTheme.StyleCache` (`EditorTheme.swift:261`): `@unchecked Sendable` + `NSLock`,
    kept (a `Mutex` can't hand out the non-Sendable attribute dictionaries; region
    checking rejects it) and now uses `withLock` with the reason written down.
  - `nonisolated(unsafe)` + `MainActor.assumeIsolated` in TextKit/AppKit callbacks
    (`BlockEditorController.swift:745` storage delegate, `TableBlock.swift:452` cell
    storages, `Attachments.swift:107/224` attachment and view provider, table
    accessibility elements): the SDK declares these nonisolated; they run on the main
    thread for an NSTextView, and `assumeIsolated` traps rather than races. Commented.
  - `MediaCache.Memory` (NSCache, thread-safe), `ImagePipeline.Decoded` (CGImage),
    `CaptureDraftStore` (UserDefaults): `@unchecked Sendable`, commented.
  - Test doubles (`URLProtocol` subclasses with static reply queues) keep
    `nonisolated(unsafe)`; test-only.
- **A2 [S] default MainActor isolation for the UI target — fail → deferred.** One module
  (`FortressKit`) holds both the API/codec layer (nonisolated, Sendable structs that must
  stay off the main actor) and the UI. `.defaultIsolation(MainActor.self)` on it would
  pull `APIClient`, `BlockCodec`, `BlockDiff` and the caches onto the main actor. Needs the
  package split into `FortressCore` (API/, Editor/Model) and `FortressUI` first.
- **A3 [M] no `UIDesignRequiresCompatibility` — pass.** No hit; no Info.plist opted out
  (the new `Packaging/Info.plist` doesn't set it).
- **A4 [M] standard components — pass.** `NavigationSplitView` + `.inspector` +
  `.searchable(placement: .toolbar)` (`MainWindow.swift:31–49`), `List` for notes and
  sidebar, `Settings` scene, `.fileImporter`/`.fileExporter`, `.confirmationDialog`.
  (Details of each component are the UX audit's.)
- **A5 [S] profile with the SwiftUI instrument each release — fail → deferred.** Signposts
  cover note open, load/append, decode, save diff, palette, Ask (`App/Signposts.swift`);
  CI runs the editor and Finding perf suites. No release checklist step runs Instruments;
  that needs a release process (there is none yet). Added an opt-in sampled keystroke
  profile (`Tests/FortressKitTests/KeystrokeProfile.swift`, `KEYSTROKE_PROFILE=1`).

## I. Editor (TextKit 2, AppKit bridge)

- **I1 [M] never touch `layoutManager`, no NSTextTable — pass.** No `.layoutManager`,
  `NSLayoutManager` or `NSTextTable` anywhere in Sources or Tests; tables and columns are
  attachment view providers. Added a debug `assert(textView.textLayoutManager != nil)` at
  the end of `BlockEditorController.load` (`:223`) and a test that walks every text view
  of a note with tables, columns and pictures — the note, a table cell editor, the
  column editors — and checks each is still TextKit 2 (`CodeAuditTests.richNoteStaysOnTextKit2`).
- **I2 [M] `updateNSView` doesn't reset text — pass.** `BlockTextEditor.updateNSView`
  (`NoteEditorView.swift:176`) only forwards focus requests; text is loaded once in
  `makeNSView`, the view is recreated per note (`.id(id)`, `MainWindow.swift:141`), and
  the controller is the source of truth while it lives. Removed a mis-indented duplicate
  `documentId` assignment there and documented the rule.
- **I3 [M] typing latency under a frame on large notes — see "Performance" below.**
- **I4 [M] system text services — fail → fixed in part.** Find bar, incremental search,
  spelling, Writing Tools, smart insert are on (`FortressTextView.swift:24–47`). Smart
  quotes and dashes were forced on in the note and in table cells; they now follow
  System Settings (`NSSpellChecker.isAutomaticQuoteSubstitutionEnabled` /
  `…DashSubstitutionEnabled`), test `substitutionsFollowTheSystemSetting`. Link detection
  stays off on purpose (links are marks). *Deferred:* a Format menu with checkmarks for
  active marks needs menu commands (UX audit, section E) plus responder-chain actions on
  the text view (`toggleBold:` etc.); today ⌘B/⌘I/⌘U are key equivalents in
  `BlockEditorController.handleKeyEquivalent`.
- **I5 [S] Writing Tools, protect exact ranges — fail → fixed.** `writingToolsBehavior =
  .complete` was set, but nothing was protected. The controller now answers
  `textView(_:writingToolsIgnoredRangesInEnclosingRange:)` with links, note links and
  block references, inline code, code blocks and attachments
  (`BlockEditorController.swift:951`), test `writingToolsSkipsExactText`.
- **I6 [M] system text colors — pass.** `.textBackgroundColor` behind the text,
  `controlAccentColor` links, system selection; the highlight palette in `EditorTheme` is
  the theme audit's.
- **I7 [S] Bigger/Smaller/Actual Size — fail → deferred.** `EditorTheme.baseSize` and
  `FortressTextView.measureCharacters` already scale the editor; what's missing is the
  View menu commands and a stored scale, which is menu/commands work (UX audit, E).

## L. Observation, SwiftUI performance, concurrency

- **L1 [M] `@Observable` only — pass.** No `ObservableObject`, `@Published`,
  `@StateObject`, `@ObservedObject`, `@EnvironmentObject`. Twelve `@Observable` models;
  `AppModel` comes in through `@Environment(AppModel.self)`.
- **L2 [M] fine-grained observation — pass.** Rows take values (`NoteRow(note:folderName:)`,
  `NoteListView.swift`), bookkeeping is `@ObservationIgnored` (66 uses across 9 models,
  e.g. `AppModel.openedMarkTask`). Minor: sidebar `FolderRow` reads `model.scope` for its
  icon, so every folder row re-renders on a scope change (cheap; left).
- **L3 [M] cheap `body` — fail → fixed.**
  - `NoteListView.body` filtered, sorted and grouped every note on every evaluation, and
    it re-evaluates on each selection change (↑/↓ through the list). Now a
    `NoteListCache` held in `@State` (`NoteListView.swift:214`) remakes the list only when
    the scope, the notes, the sort or the day change (test
    `noteListIsRemadeOnlyWhenItsInputsChange`).
  - `MetaLine` made a `RelativeDateTimeFormatter` per render; now a `FormatStyle`
    (`NoteEditorView.edited`), and the block hover popover likewise.
  - Left: `MetaLine` walks the folder tree for the note's folder name per render
    (`AppModel.folderName(forNote:)`); small trees, one row.
- **L4 [M] stable identity — pass.** `ForEach` uses model ids; the `id: \.self` hits are
  enum cases (`NoteStatus.allCases`, `OneNoteImport.Step.allCases`) or short immutable
  string lists in the import sheets; no `indices`, no `.id(UUID())`.
- **L5 [M] no `AnyView`, no `.if` — pass.** No hits.
- **L6 [M] lazy containers for large collections — pass.** Notes and sidebar are `List`;
  search results a `LazyVStack` (`SearchResultsView.swift:19`); the palette's `VStack`
  holds a capped section list; the one `GeometryReader` (`Attachments.swift:304`) is in an
  embed view, not a row.
- **L7 [M] fast-changing values out of the environment — pass.** The one `@Entry` is a
  `FocusedValues` key (`NoteListView.swift`); Ask's streaming text lives in `AskState`.
- **L8 [M] view-tied async work in `.task` — fail → fixed.** `NoteEditorView` started an
  unstructured `Task { sleep 300 ms; showProgress = true }` inside its `.task`, which
  outlived the view (and was never reset). Now its own `.task(id: documentId)` that is
  cancelled with the view (`NoteEditorView.swift:49`). Other `Task {}` in views are button
  actions (user-initiated, fire-and-forget); model-owned tasks (`InspectorModel`,
  `SearchController`, `EditorSession.flushTask`, `AppModel.openedMarkTask`) cancel the
  previous one before starting the next and hold `[weak self]`.
- **L9 [M] heavy work off the main actor — pass.** Note decode
  (`EditorSession.decode`, now `@concurrent`), huge-note diffs (`EditorSession.swift:278`),
  image decode (`ImagePipeline`, `MediaCache`), OCR (`PictureTextReader.readOne`,
  nonisolated) and `APIClient` (a Sendable struct with nonisolated async methods) all run
  off the main actor.
- **L10 [M] no GCD hops, `Task.detached` with reasons, `assumeIsolated` at AppKit
  boundaries — fail → fixed.** No `DispatchQueue.main`/`.global` in Sources (one serial
  queue orders pending-edit writes, `PendingEdits.swift`; one thread blocks in `waitpid`
  for the backend, `BackendController.swift`). Three `Task.detached` that only moved work
  off the main actor became `@concurrent` functions, which keep priority and
  cancellation: `EditorSession.decode`, `BlockEditorController.read(_:)`,
  `.pixelSize(of:)`. The rest state their reason (image decode sharing one task per URL,
  request coalescing in `APIStore`, the huge-diff case). `assumeIsolated` appears only in
  AppKit/TextKit/RunLoop callbacks, commented (see A1).
- **L11 [S] shared caches are actors or `Mutex` — pass.** `MediaCache` (`Mutex` +
  NSCache), `Timestamp` memo (`Mutex`), `APIStore` (`Mutex`), `EditorTheme.StyleCache`
  (`NSLock`, justified), `PaletteCache` and Finding caches are main-actor.

## N. Localization readiness

- **N1 [M] all UI strings localizable — fail → fixed in part.** SwiftUI literals
  (`Text("…")`, `Button("…")`) are `LocalizedStringKey` already. Strings built in models
  were plain `String`; converted to `String(localized:)` in the editor (status labels,
  meta line, block provenance popover, table accessibility labels, link tooltip), the
  note list (sort names, date sections, "New Note", Trash dates), OneNote import
  summaries and Picture/PDF titles. Added `defaultLocalization: "en"` and a String
  Catalog, `Packaging/Localizable.xcstrings`, compiled into the app bundle's resources by
  `Scripts/package_app.sh` (SwiftUI and `String(localized:)` look in the main bundle;
  the package's targets have no resource bundle). *Deferred:* model strings still plain in
  `Finding/NoteInfo.swift`, `Finding/AskState.swift`, `Finding/PaletteModel.swift`,
  `Finding/QuickCapture.swift`, `Shell/AppModel+Shell.swift`,
  `Shell/BackendController.swift` (status messages) — mechanical, but those files are
  being changed by the UX audit; and catalog extraction needs Xcode's string extraction
  (no `.xcodeproj` here), so keys are added by hand for now.
- **N2 [M] no concatenated sentences, plural rules — fail → fixed.** `"Deleted " + date`
  (`NoteListing.deletedLabel`), `"Today, " + time` (`InspectorView.format`), `"Math: " +`
  (math accessibility label), `plural(...) + " from the converter"`, and
  `OneNoteImport.plural` appending an English "s" are now single interpolated keys;
  counts use automatic grammar agreement (`^[\(count) page](inflect: true)`), so "1 page",
  "1,204 images", "1 of 1 page" come out right and each noun is its own catalog key
  (tests in `OneNoteImportTests`).
- **N3 [M] format styles, not fixed formats — fail → fixed.** `String(format: "%.1f s")`
  for Ask's timing and the OneNote "2 min 5 s" durations now use `Duration` unit styles;
  the remaining `String(format: "%02x")` calls build hex ids, not UI text.
- **N4 [S] RTL-safe layout — pass.** No `.left`/`.right` alignment; shortcuts are
  `KeyEquivalent`s. (Pseudo-language screenshots not taken.)
- **N5 [S] locale-aware matching — fail → fixed.** Fuzzy matching folds case and
  diacritics (`FuzzyMatch.swift:81`), title sort uses `localizedStandardCompare`; the
  `[[` suggestions used `lowercased().contains`, now `localizedStandardContains`.

## O. Sandbox, hardened runtime, privacy

**Decision (O2): Developer ID distribution, Hardened Runtime, not sandboxed.** The app
starts the local backend (`uv`/Python from the Fortress folder chosen in Settings, in its
own process group), reads that folder, writes `~/Library/Logs/Fortress Notes/backend.log`
and signals the process group on quit. A sandboxed app's children inherit its sandbox,
so the backend couldn't run the repository's tools or reach the notes folder. If the
backend ever ships inside the bundle, App Sandbox becomes possible with
`com.apple.security.network.client` (the localhost API),
`com.apple.security.files.user-selected.read-write` (import/export) and a
security-scoped bookmark for the notes folder.

- **O1 [M] Hardened Runtime, notarized — fail → fixed.** There was no bundle, signing or
  entitlements at all (CI only ran `swift build`). Added `Packaging/Info.plist`,
  `Packaging/FortressNotes.entitlements` (empty on purpose: no JIT, no
  library-validation or dyld exceptions, no Apple Events/camera/microphone; the comment
  in the file gives the reasons) and `Scripts/package_app.sh`: release build →
  `Fortress Notes.app` → catalog compiled in → `codesign --options runtime` (ad hoc by
  default, `SIGN_IDENTITY` for Developer ID with `--timestamp`) → verifies the runtime
  flag → notarizes and staples when `NOTARY_PROFILE` is set. CI runs it ("Package app").
- **O2 [M] sandbox decision documented — fail → fixed.** Above, and in the entitlements file.
- **O3 [M] secrets in the Keychain, never defaults — pass.** The backend token is made
  per launch in memory (`BackendAuth.token`), passed to the child in its environment and
  sent as a header; nothing secret is in `UserDefaults`/`@AppStorage`/`@SceneStorage`
  (those hold the backend URL, folder path, sort, column layout, the Quick Capture draft).
- **O4 [M] `os.Logger`, private by default, no `print` — fail → fixed (minor).** No
  `print`/`NSLog` in Sources; the one `Logger` logs a duration only. Its subsystem was
  "FortressNotes"; now `app.fortress.notes`, matching the signposts, so one filter shows both.
- **O5 [S] system panels — pass.** `.fileImporter` (folder, Markdown import),
  `.fileExporter` (Markdown export), `NSOpenPanel` (OneNote export folder).

## Performance (I3)

See the section below for before/after numbers and the keystroke investigation.
