import Foundation
import Observation

/// Reads the text in the workspace's pictures and PDFs on this Mac (Apple Vision) and
/// gives it to the backend, which prefers it to its own CPU reading and indexes it for
/// search, related notes and Ask (backend/app/ocr.py, "Which reading wins").
///
/// It asks the backend for what its reader hasn't read yet (newest first, a few at a
/// time), downloads each file, reads it off the main thread with at most `concurrency`
/// reads at once (one in Low Power Mode), and stores the text, or why it couldn't be
/// read. While it asks, the backend's CPU reader leaves the backlog to it, so nothing is
/// read twice for nothing. When there's nothing left it checks again every `idleDelay`
/// (or at once on `wake()`); when the backend can't be reached, after `retryDelay`.
///
/// The work itself runs in a `PictureTextLoop`, off the main actor: asking for the next
/// batch and starting the next read never wait for the main thread, so a busy main
/// thread (a long note opening, a big render) doesn't stall the backlog between reads.
/// This object only shows the loop's progress, which the loop sends to the main actor
/// without waiting for it to be shown.
@MainActor
@Observable
public final class PictureTextReader {
    /// Pictures and PDFs this Mac has yet to read (nil until the backend has said).
    public private(set) var remaining: Int?
    /// Content hashes being read right now.
    public private(set) var reading: Set<String> = []
    /// Whether it is working through a backlog (not idle, not waiting for the backend).
    public private(set) var isWorking = false
    /// The last picture whose text was stored, and how many have been: an inspector
    /// showing that picture reloads.
    public private(set) var lastStored: String?
    public private(set) var storedCount = 0
    /// Why the last attempt to reach the backend failed, until one succeeds.
    public private(set) var lastError: String?

    @ObservationIgnored public let recognizer: any TextRecognizer
    /// These four take effect when the reader (re)starts.
    @ObservationIgnored public var concurrency: Int
    @ObservationIgnored public var batchSize: Int
    @ObservationIgnored public var idleDelay: Duration
    @ObservationIgnored public var retryDelay: Duration
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var backendKey: String?
    /// The loop doing the work; a new one on each start and stop, so a stopped loop's
    /// late progress is never shown.
    @ObservationIgnored private(set) var loop: PictureTextLoop
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var shownVersion = 0
    /// Files the backend refused to take a reading for: not asked about again this launch.
    @ObservationIgnored private(set) var skipped: Set<String> = []
    /// The most reads that ran at once (tests).
    @ObservationIgnored private(set) var peakConcurrency = 0

    /// Whether the app reads pictures with Apple Vision (Settings; on by default).
    public static let enabledKey = "readsPictureTextWithVision"
    public static var isEnabledInSettings: Bool {
        UserDefaults.standard.object(forKey: enabledKey) as? Bool ?? true
    }

    public init(recognizer: any TextRecognizer = VisionTextRecognizer(),
                concurrency: Int = PictureTextReader.defaultConcurrency, batchSize: Int = 8,
                idleDelay: Duration = .seconds(30), retryDelay: Duration = .seconds(30)) {
        self.recognizer = recognizer
        self.concurrency = max(1, concurrency)
        self.batchSize = max(1, batchSize)
        self.idleDelay = idleDelay
        self.retryDelay = retryDelay
        loop = PictureTextLoop(settings: .init(recognizer: recognizer, concurrency: 1, batchSize: 1,
                                               idleDelay: idleDelay, retryDelay: retryDelay),
                               state: .init(), publish: { _, _ in })
        loop = makeLoop()
    }

    isolated deinit {
        task?.cancel()
    }

    /// Two reads at once on a Mac with eight or more cores, else one: Vision uses the
    /// Neural Engine and GPU too, and the app must stay responsive.
    public nonisolated static var defaultConcurrency: Int {
        ProcessInfo.processInfo.activeProcessorCount >= 8 ? 2 : 1
    }

    public var isRunning: Bool { task != nil }

    /// Starts reading with `backend` (`key` names it: starting again with the same key
    /// keeps the running reader; another key restarts it).
    public func start(backend: any PictureTextBackend, key: String) {
        guard key != backendKey || task == nil else { return }
        stop()
        backendKey = key
        let loop = loop
        // Detached: the loop starts without first waiting for the main thread.
        task = Task.detached { await loop.run(backend: backend) }
    }

