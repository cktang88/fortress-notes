import Foundation

// What the OneNote import endpoints answer (`POST /api/import/onenote`,
// `GET /api/import/onenote/{job_id}`, `GET /api/import/onenote`). Decoding is lenient:
// missing counts read as zero and unknown statuses as still running, so a newer
// backend never breaks the sheet. Foundation-only for unit tests.

public struct OneNoteImportJob: Decodable, Hashable, Sendable, Identifiable {
    public enum Status: String, Hashable, Sendable {
        case queued, converting, importing, linking, done, failed

        public var isFinished: Bool { self == .done || self == .failed }
    }

    public struct Progress: Decodable, Hashable, Sendable {
        public var pagesTotal: Int
        public var pagesDone: Int
        /// "Notebook › Section › Page" being imported.
        public var current: String?

        public init(pagesTotal: Int = 0, pagesDone: Int = 0, current: String? = nil) {
            self.pagesTotal = pagesTotal
            self.pagesDone = pagesDone
            self.current = current
        }

        enum CodingKeys: String, CodingKey {
            case pagesTotal = "pages_total", pagesDone = "pages_done", current
        }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            pagesTotal = (try? container.decodeIfPresent(Int.self, forKey: .pagesTotal)) ?? 0
            pagesDone = (try? container.decodeIfPresent(Int.self, forKey: .pagesDone)) ?? 0
            current = try? container.decodeIfPresent(String.self, forKey: .current)
        }

        /// 0…1 when the total is known.
        public var fraction: Double? {
            guard pagesTotal > 0 else { return nil }
            return min(1, Double(pagesDone) / Double(pagesTotal))
        }
    }

    public var jobId: String
    public var status: Status
    public var progress: Progress
    public var result: OneNoteImportResult?
    public var error: String?
    /// When the job was created, if the backend says (recent jobs).
    public var createdAt: String?
    /// The uploaded file's name, if the backend says (recent jobs).
    public var filename: String?

    public var id: String { jobId }

    public init(jobId: String, status: Status, progress: Progress = Progress(), result: OneNoteImportResult? = nil,
                error: String? = nil, createdAt: String? = nil, filename: String? = nil) {
        self.jobId = jobId
        self.status = status
        self.progress = progress
        self.result = result
        self.error = error
        self.createdAt = createdAt
        self.filename = filename
    }

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id", id, status, progress, result, error, createdAt = "created_at", filename
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let id = try container.decodeIfPresent(String.self, forKey: .jobId) {
            jobId = id
        } else {
            jobId = try container.decode(String.self, forKey: .id)
        }
        let raw = (try? container.decodeIfPresent(String.self, forKey: .status)) ?? "queued"
        // A phase this app doesn't know yet is still work in progress.
        status = Status(rawValue: raw.lowercased()) ?? .converting
        progress = (try? container.decodeIfPresent(Progress.self, forKey: .progress)) ?? Progress()
        result = try container.decodeIfPresent(OneNoteImportResult.self, forKey: .result)
        error = try? container.decodeIfPresent(String.self, forKey: .error)
        createdAt = try? container.decodeIfPresent(String.self, forKey: .createdAt)
        filename = try? container.decodeIfPresent(String.self, forKey: .filename)
    }

    /// Whether the backend couldn't run because the converter isn't installed.
    public var converterMissing: Bool { error.map(OneNoteImport.isConverterMissing) ?? false }
}

public struct OneNoteImportResult: Decodable, Hashable, Sendable {
    public struct FailedSection: Decodable, Hashable, Sendable {
        /// "Notebook › Section".
        public var path: String
        public var reason: String
        public var hint: String?

        public init(path: String, reason: String, hint: String? = nil) {
            self.path = path
            self.reason = reason
            self.hint = hint
        }

