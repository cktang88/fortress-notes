# Fortress Notes for macOS: design

The native SwiftUI app in `mac/` is the only Fortress client; the backend is
described in [../spec.md](../spec.md) and the block contract in
[../docs/block-model.md](../docs/block-model.md). This file holds the design
decisions and rules; the code is the reference for sizes and layout. The research
behind it (admired apps, Liquid Glass, editor alternatives, CI runners) is in git
history.

Target: **macOS 26** (`mac/Package.swift`), built with Xcode 26. The package has no
third-party dependencies.

## Principles

1. **The note is the hero.** Chrome recedes; the editor is a calm single column
   (about 68 characters wide, larger text than the UI, generous line height).
2. **Standard components first, glass only on the navigation layer.** Sidebars,
   toolbars, sheets and popovers get the system's Liquid Glass on their own; content
   (note list, editor, inspector) never has glass. The only custom glass is Quick
   Capture, the Ask composer and the undo toast (`Shell/FloatingGlass.swift`), which
   become opaque under Reduce Transparency.
3. **One accent, for state only**: the caret, links, checked boxes, the selected
   sidebar glyph, prominent buttons, citation chips. Never decoration.
4. **Keyboard-complete.** Every action is in the menu bar (frequent ones with a
   shortcut) and in context menus. Notes are found in one place, the search field;
   there is no command palette.
5. **Instant and local.** No spinners or skeletons for local work; progress appears
   only for network or model work, after 300 ms.
6. **Calm motion.** Short, critically damped animations (under 300 ms); Reduce Motion
   turns moves into fades.
7. **Least surprise.** The Mac's keys, menus, pasteboard and text system are never
   overridden. App shortcuts live in one table, `Shell/Shortcuts.swift`, which
   `MacConventionsTests` checks against the system's.

## Window

`NavigationSplitView` with sidebar, note list and editor, plus an inspector (hidden
by default, ⌥⌘I). ⌃⌘S hides the sidebar; Focus mode (⌃⌘↩) shows only the editor.
Each window has its own `AppModel` (selection, open note, sheets) over the shared
`Library`; menu commands act on the window in front and are disabled, never hidden,
when they don't apply.

- **Sidebar**: Library (All Notes, Recent), nested folders (collapsed by default),
  then Trash. No counts or badges. Notes and folders drag onto folders, notes onto
  the Trash; `.md` files dropped on a folder are imported there. Every drop has a
  menu alternative and is undoable.
- **Note list**: grouped by date like Apple Notes (View › Sort By, Group by Date);
  rows show title, two-line snippet and date · folder, with no separators.
- **Editor**: the title is its own field and follows the first line until the user
  types one; a meta line shows when it was edited and its folder. The toolbar has
  Export, a ⋯ menu, the inspector toggle and **Ask** beside the search field.
- **Inspector**: *Seen before* (passages like the block under the caret, after it
  settles), *Related notes*, *Backlinks*, *Info* (how the note came in, folder, dates,
  length, links) and *Text in pictures*.
- **Trash**: trashed notes open read-only with Restore and Delete; Empty Trash asks
  first. Every move to the Trash shows an Undo toast.
- **Empty states** use `ContentUnavailableView` and say what to do next.

## Search and Ask

- **One box, no modes** (`.searchable` in the toolbar, "Search or ask a question").
  Results (words, meaning and titles fused by the backend) replace the note list
  while there is a query; each row is a passage with its note and why it matched.
  ↓/↑ move, ↩ opens the highlighted row at that block, esc clears. Results always
  say which query they answer, so ↩ never opens a stale one.
- **Questions ask.** Text that reads as a question (ends with `?`, or starts with
  who/what/when/where/why/how/which/is/are/can/do/does/did/should/could/would/will)
  puts the *Ask your notes* row first, so ↩ asks; ⌘↩ always asks.
- **Ask** replaces the editor with an answer page: the question, a trace of what Ask
  did, the answer streamed sentence by sentence with numbered citation chips (click
  to open the note at that block), the sources, and a floating follow-up composer.
  Failures are inline messages, never alerts; without an OpenRouter key the page says
  how to add one.
- **Quick Capture** (⇧⌘Space, global, configurable in Settings; combinations macOS
  or the app use are refused): a non-activating floating panel. ↩ saves to
  *Uncategorized*, ⇧↩ adds a line, ⌘↩ saves and opens, esc puts it away. Nothing
  typed is lost: drafts survive esc and quit, and captures made while the backend is
  down wait on disk.

## Menus and shortcuts

Menus are in the standard order: App, File, Edit, Format, View, Note, Go, Window,
Help. The system command sets are kept (`SidebarCommands`, `TextEditingCommands`,
`ToolbarCommands`); `TextFormattingCommands` is replaced by our Format menu, whose
items send the editor's own actions.

- **File**: New Note, New Folder, New Window, Import ▸ Markdown Files… / From
  OneNote…, Export as Markdown…, Back Up Now, Restore from Backup…, Empty Trash…