    public func stop() {
        task?.cancel()
        task = nil
        backendKey = nil
        isWorking = false
        reading = []
        loop = makeLoop()
    }

    /// Something new may be waiting (a picture was pasted): look now instead of after
    /// the idle delay (or, while a batch is being read, right after it).
    public func wake() {
        let loop = loop
        Task.detached { await loop.wake() }
    }

    enum BatchOutcome: Equatable, Sendable { case worked, idle, unreachable }

    /// Asks for the next few files and reads them (tests drive this directly); what it
    /// did shows here by the time it returns.
    func readNextBatch(backend: any PictureTextBackend) async -> BatchOutcome {
        let (outcome, state, version) = await loop.readNextBatchAndReport(backend: backend)
        show(state, generation: generation, version: version)
        return outcome
    }

    /// A loop for the current settings that starts from what has been shown so far.
    private func makeLoop() -> PictureTextLoop {
        generation += 1
        shownVersion = 0
        let generation = generation
        let state = PictureTextLoop.State(
            remaining: remaining, lastStored: lastStored, storedCount: storedCount,
            lastError: lastError, skipped: skipped, peakConcurrency: peakConcurrency)
        let settings = PictureTextLoop.Settings(
            recognizer: recognizer, concurrency: concurrency, batchSize: batchSize,
            idleDelay: idleDelay, retryDelay: retryDelay)
        return PictureTextLoop(settings: settings, state: state) { [weak self] state, version in
            guard let reader = self else { return }
            Task { @MainActor in reader.show(state, generation: generation, version: version) }
        }
    }

    /// Shows the loop's progress (only the current loop's, and never older than what is shown).
    private func show(_ state: PictureTextLoop.State, generation: Int, version: Int) {
        guard generation == self.generation, version > shownVersion else { return }
        shownVersion = version
        // Set only what changed, so views observing the rest aren't invalidated.
        if remaining != state.remaining { remaining = state.remaining }
        if reading != state.reading { reading = state.reading }
        if isWorking != state.isWorking { isWorking = state.isWorking }
        if lastStored != state.lastStored { lastStored = state.lastStored }
        if storedCount != state.storedCount { storedCount = state.storedCount }
        if lastError != state.lastError { lastError = state.lastError }
        skipped = state.skipped
        peakConcurrency = state.peakConcurrency
    }
}

