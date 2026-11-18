import Foundation
import Testing

@testable import FortressKit

/// The data layer against a real backend (seeded by `Scripts/seed_backend.py`), run by
/// the `integration` CI job with `FORTRESS_INTEGRATION_URL` set. Each scenario runs with
/// the store's caches and coalescing off (how the client behaved before) and on, and
/// prints request counts and latencies as `[perf]` lines.
@MainActor
@Suite(.serialized, .enabled(if: IntegrationBackend.url != nil))
struct IntegrationTests {
    static var baseURL: URL? { IntegrationBackend.url }

    private func client(_ options: APIStore.Options) -> APIClient {
        APIClient(baseURL: Self.baseURL!, store: APIStore(options: options))
    }

    private func noteIds(_ api: APIClient, count: Int) async throws -> [String] {
        let navigation = try await api.navigation(recentLimit: 20)
        let notes = NoteListing.sorted(NoteListing.allDocuments(in: navigation.items), by: .title)
        try #require(notes.count >= count, "seed the backend first")
        return Array(notes.prefix(count).map(\.id))
    }

    struct Timing: CustomStringConvertible {
        var samples: [Duration]
        private func percentile(_ p: Double) -> Double {
            let sorted = samples.map { Double($0.components.attoseconds) / 1e15 + Double($0.components.seconds) * 1000 }.sorted()
            return sorted.isEmpty ? 0 : sorted[min(sorted.count - 1, Int(Double(sorted.count) * p))]
        }
        var total: Double { samples.reduce(0) { $0 + Double($1.components.attoseconds) / 1e15 + Double($1.components.seconds) * 1000 } }
        var description: String {
            String(format: "p50 %.2f ms, p95 %.2f ms, total %.0f ms", percentile(0.5), percentile(0.95), total)
        }
    }

    private func report(_ scenario: String, _ label: String, requests: Int, timing: Timing? = nil) {
        print("[perf] \(scenario) [\(label)]: \(requests) requests" + (timing.map { ", \($0)" } ?? ""))
    }

    /// Opening 50 notes in a row, as the app does: the editor and the inspector each
    /// read the note; while it is read, the list prefetches the next and previous one.
    @Test func openFiftyNotesInARow() async throws {
        var results: [String: (Int, Timing)] = [:]
        for (label, options) in [("before", APIStore.Options.uncached), ("after", APIStore.Options())] {
            let api = client(options)
            let ids = try await noteIds(api, count: 51)
            api.store.resetStats()
            var timing = Timing(samples: [])
            let clock = ContinuousClock()
            for index in 0..<50 {
                let id = ids[index]
                let start = clock.now
                async let editor = api.document(id: id)
                async let inspector = api.document(id: id)
                _ = try await (editor, inspector)
                timing.samples.append(clock.now - start)
                // The person reads the note; the list prefetches its neighbours meanwhile.
                await api.prefetchDocuments(ids: NoteListing.neighbours(of: id, in: ids))
            }
            results[label] = (api.store.stats.requests, timing)
            report("open 50 notes in a row (editor + inspector)", label, requests: api.store.stats.requests, timing: timing)
        }
        #expect(results["after"]!.0 < results["before"]!.0)
    }

    /// Going back over notes just seen (⌘[ / ↑): from memory.
    @Test func revisitTwentyNotes() async throws {
        var counts: [String: Int] = [:]
        for (label, options) in [("before", APIStore.Options.uncached), ("after", APIStore.Options())] {
            let api = client(options)
            let ids = try await noteIds(api, count: 20)
            api.store.resetStats()
            var timing = Timing(samples: [])
            let clock = ContinuousClock()
            for id in ids + ids.reversed() {
                let start = clock.now
                _ = try await api.document(id: id)
                timing.samples.append(clock.now - start)
            }
            counts[label] = api.store.stats.requests
            report("open 20 notes, then again in reverse", label, requests: api.store.stats.requests, timing: timing)
        }
        #expect(counts["after"]! == 20)
        #expect(counts["before"]! == 40)
    }

