import Foundation
import Testing

@testable import FortressKit

private var utc: Calendar {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(identifier: "UTC")!
    calendar.locale = Locale(identifier: "en_US_POSIX")
    return calendar
}

private func date(_ string: String) -> Date { Timestamp.date(string)! }

private func doc(_ id: String, _ title: String = "", updated: String, created: String = "2026-01-01T00:00:00+00:00", folder: String? = nil, snippet: String = "") -> NavigationDocument {
    NavigationDocument(id: id, title: title, updatedAt: updated, createdAt: created, snippet: snippet, folderId: folder, position: 0)
}

// MARK: Date sections

@Test func dateBucketsFollowNotes() {
    let now = date("2026-11-18T15:00:00+00:00")
    let cases: [(String, String)] = [
        ("2026-11-18T00:00:01+00:00", "Today"),
        ("2026-11-17T23:59:00+00:00", "Yesterday"),
        ("2026-11-17T00:00:00+00:00", "Yesterday"),
        ("2026-11-12T10:00:00+00:00", "Previous 7 Days"),
        ("2026-11-11T00:00:00+00:00", "Previous 7 Days"),
        ("2026-10-25T00:00:00+00:00", "Previous 30 Days"),
        ("2026-10-10T00:00:00+00:00", "October"),
        ("2026-02-01T00:00:00+00:00", "February"),
        ("2025-12-31T00:00:00+00:00", "2025"),
    ]
    for (stamp, expected) in cases {
        #expect(NoteListing.dateBucket(for: date(stamp), now: now, calendar: utc) == expected, "\(stamp)")
    }
}

@Test func sectionsAreContiguousAndNewestFirst() {
    let now = date("2026-11-18T15:00:00+00:00")
    let notes = [
        doc("old", updated: "2026-03-02T00:00:00+00:00"),
        doc("today", updated: "2026-11-18T09:00:00+00:00"),
        doc("week", updated: "2026-11-14T09:00:00+00:00"),
        doc("today2", updated: "2026-11-18T12:00:00.123456+00:00"),
    ]
    let sections = NoteListing.sections(notes, sort: .edited, grouped: true, now: now, calendar: utc)
    #expect(sections.map(\.title) == ["Today", "Previous 7 Days", "March"])
    #expect(sections[0].notes.map(\.id) == ["today2", "today"])

    let flat = NoteListing.sections(notes, sort: .edited, grouped: false, now: now, calendar: utc)
    #expect(flat.count == 1 && flat[0].title.isEmpty && flat[0].notes.count == 4)
    #expect(NoteListing.sections([], sort: .edited, grouped: false).isEmpty)
}

@Test func sortsByCreatedAndTitle() {
    let notes = [
        doc("a", "banana", updated: "2026-11-18T00:00:00+00:00", created: "2026-01-02T00:00:00+00:00"),
        doc("b", "Apple", updated: "2026-11-17T00:00:00+00:00", created: "2026-01-03T00:00:00+00:00"),
        doc("c", "", updated: "2026-11-16T00:00:00+00:00", created: "2026-01-01T00:00:00+00:00"),
        doc("d", "note 10", updated: "2026-11-16T00:00:00+00:00"),
        doc("e", "note 9", updated: "2026-11-16T00:00:00+00:00"),
    ]
    #expect(NoteListing.sorted(notes, by: .created).prefix(3).map(\.id) == ["b", "a", "c"])
    #expect(NoteListing.sorted(notes, by: .title).map(\.id) == ["b", "a", "c", "e", "d"])
    // Title order never groups by date.
    #expect(NoteListing.sections(notes, sort: .title, grouped: true).map(\.title) == [""])
}

// MARK: Scopes and folders

@Test @MainActor func scopesFilterTheTree() throws {
    let navigation = try JSONDecoder().decode(NavigationResponse.self, from: fixtureData("navigation.json"))
    #expect(NoteListing.notes(in: .all, navigation: navigation).count == 3)
    #expect(NoteListing.notes(in: .recent, navigation: navigation).count == navigation.recent.count)
    #expect(NoteListing.notes(in: .trash, navigation: navigation).isEmpty)
    let travel = try #require(NoteListing.folders(in: navigation.items).first { $0.folder.name == "Travel" })
    #expect(NoteListing.notes(in: .folder(id: travel.folder.id), navigation: navigation).map(\.title) == ["Kyoto trip"])

    let library = ShellFixtures.navigation
    let tree = NoteListing.folderTree(library.items)
    #expect(tree.map(\.name) == ["Inbox", "Work", "Travel", "Reading", "Home"])
    #expect(tree[1].subfolders?.map(\.name) == ["Meetings"])
    #expect(tree[0].subfolders == nil)
    #expect(NoteListing.folders(in: library.items).first { $0.folder.id == "meetings" }?.depth == 1)
    #expect(NoteListing.isFolder("meetings", inside: "work", items: library.items))
    #expect(!NoteListing.isFolder("travel", inside: "work", items: library.items))
    #expect(NoteListing.folderNames(library.items)["reading"] == "Reading")
}

