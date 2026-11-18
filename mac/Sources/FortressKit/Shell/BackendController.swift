import Foundation
import Observation

/// How to start the Python backend from its folder: the project's own virtualenv when it
/// has one, otherwise `uv run`.
public struct BackendLaunch: Hashable, Sendable {
    public var executable: URL
    public var arguments: [String]
    public var workingDirectory: URL
    /// PATH for the child: GUI apps start with a bare PATH that lacks Homebrew and ~/.local/bin.
    public var path: String
    /// The `fortress-onenote` converter, passed to the backend as `FORTRESS_ONENOTE_BIN`.
    public var onenoteConverter: URL? = nil

    /// The folder that holds `app/main.py`: `folder` itself, or its `backend/` subfolder
    /// when `folder` is the repository.
    public static func backendDirectory(for folder: URL, fileExists: (String) -> Bool) -> URL? {
        for candidate in [folder, folder.appending(path: "backend")]
        where fileExists(candidate.appending(path: "app/main.py").path) {
            return candidate.standardizedFileURL
        }
        return nil
    }

    /// The command that serves the backend on `port`, or nil when `folder` has no backend.
    public static func make(
        folder: URL, port: Int, home: String,
        fileExists: (String) -> Bool = { FileManager.default.isExecutableFile(atPath: $0) || FileManager.default.fileExists(atPath: $0) }
    ) -> BackendLaunch? {
        guard let directory = backendDirectory(for: folder, fileExists: fileExists) else { return nil }
        let searchPath = ["\(home)/.local/bin", "\(home)/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        let path = searchPath.joined(separator: ":")
        let serve = ["app.main:app", "--port", String(port)]

        let converter = onenoteConverter(folder: folder, backend: directory, fileExists: fileExists)
        let venv = directory.appending(path: ".venv/bin/uvicorn")
        if fileExists(venv.path) {
            return BackendLaunch(executable: venv, arguments: serve, workingDirectory: directory, path: path,
                                 onenoteConverter: converter)
        }
        let uv = searchPath.prefix(4).map { URL(fileURLWithPath: "\($0)/uv") }.first { fileExists($0.path) }
        if let uv {
            return BackendLaunch(executable: uv, arguments: ["run", "uvicorn"] + serve, workingDirectory: directory, path: path,
                                 onenoteConverter: converter)
        }
        return BackendLaunch(
            executable: URL(fileURLWithPath: "/usr/bin/env"), arguments: ["uv", "run", "uvicorn"] + serve,
            workingDirectory: directory, path: path, onenoteConverter: converter)
    }

    /// Where the converter was built: `tools/fortress-onenote/target/release/fortress-onenote`
    /// in the repository (the folder, or the parent of its `backend/`), else a copy
    /// bundled with the app.
    public static func onenoteConverter(
        folder: URL, backend: URL? = nil, bundled: URL? = Bundle.main.url(forAuxiliaryExecutable: "fortress-onenote"),
        fileExists: (String) -> Bool
    ) -> URL? {
        var roots = [folder.standardizedFileURL]
        if let backend { roots.append(backend.standardizedFileURL.deletingLastPathComponent()) }
        for root in roots {
            let built = root.appending(path: "tools/fortress-onenote/target/release/fortress-onenote")
            if fileExists(built.path) { return built }
        }
        if let bundled, fileExists(bundled.path) { return bundled }
        return nil
    }

    /// The repository that contains `start` (e.g. the app running from `mac/.build/debug`).
    public static func repository(containing start: URL, fileExists: (String) -> Bool) -> URL? {
        var directory = start.standardizedFileURL
        for _ in 0..<10 {
            if fileExists(directory.appending(path: "backend/app/main.py").path) { return directory }
            let parent = directory.deletingLastPathComponent()
            if parent == directory { break }
            directory = parent
        }
        return nil
    }

    /// The command line as someone would type it, for Settings.
    public var displayCommand: String {
        let name = executable.path.hasSuffix("/env") ? "" : executable.lastPathComponent + " "
        return name + arguments.joined(separator: " ")
    }
}

/// Owns the backend address and, when the backend isn't running, a backend process the
/// app starts itself (and stops on quit).
@MainActor
@Observable
public final class BackendController {
    public enum State: Equatable, Sendable {
        case idle
        case starting
        case running
        case failed(String)
    }

    public static let defaultURL = URL(string: "http://127.0.0.1:8000")!

    /// The state of the process this app started (idle when it started none).
    public private(set) var state: State = .idle

