import Foundation
import Observation
import os

/// Everything known so far about one streamed answer. Pure: events go in through
/// `apply(_:)`, the view reads the rest.
///
/// Built for long answers: appending a sentence is O(its citations) (citation numbers and
/// paragraph breaks are kept as the sentences arrive, never recomputed from the start),
/// and finished paragraphs never change, so the view only re-lays out the last one.
struct AskState: Equatable, Sendable {
    enum Phase: Equatable, Sendable {
        case streaming
        case finished(AskStatus)
        /// The request failed (backend unreachable, bad response…).
        case failed(String)
        /// The person pressed Stop.
        case stopped
    }

    /// A run of sentences shown as one paragraph. Only the last one grows.
    struct Paragraph: Equatable, Sendable, Identifiable {
        var id: Int
        var sentences: Range<Int>
    }

    /// A paragraph closes once it has this many characters (and at least `minSentences`).
    static let paragraphCharacters = 600
    static let minSentences = 3

    private(set) var sentences: [AskSentence] = []
    var steps: [AskStep] = []
    var sources: [AskSource] = []
    var phase: Phase = .streaming
    var elapsedMs: Int?

    /// Cited passages, numbered in order of first citation (one number per block).
    private(set) var citations: [AskCitation] = []
    private var numbers: [String: Int] = [:]
    private(set) var paragraphs: [Paragraph] = []
    private var openParagraphCharacters = 0

    init() {}

    var isStreaming: Bool { phase == .streaming }

    mutating func apply(_ event: AskEvent) {
        guard isStreaming else { return }
        switch event {
        case .sentence(let sentence):
            append(sentence)
        case .step(let step):
            steps.append(step)
        case .done(let result):
            // The final answer is authoritative, but never take sentences away that were
            // already shown unless the server sent a complete replacement.
            if (!result.answer.isEmpty || sentences.isEmpty) && result.answer != sentences {
                replaceSentences(with: result.answer)
            }
            if let finalSteps = result.steps, finalSteps.count >= steps.count { steps = finalSteps }
            sources = result.sources ?? []
            elapsedMs = result.elapsedMs
            phase = .finished(result.status)
        }
    }

    private mutating func append(_ sentence: AskSentence) {
        let index = sentences.count
        sentences.append(sentence)
        for citation in sentence.citations where numbers[citation.blockId] == nil {
            citations.append(citation)
            numbers[citation.blockId] = citations.count
        }
        if let last = paragraphs.last, openParagraphCharacters < Self.paragraphCharacters
            || last.sentences.count < Self.minSentences {
            paragraphs[paragraphs.count - 1].sentences = last.sentences.lowerBound..<(index + 1)
            openParagraphCharacters += sentence.text.count + 1
        } else {
            paragraphs.append(Paragraph(id: paragraphs.count, sentences: index..<(index + 1)))
            openParagraphCharacters = sentence.text.count
        }
    }

    private mutating func replaceSentences(with answer: [AskSentence]) {
        sentences = []
        citations = []
        numbers = [:]
        paragraphs = []
        openParagraphCharacters = 0
        answer.forEach { append($0) }
    }

    /// The stream ended; if no `done` arrived, keep what streamed or say it broke off.
    mutating func streamEnded() {
        guard isStreaming else { return }
        phase = sentences.isEmpty ? .failed("The answer stopped before it finished.") : .finished(.answered)
    }

    mutating func fail(_ message: String) {
        guard isStreaming else { return }
        phase = .failed(message)
    }

    mutating func stop() {
        guard isStreaming else { return }
        phase = .stopped
    }

    /// The 1-based number shown on a citation chip.
    func number(of citation: AskCitation) -> Int? {
        numbers[citation.blockId]
    }

    /// "Searched for “ryokan|hotel” · Read “Kyoto trip”"
    var trace: String {
        steps.map { step in
            switch step.action {
            case "grep": "Searched for “\(step.detail)”"
            case "read": "Read “\(step.detail)”"
            default: "\(step.action.capitalized) “\(step.detail)”"
            }
        }
        .joined(separator: " · ")
    }
}

/// One question and its answer, streamed from the backend.
@MainActor
@Observable
final class AskTurn: Identifiable {
    let id = UUID()
    let question: String
    private(set) var state: AskState
    let started: Date
    /// When the first sentence arrived (time to first sentence = this − `started`).
    private(set) var firstSentenceAt: Date?

    /// No event for this long ends the answer ("That took too long"); what arrived stays.
    @ObservationIgnored var stallTimeout: Duration = .seconds(45)
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var watchdog: Task<Void, Never>?
    @ObservationIgnored private var lastEvent = ContinuousClock.now
    /// The "First Ask token" signpost interval, open until the first sentence arrives.
    @ObservationIgnored private var firstTokenInterval: OSSignpostIntervalState?

    private static let log = Logger(subsystem: "app.fortress.notes", category: "Ask")

    init(question: String, state: AskState = AskState(), started: Date = .now) {
        self.question = question
        self.state = state
        self.started = started
    }

    isolated deinit {
        task?.cancel()
        watchdog?.cancel()
    }

    /// Seconds from asking to the first sentence on screen.
    var timeToFirstSentence: TimeInterval? {
        firstSentenceAt.map { $0.timeIntervalSince(started) }
    }

    func start(_ stream: AsyncThrowingStream<AskEvent, Error>) {
        task?.cancel()
        lastEvent = .now
        if firstSentenceAt == nil, firstTokenInterval == nil { firstTokenInterval = Signposts.begin("First Ask token") }
        task = Task { [weak self] in
            do {
                for try await event in stream {
                    guard let self, !Task.isCancelled else { return }
                    self.receive(event)
                }
                self?.state.streamEnded()
            } catch is CancellationError {
                self?.state.stop()
            } catch {
                self?.state.fail(Self.message(for: error, partial: !(self?.state.sentences.isEmpty ?? true)))
            }
            self?.watchdog?.cancel()
        }
        startWatchdog()
    }

    private func receive(_ event: AskEvent) {
        lastEvent = .now
        if case .sentence = event, firstSentenceAt == nil, state.isStreaming {
            firstSentenceAt = .now
            if let firstTokenInterval { Signposts.end("First Ask token", firstTokenInterval) }
            firstTokenInterval = nil
            Self.log.info("Ask: first sentence after \(self.timeToFirstSentence ?? 0, format: .fixed(precision: 3)) s")
        }
        state.apply(event)
    }

    /// Ends an answer whose stream went quiet (the server or the model hung).
    private func startWatchdog() {
        watchdog?.cancel()
        let timeout = stallTimeout
        watchdog = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: min(timeout, .seconds(1)))
                guard let self, !Task.isCancelled, self.state.isStreaming else { return }
                if ContinuousClock.now - self.lastEvent >= timeout {
                    self.task?.cancel()
                    self.task = nil
                    self.state.fail(self.state.sentences.isEmpty
                        ? "That took too long to answer."
                        : "The answer stopped coming in.")
                    return
                }
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
        watchdog?.cancel()
        watchdog = nil
        state.stop()
    }

    static func message(for error: Error, partial: Bool = false) -> String {
        if let error = error as? URLError {
            switch error.code {
            case .timedOut:
                return partial ? "The answer stopped coming in." : "That took too long to answer."
            case .networkConnectionLost:
                return partial ? "The connection dropped before the answer finished." : "The connection to the local server dropped."
            default:
                return "The local server isn’t responding."
            }
        }
        return error.localizedDescription
    }
}
