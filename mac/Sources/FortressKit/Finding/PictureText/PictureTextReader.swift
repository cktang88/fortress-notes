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
    @ObservationIgnored public var concurrency: Int
    @ObservationIgnored public var batchSize: Int
    @ObservationIgnored public var idleDelay: Duration
    @ObservationIgnored public var retryDelay: Duration
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var sleeper: Task<Void, Never>?
    @ObservationIgnored private var backendKey: String?
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
    }

    isolated deinit {
        task?.cancel()
        sleeper?.cancel()
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
        task = Task { [weak self] in await self?.run(backend: backend) }
    }

    public func stop() {
        task?.cancel()
        sleeper?.cancel()
        task = nil
        sleeper = nil
        backendKey = nil
        isWorking = false
        reading = []
    }

    /// Something new may be waiting (a picture was pasted): look now instead of after the idle delay.
    public func wake() {
        sleeper?.cancel()
    }

    // MARK: The loop

    private func run(backend: any PictureTextBackend) async {
        while !Task.isCancelled {
            switch await readNextBatch(backend: backend) {
            case .worked: continue
            case .idle: await sleep(idleDelay)
            case .unreachable: await sleep(retryDelay)
            }
        }
    }

    enum BatchOutcome: Equatable { case worked, idle, unreachable }

    /// Asks for the next few files and reads them (tests drive this directly).
    func readNextBatch(backend: any PictureTextBackend) async -> BatchOutcome {
        let page: OCRPending
        do {
            page = try await backend.ocrPending(model: recognizer.model, limit: min(100, batchSize + skipped.count))
        } catch {
            lastError = error.localizedDescription
            isWorking = false
            return .unreachable
        }
        lastError = nil
        remaining = page.total
        let items = page.items.filter { !skipped.contains($0.contentHash) }.prefix(batchSize)
        guard !items.isEmpty, !Task.isCancelled else {
            isWorking = false
            return .idle
        }
        isWorking = true
        let reachable = await read(Array(items), backend: backend)
        return reachable ? .worked : .unreachable
    }

    /// Reads `items`, at most `concurrency` at once; false when the backend went away.
    private func read(_ items: [OCRPending.Item], backend: any PictureTextBackend) async -> Bool {
        let recognizer = recognizer
        let limit = ProcessInfo.processInfo.isLowPowerModeEnabled ? 1 : concurrency
        var reachable = true
        await withTaskGroup(of: (String, ReadOutcome).self) { group in
            var pending = items[...]
            func startNext() -> Bool {
                guard reachable, let item = pending.popFirst() else { return false }
                reading.insert(item.contentHash)
                peakConcurrency = max(peakConcurrency, reading.count)
                group.addTask {
                    (item.contentHash, await Self.readOne(item, backend: backend, recognizer: recognizer))
                }
                return true
            }
            for _ in 0..<limit {
                if !startNext() { break }
            }
            while let (contentHash, outcome) = await group.next() {
                reading.remove(contentHash)
                switch outcome {
                case .stored:
                    lastStored = contentHash
                    storedCount += 1
                    if let remaining { self.remaining = max(0, remaining - 1) }
                case .refused:
                    skipped.insert(contentHash)
                case .unreachable(let message):
                    lastError = message
                    reachable = false
                case .cancelled:
                    reachable = false
                }
                _ = startNext()
            }
        }
        return reachable
    }

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

    private func sleep(_ delay: Duration) async {
        let sleeper = Task { try? await Task.sleep(for: delay) }
        self.sleeper = sleeper
        await sleeper.value
        if self.sleeper == sleeper { self.sleeper = nil }
    }
}
