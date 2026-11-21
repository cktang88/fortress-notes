import Foundation

// The OneNote download from Microsoft as the backend reports it (app/onenote_graph.py:
// `GET /api/import/onenote-graph` → availability and the latest job;
// `POST /api/import/onenote-graph[/notebooks]` → a job; `GET …/{job_id}`). The backend
// runs tools/fortress-onenote-graph and, once the download is written, starts an
// ordinary OneNote import job (`import_job_id`). Decoding is lenient like the import
// job's. Foundation-only for unit tests.

/// Which kind of Microsoft account the notebooks are in.
public enum OneNoteGraphAccount: String, CaseIterable, Hashable, Sendable {
    case personal, work

    public var title: String {
        switch self {
        case .personal: String(localized: "Personal")
        case .work: String(localized: "Work or school")
        }
    }
}

public struct OneNoteGraphJob: Decodable, Hashable, Sendable, Identifiable {
    public enum Kind: String, Hashable, Sendable { case download, notebooks }

    public enum Status: String, Hashable, Sendable {
        case queued, starting, signingIn = "signing_in", downloading, converting, importing, done, failed, cancelled

        public var isFinished: Bool { self == .done || self == .failed || self == .cancelled }
    }

    public struct Progress: Decodable, Hashable, Sendable {
        /// The tool's phase: signin, hierarchy, sections, pages, convert, done.
        public var phase: String?
        public var done: Int
        public var total: Int
        /// "Notebook › Section › Page".
        public var current: String?

        public init(phase: String? = nil, done: Int = 0, total: Int = 0, current: String? = nil) {
            self.phase = phase
            self.done = done
            self.total = total
            self.current = current
        }

        enum CodingKeys: String, CodingKey { case phase, done, total, current }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            phase = try? container.decodeIfPresent(String.self, forKey: .phase)
            done = (try? container.decodeIfPresent(Int.self, forKey: .done)) ?? 0
            total = (try? container.decodeIfPresent(Int.self, forKey: .total)) ?? 0
            current = try? container.decodeIfPresent(String.self, forKey: .current)
        }

        /// 0…1 while pages download.
        public var fraction: Double? {
            guard phase == "pages", total > 0 else { return nil }
            return min(1, Double(done) / Double(total))
        }
    }

    /// The browser sign-in didn't work: the user enters this code at the page instead.
    public struct DeviceCode: Decodable, Hashable, Sendable {
        public var userCode: String
        public var verificationURI: String
        public var expiresAt: Date?

        public init(userCode: String, verificationURI: String, expiresAt: Date? = nil) {
            self.userCode = userCode
            self.verificationURI = verificationURI
            self.expiresAt = expiresAt
        }

        enum CodingKeys: String, CodingKey { case userCode = "user_code", verificationURI = "verification_uri", expiresAt = "expires_at" }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            userCode = (try? container.decodeIfPresent(String.self, forKey: .userCode)) ?? ""
            verificationURI = (try? container.decodeIfPresent(String.self, forKey: .verificationURI)) ?? "https://microsoft.com/devicelogin"
            expiresAt = OneNoteGraph.date(try? container.decodeIfPresent(String.self, forKey: .expiresAt))
        }

        public var url: URL? { URL(string: verificationURI) }
    }

    /// Microsoft's rate limit: the download pauses until `until`.
    public struct Wait: Decodable, Hashable, Sendable {
        public var seconds: Double
        public var reason: String
        public var until: Date?

        public init(seconds: Double, reason: String = "", until: Date? = nil) {
            self.seconds = seconds
            self.reason = reason
            self.until = until
        }

        enum CodingKeys: String, CodingKey { case seconds, reason, until }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            seconds = (try? container.decodeIfPresent(Double.self, forKey: .seconds)) ?? 0
            reason = (try? container.decodeIfPresent(String.self, forKey: .reason)) ?? ""
            until = OneNoteGraph.date(try? container.decodeIfPresent(String.self, forKey: .until))
        }
    }

    public struct Notebook: Decodable, Hashable, Sendable, Identifiable {
        public var name: String
        public var sections: Int
        public var id: String { name }

        public init(name: String, sections: Int = 0) {
            self.name = name
            self.sections = sections
        }

        enum CodingKeys: String, CodingKey { case name, sections }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            name = (try? container.decodeIfPresent(String.self, forKey: .name)) ?? "Notebook"
            sections = (try? container.decodeIfPresent(Int.self, forKey: .sections)) ?? 0
        }
    }

    public var jobId: String
    public var kind: Kind
    public var status: Status
    public var progress: Progress
    public var deviceCode: DeviceCode?
    public var wait: Wait?
    /// Seconds the download probably still takes (once enough pages are behind it).
    public var etaSeconds: Int?
    public var warningCount: Int
    /// The notebooks (a `notebooks` job, when done).
    public var notebooks: [Notebook]?
    /// The OneNote import job the download handed its result to.
    public var importJobId: String?
    public var error: String?
    /// Interrupted by throttling or the network: running it again continues.
    public var resumable: Bool

    public var id: String { jobId }

    public init(jobId: String, kind: Kind = .download, status: Status, progress: Progress = Progress(),
                deviceCode: DeviceCode? = nil, wait: Wait? = nil, etaSeconds: Int? = nil, warningCount: Int = 0,
                notebooks: [Notebook]? = nil, importJobId: String? = nil, error: String? = nil, resumable: Bool = false) {
        self.jobId = jobId
        self.kind = kind
        self.status = status
        self.progress = progress
        self.deviceCode = deviceCode
        self.wait = wait
        self.etaSeconds = etaSeconds
        self.warningCount = warningCount
        self.notebooks = notebooks
        self.importJobId = importJobId
        self.error = error
        self.resumable = resumable
    }

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id", kind, status, progress, deviceCode = "device_code", wait, etaSeconds = "eta_seconds"
        case warningCount = "warning_count", notebooks, importJobId = "import_job_id", error, resumable
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        jobId = try container.decode(String.self, forKey: .jobId)
        kind = Kind(rawValue: (try? container.decodeIfPresent(String.self, forKey: .kind)) ?? "") ?? .download
        // A status this app doesn't know yet is still work in progress.
        status = Status(rawValue: (try? container.decodeIfPresent(String.self, forKey: .status)) ?? "queued") ?? .downloading
        progress = (try? container.decodeIfPresent(Progress.self, forKey: .progress)) ?? Progress()
        deviceCode = try? container.decodeIfPresent(DeviceCode.self, forKey: .deviceCode)
        wait = try? container.decodeIfPresent(Wait.self, forKey: .wait)
        etaSeconds = try? container.decodeIfPresent(Int.self, forKey: .etaSeconds)
        warningCount = (try? container.decodeIfPresent(Int.self, forKey: .warningCount)) ?? 0
        notebooks = try? container.decodeIfPresent([Notebook].self, forKey: .notebooks)
        importJobId = try? container.decodeIfPresent(String.self, forKey: .importJobId)
        error = try? container.decodeIfPresent(String.self, forKey: .error)
        resumable = (try? container.decodeIfPresent(Bool.self, forKey: .resumable)) ?? false
    }
}

