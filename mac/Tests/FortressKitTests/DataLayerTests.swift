import Foundation
import Synchronization
import Testing

@testable import FortressKit

/// The data layer against a fake backend (`SpyProtocol`): request counts, caching,
/// coalescing, ordering and error paths. Each test names the bug it pins down.
@MainActor
@Suite(.serialized) struct DataLayerTests {
    private func makeModel(_ api: APIClient) -> AppModel {
        let model = AppModel(api: api)
        model.connectsOnAppear = false
        model.hasLoadedNavigation = true
        return model
    }

    // MARK: Coalescing and caching

    /// Bug: opening a note with the inspector shown fetched it twice (the editor and the
    /// inspector each call `document(id:)`), and fast re-renders fetched it again.
    @Test func concurrentReadsOfOneNoteShareOneRequest() async throws {
        SpyProtocol.reset { _ in .document(SpyJSON.document("d1"), delay: 0.05) }
        // A long max age: on a busy CI machine (other suites hold the main thread while
        // they render) this test has taken over 30 s, the default, and the last read
        // then revalidated, which is right but not what this test is about.
        var options = APIStore.Options()
        options.documentMaxAge = 3600
        let api = SpyProtocol.client(options: options)
        async let a = api.document(id: "d1")
        async let b = api.document(id: "d1")
        async let c = api.document(id: "d1")
        let results = try await [a, b, c]
        #expect(results.allSatisfy { $0.id == "d1" })
        #expect(SpyProtocol.count("GET", "/api/block-documents/d1") == 1)
        // Joined the request in flight (or, on a busy machine, came late and found the cache).
        #expect(api.store.stats.coalesced + api.store.stats.cacheHits == 2)

        // And reading it again right away comes from memory.
        _ = try await api.document(id: "d1")
        #expect(SpyProtocol.count("GET", "/api/block-documents/d1") == 1)
    }

    @Test func withoutTheStoreEveryReadIsARequest() async throws {
        SpyProtocol.reset { _ in .document(SpyJSON.document("d1"), delay: 0.02) }
        let api = SpyProtocol.client(options: .uncached)
        async let a = api.document(id: "d1")
        async let b = api.document(id: "d1")
        _ = try await [a, b]
        _ = try await api.document(id: "d1")
        #expect(SpyProtocol.count("GET", "/api/block-documents/d1") == 3)
    }

    /// A stale cached note is revalidated with its ETag; a 304 is answered from memory.
    @Test func staleNoteIsRevalidatedWithItsETag() async throws {
        SpyProtocol.reset { seen in
            if seen.headers["If-None-Match"] == "\"r1\"" { return SpyProtocol.Reply(status: 304, body: Data()) }
            return .document(SpyJSON.document("d1", title: "First"), etag: "\"r1\"")
        }
        var options = APIStore.Options()
        options.documentMaxAge = 0
        let api = SpyProtocol.client(options: options)
        let first = try await api.document(id: "d1")
        let second = try await api.document(id: "d1")
        #expect(first == second)
        #expect(SpyProtocol.seen.count == 2)
        #expect(SpyProtocol.seen[1].headers["If-None-Match"] == "\"r1\"")
        #expect(api.store.stats.notModified == 1)
    }

    /// GETs other than documents (navigation) honour ETags too.
    @Test func navigationAnswers304FromTheStoredBody() async throws {
        let body = SpyJSON.navigation([.document(SpyJSON.note("n1"))])
        SpyProtocol.reset { seen in
            seen.headers["If-None-Match"] == "\"nav1\""
                ? SpyProtocol.Reply(status: 304, body: Data())
                : .json(body, headers: ["ETag": "\"nav1\""])
        }
        let api = SpyProtocol.client()
        let first = try await api.navigation(recentLimit: 20)
        let second = try await api.navigation(recentLimit: 20)
        #expect(first == second)
        #expect(SpyProtocol.seen.last?.headers["If-None-Match"] == "\"nav1\"")
        #expect(api.store.stats.notModified == 1)
    }