    public var urlString: String {
        didSet { defaults.set(urlString, forKey: Keys.url) }
    }
    /// The repository or its `backend/` folder.
    public var folderPath: String {
        didSet { defaults.set(folderPath, forKey: Keys.folder) }
    }
    public var startsAutomatically: Bool {
        didSet { defaults.set(startsAutomatically, forKey: Keys.autoStart) }
    }

    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private var process: Process?
    @ObservationIgnored private var stopping = false

    private enum Keys {
        static let url = "backendURL"
        static let folder = "backendFolder"
        static let autoStart = "startBackendAutomatically"
    }

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        urlString = defaults.string(forKey: Keys.url) ?? Self.defaultURL.absoluteString
        startsAutomatically = defaults.object(forKey: Keys.autoStart) as? Bool ?? true
        if let saved = defaults.string(forKey: Keys.folder), !saved.isEmpty {
            folderPath = saved
        } else {
            let executable = Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])
            folderPath = BackendLaunch.repository(
                containing: executable.deletingLastPathComponent(),
                fileExists: { FileManager.default.fileExists(atPath: $0) })?.path ?? ""
        }
    }

    /// The address from Settings, or the default when it isn't a usable http(s) URL.
    public var baseURL: URL {
        let trimmed = urlString.trimmingCharacters(in: .whitespaces)
        guard let url = URL(string: trimmed), let scheme = url.scheme, ["http", "https"].contains(scheme),
              url.host() != nil
        else { return Self.defaultURL }
        return url
    }

    public var port: Int { baseURL.port ?? (baseURL.scheme == "https" ? 443 : 80) }

    /// Only a backend on this Mac can be started from here.
    public var isLocal: Bool {
        ["127.0.0.1", "localhost", "::1"].contains(baseURL.host() ?? "")
    }

    public var launch: BackendLaunch? {
        guard isLocal, !folderPath.isEmpty else { return nil }
        let folder = URL(fileURLWithPath: (folderPath as NSString).expandingTildeInPath)
        return BackendLaunch.make(folder: folder, port: port, home: NSHomeDirectory())
    }

    public var canStart: Bool { launch != nil && !isProcessRunning }

    public var isProcessRunning: Bool { process?.isRunning == true }

    public var logURL: URL {
        FileManager.default.homeDirectoryForCurrentUser.appending(path: "Library/Logs/Fortress Notes/backend.log")
    }

    /// Starts the backend; `state` becomes `.starting` until `markReachable()`.
    @discardableResult
    public func start() -> Bool {
        guard !isProcessRunning else { return true }
        guard let launch else {
            state = .failed(folderPath.isEmpty
                ? "Choose the Fortress folder in Settings to start the backend."
                : "No backend found in \((folderPath as NSString).abbreviatingWithTildeInPath).")
            return false
        }
        let process = Process()
        process.executableURL = launch.executable
        process.arguments = launch.arguments
        process.currentDirectoryURL = launch.workingDirectory
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = launch.path
        environment["PYTHONUNBUFFERED"] = "1"
        if let converter = launch.onenoteConverter, environment["FORTRESS_ONENOTE_BIN"] == nil {
            environment["FORTRESS_ONENOTE_BIN"] = converter.path
        }
        process.environment = environment
        if let log = openLog() {
            process.standardOutput = log
            process.standardError = log
        }
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            Task { @MainActor in self?.processExited(status: status) }
        }
        do {
            try process.run()
        } catch {
            state = .failed("Couldn't start the backend: \(error.localizedDescription)")
            return false
        }
        self.process = process
        stopping = false
        state = .starting
        return true
    }

    /// Called once the started backend answers.
    public func markReachable() {
        if isProcessRunning { state = .running }
    }

    public func markUnreachable(_ message: String) {
        if state == .starting { state = .failed(message) }
    }

    /// Stops the backend this app started, waiting briefly for it to exit.
    public func stop(waitUpTo seconds: TimeInterval = 0) {
        guard let process, process.isRunning else { return }
        stopping = true
        process.terminate()
        let deadline = Date().addingTimeInterval(seconds)
        while process.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
    }

    private func processExited(status: Int32) {
        process = nil
        if stopping {
            state = .idle
        } else {
            state = .failed("The backend stopped (exit code \(status)). See the log for details.")
        }
    }

    private func openLog() -> FileHandle? {
        let manager = FileManager.default
        try? manager.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !manager.fileExists(atPath: logURL.path) { manager.createFile(atPath: logURL.path, contents: nil) }
        guard let handle = try? FileHandle(forWritingTo: logURL) else { return nil }
        handle.seekToEndOfFile()
        handle.write(Data("\n--- \(Date().formatted(.iso8601)) starting backend ---\n".utf8))
        return handle
    }
}
