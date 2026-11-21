import Foundation
import Observation

/// One OneNote import, from choosing where the notebooks come from to the report. Lives on
/// the app model so the import keeps going (and can be looked at again) when the sheet is
/// closed.
///
/// Two ways in: **Download from Microsoft** (the backend runs tools/fortress-onenote-graph
/// with the user's own Entra app id, then imports what it downloaded) and **Import a file**
/// (an export is uploaded and imported).
@MainActor
@Observable
public final class OneNoteImporter {
    public enum Source: String, CaseIterable, Hashable, Sendable {
        case microsoft, file
    }

    public enum Phase: Equatable, Sendable {
        /// Step 1: where the notebooks come from, and the destination.
        case choosing
        /// Step 2a: the export is going up (0…1).
        case uploading(Double)
        /// Step 2a': the backend signs in to Microsoft and downloads (or lists notebooks).
        case downloading(OneNoteGraphJob)
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

    /// Which way step 1 shows.
    public var source: Source
    /// Where the running (or last) import came from.
    public private(set) var origin: Source
    /// The user's own Entra app's Application (client) ID; remembered (it is not a secret).
    public var clientId: String {
        didSet {
            guard clientId != oldValue else { return }
            defaults.set(clientId.trimmingCharacters(in: .whitespacesAndNewlines), forKey: Self.clientIdKey)
            notebookChoices = nil
        }
    }
    public var account: OneNoteGraphAccount {
        didSet {
            guard account != oldValue else { return }
            defaults.set(account.rawValue, forKey: Self.accountKey)
            notebookChoices = nil
        }
    }
    /// Whether the backend can run the download (nil until asked).
    public private(set) var graphStatus: OneNoteGraphStatus?
    /// The account's notebooks once listed (nil: download all, not listed yet).
    public private(set) var notebookChoices: [OneNoteGraphJob.Notebook]?
    /// Which of `notebookChoices` to download.
    public var selectedNotebooks: Set<String> = []
    /// A line for step 1 after a stopped download or import.
    public private(set) var notice: String?
    /// Cancel was asked for and the backend hasn't stopped yet.
    public private(set) var isCancelling = false

    static let clientIdKey = "oneNoteGraphClientID"
    static let accountKey = "oneNoteGraphAccount"
    @ObservationIgnored private let defaults: UserDefaults

    /// Told when an import has finished (to reload the sidebar).
    @ObservationIgnored public var onFinished: (@MainActor (OneNoteImportJob) -> Void)?
    @ObservationIgnored var pollInterval: Duration = .seconds(1)
    @ObservationIgnored private var task: Task<Void, Never>?

    public init(phase: Phase = .choosing, file: URL? = nil, fileSize: Int64? = nil,
                source: Source? = nil, defaults: UserDefaults = .standard) {
        self.phase = phase
        self.file = file
        self.fileSize = fileSize
        self.defaults = defaults
        let chosen = source ?? (file == nil ? .microsoft : .file)
        self.source = chosen
        self.origin = chosen
        clientId = defaults.string(forKey: Self.clientIdKey) ?? ""
        account = defaults.string(forKey: Self.accountKey).flatMap(OneNoteGraphAccount.init(rawValue:)) ?? .personal
    }

    public var isWorking: Bool {
        switch phase {
        case .uploading, .downloading, .running: true
        default: false
        }
    }

    public var fileName: String { file?.lastPathComponent ?? "OneNote export" }

    /// Whether "Download and Import" can start.
    public var canDownload: Bool {
        OneNoteGraph.isValidClientId(clientId) && graphStatus?.available != false
            && (notebookChoices == nil || !selectedNotebooks.isEmpty)
    }

    /// The notebooks to ask for: nil for all.
    var requestedNotebooks: [String]? {
        guard let choices = notebookChoices, selectedNotebooks.count < choices.count else { return nil }
        return choices.map(\.name).filter(selectedNotebooks.contains)
    }

    public func setNotebook(_ name: String, selected: Bool) {
        if selected { selectedNotebooks.insert(name) } else { selectedNotebooks.remove(name) }
    }

