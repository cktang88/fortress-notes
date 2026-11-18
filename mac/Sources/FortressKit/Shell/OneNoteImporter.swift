import Foundation
import Observation

/// One OneNote import, from choosing the export to the report. Lives on the app model so
/// the import keeps going (and can be looked at again) when the sheet is closed.
@MainActor
@Observable
public final class OneNoteImporter {
    public enum Phase: Equatable, Sendable {
        /// Step 1: instructions, file and destination.
        case choosing
        /// Step 2a: the export is going up (0…1).
        case uploading(Double)
        /// Step 2b: the backend converts, imports and links.
        case running(OneNoteImportJob)
        /// Step 3: the report.
        case finished(OneNoteImportJob)
        case failed(message: String, converterMissing: Bool)
    }

    public private(set) var phase: Phase
    /// The export to import.
    public private(set) var file: URL?
    public private(set) var fileSize: Int64?
    /// Where notebook folders go (nil = top level).
    public var folderId: String?
    /// Why the dropped or chosen file can't be used.
    public private(set) var fileProblem: String?

    /// Told when an import has finished (to reload the sidebar).
    @ObservationIgnored public var onFinished: (@MainActor (OneNoteImportJob) -> Void)?
    @ObservationIgnored var pollInterval: Duration = .seconds(1)
    @ObservationIgnored private var task: Task<Void, Never>?

    public init(phase: Phase = .choosing, file: URL? = nil, fileSize: Int64? = nil) {
        self.phase = phase
        self.file = file
        self.fileSize = fileSize
    }

    public var isWorking: Bool {
        switch phase {
        case .uploading, .running: true
        default: false
        }
    }

    public var fileName: String { file?.lastPathComponent ?? "OneNote export" }

    /// Takes a dropped or chosen file if it is a OneNote export.
    @discardableResult
    public func choose(_ url: URL) -> Bool {
        guard OneNoteImport.accepts(url) else {
            fileProblem = "“\(url.lastPathComponent)” isn't a OneNote export. Choose a .zip, .onepkg or .one file."
            return false
        }
        fileProblem = nil
        file = url
        fileSize = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init)
        return true
    }

    /// Uploads the file and follows the job until it finishes.
    public func start(api: APIClient) {
        guard let file, !isWorking else { return }
        task?.cancel()
        task = Task { [weak self] in await self?.run(file: file, api: api) }
    }

    func run(file: URL, api: APIClient) async {
        phase = .uploading(0)
        do {
            let job = try await api.startOneNoteImport(file: file, folderId: folderId) { fraction in
                Task { @MainActor [weak self] in
                    guard let self, case .uploading(let shown) = self.phase, fraction > shown else { return }
                    self.phase = .uploading(fraction)
                }
            }
            try await follow(job, api: api)
        } catch is CancellationError {
            return
        } catch let error as APIError {
            phase = .failed(message: error.message,
                            converterMissing: OneNoteImport.isConverterMissing(error.message))
        } catch {
            phase = .failed(message: Self.describe(error), converterMissing: false)
        }
    }

    /// Polls the job until it is done or failed. A few failed polls in a row (the backend
    /// restarting, say) are tolerated.
    func follow(_ started: OneNoteImportJob, api: APIClient) async throws {
        var job = started
        var misses = 0
        while !job.status.isFinished {
            phase = .running(job)
            // While the backend doesn't answer (restarting, say), wait longer each time
            // instead of hammering it: 1, 2, 4, 8× the interval.
            try await Task.sleep(for: pollInterval * (1 << min(misses, 3)))
            do {
                job = try await api.oneNoteImportJob(id: job.jobId)
                misses = 0
            } catch let error as APIError where error.status == 404 {
                throw error
            } catch {
                misses += 1
                if misses >= 5 { throw error }
            }
        }
        if job.status == .done {
            phase = .finished(job)
            onFinished?(job)
        } else {
            phase = .failed(message: job.error ?? "The import stopped before it finished.", converterMissing: job.converterMissing)
        }
    }

    /// Picks up an import that is still running on the backend (the app was restarted
    /// mid-import), so the sheet shows its progress instead of step 1.
    public func resumeRunningImport(api: APIClient) {
        guard case .choosing = phase, file == nil, task == nil else { return }
        task = Task { [weak self] in
            guard let jobs = try? await api.recentOneNoteImports(),
                  let running = jobs.first(where: { !$0.status.isFinished }) else {
                self?.task = nil
                return
            }
            guard let self, case .choosing = self.phase, self.file == nil else { return }
            if let name = running.filename { self.file = URL(fileURLWithPath: "/" + name) }
            do {
                try await self.follow(running, api: api)
            } catch {
                self.phase = .failed(message: error.localizedDescription, converterMissing: false)
            }
        }
    }

    /// Back to step 1 (after a failure, or to import another export).
    public func reset(keepFile: Bool = false) {
        task?.cancel()
        task = nil
        phase = .choosing
        if !keepFile {
            file = nil
            fileSize = nil
        }
        fileProblem = nil
    }

    private static func describe(_ error: Error) -> String {
        if let error = error as? URLError {
            switch error.code {
            case .cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet, .timedOut:
                return "Fortress's local server isn't responding. Check that it's running, then try again."
            default: break
            }
        }
        if (error as NSError).domain == NSCocoaErrorDomain {
            return "The export couldn't be read. Check that the file is still there."
        }
        return error.localizedDescription
    }
}