/// `GET /api/import/onenote-graph`: whether the download can run here, and the latest job.
public struct OneNoteGraphStatus: Decodable, Hashable, Sendable {
    public var available: Bool
    /// Why not, and how to fix it.
    public var problem: String?
    public var job: OneNoteGraphJob?

    public init(available: Bool, problem: String? = nil, job: OneNoteGraphJob? = nil) {
        self.available = available
        self.problem = problem
        self.job = job
    }

    enum CodingKeys: String, CodingKey { case available, problem, job }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        available = (try? container.decodeIfPresent(Bool.self, forKey: .available)) ?? false
        problem = try? container.decodeIfPresent(String.self, forKey: .problem)
        job = try? container.decodeIfPresent(OneNoteGraphJob.self, forKey: .job)
    }
}

/// Wording and checks for the "Download from Microsoft" part of the sheet, kept testable.
public enum OneNoteGraph {
    public static let entraURL = URL(string: "https://entra.microsoft.com")!

    /// The one-time app registration in Microsoft Entra (tools/fortress-onenote-graph/README.md).
    public static let registrationSteps: [String] = [
        String(localized: "Sign in at entra.microsoft.com with your Microsoft account. (If it says you have no directory, a free Azure account creates one.)"),
        String(localized: "Open App registrations › New registration. Give it any name. Supported account types: Multitenant and personal Microsoft accounts."),
        String(localized: "Redirect URI: Public client/native (mobile & desktop), http://localhost. Click Register."),
        String(localized: "Authentication › Allow public client flows: Yes. Save."),
        String(localized: "API permissions › Add a permission › Microsoft Graph › Delegated: Notes.Read, offline_access and User.Read (work or school: also Notes.Read.All)."),
        String(localized: "Copy the Application (client) ID from the Overview page into the field above. No client secret is needed."),
    ]

    /// An Application (client) ID is a GUID.
    public static func isValidClientId(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.count == 36 && UUID(uuidString: trimmed) != nil
    }

    /// "Waiting for Microsoft's rate limit… resumes at 14:05".
    public static func waitText(_ wait: OneNoteGraphJob.Wait) -> String {
        guard let until = wait.until else { return String(localized: "Waiting for Microsoft's rate limit…") }
        let time = until.formatted(date: .omitted, time: .shortened)
        return String(localized: "Waiting for Microsoft's rate limit… resumes at \(time)")
    }

    /// "About 25 min left".
    public static func etaText(_ seconds: Int) -> String {
        if seconds < 60 { return String(localized: "Less than a minute left") }
        let rounded = seconds < 3600 ? (seconds + 59) / 60 * 60 : (seconds + 299) / 300 * 300
        let text = Duration.seconds(rounded).formatted(.units(allowed: [.hours, .minutes], width: .abbreviated))
        return String(localized: "About \(text) left", comment: "Time a download still takes; the argument is a duration")
    }

    /// "Downloading 12 of 340 pages" style detail for the download step.
    public static func pageCount(_ progress: OneNoteGraphJob.Progress) -> String? {
        guard progress.phase == "pages", progress.total > 0 else { return nil }
        return String(AttributedString(localized: "\(progress.done) of ^[\(progress.total) page](inflect: true)").characters)
    }

    /// The line under the progress bar.
    public static func caption(_ job: OneNoteGraphJob) -> String {
        switch job.status {
        case .queued, .starting: return String(localized: "Getting the downloader ready…")
        case .signingIn:
            return job.deviceCode == nil ? String(localized: "Sign in to Microsoft in the browser window that opened…")
                                         : String(localized: "Waiting for you to sign in with the code…")
        case .downloading:
            switch job.progress.phase {
            case "hierarchy": return String(localized: "Listing your notebooks…")
            case "sections": return job.progress.current ?? String(localized: "Listing pages…")
            default: return job.progress.current ?? String(localized: "Downloading pages…")
            }
        case .converting: return String(localized: "Preparing the pages for import…")
        case .importing, .done: return String(localized: "Starting the import…")
        case .failed, .cancelled: return job.error ?? ""
        }
    }

    static func date(_ text: String?) -> Date? {
        guard let text else { return nil }
        return try? Date(text, strategy: .iso8601)
    }
}