private func fixtureData(_ name: String) throws -> Data {
    let url = try #require(Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}

// MARK: Row text

@Test func previewDropsMarkdownAndRepeatedTitles() {
    #expect(NoteListing.preview(title: "Kyoto trip", snippet: "# Plan") == "Plan")
    #expect(NoteListing.preview(title: "Call the dentist", snippet: "Call the dentist") == "")
    #expect(NoteListing.preview(title: "x", snippet: "- [ ] buy **milk**") == "buy milk")
    #expect(NoteListing.preview(title: "x", snippet: "12. twelfth") == "twelfth")
    #expect(NoteListing.preview(title: "x", snippet: "3.14 is pi") == "3.14 is pi")
    #expect(NoteListing.preview(title: "x", snippet: "line one\nline two") == "line one line two")
    #expect(NoteListing.displayTitle("  ") == "New Note")
}

@Test func listDatesAreShortAndRelative() {
    let now = date("2026-11-18T15:00:00+00:00")  // a Wednesday
    #expect(NoteListing.listDate(date("2026-11-18T09:05:00+00:00"), now: now, calendar: utc).contains("9:05"))
    #expect(NoteListing.listDate(date("2026-11-17T09:05:00+00:00"), now: now, calendar: utc) == "Yesterday")
    #expect(NoteListing.listDate(date("2026-11-14T09:05:00+00:00"), now: now, calendar: utc) == "Saturday")
    #expect(NoteListing.listDate(date("2026-10-03T09:05:00+00:00"), now: now, calendar: utc).contains("10/3"))
}

// MARK: Model updates

@Test @MainActor func editorChangesPatchTheSidebarCopy() throws {
    let model = ShellFixtures.model()
    let document = BlockDocument(
        id: "n1", revision: 3, titleAuto: false, title: "Kyoto, revised",
        createdAt: "2026-01-01T00:00:00+00:00", updatedAt: "2026-11-18T10:00:00+00:00", children: [])
    model.noteDidChange(document)
    let note = try #require(model.openNote)
    #expect(note.title == "Kyoto, revised")
    #expect(model.navigation.recent.first { $0.id == "n1" }?.title == "Kyoto, revised")
    #expect(model.windowTitle == "Kyoto, revised")
    #expect(model.windowSubtitle == "Travel")
}

@Test @MainActor func windowTitleFollowsTheRoute() {
    let model = ShellFixtures.model(scope: .folder(id: "work"), openNote: nil)
    #expect(model.windowTitle == "Work")
    model.route = .ask(question: "why?")
    #expect(model.windowTitle == "Ask")
    model.select(.recent)
    #expect(model.scopeTitle == "Recent")
}

// MARK: Backend launch

@Test func launchPrefersTheProjectVirtualenv() throws {
    let files: Set<String> = ["/repo/backend/app/main.py", "/repo/backend/.venv/bin/uvicorn", "/Users/me/.local/bin/uv"]
    let launch = try #require(BackendLaunch.make(folder: URL(fileURLWithPath: "/repo"), port: 8000, home: "/Users/me", fileExists: files.contains))
    #expect(launch.executable.path == "/repo/backend/.venv/bin/uvicorn")
    #expect(launch.arguments == ["app.main:app", "--port", "8000"])
    #expect(launch.workingDirectory.path == "/repo/backend")
    #expect(launch.path.hasPrefix("/Users/me/.local/bin:"))
}

@Test func launchFallsBackToUvRun() throws {
    let files: Set<String> = ["/repo/backend/app/main.py", "/opt/homebrew/bin/uv"]
    // The backend folder itself works as well as the repository.
    let launch = try #require(BackendLaunch.make(folder: URL(fileURLWithPath: "/repo/backend"), port: 8123, home: "/Users/me", fileExists: files.contains))
    #expect(launch.executable.path == "/opt/homebrew/bin/uv")
    #expect(launch.arguments == ["run", "--frozen", "uvicorn", "app.main:app", "--port", "8123"])
    #expect(launch.displayCommand == "uv run --frozen uvicorn app.main:app --port 8123")

    let bare = try #require(BackendLaunch.make(folder: URL(fileURLWithPath: "/repo"), port: 8000, home: "/h", fileExists: { $0 == "/repo/backend/app/main.py" }))
    #expect(bare.executable.path == "/usr/bin/env")
    #expect(bare.arguments.prefix(4) == ["uv", "run", "--frozen", "uvicorn"])
    #expect(bare.displayCommand == "uv run --frozen uvicorn app.main:app --port 8000")

    #expect(BackendLaunch.make(folder: URL(fileURLWithPath: "/elsewhere"), port: 8000, home: "/h", fileExists: { _ in false }) == nil)
}

@Test @MainActor func missingUvIsSaidInWords() throws {
    let bare = try #require(BackendLaunch.make(folder: URL(fileURLWithPath: "/repo"), port: 8000, home: "/h", fileExists: { $0 == "/repo/backend/app/main.py" }))
    #expect(!bare.canFindExecutable { _ in false })
    #expect(bare.canFindExecutable { $0 == "/usr/bin/uv" })
    let venv = try #require(BackendLaunch.make(
        folder: URL(fileURLWithPath: "/repo"), port: 8000, home: "/h",
        fileExists: { ["/repo/backend/app/main.py", "/repo/backend/.venv/bin/uvicorn"].contains($0) }))
    #expect(venv.canFindExecutable { _ in false })
    // `env` exits 127 when it can't find uv: never just "exit code 127".
    #expect(BackendController.exitMessage(status: 127).contains("uv"))
    #expect(BackendController.exitMessage(status: 1).contains("Show Log"))
}