    /// A save's answer is the new note: reading it afterwards needs no request.
    @Test func savesKeepTheCacheCurrent() async throws {
        SpyProtocol.reset { seen in
            seen.method == "POST"
                ? .document(SpyJSON.document("d1", revision: 2, title: "Saved"))
                : .document(SpyJSON.document("d1", revision: 1, title: "Loaded"))
        }
        let api = SpyProtocol.client()
        _ = try await api.document(id: "d1")
        _ = try await api.apply(BlockTransaction(baseRevision: 1, operations: []), to: "d1")
        let after = try await api.document(id: "d1")
        #expect(after.revision == 2 && after.title == "Saved")
        #expect(SpyProtocol.count("GET", "/api/block-documents/d1") == 1)
    }

    /// Bug guard: a conflict means the cached copy is stale; "Load latest" must ask the server.
    @Test func conflictDropsTheCachedNote() async throws {
        SpyProtocol.reset { seen in
            seen.method == "POST"
                ? SpyProtocol.Reply(status: 409, body: Data(#"{"detail":"conflict"}"#.utf8))
                : .document(SpyJSON.document("d1"))
        }
        let api = SpyProtocol.client()
        _ = try await api.document(id: "d1")
        await #expect(throws: APIError.self) {
            _ = try await api.apply(BlockTransaction(baseRevision: 1, operations: []), to: "d1")
        }
        _ = try await api.document(id: "d1")
        #expect(SpyProtocol.count("GET", "/api/block-documents/d1") == 2)
    }

    /// An answer that left before a save must not put the older revision back.
    @Test func olderReadNeverReplacesANewerSave() async throws {
        SpyProtocol.reset { seen in
            seen.method == "POST"
                ? .document(SpyJSON.document("d1", revision: 5, title: "New"))
                : .document(SpyJSON.document("d1", revision: 4, title: "Old"), delay: 0.5)
        }
        let api = SpyProtocol.client()
        async let slowRead = api.document(id: "d1")
        try await waitUntil { SpyProtocol.seen.count >= 1 }
        _ = try await api.apply(BlockTransaction(baseRevision: 4, operations: []), to: "d1")
        _ = try await slowRead
        #expect(try await api.document(id: "d1").revision == 5)
    }

    /// Bug: coalescing must not hand a reader that starts after a change the answer to
    /// a request that started before it.
    @Test func readsAfterAChangeDontJoinReadsFromBeforeIt() async throws {
        SpyProtocol.reset { seen in
            if seen.method == "POST" { return .json(Data(#"{"id":"f1","parent_id":null,"name":"A","position":0}"#.utf8)) }
            return .json(SpyJSON.navigation([]), delay: 0.5)
        }
        let api = SpyProtocol.client()
        async let before = api.navigation()
        try await waitUntil { SpyProtocol.seen.count >= 1 }
        _ = try await api.createFolder(name: "A")
        async let after = api.navigation()
        _ = try await [before, after]
        #expect(SpyProtocol.count("GET", "/api/navigation") == 2)
    }

    // MARK: Cancellation

    /// Closing a note while it loads cancels its request, and the cancellation is not
    /// reported as an error.
    @Test func cancelledReadCancelsTheRequestAndShowsNoNotice() async throws {
        // A long delay: on a loaded CI runner the main actor can stall for seconds, and the
        // answer must not arrive before the cancel does (cancelling ends the test early).
        SpyProtocol.reset { _ in .document(SpyJSON.document("d1"), delay: 20) }
        let api = SpyProtocol.client()
        let model = makeModel(api)
        let task = Task { try await api.document(id: "d1") }
        try await waitUntil { SpyProtocol.seen.count >= 1 }
        task.cancel()
        let result = await task.result
        #expect(throws: (any Error).self) { try result.get() }
        if case .failure(let error) = result { model.report(error) }
        #expect(model.notice == nil)
        try await Task.sleep(for: .milliseconds(50))
        #expect(SpyProtocol.seen.first?.cancelled == true)
    }

    /// One of two readers going away doesn't cancel the other's request.
    @Test func cancellingOneReaderKeepsTheSharedRequest() async throws {
        SpyProtocol.reset { _ in .document(SpyJSON.document("d1"), delay: 0.5) }
        let api = SpyProtocol.client()
        let leaving = Task { try await api.document(id: "d1") }
        let staying = Task { try await api.document(id: "d1") }
        try await waitUntil { api.store.stats.coalesced >= 1 && SpyProtocol.seen.count >= 1 }
        leaving.cancel()
        #expect(try await staying.value.id == "d1")
        #expect(SpyProtocol.count("GET", "/api/block-documents/d1") == 1)
    }

    // MARK: Navigation

    /// Bug: two refreshes in flight; the older answer arriving last overwrote the newer tree.
    @Test func slowOlderRefreshDoesNotOverwriteNewerOne() async throws {
        let old = SpyJSON.navigation([.document(SpyJSON.note("n1", title: "Old"))])
        let new = SpyJSON.navigation([.document(SpyJSON.note("n1", title: "New"))])
        let calls = Counter()
        SpyProtocol.reset { _ in
            calls.next() == 0 ? .json(old, delay: 0.5) : .json(new)
        }
        // Without coalescing both refreshes go out (a mutation in between does the same).
        let model = makeModel(SpyProtocol.client(options: .uncached))
        async let first: Void = model.refreshNavigation()
        try await waitUntil { SpyProtocol.seen.count >= 1 }
        await model.refreshNavigation()
        await first
        #expect(model.navigation.items.first.map { if case .document(let n) = $0 { n.title } else { "" } } == "New")
    }

    /// Bug: an edit saved while a refresh was in flight was undone when the refresh
    /// (holding the tree from before the save) arrived.
    @Test func editDuringARefreshSurvivesIt() async throws {
        let before = SpyJSON.navigation([.document(SpyJSON.note("n1", title: "Before"))])
        SpyProtocol.reset { _ in .json(before, delay: 0.5) }
        let model = makeModel(SpyProtocol.client())
        model.navigation = NavigationResponse(items: [.document(SpyJSON.note("n1", title: "Before"))], recent: [])
        async let refresh: Void = model.refreshNavigation()
        try await waitUntil { SpyProtocol.seen.count >= 1 }
        model.noteDidChange(SpyJSON.document("n1", revision: 2, title: "After", updatedAt: "2026-11-18T10:00:00.000000+00:00"))
        await refresh
        #expect(model.openNoteTitle("n1") == "After")
        #expect(model.navigation.recent.first?.title == "After")
    }

    /// Bug: a note edited that wasn't among the 20 most recent never showed up in Recent
    /// until something reloaded the tree.
    @Test func editingAnOldNoteMovesItToTheTopOfRecent() {
        let model = makeModel(SpyProtocol.client())
        let notes = (0..<25).map { SpyJSON.note("n\($0)", updatedAt: String(format: "2026-10-%02dT00:00:00.000000+00:00", $0 + 1)) }
        model.navigation = NavigationResponse(
            items: notes.map(NavigationNode.document),
            recent: Array(notes.sorted { $0.updatedAt > $1.updatedAt }.prefix(20)))
        #expect(!model.navigation.recent.contains { $0.id == "n0" })
        model.noteDidChange(SpyJSON.document("n0", revision: 2, title: "Old note, edited", updatedAt: "2026-11-18T10:00:00.000000+00:00"))
        #expect(model.navigation.recent.first?.id == "n0")
        #expect(model.navigation.recent.count == 20)
    }

    /// Bug: every new note, move, trash and folder change reloaded the whole tree.
    @Test func changesPatchTheTreeWithoutReloadingIt() async throws {
        SpyProtocol.reset { seen in
            switch (seen.method, seen.path) {
            case ("POST", "/api/block-documents"): return .document(SpyJSON.document("new", title: ""))
            case ("POST", "/api/folders"): return .json(Data(#"{"id":"f2","parent_id":null,"name":"New Folder","position":1}"#.utf8), status: 201)
            case ("PATCH", "/api/folders/f2"): return .json(Data(#"{"id":"f2","parent_id":null,"name":"Ideas","position":1}"#.utf8))
            case ("DELETE", _): return SpyProtocol.Reply(status: 204, body: Data())
            default: return .json(Data(#"{"id":"x","folder_id":null,"position":0}"#.utf8))
            }
        }
        let model = makeModel(SpyProtocol.client())
        model.navigation = NavigationResponse(
            items: [.folder(NavigationFolder(id: "f1", parentId: nil, name: "Work", position: 0, children: [.document(SpyJSON.note("a", folder: "f1"))])),
                    .document(SpyJSON.note("b"))],
            recent: [SpyJSON.note("a", folder: "f1"), SpyJSON.note("b")])

        await model.newNote()
        #expect(NoteListing.allDocuments(in: model.navigation.items).contains { $0.id == "new" })
        #expect(model.navigation.recent.first?.id == "new")
        #expect(model.openNoteId == "new")

        await model.moveNote(id: "b", toFolder: "f1")
        #expect(NoteListing.folder(id: "f1", in: model.navigation.items)?.children.contains { $0.id == "document:b" } == true)

        await model.trashNote(id: "a")
        #expect(!NoteListing.allDocuments(in: model.navigation.items).contains { $0.id == "a" })
        #expect(!model.navigation.recent.contains { $0.id == "a" })

        await model.newFolder()
        await model.renameFolder(id: "f2", to: "Ideas")
        #expect(NoteListing.folder(id: "f2", in: model.navigation.items)?.name == "Ideas")

        #expect(SpyProtocol.count("GET", "/api/navigation") == 0)
        #expect(model.notice == nil)
    }

    /// Bug: deleting a folder sent one move per note and subfolder in it.
    @Test func deletingAFolderMovesItsContentsInOneRequest() async throws {
        SpyProtocol.reset { seen in
            seen.method == "DELETE" ? SpyProtocol.Reply(status: 204, body: Data()) : .json(Data(#"{"folders":1,"documents":3}"#.utf8))
        }
        let model = makeModel(SpyProtocol.client())
        let children: [NavigationNode] = (1...3).map { .document(SpyJSON.note("n\($0)", folder: "f1")) }
            + [.folder(NavigationFolder(id: "sub", parentId: "f1", name: "Sub", position: 0, children: []))]
        model.navigation = NavigationResponse(
            items: [.folder(NavigationFolder(id: "f1", parentId: nil, name: "Old", position: 0, children: children))], recent: [])
        await model.deleteFolder(id: "f1")
        #expect(SpyProtocol.seen.map { "\($0.method) \($0.path)" } == ["POST /api/batch/move", "DELETE /api/folders/f1"])
        #expect(NoteListing.folder(id: "f1", in: model.navigation.items) == nil)
        #expect(NoteListing.folder(id: "sub", in: model.navigation.items)?.parentId == nil)
        #expect(NoteListing.allDocuments(in: model.navigation.items).allSatisfy { $0.folderId == nil })
    }

    @Test func deletingAFolderFallsBackToSingleMovesOnAnOlderBackend() async throws {
        SpyProtocol.reset { seen in
            if seen.path == "/api/batch/move" { return .json(Data(#"{"detail":"Not Found"}"#.utf8), status: 404) }
            if seen.method == "DELETE" { return SpyProtocol.Reply(status: 204, body: Data()) }
            return .json(Data(#"{"id":"x","folder_id":null,"position":0}"#.utf8))
        }
        let model = makeModel(SpyProtocol.client())
        model.navigation = NavigationResponse(
            items: [.folder(NavigationFolder(id: "f1", parentId: nil, name: "Old", position: 0,
                                             children: [.document(SpyJSON.note("n1", folder: "f1")), .document(SpyJSON.note("n2", folder: "f1"))]))],
            recent: [])
        await model.deleteFolder(id: "f1")
        #expect(SpyProtocol.count("POST", "/move") == 3)  // the batch, then one per note
        #expect(model.notice == nil)
    }

    // MARK: Prefetch

    /// Prefetching the neighbours is one request; opening them is then instant, and a
    /// note opened while its prefetch is in flight joins it.
    @Test func prefetchLoadsNeighboursInOneRequest() async throws {
        SpyProtocol.reset { seen in
            if seen.path == "/api/batch/block-documents" {
                struct Asked: Decodable { var ids: [String] }
                let ids = (try? JSONDecoder().decode(Asked.self, from: seen.body))?.ids ?? []
                let documents = ids.map { SpyJSON.document($0) }
                let body = try! JSONSerialization.data(withJSONObject: [
                    "documents": documents.map { try! JSONSerialization.jsonObject(with: SpyJSON.encoded($0)) },
                    "unchanged": [String](), "missing": [String](),
                ])
                return .json(body, delay: 0.5)
            }
            return .document(SpyJSON.document(String(seen.path.split(separator: "/").last!)))
        }
        let api = SpyProtocol.client()
        let model = makeModel(api)
        let prefetch = Task { await model.prefetch(noteIds: ["n2", "n3"]) }
        try await waitUntil { api.store.isInFlight("GET document n2 @\(api.store.epoch)") }
        let joined = try await api.document(id: "n2")  // while the batch is in flight
        await prefetch.value
        let cached = try await api.document(id: "n3")
        #expect(joined.id == "n2" && cached.id == "n3")
        #expect(SpyProtocol.seen.map(\.path) == ["/api/batch/block-documents"])
        #expect(SpyProtocol.seen.first?.headers["X-Fortress-Prefetch"] == "1")
    }

    /// An older backend without the batch endpoint: prefetch stops (reading notes one by
    /// one would mark them opened) and reads still work.
    @Test func prefetchStopsOnABackendWithoutBatches() async throws {
        SpyProtocol.reset { seen in
            seen.path.hasPrefix("/api/batch")
                ? .json(Data(#"{"detail":"Not Found"}"#.utf8), status: 404)
                : .document(SpyJSON.document("n2"))
        }
        let api = SpyProtocol.client()
        await api.prefetchDocuments(ids: ["n2"])
        await api.prefetchDocuments(ids: ["n2"])
        #expect(try await api.document(id: "n2").id == "n2")
        #expect(SpyProtocol.seen.map(\.path) == ["/api/batch/block-documents", "/api/block-documents/n2"])
    }

    @Test func neighboursAreNextThenPrevious() {
        #expect(NoteListing.neighbours(of: "b", in: ["a", "b", "c"]) == ["c", "a"])
        #expect(NoteListing.neighbours(of: "a", in: ["a"]) == [])
        #expect(NoteListing.neighbours(of: "z", in: ["a"]) == [])
    }

    // MARK: Errors

    /// Bug: Shell requests (export, import, folder moves) used `URLSession.shared`,
    /// bypassing the client's session (its timeouts, and any stub in tests).
    @Test func shellRequestsGoThroughTheClientsSession() async throws {
        SpyProtocol.reset { _ in SpyProtocol.Reply(status: 200, body: Data("# Hello".utf8)) }
        let api = SpyProtocol.client()
        #expect(try await api.markdown(documentId: "d1") == "# Hello")
        #expect(SpyProtocol.seen.first?.path == "/api/block-documents/d1/markdown")
    }

    @Test func malformedJSONIsACalmNotice() async throws {
        SpyProtocol.reset { _ in .json(Data("<html>oops</html>".utf8)) }
        let model = makeModel(SpyProtocol.client())
        await model.refreshNavigation()
        #expect(model.notice?.message == "Fortress's server sent a reply the app couldn't read.")
        #expect(model.isConnected)
    }

    @Test func serverErrorWithoutDetailIsACalmNotice() async throws {
        SpyProtocol.reset { _ in SpyProtocol.Reply(status: 502, body: Data("Bad Gateway".utf8)) }
        let model = makeModel(SpyProtocol.client())
        await model.refreshNavigation()
        #expect(model.notice?.message == "Fortress's server had a problem (502). Try again in a moment.")
    }

    /// Losing the backend shows the offline state (not a notice per request) and
    /// reconnects with backoff, reloading once it answers.
    @Test func reconnectsWithBackoffWhenTheBackendComesBack() async throws {
        let calls = Counter()
        SpyProtocol.reset { seen in
            if seen.path == "/api/health" {
                return calls.next() < 2 ? SpyProtocol.Reply(failure: .cannotConnectToHost) : .json(Data(#"{"status":"ok"}"#.utf8))
            }
            if seen.path == "/api/navigation" && calls.value < 2 { return SpyProtocol.Reply(failure: .cannotConnectToHost) }
            return .json(SpyJSON.navigation([.document(SpyJSON.note("n1"))]))
        }
        let model = makeModel(SpyProtocol.client())
        model.connectsOnAppear = true
        model.reconnectDelays = (.milliseconds(10), .milliseconds(40))
        await model.refreshNavigation()
        #expect(!model.isConnected)
        #expect(model.notice == nil)
        #expect(model.isReconnecting)
        try await waitUntil { model.isConnected && model.navigation.items.count == 1 }
        #expect(model.isConnected)
        #expect(!model.isReconnecting)
        #expect(model.navigation.items.count == 1)
        #expect(SpyProtocol.count("GET", "/api/health") == 3)
    }

    @Test func portInUseIsReadFromTheLastStartInTheLog() {
        let log = """
            --- 2026-11-18 starting backend ---
            ERROR: [Errno 48] Address already in use
            --- 2026-11-18 starting backend ---
            INFO: Uvicorn running on http://127.0.0.1:8000
            """
        #expect(!BackendController.isPortInUse(log: log))
        #expect(BackendController.isPortInUse(log: log + "\n--- x starting backend ---\nOSError: [Errno 48] address already in use"))
    }

    // MARK: Main-thread work

    /// List rows parse every note's timestamps on every render; the memo makes repeats
    /// nearly free.
    @Test func timestampParsingIsMemoized() {
        let stamps = (0..<500).map { String(format: "2026-11-%02dT%02d:%02d:00.123456+00:00", $0 % 28 + 1, $0 % 24, $0 % 60) }
        let clock = ContinuousClock()
        let uncached = clock.measure { for _ in 0..<20 { for stamp in stamps { _ = Timestamp.parse(stamp) } } }
        let cached = clock.measure { for _ in 0..<20 { for stamp in stamps { _ = Timestamp.date(stamp) } } }
        print("[perf] 10k timestamp parses: uncached \(uncached), memoized \(cached)")
        #expect(cached < uncached)
        #expect(Timestamp.date(stamps[0]) == Timestamp.parse(stamps[0]))
        #expect(Timestamp.date("not a date") == nil)
    }

    @Test func serverStampsSortWithTheServers() {
        let stamp = Timestamp.serverString(Date(timeIntervalSince1970: 1_790_000_000.5))
        #expect(stamp == "2026-09-21T14:13:20.500000+00:00")
        #expect(abs(Timestamp.date(stamp)!.timeIntervalSince1970 - 1_790_000_000.5) < 0.001)
    }
}

/// A thread-safe counter for fake-backend handlers.
final class Counter: Sendable {
    private let state = Mutex(0)
    /// The current count, then adds one.
    func next() -> Int {
        state.withLock { count in
            count += 1
            return count - 1
        }
    }
    var value: Int { state.withLock { $0 } }
}

/// Polls until `condition` holds (CI machines are busy: fixed sleeps race), up to 30 s.
/// The limit only matters on failure: a passing condition returns as soon as it holds,
/// and the snapshot suites can keep the main actor busy for over 10 s on CI.
@MainActor
func waitUntil(_ condition: () -> Bool) async throws {
    let deadline = ContinuousClock.now + .seconds(60)
    while !condition() {
        try #require(ContinuousClock.now < deadline, "timed out waiting")
        try await Task.sleep(for: .milliseconds(5))
    }
}

extension AppModel {
    func openNoteTitle(_ id: String) -> String? {
        NoteListing.allDocuments(in: navigation.items).first { $0.id == id }?.title
    }
}