- **Edit**: the system's (Undo, Find, Spelling, Substitutions, …) plus Search All
  Notes.
- **Format**: marks, paragraph styles, Mark as Checked, Indent/Outdent, Move Up/Down,
  **Block ▸** (Select Block, Insert Block…, Duplicate, Delete Block, Copy Link to
  Block, Turn into 2/3 Columns, Add Column, Remove Column, Unwrap Columns), Add Link…,
  Insert Table.
- **View**: Show Inspector, Focus Mode, Show Blocks, Sort By, Group by Date, Ask Your
  Notes…, Quick Capture, Reload Notes, then the system's toolbar and full-screen items.
- **Note**: Move To ▸, Open in New Window, Move to Trash.
- **Go**: All Notes, Recent, Trash.

The app's shortcuts (`Shortcuts.all`):

| Command | Shortcut | | Command | Shortcut |
|---|---|---|---|---|
| New Note | ⌘N | | Superscript | ⌃⌘= |
| New Folder | ⌥⌘N | | Subscript | ⌃⌘- |
| Import Markdown Files… | ⇧⌘I | | Add Link… | ⌘K |
| Export as Markdown… | ⇧⌘E | | Body | ⌥⌘0 |
| Search All Notes | ⌥⌘F | | Heading 1 / 2 / 3 | ⌥⌘1 / ⌥⌘2 / ⌥⌘3 |
| Show Inspector | ⌥⌘I | | Bulleted List | ⇧⌘7 |
| Enter Focus Mode | ⌃⌘↩ | | Numbered List | ⇧⌘8 |
| Ask Your Notes… | ⇧⌘A | | Checklist | ⇧⌘L |
| Reload Notes | ⌘R | | Quote | ⌥⌘' |
| Move to Trash (note list) | ⌘⌫ | | Code Block | ⌃⌥⌘C |
| All Notes / Recent / Trash | ⌘1 / ⌘2 / ⌘3 | | Mark as Checked | ⇧⌘U |
| Bold / Italic / Underline | ⌘B / ⌘I / ⌘U | | Indent / Outdent | ⌘] / ⌘[ |
| Strikethrough | ⇧⌘X | | Move Up / Move Down | ⌃⌘↑ / ⌃⌘↓ |
| Inline Code | ⌥⌘E | | Duplicate (blocks) | ⌘D |
| Highlight | ⇧⌘H | | Show Blocks | ⌥⌘B |

Plus Quick Capture (⇧⌘Space by default, global) and the system's own keys (⌘Z/⇧⌘Z,
⌘F/⌘G/⌘E, ⌘P, ⌥⇧⌘V, ⌃⌘S, ⌃⌘F, …). Choices forced by least surprise: Inline Code is
⌥⌘E because ⌘E is Use Selection for Find; Code Block is ⌃⌥⌘C because ⌥⌘C is Copy
Style; Insert Table has no key because ⌥⌘T is Show/Hide Toolbar; ⌘⌫ moves a note to
the Trash only while the list has focus (in text it deletes to the line start).

Inside surfaces: in a table cell Tab/⇧Tab move between cells (Tab in the last adds a
row); in the `/` menu ↑/↓ move, ↩ or ⇥ insert, esc closes and leaves the text; with
blocks selected, ↑/↓ choose a block, ⇧↑/⇧↓ extend, ⌥⇧↑/⌥⇧↓ move them, Tab/⇧Tab
indent, ⌫ deletes, ⌘C/⌘X copy and cut, ⌘D duplicates, ↩ or typing returns to the
caret, esc lets go. Word completion stays on ⌥Esc and F5; text navigation, Emacs
keys, Services, Look Up, dictation and text replacements are NSTextView's own.

## Editor

**One `NSTextView` on TextKit 2 per note**; each paragraph is a block, carrying its
block id, kind, indent and state as attributes. (Rejected: SwiftUI `TextEditor`, no
lists or attachments; one view per block, which breaks cross-block selection, IME,
Find and undo; STTextView, GPL; a web view, not native.) Never touch
`textView.layoutManager`: it silently drops to TextKit 1.

- **Saving.** Edits are diffed (`BlockDiff`) into one revision-checked transaction
  of `insert`/`update`/`move`/`delete` about 400 ms after typing stops; the whole
  note is never re-sent. Unsent edits are kept on disk until the server has them. A
  409 (changed elsewhere) stops saving and offers **Load Latest**.
- **Block identity.** ↩ makes a new block with a new id; joining keeps the upper
  block's id; indent maps to `parent_id`. Pasted content gets new ids.
- **Markdown shortcuts** at line start (`#`…`######`, `-`/`*`/`+`, `1.`/`1)`,
  `[ ]`/`[x]`, `>`) run after the edit, never during IME composition, and are their
  own undo step, so ⌘Z restores the typed characters.
- **Links.** `[[` opens the note picker; note links and block references show the
  target's current title or text (accent colour; block references underlined).
- **Pictures and files** paste or drop as uploads; pictures keep their stored size
  (no jump on load, at most 480 pt tall), files are chips. Text read from pictures
  shows in the inspector, never in the note.