@Test func findsTheRepositoryAboveTheBuiltApp() {
    let found = BackendLaunch.repository(
        containing: URL(fileURLWithPath: "/src/fortress/mac/.build/debug"),
        fileExists: { $0 == "/src/fortress/backend/app/main.py" })
    #expect(found?.path == "/src/fortress")
    #expect(BackendLaunch.repository(containing: URL(fileURLWithPath: "/tmp/x"), fileExists: { _ in false }) == nil)
}

@Test @MainActor func backendSettingsPersistAndValidate() throws {
    let defaults = try #require(UserDefaults(suiteName: "fortress-tests-\(UUID().uuidString)"))
    let backend = BackendController(defaults: defaults)
    #expect(backend.baseURL == BackendController.defaultURL)
    #expect(backend.startsAutomatically)
    backend.urlString = "http://localhost:8123"
    #expect(backend.port == 8123 && backend.isLocal)
    backend.urlString = "not a url"
    #expect(backend.baseURL == BackendController.defaultURL)
    backend.urlString = "https://notes.example.com"
    #expect(!backend.isLocal && backend.launch == nil)
    backend.folderPath = "/nowhere"
    backend.startsAutomatically = false
    let reloaded = BackendController(defaults: defaults)
    #expect(reloaded.urlString == "https://notes.example.com")
    #expect(reloaded.folderPath == "/nowhere")
    #expect(!reloaded.startsAutomatically)
    #expect(!reloaded.start())
    if case .failed = reloaded.state {} else { Issue.record("expected a failure state") }
}

// MARK: Import and export

@Test func multipartBodyCarriesFilesAndFolder() {
    let body = APIClient.multipartBody(
        files: [(name: "a.md", data: Data("# A".utf8)), (name: "b\".md", data: Data("B".utf8))],
        folderId: "f1", boundary: "XYZ")
    let text = String(decoding: body, as: UTF8.self)
    #expect(text.contains("--XYZ\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.md\"\r\nContent-Type: text/markdown\r\n\r\n# A\r\n"))
    #expect(text.contains("filename=\"b'.md\""))
    #expect(text.contains("name=\"folder_id\"\r\n\r\nf1\r\n"))
    #expect(text.hasSuffix("--XYZ--\r\n"))
    #expect(!String(decoding: APIClient.multipartBody(files: [], folderId: nil, boundary: "Q"), as: UTF8.self).contains("folder_id"))
}

@Test func importCollectsMarkdownFromFolders() throws {
    let root = FileManager.default.temporaryDirectory.appending(path: "fortress-import-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: root.appending(path: "sub"), withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    for name in ["b.md", "a.markdown", "sub/c.md", "skip.png", ".hidden.md"] {
        try Data("x".utf8).write(to: root.appending(path: name))
    }
    let files = MarkdownFiles.collect([root, root.appending(path: "b.md")])
    #expect(files.map(\.lastPathComponent) == ["a.markdown", "b.md", "c.md"])
    #expect(MarkdownFiles.filename(for: "Trip: Kyoto/Nara") == "Trip- Kyoto-Nara.md")
    #expect(MarkdownFiles.filename(for: "  ") == "Note.md")
}

@Test func decodesImportResult() throws {
    let json = #"{"imported":[{"name":"a.md","id":"x","title":"A"}],"errors":[{"name":"b.md","detail":"file is not UTF-8 text"}]}"#
    let result = try JSONDecoder().decode(MarkdownImportResult.self, from: Data(json.utf8))
    #expect(result.imported.first?.title == "A")
    #expect(result.errors.first?.detail == "file is not UTF-8 text")
}

@Test func columnLayoutRoundTrips() {
    for layout in [ColumnLayout.all, .doubleColumn, .detailOnly] {
        #expect(ColumnLayout(layout.visibility) == layout)
    }
}

