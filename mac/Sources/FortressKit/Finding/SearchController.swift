import Foundation
import Observation

/// What activating a search row does.
enum SearchAction: Equatable, Sendable {
    case open(documentId: String, blockId: String)
    case ask(question: String)
}

/// The highlighted row: the "Ask your notes" row or one passage.
enum SearchSelection: Hashable, Sendable {
    case ask
    case result(String)
}

/// Debounced search as you type. Results always say which query they answer, so a
/// stale list is never opened: pressing ↩ before the current query's results arrive
/// waits for them and then opens the first.
@MainActor
@Observable
final class SearchController {
    typealias Fetch = @Sendable (String) async throws -> [SearchMatch]

    private(set) var results: [SearchMatch] = []
    /// The query `results` belong to (nil before the first answer).
    private(set) var resultsQuery: String?
    /// The latest query typed (trimmed).
    private(set) var query = ""
    private(set) var isSearching = false
    private(set) var errorMessage: String?
    /// Requests sent so far (for tests and the debug HUD).
    @ObservationIgnored private(set) var requestCount = 0
    var selection: SearchSelection = .ask

    @ObservationIgnored private let debounce: Duration
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var pending: (@MainActor (SearchAction) -> Void)?
    /// Answers from the last few seconds, so backspacing over a typo asks nothing.
    @ObservationIgnored private var recent = ExpiringCache<String, [SearchMatch]>(capacity: 24, lifetime: 20)
    /// The query whose request is waiting or in flight (nil when none is).
    @ObservationIgnored private var inFlightQuery: String?

    init(debounce: Duration = .milliseconds(150)) {
        self.debounce = debounce
    }

    isolated deinit {
        task?.cancel()
    }

    /// Stops the request in flight (the results view went away).
    func cancel() {
        generation += 1
        task?.cancel()
        task = nil
        inFlightQuery = nil
        isSearching = false
    }

    /// Whether the visible results answer an older query.
    var isStale: Bool { resultsQuery != query }

    /// Rows in display order: the Ask row first, then passages.
    var rows: [SearchSelection] { [.ask] + results.map { .result($0.id) } }

    func update(query raw: String, fetch: @escaping Fetch) {
        let query = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // Nothing new to ask: the same words are already answered or on their way. (Typing
        // "ryokan " after "ryokan" used to cancel and resend the request in flight.) A query
        // whose request failed is sent again.
        if query == self.query, !query.isEmpty, inFlightQuery == query || resultsQuery == query { return }
        if query.isEmpty, self.query.isEmpty, resultsQuery == nil, task == nil { return }
        let previous = self.query
        self.query = query
        generation += 1
        task?.cancel()
        guard !query.isEmpty else {
            task = nil
            inFlightQuery = nil
            results = []
            resultsQuery = nil
            errorMessage = nil
            isSearching = false
            pending = nil
            selection = .ask
            return
        }
        // Backspacing to words answered a moment ago: show those at once, ask nothing.
        if let cached = recent.value(for: query, now: .now) {
            task = nil
            inFlightQuery = nil
            isSearching = false
            receive(.success(cached), for: query, generation: generation, remember: false)
            return
        }
        inFlightQuery = query
        let current = generation
        // The first query (results shown again, or the first letter typed) goes out at once.
        let debounce = resultsQuery == nil && previous.isEmpty ? .zero : self.debounce
        task = Task { [weak self] in
            if debounce > .zero { try? await Task.sleep(for: debounce) }
            if Task.isCancelled { return }
            self?.isSearching = true
            self?.requestCount += 1
            let outcome: Result<[SearchMatch], Error>
            do { outcome = .success(try await fetch(query)) } catch { outcome = .failure(error) }
            self?.receive(outcome, for: query, generation: current)
        }
    }

    private func receive(_ outcome: Result<[SearchMatch], Error>, for query: String, generation: Int, remember: Bool = true) {
        guard generation == self.generation else { return }  // A newer query is on its way.
        isSearching = false
        inFlightQuery = nil
        switch outcome {
        case .success(let matches):
            if remember { recent.insert(matches, for: query, now: .now) }
            results = matches
            resultsQuery = query
            errorMessage = nil
            selection = matches.first.map { .result($0.id) } ?? .ask
            if let pending {
                self.pending = nil
                if let action = action(for: selection) { pending(action) }
            }
        case .failure(let error):
            if error is CancellationError || (error as? URLError)?.code == .cancelled { return }
            errorMessage = error.localizedDescription
            pending = nil
        }
    }

    /// Moves the highlight up or down, staying within the rows.
    func move(by delta: Int) {
        let rows = self.rows
        let index = rows.firstIndex(of: selection) ?? 0
        selection = rows[min(max(index + delta, 0), rows.count - 1)]
    }

    /// ↩: runs the highlighted row, or (if the results are for an older query) the first
    /// result of the current query as soon as it arrives.
    func activate(perform: @escaping @MainActor (SearchAction) -> Void) {
        guard !query.isEmpty else { return }
        if selection == .ask {
            perform(.ask(question: query))
        } else if isStale {
            pending = perform
        } else if let action = action(for: selection) {
            perform(action)
        }
    }

    func action(for selection: SearchSelection) -> SearchAction? {
        switch selection {
        case .ask:
            return query.isEmpty ? nil : .ask(question: query)
        case .result(let id):
            guard let match = results.first(where: { $0.id == id }) else { return nil }
            return .open(documentId: match.documentId, blockId: match.blockId)
        }
    }
}