/// The picture reader's work, off the main actor (see `PictureTextReader`).
actor PictureTextLoop {
    struct Settings: Sendable {
        var recognizer: any TextRecognizer
        var concurrency: Int
        var batchSize: Int
        var idleDelay: Duration
        var retryDelay: Duration
    }

    /// What `PictureTextReader` shows.
    struct State: Sendable, Equatable {
        var remaining: Int?
        var reading: Set<String> = []
        var isWorking = false
        var lastStored: String?
        var storedCount = 0
        var lastError: String?
        var skipped: Set<String> = []
        var peakConcurrency = 0
    }

    private let settings: Settings
    private let publish: @Sendable (State, Int) -> Void
    /// Each change is published with a higher version, without waiting for it to be shown.
    private(set) var state: State {
        didSet {
            guard state != oldValue else { return }
            version += 1
            publish(state, version)
        }
    }
    private var version = 0
    private var sleeper: Task<Void, Never>?
    /// `wake()` came while there was no sleep to cut short: skip the next one.
    private var woken = false

    init(settings: Settings, state: State, publish: @escaping @Sendable (State, Int) -> Void) {
        self.settings = settings
        self.state = state
        self.publish = publish
    }

    func run(backend: any PictureTextBackend) async {
        while !Task.isCancelled {
            switch await readNextBatch(backend: backend) {
            case .worked: continue
            case .idle: await sleep(settings.idleDelay)
            case .unreachable: await sleep(settings.retryDelay)
            }
        }
    }

    func wake() {
        woken = true
        sleeper?.cancel()
    }

    func readNextBatchAndReport(
        backend: any PictureTextBackend
    ) async -> (PictureTextReader.BatchOutcome, State, Int) {
        let outcome = await readNextBatch(backend: backend)
        return (outcome, state, version)
    }

    /// Asks for the next few files and reads them.
    func readNextBatch(backend: any PictureTextBackend) async -> PictureTextReader.BatchOutcome {
        let recognizer = settings.recognizer
        let page: OCRPending
        do {
            page = try await backend.ocrPending(
                model: recognizer.model, limit: min(100, settings.batchSize + state.skipped.count))
        } catch {
            state.lastError = error.localizedDescription
            state.isWorking = false
            return .unreachable
        }
        let items = page.items.filter { !state.skipped.contains($0.contentHash) }.prefix(settings.batchSize)
        var next = state
        next.lastError = nil
        next.remaining = page.total
        guard !items.isEmpty, !Task.isCancelled else {
            next.isWorking = false
            state = next
            return .idle
        }
        next.isWorking = true
        state = next
        let reachable = await read(Array(items), backend: backend)
        return reachable ? .worked : .unreachable
    }

    /// Reads `items`, at most `concurrency` at once; false when the backend went away.
    private func read(_ items: [OCRPending.Item], backend: any PictureTextBackend) async -> Bool {
        let recognizer = settings.recognizer
        let limit = ProcessInfo.processInfo.isLowPowerModeEnabled ? 1 : settings.concurrency
        var reachable = true
        await withTaskGroup(of: (String, PictureTextReader.ReadOutcome).self) { group in
            var pending = items[...]
            var running = 0
            while true {
                while reachable, running < limit, let item = pending.popFirst() {
                    state.reading.insert(item.contentHash)
                    state.peakConcurrency = max(state.peakConcurrency, state.reading.count)
                    running += 1
                    group.addTask {
                        (item.contentHash, await PictureTextReader.readOne(item, backend: backend, recognizer: recognizer))
                    }
                }
                guard running > 0, let (contentHash, outcome) = await group.next() else { break }
                running -= 1
                var next = state
                next.reading.remove(contentHash)
                switch outcome {
                case .stored:
                    next.lastStored = contentHash
                    next.storedCount += 1
                    if let remaining = next.remaining { next.remaining = max(0, remaining - 1) }
                case .refused:
                    next.skipped.insert(contentHash)
                case .unreachable(let message):
                    next.lastError = message
                    reachable = false
                case .cancelled:
                    reachable = false
                }
                state = next
            }
        }
        return reachable
    }

    private func sleep(_ delay: Duration) async {
        if woken {
            woken = false
            return
        }
        let sleeper = Task { _ = try? await Task.sleep(for: delay) }
        self.sleeper = sleeper
        await withTaskCancellationHandler { await sleeper.value } onCancel: { sleeper.cancel() }
        if self.sleeper == sleeper { self.sleeper = nil }
        woken = false
    }
}

extension PictureTextReader {
    enum ReadOutcome: Sendable, Equatable {
        case stored
        /// The backend answered with an error (the file was deleted meanwhile, say).
        case refused
        case unreachable(String)
        case cancelled
    }

    /// Downloads, reads and stores one file, off the main actor. A file that can't be
    /// read is stored as failed with the reason (the backend's CPU reader then tries it).
    nonisolated static func readOne(
        _ item: OCRPending.Item, backend: any PictureTextBackend, recognizer: any TextRecognizer
    ) async -> ReadOutcome {
        let clock = ContinuousClock()
        let started = clock.now
        var result: OCRResult
        do {
            let data = try await backend.media(at: item.mediaURL)
            let found = try await recognizer.recognize(data, mimeType: item.mimeType)
            result = OCRResult(model: recognizer.model, status: "done", text: found.text,
                               method: found.method, pages: found.pages)
        } catch is CancellationError {
            return .cancelled
        } catch let error as URLError {
            return error.code == .cancelled ? .cancelled : .unreachable(error.localizedDescription)
        } catch {
            result = OCRResult(model: recognizer.model, status: "failed",
                               error: String(error.localizedDescription.prefix(500)))
        }
        if Task.isCancelled { return .cancelled }
        let elapsed = clock.now - started
        result.durationMs = Int((elapsed / .milliseconds(1)).rounded())
        do {
            try await backend.storeOCR(contentHash: item.contentHash, result: result)
            return .stored
        } catch let error as URLError {
            return error.code == .cancelled ? .cancelled : .unreachable(error.localizedDescription)
        } catch is CancellationError {
            return .cancelled
        } catch {
            return .refused
        }
    }
}