    /// Ten views asking for the tree at once (window open, sidebar, palette…).
    @Test func navigationStorm() async throws {
        var counts: [String: Int] = [:]
        for (label, options) in [("before", APIStore.Options.uncached), ("after", APIStore.Options())] {
            let api = client(options)
            let clock = ContinuousClock()
            let start = clock.now
            try await withThrowingTaskGroup(of: NavigationResponse.self) { group in
                for _ in 0..<10 { group.addTask { try await api.navigation(recentLimit: 20) } }
                for try await _ in group {}
            }
            counts[label] = api.store.stats.requests
            report("10 concurrent navigation loads", label, requests: api.store.stats.requests,
                   timing: Timing(samples: [clock.now - start]))
        }
        #expect(counts["after"]! == 1)
    }

    /// The editor's operations against the real server (docs/block-model.md): a note
    /// written block by block (marks, a level-5 heading, a quote, code, a table), then
    /// edited (a cell, a new row, a quote line out of its quote, a paragraph that becomes
    /// a divider), reads back as exactly what the editor holds, every time.
    @Test func editorOperationsRoundTripThroughTheServer() async throws {
        let api = client(APIStore.Options.uncached)
        var document = try await api.createDocument(title: "Block model round trip")
        var base = SyncedTree(BlockCodec.flatten(document.children))
        var blocks = [
            EditorBlock(kind: .paragraph, runs: [InlineRun("Plain, "), InlineRun("bold", InlineMarks(bold: true)),
                                                 InlineRun(" and a "), InlineRun("link", InlineMarks(link: "https://example.com"))]),
            EditorBlock(kind: .heading5, runs: [InlineRun("Fifth level")]),
            EditorBlock(kind: .bullet, runs: [InlineRun("item")]),
            EditorBlock(kind: .check, depth: 1, checked: true, runs: [InlineRun("done")]),
            EditorBlock(kind: .quote, runs: [InlineRun("quoted one")]),
            EditorBlock(kind: .quote, runs: [InlineRun("quoted two")]),
            EditorBlock(kind: .code, language: "swift", runs: [InlineRun("let x = 1\u{2028}print(x)")]),
            EditorBlock(kind: .table, table: {
                var table = TableContent.blank(rows: 2, columns: 2)
                table.setRuns([InlineRun("A")], row: 0, column: 0)
                table.setRuns([InlineRun("1", InlineMarks(italic: true))], row: 1, column: 1)
                return table
            }()),
            EditorBlock(kind: .paragraph, runs: [InlineRun("Last")]),
        ]
        func save() async throws {
            let (operations, _) = BlockDiff.operations(from: base, to: blocks)
            try #require(!operations.isEmpty)
            document = try await api.apply(BlockTransaction(baseRevision: document.revision, operations: operations), to: document.id)
            let reloaded = BlockCodec.flatten(document.children, resolved: document.resolvedLinks)
            base = SyncedTree(reloaded)
            let left = BlockDiff.operations(from: base, to: blocks).operations
            #expect(left.isEmpty, "not what the editor holds: \(left.map { "\($0.operation) \($0.type ?? "") \($0.blockId ?? "")" })")
            #expect(reloaded.map(\.kind) == BlockDiff.shape(blocks).map(\.block.kind))
            blocks = reloaded
        }
        try await save()
        #expect(document.children.contains { $0.type == "quote" && $0.children.count == 2 })
        #expect(document.children.contains { $0.type == "heading" && $0.attrs["level"] == .number(5) })

        let table = try #require(blocks.firstIndex { $0.kind == .table })
        blocks[table].table?.setRuns([InlineRun("B")], row: 0, column: 1)
        blocks[table].table?.insertRow(at: 2)
        blocks[table].table?.setRuns([InlineRun("new row")], row: 2, column: 0)
        let quote = try #require(blocks.lastIndex { $0.kind == .quote })
        blocks[quote].kind = .paragraph
        blocks[blocks.count - 1].kind = .divider
        blocks[blocks.count - 1].runs = []
        blocks[blocks.count - 1].origin = nil
        try await save()
        try await api.trashDocument(id: document.id)
    }