// MARK: Backend process and token

@Test func tokenAndInstanceAreStable() {
    #expect(BackendAuth.token.count >= 32)
    #expect(BackendAuth.instanceID == BackendAuth.instance(for: BackendAuth.token))
    // The backend computes the same: sha256(token) hex, first 16 digits (backend/app/auth.py).
    #expect(BackendAuth.instance(for: "abc") == "ba7816bf8f01cfea")
    #expect(APIClient.sessionConfiguration().httpAdditionalHeaders?[BackendAuth.tokenHeader] as? String == BackendAuth.token)
}

@Test @MainActor func appOnlyUsesItsOwnBackendOnceItStartedOne() throws {
    let defaults = try #require(UserDefaults(suiteName: "fortress-tests-\(UUID().uuidString)"))
    let backend = BackendController(defaults: defaults)
    // Started none: whatever is at the address (a backend run by hand).
    #expect(backend.accepts(instance: nil) && backend.accepts(instance: "someone-else"))
    #expect(backend.serverURL == backend.baseURL)
}

@Test func freePortsAreFound() throws {
    let port = try #require(BackendController.freePort())
    #expect(port > 0)
    // Another test's connection can take a just-freed ephemeral port; a few tries settle it.
    #expect((0..<5).contains { _ in BackendController.freePort().map(BackendController.isPortFree) == true })
}

/// Whether `pid` is gone (a zombie reparented to launchd is reaped quickly).
private func waitUntilGone(_ pid: pid_t, seconds: Double = 5) async -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        if kill(pid, 0) != 0 && errno == ESRCH { return true }
        try? await Task.sleep(for: .milliseconds(50))
    }
    return false
}

/// A stand-in backend: a shell (like `uv run`) whose child (like uvicorn) ignores
/// nothing, writes its pid to a file and waits.
@MainActor
private func startStandIn(_ backend: BackendController, folder: URL, trapTerm: Bool = false) async throws -> pid_t {
    let pidFile = folder.appending(path: "child.pid")
    let trap = trapTerm ? "trap '' TERM; " : ""
    let launch = BackendLaunch(
        executable: URL(fileURLWithPath: "/bin/sh"),
        arguments: ["-c", "\(trap)/bin/sleep 600 & echo $! > '\(pidFile.path)'; wait"],
        workingDirectory: folder, path: "/usr/bin:/bin")
    #expect(backend.start(launch, port: 1))
    #expect(backend.isProcessRunning && backend.ownsServer)
    #expect(backend.serverURL.absoluteString == "http://127.0.0.1:1")
    #expect(!backend.accepts(instance: "someone-else") && backend.accepts(instance: BackendAuth.instanceID))
    // Generous: on CI the snapshot suites can keep the main actor busy for many seconds.
    let deadline = Date().addingTimeInterval(180)
    var child: pid_t?
    while child == nil, Date() < deadline {
        child = (try? String(contentsOf: pidFile, encoding: .utf8)).flatMap { pid_t($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
        if child == nil { try await Task.sleep(for: .milliseconds(50)) }
    }
    return try #require(child)
}

@Test @MainActor func stoppingTheBackendStopsItsWholeProcessGroup() async throws {
    let folder = FileManager.default.temporaryDirectory.appending(path: "fortress-pg-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    let defaults = try #require(UserDefaults(suiteName: "fortress-tests-\(UUID().uuidString)"))
    let backend = BackendController(defaults: defaults)
    backend.logURL = folder.appending(path: "backend.log")

    // The shell ignores SIGTERM, so the group's SIGKILL after the grace period ends it.
    let child = try await startStandIn(backend, folder: folder, trapTerm: true)
    // (No timing bound: on a busy CI runner the main actor is shared with snapshot tests.)
    await backend.stop(gracePeriod: .milliseconds(500))
    #expect(await waitUntilGone(child))
    try await waitUntil { !backend.isProcessRunning }
    #expect(backend.state == .idle)
}

@Test @MainActor func aBackendThatDiesTakesItsChildrenWithIt() async throws {
    let folder = FileManager.default.temporaryDirectory.appending(path: "fortress-pg-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    let defaults = try #require(UserDefaults(suiteName: "fortress-tests-\(UUID().uuidString)"))
    let backend = BackendController(defaults: defaults)
    backend.logURL = folder.appending(path: "backend.log")

    let child = try await startStandIn(backend, folder: folder)
    // SIGKILL only the shell (like `uv` being killed): the sleep it started must not linger.
    let leader = try #require(backend.processGroup)
    kill(leader, SIGKILL)
    try await waitUntil { !backend.isProcessRunning }
    #expect(await waitUntilGone(child))
    if case .failed = backend.state {} else { Issue.record("expected a failure state, got \(backend.state)") }
}
