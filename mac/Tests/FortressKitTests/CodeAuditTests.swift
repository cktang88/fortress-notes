import AppKit
import Testing

@testable import FortressKit

// One test per fix from the code audit (mac/CODE_AUDIT.md).

private func note(_ id: String, updated: String) -> NavigationDocument {
    NavigationDocument(id: id, title: id, status: .rough, updatedAt: updated, createdAt: updated, snippet: "",
                       folderId: nil, position: 0)
}

@MainActor
@Suite struct CodeAuditTests {
    // L3: the note list sorts and groups when its inputs change, not on every render
    // (a selection change re-renders the list).
    @Test func noteListIsRemadeOnlyWhenItsInputsChange() {
        let navigation = NavigationResponse(items: [
            .document(note("a", updated: "2026-11-18T10:00:00+00:00")),
            .document(note("b", updated: "2026-11-17T10:00:00+00:00")),
        ], recent: [])
        let now = Timestamp.date("2026-11-18T12:00:00+00:00")!
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone(identifier: "UTC")!
        let cache = NoteListCache()
        let first = cache.list(scope: .all, navigation: navigation, sort: .edited, grouped: true, now: now, calendar: utc)
        #expect(first.orderedIds == ["a", "b"])
        #expect(first.sections.map(\.title) == ["Today", "Yesterday"])
        _ = cache.list(scope: .all, navigation: navigation, sort: .edited, grouped: true, now: now.addingTimeInterval(60), calendar: utc)
        #expect(cache.builds == 1)
        var changed = navigation
        changed.items.append(.document(note("c", updated: "2026-11-18T11:00:00+00:00")))
        let second = cache.list(scope: .all, navigation: changed, sort: .edited, grouped: true, now: now, calendar: utc)
        #expect(cache.builds == 2)
        #expect(second.orderedIds == ["c", "a", "b"])
        // The next day regroups ("Today" becomes "Yesterday") with the same notes.
        let tomorrow = cache.list(scope: .all, navigation: changed, sort: .edited, grouped: true, now: now.addingTimeInterval(86_400), calendar: utc)
        #expect(cache.builds == 3)
        #expect(tomorrow.sections.first?.title == "Yesterday")
    }

    // N2: sentences with a date are one localizable string, not concatenations.
    @Test func relativeDatesAreOneSentence() {
        let now = Date()
        #expect(NoteEditorView.edited(now.addingTimeInterval(-20), now: now) == "Edited just now")
        let edited = NoteEditorView.edited(now.addingTimeInterval(-300), now: now)
        #expect(edited.hasPrefix("Edited ") && edited.contains("5"))
        let stamp = Timestamp.serverString(now.addingTimeInterval(-3 * 86_400))
        #expect(NoteListing.deletedLabel(stamp, now: now).hasPrefix("Deleted "))
        #expect(NoteListing.deletedLabel("not a date", now: now) == "Deleted")
    }

    // I4: smart quotes and dashes follow System Settings instead of being forced on.
    @Test func substitutionsFollowTheSystemSetting() {
        let view = FortressTextView.make()
        #expect(view.isAutomaticQuoteSubstitutionEnabled == NSSpellChecker.isAutomaticQuoteSubstitutionEnabled)
        #expect(view.isAutomaticDashSubstitutionEnabled == NSSpellChecker.isAutomaticDashSubstitutionEnabled)
        let cell = CellTextView.make()
        #expect(cell.isAutomaticQuoteSubstitutionEnabled == NSSpellChecker.isAutomaticQuoteSubstitutionEnabled)
    }

    // I1: a note with tables, columns and pictures stays on TextKit 2 everywhere (touching
    // `layoutManager` or inserting an NSTextTable would drop a view to TextKit 1).
    @Test func richNoteStaysOnTextKit2() throws {
        let editor = PerfEditor(document: LongNote.document(blocks: 300))
        editor.open()
        editor.settle()
        let tableRange = editor.range(ofBlock: "t0")
        editor.controller.scrollRangeToVisibleForTests(tableRange)
        editor.layoutViewport()
        let table = try #require((editor.controller.storage.attribute(.attachment, at: tableRange.location, effectiveRange: nil)
            as? BlockAttachment)?.live as? TableBlockController)
        _ = table.height(forWidth: 600)
        let cell = try #require(table.editor(row: 1, column: 1))
        let columnsRange = editor.range(ofBlock: "k0")
        editor.controller.scrollRangeToVisibleForTests(columnsRange)
        editor.layoutViewport()
        var textViews: [NSTextView] = [cell]
        func collect(_ view: NSView) {
            if let textView = view as? NSTextView { textViews.append(textView) }
            view.subviews.forEach(collect)
        }
        collect(editor.controller.scrollView)
        #expect(textViews.count > 2, "the note, a cell and at least one column")
        for textView in textViews {
            #expect(textView.textLayoutManager != nil, "\(type(of: textView)) fell back to TextKit 1")
        }
    }

    // A1: the pending-edits folder is behind a lock, not a `nonisolated(unsafe)` global.
    @Test func pendingEditsDirectoryRoundTrips() {
        let original = PendingEdits.directory
        defer { PendingEdits.directory = original }
        let other = FileManager.default.temporaryDirectory.appending(path: "audit-pending", directoryHint: .isDirectory)
        PendingEdits.directory = other
        #expect(PendingEdits.url(for: "doc/1").deletingLastPathComponent().path == other.path)
    }
}