    /// Takes a dropped or chosen file if it is a OneNote export.
    @discardableResult
    public func choose(_ url: URL) -> Bool {
        guard OneNoteImport.accepts(url) else {
            fileProblem = "“\(url.lastPathComponent)” isn't a OneNote export. Choose a .zip, .onepkg or .one file."
            return false
        }
        fileProblem = nil
        source = .file
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
        origin = .file
        notice = nil
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

    // MARK: Download from Microsoft

    /// Asks the backend whether it can run the download, and picks up a download that is
    /// still running (the sheet was closed, or the app restarted).
    public func checkMicrosoft(api: APIClient) async {
        guard let status = try? await api.oneNoteGraphStatus() else { return }
        graphStatus = status
        if let job = status.job, !job.status.isFinished, case .choosing = phase, task == nil {
            origin = .microsoft
            source = .microsoft
            phase = .downloading(job)
            task = Task { [weak self] in await self?.followDownload(job, api: api) }
        }
    }

    /// Signs in, downloads the chosen notebooks and imports them.
    public func startDownload(api: APIClient) {
        guard canDownload, !isWorking else { return }
        task?.cancel()
        task = Task { [weak self] in await self?.runDownload(kind: .download, api: api) }
    }

    /// Signs in and lists the account's notebooks, so some can be left out.
    public func listNotebooks(api: APIClient) {
        guard OneNoteGraph.isValidClientId(clientId), graphStatus?.available != false, !isWorking else { return }
        task?.cancel()
        task = Task { [weak self] in await self?.runDownload(kind: .notebooks, api: api) }
    }

    /// The account's notebooks to choose from, all selected.
    func offerNotebooks(_ notebooks: [OneNoteGraphJob.Notebook]) {
        notebookChoices = notebooks
        selectedNotebooks = Set(notebooks.map(\.name))
    }

    /// Back to every notebook (the choice list goes away).
    public func clearNotebookChoices() {
        notebookChoices = nil
        selectedNotebooks = []
    }

    func runDownload(kind: OneNoteGraphJob.Kind, api: APIClient) async {
        origin = .microsoft
        notice = nil
        isCancelling = false
        phase = .downloading(OneNoteGraphJob(jobId: "", kind: kind, status: .queued))
        do {
            let job = try await api.startOneNoteGraph(
                kind: kind, clientId: clientId, account: account,
                notebooks: kind == .download ? requestedNotebooks : nil, folderId: folderId)
            await followDownload(job, api: api)
        } catch is CancellationError {
            return
        } catch let error as APIError {
            phase = .failed(message: error.message, converterMissing: false)
        } catch {
            phase = .failed(message: Self.describe(error), converterMissing: false)
        }
    }

    /// Polls the download until it ends; a finished download's import is followed next.
    func followDownload(_ started: OneNoteGraphJob, api: APIClient) async {
        var job = started
        var misses = 0
        do {
            while !job.status.isFinished {
                phase = .downloading(job)
                try await Task.sleep(for: pollInterval * (1 << min(misses, 3)))
                do {
                    job = try await api.oneNoteGraphJob(id: job.jobId)
                    misses = 0
                } catch let error as APIError where error.status == 404 {
                    throw error
                } catch {
                    misses += 1
                    if misses >= 5 { throw error }
                }
            }
            isCancelling = false
            switch job.status {
            case .done where job.kind == .notebooks:
                offerNotebooks(job.notebooks ?? [])
                phase = .choosing
            case .done:
                guard let importId = job.importJobId else {
                    phase = .failed(message: String(localized: "The download finished but nothing was imported."), converterMissing: false)
                    return
                }
                try await follow(OneNoteImportJob(jobId: importId, status: .queued), api: api)
            case .cancelled:
                notice = job.error ?? String(localized: "Download stopped. Download and Import again continues where it stopped.")
                phase = .choosing
            default:
                phase = .failed(message: job.error ?? String(localized: "The download stopped before it finished."), converterMissing: false)
            }
        } catch is CancellationError {
            return
        } catch let error as APIError {
            isCancelling = false
            phase = .failed(message: error.message, converterMissing: false)
        } catch {
            isCancelling = false
            phase = .failed(message: Self.describe(error), converterMissing: false)
        }
    }

    /// Stops the download or import that is running. A stopped download resumes next
    /// time; pages already imported are kept.
    public func cancel(api: APIClient) {
        let target: (download: Bool, id: String)
        switch phase {
        case .downloading(let job) where !job.jobId.isEmpty: target = (true, job.jobId)
        case .running(let job): target = (false, job.jobId)
        default: return
        }
        isCancelling = true
        Task { [weak self] in
            do {
                if target.download {
                    try await api.cancelOneNoteGraph(id: target.id)
                } else {
                    try await api.cancelOneNoteImport(id: target.id)
                }
            } catch {
                // Already ended: the poll shows how.
                self?.isCancelling = false
            }
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
        isCancelling = false
        if job.status == .done {
            phase = .finished(job)
            onFinished?(job)
        } else if job.status == .cancelled {
            notice = job.error ?? String(localized: "Import cancelled. Pages imported before cancelling are kept.")
            phase = .choosing
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
                // No import running: a download from Microsoft may be.
                await self?.checkMicrosoft(api: api)
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
        isCancelling = false
        notice = nil
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