        enum CodingKeys: String, CodingKey { case path, reason, hint }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            path = (try? container.decodeIfPresent(String.self, forKey: .path)) ?? "Section"
            reason = (try? container.decodeIfPresent(String.self, forKey: .reason)) ?? ""
            hint = try? container.decodeIfPresent(String.self, forKey: .hint)
        }
    }

    public struct Notebook: Decodable, Hashable, Sendable, Identifiable {
        public var name: String
        /// The folder the notebook's notes went into.
        public var folderId: String?
        public var sections: Int
        public var pagesCreated: Int
        public var pagesUpdated: Int
        public var pagesUnchanged: Int
        public var pagesSkipped: Int
        public var images: Int
        public var files: Int
        public var tables: Int
        public var columnsPages: Int
        public var warnings: [String]
        public var failedSections: [FailedSection]

        public var id: String { folderId ?? name }
        public var pages: Int { pagesCreated + pagesUpdated + pagesUnchanged }

        public init(name: String, folderId: String? = nil, sections: Int = 0, pagesCreated: Int = 0, pagesUpdated: Int = 0,
                    pagesUnchanged: Int = 0, pagesSkipped: Int = 0, images: Int = 0, files: Int = 0, tables: Int = 0,
                    columnsPages: Int = 0, warnings: [String] = [], failedSections: [FailedSection] = []) {
            self.name = name
            self.folderId = folderId
            self.sections = sections
            self.pagesCreated = pagesCreated
            self.pagesUpdated = pagesUpdated
            self.pagesUnchanged = pagesUnchanged
            self.pagesSkipped = pagesSkipped
            self.images = images
            self.files = files
            self.tables = tables
            self.columnsPages = columnsPages
            self.warnings = warnings
            self.failedSections = failedSections
        }

        enum CodingKeys: String, CodingKey {
            case name, folderId = "folder_id", sections, pagesCreated = "pages_created", pagesUpdated = "pages_updated"
            case pagesUnchanged = "pages_unchanged", pagesSkipped = "pages_skipped", images, files, tables
            case columnsPages = "columns_pages", warnings, failedSections = "failed_sections"
        }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            func count(_ key: CodingKeys) -> Int { (try? container.decodeIfPresent(Int.self, forKey: key)) ?? 0 }
            name = (try? container.decodeIfPresent(String.self, forKey: .name)) ?? "Notebook"
            folderId = try? container.decodeIfPresent(String.self, forKey: .folderId)
            sections = count(.sections)
            pagesCreated = count(.pagesCreated)
            pagesUpdated = count(.pagesUpdated)
            pagesUnchanged = count(.pagesUnchanged)
            pagesSkipped = count(.pagesSkipped)
            images = count(.images)
            files = count(.files)
            tables = count(.tables)
            columnsPages = count(.columnsPages)
            // Warnings are strings, or objects with a message.
            let raw = (try? container.decodeIfPresent([JSONValue].self, forKey: .warnings)) ?? []
            warnings = raw.compactMap { value in
                if let text = value.stringValue { return text }
                for key in ["message", "warning", "detail", "reason"] {
                    if let text = value[key]?.stringValue {
                        if let page = value["page"]?.stringValue ?? value["path"]?.stringValue { return "\(page): \(text)" }
                        return text
                    }
                }
                return nil
            }
            failedSections = (try? container.decodeIfPresent([FailedSection].self, forKey: .failedSections)) ?? []
        }
    }

    public struct UnresolvedLink: Decodable, Hashable, Sendable {
        public var page: String
        public var target: String

        public init(page: String, target: String) {
            self.page = page
            self.target = target
        }
    }

    public struct Links: Decodable, Hashable, Sendable {
        public var rewritten: Int
        public var unresolved: [UnresolvedLink]

        public init(rewritten: Int = 0, unresolved: [UnresolvedLink] = []) {
            self.rewritten = rewritten
            self.unresolved = unresolved
        }

        enum CodingKeys: String, CodingKey { case rewritten, unresolved }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            rewritten = (try? container.decodeIfPresent(Int.self, forKey: .rewritten)) ?? 0
            unresolved = (try? container.decodeIfPresent([UnresolvedLink].self, forKey: .unresolved)) ?? []
        }
    }

    public var notebooks: [Notebook]
    public var links: Links
    /// The converter's error log, if it wrote one.
    public var errorsTxt: String?
    public var durationMs: Int?

    public init(notebooks: [Notebook], links: Links = Links(), errorsTxt: String? = nil, durationMs: Int? = nil) {
        self.notebooks = notebooks
        self.links = links
        self.errorsTxt = errorsTxt
        self.durationMs = durationMs
    }

    enum CodingKeys: String, CodingKey {
        case notebooks, links, errorsTxt = "errors_txt", durationMs = "duration_ms"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        notebooks = (try? container.decodeIfPresent([Notebook].self, forKey: .notebooks)) ?? []
        links = (try? container.decodeIfPresent(Links.self, forKey: .links)) ?? Links()
        errorsTxt = try? container.decodeIfPresent(String.self, forKey: .errorsTxt)
        durationMs = try? container.decodeIfPresent(Int.self, forKey: .durationMs)
    }

    // MARK: Totals

    public var pages: Int { notebooks.reduce(0) { $0 + $1.pages } }
    public var pagesCreated: Int { notebooks.reduce(0) { $0 + $1.pagesCreated } }
    public var pagesUpdated: Int { notebooks.reduce(0) { $0 + $1.pagesUpdated } }
    public var images: Int { notebooks.reduce(0) { $0 + $1.images } }
    public var files: Int { notebooks.reduce(0) { $0 + $1.files } }
    public var tables: Int { notebooks.reduce(0) { $0 + $1.tables } }
    public var failedSections: [OneNoteImportResult.FailedSection] { notebooks.flatMap(\.failedSections) }
    public var warningCount: Int { notebooks.reduce(0) { $0 + $1.warnings.count } }
    public var problemCount: Int { failedSections.count + links.unresolved.count }
}