    /// Rename then reopen: the save's answer is the note, so reopening is free.
    @Test func saveThenReopen() async throws {
        var counts: [String: Int] = [:]
        for (label, options) in [("before", APIStore.Options.uncached), ("after", APIStore.Options())] {
            let api = client(options)
            let ids = try await noteIds(api, count: 10)
            api.store.resetStats()
            for id in ids {
                let document = try await api.document(id: id)
                _ = try await api.updateDocument(id: id, title: document.title)
                _ = try await api.document(id: id)
            }
            counts[label] = api.store.stats.requests
            report("load, rename, reopen 10 notes", label, requests: api.store.stats.requests)
        }
        #expect(counts["after"]! == 20)
    }

    /// New notes and a folder delete through the app model: no tree reloads, one move request.
    @Test func modelChangesDontReloadTheTree() async throws {
        let api = client(APIStore.Options())
        let model = AppModel(api: api)
        model.connectsOnAppear = false
        await model.refreshNavigation()
        api.store.resetStats()
        for _ in 0..<10 { await model.newNote() }
        report("create 10 notes (main: 10 creates + 10 tree reloads = 20)", "after", requests: api.store.stats.requests)
        #expect(api.store.stats.requests == 10)

        let folder = try await api.createFolder(name: "Integration \(UUID().uuidString.prefix(6))")
        for index in 0..<8 { _ = try await api.createDocument(title: "In folder \(index)", folderId: folder.id) }
        await model.refreshNavigation()
        api.store.resetStats()
        await model.deleteFolder(id: folder.id)
        report("delete a folder holding 8 notes (main: 8 moves + delete + reload = 10)", "after", requests: api.store.stats.requests)
        #expect(api.store.stats.requests == 2)
        #expect(model.notice == nil)
        #expect(NoteListing.folder(id: folder.id, in: model.navigation.items) == nil)

        // The local tree matches the server's.
        let local = Set(NoteListing.allDocuments(in: model.navigation.items).map { "\($0.id) \($0.folderId ?? "-")" })
        await model.refreshNavigation()
        let server = Set(NoteListing.allDocuments(in: model.navigation.items).map { "\($0.id) \($0.folderId ?? "-")" })
        #expect(local == server)
    }

    /// Clicking through notes faster than they load: superseded loads are cancelled.
    @Test func rapidSelectionChanges() async throws {
        let api = client(APIStore.Options())
        let ids = try await noteIds(api, count: 30)
        api.store.resetStats()
        let clock = ContinuousClock()
        let start = clock.now
        var current: Task<BlockDocument, Error>?
        for id in ids {
            current?.cancel()
            current = Task { try await api.document(id: id) }
            try await Task.sleep(for: .milliseconds(2))
        }
        let last = try await current!.value
        #expect(last.id == ids.last)
        report("30 selections 2 ms apart, then settle", "after", requests: api.store.stats.requests,
               timing: Timing(samples: [clock.now - start]))
    }
    @Test func backendRequiresThisLaunchsToken() async throws {
        let api = APIClient(baseURL: Self.baseURL!)
        #expect(try await api.healthInstance() == BackendAuth.instanceID)
        _ = try await api.navigation(recentLimit: 1)
        // Without the token (a session without BackendAuth.headers): refused.
        let bare = URLSession(configuration: .ephemeral)
        let (_, response) = try await bare.data(from: Self.baseURL!.appending(path: "api/navigation"))
        #expect((response as? HTTPURLResponse)?.statusCode == 401)
        var browser = URLRequest(url: Self.baseURL!.appending(path: "api/backups"))
        browser.httpMethod = "POST"
        browser.setValue("https://evil.example", forHTTPHeaderField: "Origin")
        let (_, refused) = try await APIClient.sharedSession.data(for: browser)
        #expect((refused as? HTTPURLResponse)?.statusCode == 403)
    }
}

/// The backend the integration tests use (`FORTRESS_INTEGRATION_URL`), if any.
enum IntegrationBackend {
    static let url = ProcessInfo.processInfo.environment["FORTRESS_INTEGRATION_URL"].flatMap(URL.init(string:))
}
