# Fortress Notes (mac/): undo and redo audit

Audited 19 Nov 2026 on branch `undo-stack`. The requirement: **⌘Z keeps undoing, ⇧⌘Z keeps redoing, as in TextEdit, Notes and Pages.** Each note has one unbounded stack. Every edit is one step on it, grouped as NSTextView groups typing. Edit ▸ Undo and Redo name the step ("Undo Paste", "Undo Typing", "Redo Move"). Loading and saving add no steps and remove none.

Evidence: code under `mac/Sources/FortressKit/Editor/` and the tests in `mac/Tests/FortressKitTests/UndoStackTests.swift`. Those tests drive the editor in a real window. They type, paste through the general pasteboard and run menu commands. They send `undo:` and `redo:` up the responder chain from the window's first responder, as the menu does, and read the menu item's title from the responder that validates it. The integration job (`IntegrationTests.undoAndRedoSaveWhatTheEditorShows`) checks the saved note against a real backend after every undo and every redo.

"Before" is `main` at 6aa7aad. It was measured by a probe test run in CI before any fix (run 44441335908), and by reading the code. "After" is this branch.

## Root causes

1. **Edit ▸ Undo never reached the note's stack.** The note's text views override `undoManager` to return the note's own `UndoManager`. But the menu's `undo:`/`redo:` actions travel up the responder chain: `FortressTextView → NSClipView → NSScrollView → NSWindow`. NSWindow answers them with *its* undo manager, which is SwiftUI's window stack where the list's Move to Trash and moves live. The probe showed it: the window validated Undo as disabled and titled it plain "Undo", while the note's own manager said "Undo Typing", and sending `undo:` changed nothing. So ⌘Z in a note did not undo the note. The earlier tests called `textView.undoManager?.undo()` directly, so they never caught it.
2. **The editor's own edits were unnamed, and some merged into typing.** `replace` and `changeAttributes` register through `shouldChangeText`, which names nothing. These carry every structural edit: Return, Backspace across blocks, Markdown shortcuts, Bold, paragraph styles, moves, Insert Table, links and checkboxes. The probe showed '' as the action name for a Return split, a block merge, a list conversion, Bold and setKind. The menu said only "Undo". Return and merges could also join the typing step around them.
3. **Table cell edits lived on throwaway text views.** Each cell is edited by a temporary `CellTextView`, and its undo steps pointed at that view's storage. A shape change (Insert Row/Column, Tab past the last cell) rebuilds the editors. After one, ⌘Z found the step and did nothing: in the probe, the cell kept "cell" after "Undo Typing". An editor taken out of the view tree also lost its manager. `noteUndoManager` walked up superviews and fell back to the window's stack.
4. **A columns block cleared the note's whole stack.** A column's editor is a `BlockEditorController` that shares the note's stack. Its `load` called `undoManager.removeAllActions()`. The columns are built lazily, when the block first scrolls into view or is entered, or comes back by undo. That wiped every step in the note.
5. **The editor was rebuilt on every note switch.** `NoteEditorView` is `.id(noteId)`, so going to another note destroyed the text view, and with it the note's `UndoManager`. Coming back always started with an empty stack.
6. **CRLF pastes changed length under NSTextView's undo step.** Normalizing turned "\r\n" into "\n" inside the edit. NSTextView's step still covered the pasted length. Undo removed one character too many, which normalizing happened to repair. Redo then added an empty paragraph (probe: `threea | b | ''`).
7. **Title and status were not on the note's stack.** The title field's typing undoes on the window's stack and is dropped when editing ends. Status changes had no undo at all.
8. **Undo and redo didn't always save.** NSTextView's undo of its own steps (typing, paste, the editor's replacements) edits the storage without always calling the delegate's `textDidChange`. The undone text was shown but never sent to the server. The integration test caught it: after each undo, the server was one step behind.
9. **Typing inside a paragraph moved the caret past it.** Normalizing restyled the edited paragraph on every keystroke. It set attributes over the whole paragraph even when they were already equal, and TextKit 2 then put the selection after the paragraph. Typing "X" inside "abc" left the caret at the start of the next line, and the next key landed there. This was confirmed on screen through a key event (`CaretTests`). It also meant a Markdown shortcut typed before existing text (`# ` at the start of a line) was never seen, because the shortcut reads the caret. Undo steps came out fragmented as well.

## Fixes

- **One stack per note, reachable from the menu** (`FortressTextView`, `CellTextView`, `NoteUndoActions`). The note's text view, every column's text view and every cell editor answer `undo:` and `redo:` before the window does. They undo on the note's stack, and `validateMenuItem` names the items from it. When focus is in the note list, the window still answers, with the library stack. The stack is found through the editor's `AttachmentContext.undoManager`, not by walking views, so a view TextKit has taken out of the window still finds it. If undo removes the view that had focus, such as a table inserted while you typed in its cell, focus goes back to the note's text. Otherwise the next ⌘Z would go to the window.
- **Named, separate steps for the editor's commands** (`BlockEditorController.undoStep`, `UndoNames`). Each command runs as one step. Typing in progress ends before it changes anything, and the step is named after the command: Return, Tab and Backspace are "Typing", and the others carry their own names ("Bold", "Bulleted List", "Move", "Insert Table", "Link", "Checkbox", "Indent", "Paste"…). A command that changes nothing, such as Backspace inside a line handled by NSTextView, doesn't interrupt typing. NSTextView still groups and names typing, Paste, Cut and spelling, exactly as in TextEdit.
- **Cell edits undone on the table** (`TableBlockController.registerCellUndo`). Cell editors no longer keep undo steps (`allowsUndo = false`). Before a cell's text changes, the table registers one step that restores the table as it is now, and puts the caret back where it was in that cell. Typing joins one step, as NSTextView joins it: same cell, same direction, caret not moved, and nothing else registered since (`NSUndoManagerDidOpenUndoGroup`). Paste, Cut, Delete and marks are steps of their own. Shape changes register on the same stack, so a row added between two edits undoes in order.
- **Loading never touches a note's stack from inside.** Only the note's own `load` (opening a note, Load Latest after a conflict) starts the history over. A column's `load` doesn't.
- **Each note keeps its editor while the window shows others** (`NoteEditorCache`, per window in `AppModel.noteEditors`). The last 8 notes keep their editor and session. Going back puts the same text view in place, with its stack, caret and scroll position. The note is checked against the server first (`EditorSession.refreshIfChangedElsewhere`). If it changed elsewhere meanwhile, the new version is shown and the history starts over, because the old steps can't apply to text that isn't there. A window's notes, and their stacks, close with the window, as documents do in other apps.
- **Pastes with foreign line ends are pasted by the editor** (`pasteConvertingSeparators`). Text holding "\r" or U+2029 is converted before it goes in, as one "Paste" step, so normalizing no longer changes the length.
- **Undo and redo are saved** (`BlockEditorController.textStorage(_:didProcessEditing:)`). A storage edit made while the note's stack is undoing or redoing is reported as a change once per run loop turn: it is saved, and list numbers below it are redrawn.
- **Restyling leaves runs alone when they are already styled** (`EditorTheme.isStyled`). Only attributes that actually change are set, so the edit TextKit sees is the typed character and the caret stays after it. A side benefit: typing no longer re-attributes the whole paragraph.
- **Cell text views have no undo manager of their own.** NSTextView registered its typing steps on a cell editor's `undoManager` even with `allowsUndo` off. Undoing one emptied the cell before the table's step could record what to redo. The cell's `undoManager` is now nil. Its `undo:`/`redo:` and menu validation use the note's stack (`noteStack`).
- **Title and status are steps on the note's stack** ("Undo Title Change", "Undo Status Change"). Title typing joins one step until the title is saved. Undo restores and saves the old title, including handing it back to the first line if it was automatic.

## Audit table

✅ means undoable step by step, redoable, correctly grouped and named. Each row says what changed.

| Operation | Before (main) | After (this branch) | Test |
|---|---|---|---|
| ⌘Z / ⇧⌘Z from the Edit menu or keyboard, in the note | ❌ went to the window's stack (library actions); the note's never reached | ✅ the note's stack, named | all `UndoStackTests` (menu path) |
| Typing | step named "Typing" on the note's stack, but unreachable from the menu | ✅ NSTextView's grouping: one step until the caret moves, Return, or another command; words don't split (NSTextView default) | `typingStepsBreakAtCaretMovesAndReturn` |
| Typing pause | NSTextView doesn't break on a pause | same (NSTextView default, as TextEdit) | — |
| Typing inside a paragraph | ❌ caret moved past the paragraph after each key | ✅ caret stays; one step | `CaretTests`, `loadingTheRestOfALongNote…`, `columnEdits…` |
| Undo/redo of anything, saved | ❌ often not saved (no `textDidChange`) | ✅ saved after each undo and redo | `IntegrationTests.undoAndRedoSaveWhatTheEditorShows` |
| Return splitting a block | separate step, unnamed ("Undo") | ✅ "Typing", its own step | `everyEdit…`, `typingSteps…` |
| Backspace/Delete merging blocks, removing a divider/image | unnamed, could join typing | ✅ "Typing", its own step; the block comes back with its id | `structuralKeys…`, `pastedPictures…` |
| Paste (plain, rich, Markdown text, several paragraphs) | NSTextView "Paste" (menu unreachable) | ✅ "Paste", one step per paste; new blocks keep their ids on redo | `everyEdit…` |
| Paste with "\r\n" / U+2029 (Word, Outlook) | ❌ redo added an empty paragraph | ✅ exact undo and redo | `pasteWithWindowsLineEnds…` |
| Paste and Match Style | NSTextView | ✅ "Paste" / NSTextView's name | `pasteAndMatchStyleAndCut` |
| Paste of the note's own blocks | unnamed | ✅ "Paste" | — (same step code) |
| Pasted or dropped pictures and files | unnamed, one step per upload | ✅ "Paste" / "Drop", one step per block as each upload finishes | `pastedPictures…` |
| Paste into a table cell | step on a throwaway editor, lost after a shape change | ✅ "Paste" on the note's stack | (cell step code; `cellEdits…` covers typing and marks) |
| Cut | NSTextView "Cut" | ✅ "Cut" | `pasteAndMatchStyleAndCut` |
| Markdown shortcuts (`- `, `1. `, `# `, `> `, `[ ]`, ```` ``` ````, `---`) | own step, unnamed; ❌ not applied when typed before existing text (caret bug) | ✅ own step named after the style ("Bulleted List", "Heading 1", "Code Block", "Divider"); the next ⌘Z takes back the typed characters | `everyEdit…`, `structuralKeys…` |
| Bold, italic, underline, strike, inline code, sup/sub | unnamed | ✅ "Bold", "Italic"… | `everyEdit…`, `cellEdits…` |
| Highlight, text color, links (⌘K add/edit/remove) | unnamed | ✅ "Highlight", "Text Color", "Link", "Remove Link" | — (same step code) |
| Paragraph styles (menu, ⌥⌘1…) | unnamed | ✅ named after the style | `everyEdit…` (Checklist), `columnEdits…` |
| Checkbox toggle (click, ⇧⌘U) | unnamed | ✅ "Checkbox" | `everyEdit…` |
| Indent / outdent (Tab, ⇧Tab, ⌘], ⌘[) | unnamed | ✅ "Indent" / "Outdent" | `everyEdit…` |
| Move block up/down (⌃⌘↑/↓) | unnamed | ✅ "Move" | `everyEdit…` |
| Drag of text within the note | NSTextView | NSTextView (unchanged) | — |
| Insert table | unnamed | ✅ "Insert Table"; undoing it while typing in its cell keeps ⌘Z in the note | `everyEdit…`, `undoingATable…` |
| Insert/delete row or column, header row | named, but cell edits around it were lost | ✅ named, in order with cell edits | `everyEdit…`, `cellEdits…` |
| Typing in table cells | ❌ lost after a shape change; stack fell back to the window's when the editor was out of the view tree | ✅ "Typing", grouped as NSTextView groups it, restored with the caret | `cellEdits…` |
| Typing in columns | ❌ building a columns block cleared the note's stack | ✅ same stack, in order with the note's text | `columnEdits…` |
| `[[` note link insertion | own step, unnamed | ✅ "Note Link" | — (same step code) |
| Block handle and Format ▸ Block: Turn Into, Duplicate (⌘D), Delete, Move Up/Down (with children), columns (turn into, add, remove, unwrap) | — (new) | ✅ one step each, named ("Heading 2", "Callout", "Duplicate", "Delete", "Move", "Columns", "Add Column", "Remove Column", "Unwrap Columns") | `BlockToolsTests` |
| Dragging blocks by the handle (between blocks, nested, into/out of columns, beside a block, into a cell) | — (new) | ✅ "Move", one step however many editors it touches (columns blocks are rewritten in the note's text) | `draggingMoves…`, `draggingBeside…`, `draggingTextIntoATableCell` |
| Block selection (Esc) keys: ⌥⇧↑/↓, Tab/⇧Tab, ⌫, ⌘X, ⌘D | — (new) | ✅ "Move", "Indent"/"Outdent", "Delete", "Cut", "Duplicate" | `escSelectsBlocks…`, `copyingSelectedBlocks…` |
| `/` menu, `+` | — (new) | ✅ the typed `/…` is typing; the insert is its own step named after it; `+` is "Insert Block" | `slashMenuInserts…`, `plusAdds…` |
| Paste of a copied block link | — (new) | ✅ "Paste" (a block reference) | `aCopiedBlockLinkPastesAsABlockReference` |
| Find & Replace, Replace All | NSTextView (Replace All is one step) | unchanged; now reachable from the menu | — |
| Writing Tools, spelling and autocorrect | NSTextView | unchanged; reachable from the menu | — |
| Title edits | window's stack while editing, dropped after | ✅ "Title Change" on the note's stack | `titleAndStatus…` |
| Status changes | ❌ not undoable | ✅ "Status Change" | `titleAndStatus…` |
| Saves (debounced block transactions, revision updates) | never touch the text or the stack | same | integration test |
| Background load of a long note (final append) | adds no step, removes none | same, now tested | `loadingTheRestOfALongNote…` |
| Load Latest after a conflict | clears the stack (the text is replaced) | same, by design: the old steps can't apply | — |
| Switching notes and back | ❌ stack lost (editor rebuilt) | ✅ editor and stack kept (last 8 notes per window); a note changed elsewhere reloads and starts over | `eachNoteKeepsItsStack…`, `theCacheKeeps…` |
| Focus moving between note, cells, columns | one stack while views were in the window; fell back when they weren't | ✅ one stack via the editor's context | `cellEdits…`, `columnEdits…` |
| Window close | stack gone | stack gone with the window's editors (as documents close) | — |
| Undo/redo saved to the server | not tested | ✅ the diff-based save sends exactly what is shown after each undo and redo, block ids included | `IntegrationTests.undoAndRedoSaveWhatTheEditorShows` |

The server's `/undo` and `/redo` endpoints stay unused. Undo is a local edit like any other, and saving it is an ordinary diff transaction, so it works offline and through conflicts the same way.

## Notes and limits

- **Performance note on root cause 9.** Before the caret fix, `keystroke.long50k` (typing inside a 50,000-character paragraph) only typed its first key there. That key moved the caret past the paragraph, so the other 19 keys landed in the short paragraph after it, and main reported a 4–10 ms median. Now every key really lands inside the long paragraph, and the median is about 25 ms on CI (budget 8 ms; not enforced). The cost was always there for real typing, and the old number hid it. `keystroke.afterLong50k` reproduces what the old test measured: 4.6 ms, the same as main. The other editor numbers stay within CI noise of main: keystroke.middle 3.1 ms vs 5.5 ms, Return 3.2 vs 3.6, table cell 0.52 vs 0.49, firstPaint 34 vs 46 ms, paste 5 MB 219 vs 225 ms. Typing inside a very long paragraph is worth a separate look. That look (block-tools branch): the editor's own work per key there is about 1.1 ms (normalize 0.5, restyle 0.3, callbacks 0.5). The rest is TextKit 2 laying out the whole paragraph again, because it makes one layout fragment per paragraph. A bare TextKit 2 NSTextView with no Fortress code takes about 15 ms for the same relayout (`keystroke.long50k.part.bareTextKitRelayout`). Serving the paragraph in chunks through `NSTextContentManagerDelegate` did not split the fragment. The fix is structural: store a long block as several storage paragraphs ("soft paragraphs") that read back as one block. That fix is in (`Editor/Text/SoftBreaks.swift`): in a paragraph block longer than 3,000 characters, a space every ~1,500 characters (after a sentence where there is one) is stored as U+2029 marked `.fnSoft`, so TextKit 2 lays out one ~1,500-character paragraph per key; block ranges step over it and reading the text back, copying and saving see the space. `keystroke.long50k` went from 22.3 ms to 4.8 ms on CI (a bare TextKit 2 key in the same text: 14.1 ms as one paragraph, 0.5 ms as soft paragraphs); the other editor numbers stay within CI noise. What shows: such a paragraph's line ends early about every 1,500 characters. Find doesn't match a phrase across that one space (it sees the U+2029), and Show Blocks outlines each soft paragraph.

- Words don't split typing steps. NSTextView doesn't split on word boundaries or pauses, and TextEdit behaves the same. Return and caret moves do split steps.
- A picture paste uploads first. Each block becomes its own "Paste" step when its upload finishes.
- Load Latest, and a note that changed elsewhere while the window showed another, start the history over. Their text is replaced wholesale.