- **Tables** are one attachment whose view is a grid of small text views (rich
  marks, links); the context menu adds and removes rows and columns. Nested blocks
  in cells (from OneNote) show read-only and are kept untouched.
- **Columns** host an embedded editor each, inside the measure; made with Turn Into,
  `/2 columns`, or by dragging a block beside another. A column left empty goes; one
  column left is unwrapped.
- A block type the app doesn't render (raw `html`) is shown read-only and saved
  untouched. There is no math or ink.
- **Printing** (⌘P) lays out a copy of the whole note in light colours.
- **Free system behaviour**: find bar, spelling and grammar, Writing Tools,
  Services, dictation, drag and drop, accessibility.

### Block tools

Nothing shows until the pointer is over a block.

- **Handle.** Hovering shows a ⠿ grip and a `+` in the left margin. Clicking the grip
  selects the block and opens its menu: **Turn Into** (Text, Heading 1–6, lists,
  Checklist, Quote, Callout, Code Block, Divider for an empty block, Table, 2/3
  Columns), Duplicate ⌘D, Delete ⌫, Copy Link to Block (pastes as a block reference;
  as text `[[Note#^id]]`), Move Up/Down, and the column actions inside columns.
- **Drag** the grip to move the block with its children: a line shows where, indented
  to the depth it will get; a vertical line beside a block makes columns; a box round
  a table cell drops into it. Dragged out of the app, blocks are Markdown text.
- **`+`** adds a block below with `/` typed, opening the insert menu.
- **`/` menu** at the start of a block or after a space: block kinds filtered as you
  type (`h2`, `todo`, `col`, `table 4x5`); Image… and File… upload, Note Link types `[[`.
- **Esc** selects the blocks the caret is in (an accent-tinted highlight, no caret).
- **Show Blocks** (⌥⌘B) draws faint outlines and grips on every block.
- **Accessibility.** The grip is a button named for its block with Move Up/Down,
  Duplicate and Delete as actions; everything has a keyboard path (Esc, Format ▸ Block).

### Undo

- One undo stack per note (`FortressTextView.noteUndo`), shared by the note's text,
  its columns and its table cells, and separate from the window's stack, where
  library actions (Move to Trash, Restore, moves, New/Rename Folder) are undone. ⌘Z
  in the editor undoes the note; in the list, the library.
- Every editor command is one named step (`UndoNames`: "Undo Move", "Undo Heading
  2", …); typing in progress ends first. NSTextView names typing, Paste and Cut.
- Undo and redo are ordinary edits and save like any other (there is no server
  undo). Only loading a note (open, Load Latest) clears its stack; each window keeps
  the last 8 notes' editors, so switching back keeps their stacks.

### Pasteboard

Copy puts the blocks (for pasting between notes), Markdown, RTF and plain text on
the pasteboard. Paste takes blocks, rich text (marks the editor knows are kept),
plain text, pictures and files (uploaded as blocks). Paste and Match Style pastes
plain text.

## Theming

- Content colours come from **one semantic palette** (`App/Theme/Palette.swift`):
  background, sunken, surface, border, three text levels, accent, link, selection,
  highlight, code, quote bar and ten named mark hues. Chrome stays native and takes
  only the accent through `.tint`. Views never use hex values.
- Defaults, designed in OKLCH: **Paper** (light, warm cream `#FBF8F1`) and **Night**
  (dark blue-grey `#13151C`, not black). Accent "Fortress Ember" (`#AA4C23` /
  `#F09159`) when the system accent is Multicolor, else the user's accent made
  legible.
- **Contrast is enforced** (`Palette.legible`, `ThemeTests`): body text ≥ 7:1, every
  other text colour ≥ 4.5:1 on its backgrounds, more under Increase Contrast. A
  failing colour keeps its hue and only moves in lightness. Imported text and cell
  colours (OneNote) are adjusted the same way.
- **Themes** (Settings › Appearance): System/Light/Dark, plus a light and a dark
  theme chosen from Base16/Base24 schemes (nine bundled) or VS Code colour themes
  imported as files. There is no Fortress theme format.
- Type is SF Pro and SF Mono; icons are SF Symbols, icons-only in the toolbar with
  accessibility labels.

## Settings

Three tabs: **General** (backend status, address, folder, start automatically, its
command, Start/Stop, Show Log; Apple Vision text reading; the Quick Capture
shortcut; the OneNote converter and Import from OneNote…), **Appearance** and
**Backups** (snapshots with Back Up Now and Restore; restoring saves the current
notes first).

## Tests and CI

`swift test` runs the unit tests; `.github/workflows/mac.yml` also renders window
snapshots (artifact `snapshots`, `SNAPSHOT_DIR`), runs the editor and finding
performance suites (`EditorPerformanceTests`, `FINDING_PERF=1`) and integration tests
against a real backend seeded by `mac/Scripts/seed_backend.py`
(`FORTRESS_INTEGRATION_URL`).