/// Pure helpers for the import sheet (phases, wording), kept testable.
public enum OneNoteImport {
    /// File types the sheet accepts.
    public static let fileExtensions = ["zip", "onepkg", "one"]

    public static func accepts(_ url: URL) -> Bool {
        fileExtensions.contains(url.pathExtension.lowercased())
    }

    /// The backend says the `fortress-onenote` converter isn't there (a job error or a
    /// 503 detail).
    public static func isConverterMissing(_ message: String) -> Bool {
        let text = message.lowercased()
        let names = ["fortress-onenote", "fortress_onenote_bin", "converter"]
        let missing = ["not found", "missing", "not installed", "isn't installed", "no such file", "not built", "unavailable", "not available"]
        return names.contains { text.contains($0) } && missing.contains { text.contains($0) }
    }

    /// The steps the sheet lists while an import runs.
    public enum Step: Int, CaseIterable, Sendable, Comparable {
        case upload, convert, importPages, link

        public var title: String {
            switch self {
            case .upload: "Uploading"
            case .convert: "Converting"
            case .importPages: "Importing pages"
            case .link: "Linking pages"
            }
        }

        public static func < (lhs: Step, rhs: Step) -> Bool { lhs.rawValue < rhs.rawValue }
    }

    /// The step a job is on (queued counts as converting: the upload is done).
    public static func step(for status: OneNoteImportJob.Status) -> Step {
        switch status {
        case .queued, .converting: .convert
        case .importing: .importPages
        case .linking, .done, .failed: .link
        }
    }

    /// "12 of 48 pages".
    public static func pageCount(_ progress: OneNoteImportJob.Progress) -> String? {
        guard progress.pagesTotal > 0 else { return nil }
        return "\(progress.pagesDone) of \(progress.pagesTotal) \(progress.pagesTotal == 1 ? "page" : "pages")"
    }

    /// "148 pages · 36 images · 9 files · 12 tables".
    public static func summary(_ result: OneNoteImportResult) -> String {
        var parts = [plural(result.pages, "page")]
        if result.images > 0 { parts.append(plural(result.images, "image")) }
        if result.files > 0 { parts.append(plural(result.files, "file")) }
        if result.tables > 0 { parts.append(plural(result.tables, "table")) }
        if let duration = result.durationMs, duration > 0 { parts.append("in \(durationText(duration))") }
        return parts.joined(separator: " · ")
    }

    public static func headline(_ result: OneNoteImportResult) -> String {
        let notebooks = result.notebooks.count
        let what = notebooks == 1 ? "1 notebook" : "\(notebooks) notebooks"
        if notebooks == 0 { return "Nothing was imported" }
        if result.pagesCreated == 0 && result.pagesUpdated == 0 { return "\(what) already up to date" }
        return "Imported \(what)"
    }

    public static func plural(_ count: Int, _ noun: String) -> String {
        "\(count.formatted()) \(count == 1 ? noun : noun + "s")"
    }

    public static func durationText(_ milliseconds: Int) -> String {
        let seconds = Double(milliseconds) / 1000
        if seconds < 1 { return "under a second" }
        if seconds < 60 { return "\(Int(seconds.rounded())) s" }
        let minutes = Int(seconds) / 60
        return "\(minutes) min \(Int(seconds) % 60) s"
    }

    /// Recent jobs come as a list or as `{"jobs": [...]}`.
    public static func decodeRecent(_ data: Data) throws -> [OneNoteImportJob] {
        let decoder = JSONDecoder()
        if let jobs = try? decoder.decode([OneNoteImportJob].self, from: data) { return jobs }
        struct Wrapped: Decodable { var jobs: [OneNoteImportJob] }
        return try decoder.decode(Wrapped.self, from: data).jobs
    }
}
