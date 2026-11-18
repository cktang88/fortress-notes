import Foundation
import Observation

/// Everything known so far about one streamed answer. Pure: events go in through
/// `apply(_:)`, the view reads the rest.
struct AskState: Equatable, Sendable {
    enum Phase: Equatable, Sendable {
        case streaming
        case finished(AskStatus)
        /// The request failed (backend unreachable, bad response…).
        case failed(String)
        /// The person pressed Stop.
        case stopped
    }

    var sentences: [AskSentence] = []
    var steps: [AskStep] = []
    var sources: [AskSource] = []
    var phase: Phase = .streaming
    var elapsedMs: Int?

    var isStreaming: Bool { phase == .streaming }

    mutating func apply(_ event: AskEvent) {
        guard isStreaming else { return }
        switch event {
        case .sentence(let sentence):
            sentences.append(sentence)
        case .step(let step):
            steps.append(step)
        case .done(let result):
            // The final answer is authoritative, but never take sentences away that were
            // already shown unless the server sent a complete replacement.
            if !result.answer.isEmpty || sentences.isEmpty { sentences = result.answer }
            if let finalSteps = result.steps, finalSteps.count >= steps.count { steps = finalSteps }
            sources = result.sources ?? []
            elapsedMs = result.elapsedMs
            phase = .finished(result.status)
        }
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

    /// Cited passages, numbered in order of first citation (one number per block).
    var citations: [AskCitation] {
        var seen = Set<String>()
        return sentences.flatMap(\.citations).filter { seen.insert($0.blockId).inserted }
    }

    /// The 1-based number shown on a citation chip.
    func number(of citation: AskCitation) -> Int? {
        citations.firstIndex { $0.blockId == citation.blockId }.map { $0 + 1 }
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

    @ObservationIgnored private var task: Task<Void, Never>?

    init(question: String, state: AskState = AskState(), started: Date = .now) {
        self.question = question
        self.state = state
        self.started = started
    }

    func start(_ stream: AsyncThrowingStream<AskEvent, Error>) {
        task?.cancel()
        task = Task { [weak self] in
            do {
                for try await event in stream {
                    self?.state.apply(event)
                }
                self?.state.streamEnded()
            } catch is CancellationError {
                self?.state.stop()
            } catch {
                self?.state.fail(Self.message(for: error))
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
        state.stop()
    }

    static func message(for error: Error) -> String {
        if error is URLError { return "The local server isn’t responding." }
        return error.localizedDescription
    }
}
