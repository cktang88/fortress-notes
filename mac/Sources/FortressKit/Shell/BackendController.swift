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

/// Owns the backend address and, when Settings let the app start the backend, the
/// backend process this launch started (and stops when it quits).
///
/// The process gets this launch's token (`BackendAuth`) and the app's pid (it stops
/// itself if the app dies), runs in its own process group so stopping it also stops
/// what it started (uvicorn under `uv run`, a OneNote converter), and is watched from a
/// background thread: nothing here blocks the main actor.
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

    /// The port of the backend this launch started (the one in Settings when it was
    /// free, else one the system picked). Kept after the process exits, so reconnecting
    /// and restarting look in the same place.
    public private(set) var activePort: Int?

    @ObservationIgnored private let defaults: UserDefaults
    /// The process group leader this app started (its pid is the group id).
    @ObservationIgnored private var pid: pid_t?
    @ObservationIgnored private var stopping = false
    @ObservationIgnored public var logURL: URL = FileManager.default.homeDirectoryForCurrentUser
        .appending(path: "Library/Logs/Fortress Notes/backend.log")

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

    /// Where the app sends requests: the backend this launch started, else the address
    /// from Settings.
    public var serverURL: URL {
        guard let activePort else { return baseURL }
        return URL(string: "http://127.0.0.1:\(activePort)")!
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

    public var isProcessRunning: Bool { pid != nil }

    /// The started backend's process group (its leader's pid), while it runs.
    var processGroup: pid_t? { pid }

    /// Whether the app talks only to a backend it started: then a server that answers
    /// with another instance id (another launch's, another user's, anything else on
    /// the port) is never used.
    public var ownsServer: Bool { activePort != nil }

    /// Whether `instance` (a health answer's `X-Fortress-Instance`) is a server the app
    /// may use: its own backend, or anything at the Settings address when the app
    /// started none (a backend run by hand, without a token).
    public func accepts(instance: String?) -> Bool {
        !ownsServer || instance == BackendAuth.instanceID
    }

    /// Starts the backend on the port from Settings when it's free, else on one the
    /// system picks; `state` becomes `.starting` until `markReachable()`.
    @discardableResult
    public func start() -> Bool {
        guard !isProcessRunning else { return true }
        guard isLocal, !folderPath.isEmpty, launch != nil else {
            state = .failed(folderPath.isEmpty
                ? "Choose the Fortress folder in Settings to start the backend."
                : "No backend found in \((folderPath as NSString).abbreviatingWithTildeInPath).")
            return false
        }
        let chosen = Self.isPortFree(port) ? port : (Self.freePort() ?? port)
        let folder = URL(fileURLWithPath: (folderPath as NSString).expandingTildeInPath)
        guard let launch = BackendLaunch.make(folder: folder, port: chosen, home: NSHomeDirectory()) else { return false }
        return start(launch, port: chosen)
    }

    /// Starts `launch` (which serves on `port`) in its own process group.
    @discardableResult
    func start(_ launch: BackendLaunch, port: Int) -> Bool {
        guard !isProcessRunning else { return true }
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = launch.path
        environment["PYTHONUNBUFFERED"] = "1"
        environment[BackendAuth.tokenEnvironment] = BackendAuth.token
        environment["FORTRESS_PARENT_PID"] = String(getpid())
        if let converter = launch.onenoteConverter, environment["FORTRESS_ONENOTE_BIN"] == nil {
            environment["FORTRESS_ONENOTE_BIN"] = converter.path
        }
        prepareLog()
        let pid: pid_t
        do {
            pid = try Self.spawn(launch, environment: environment, log: logURL)
        } catch {
            state = .failed("Couldn't start the backend: \(error.localizedDescription)")
            return false
        }
        self.pid = pid
        activePort = port
        stopping = false
        exitedUnexpectedly = false
        state = .starting
        // Reap it on a background thread (waitpid blocks) and report back here.
        Thread.detachNewThread { [weak self] in
            var status: Int32 = 0
            while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
            let code = status & 0x7f == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
            Task { @MainActor in self?.processExited(pid: pid, status: code) }
        }
        return true
    }

    /// Called once the started backend answers.
    public func markReachable() {
        if isProcessRunning { state = .running }
    }

    public func markUnreachable(_ message: String) {
        if state == .starting { state = .failed(message) }
    }

    /// Stops the backend this app started: SIGTERM to its process group, then SIGKILL
    /// to whatever is left after `gracePeriod`. Waits without blocking the main actor.
    public func stop(gracePeriod: Duration = .seconds(3)) async {
        guard let pid else { return }
        stopping = true
        kill(-pid, SIGTERM)
        let deadline = ContinuousClock.now + gracePeriod
        while self.pid == pid, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
        // Also what outlived the leader (uvicorn under a killed `uv`, a converter).
        kill(-pid, SIGKILL)
    }

    /// The app is quitting: ask the backend's group to stop and return at once. It
    /// shuts down by itself, and it stops itself (hard, if it must) once the app is gone.
    public func terminateForQuit() {
        guard let pid else { return }
        stopping = true
        kill(-pid, SIGTERM)
    }

    /// Whether the process this app started stopped by itself after it was running (a
    /// crash, not a failed start): the app may start it again.
    public private(set) var exitedUnexpectedly = false

    func processExited(pid exited: pid_t, status: Int32) {
        guard pid == exited else { return }
        pid = nil
        // Nothing the backend started outlives it.
        kill(-exited, SIGKILL)
        let wasRunning = state == .running
        if stopping {
            state = .idle
            exitedUnexpectedly = false
        } else {
            exitedUnexpectedly = wasRunning
            state = .failed("The backend stopped (exit code \(status)). See the log for details.")
            if wasRunning { onUnexpectedExit?() }
        }
    }

    /// Called when the backend this app started stops by itself.
    @ObservationIgnored public var onUnexpectedExit: (@MainActor () -> Void)?

    /// The end of the backend's log.
    public func logTail(bytes: Int = 4096) -> String {
        guard let handle = try? FileHandle(forReadingFrom: logURL) else { return "" }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size > UInt64(bytes) ? size - UInt64(bytes) : 0)
        return String(decoding: (try? handle.readToEnd()) ?? Data(), as: UTF8.self)
    }

    /// Whether the last start failed because something already listens on the port.
    public func logSaysPortInUse() -> Bool {
        Self.isPortInUse(log: logTail())
    }

    static func isPortInUse(log: String) -> Bool {
        let lastStart = log.range(of: "starting backend ---", options: .backwards).map { String(log[$0.upperBound...]) } ?? log
        let text = lastStart.lowercased()
        return text.contains("address already in use") || text.contains("errno 48") || text.contains("errno 98")
    }

    private func prepareLog() {
        let manager = FileManager.default
        try? manager.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !manager.fileExists(atPath: logURL.path) { manager.createFile(atPath: logURL.path, contents: nil) }
        guard let handle = try? FileHandle(forWritingTo: logURL) else { return }
        defer { try? handle.close() }
        handle.seekToEndOfFile()
        handle.write(Data("\n--- \(Date().formatted(.iso8601)) starting backend ---\n".utf8))
    }

    // MARK: Process and ports

    /// posix_spawn (Foundation's Process can't start a process group): the child leads
    /// a new group, gets default signal handling, /dev/null for input, the log for
    /// output, and none of the app's other file descriptors.
    nonisolated static func spawn(_ launch: BackendLaunch, environment: [String: String], log: URL) throws -> pid_t {
        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_CLOEXEC_DEFAULT))
        posix_spawnattr_setpgroup(&attributes, 0)
        var noSignals = sigset_t()
        sigemptyset(&noSignals)
        posix_spawnattr_setsigmask(&attributes, &noSignals)
        var defaulted = sigset_t()
        sigemptyset(&defaulted)
        for signal in [SIGTERM, SIGINT, SIGHUP, SIGPIPE, SIGCHLD] { sigaddset(&defaulted, signal) }
        posix_spawnattr_setsigdefault(&attributes, &defaulted)

        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_addopen(&actions, 1, log.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        posix_spawn_file_actions_adddup2(&actions, 1, 2)
        posix_spawn_file_actions_addchdir(&actions, launch.workingDirectory.path)

        let argv = ([launch.executable.path] + launch.arguments).map { strdup($0) } + [nil]
        let envp = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer {
            argv.forEach { free($0) }
            envp.forEach { free($0) }
        }
        var pid: pid_t = 0
        let result = posix_spawn(&pid, launch.executable.path, &actions, &attributes, argv, envp)
        guard result == 0 else { throw POSIXError(POSIXErrorCode(rawValue: result) ?? .EIO) }
        return pid
    }

    /// Whether nothing listens on 127.0.0.1:`port`.
    nonisolated static func isPortFree(_ port: Int) -> Bool {
        guard let port = UInt16(exactly: port), port > 0 else { return false }
        return bindLoopback(port: port) != nil
    }

    /// A port the system says is free now.
    nonisolated static func freePort() -> Int? {
        bindLoopback(port: 0)
    }

    /// Binds 127.0.0.1:`port` (as uvicorn will, with SO_REUSEADDR) and closes again;
    /// the port it got, or nil when it's taken.
    private nonisolated static func bindLoopback(port: UInt16) -> Int? {
        let socket = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard socket >= 0 else { return nil }
        defer { close(socket) }
        var reuse: Int32 = 1
        setsockopt(socket, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafeMutablePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { address in
                bind(socket, address, length) == 0 && getsockname(socket, address, &length) == 0
            }
        }
        guard bound else { return nil }
        return Int(UInt16(bigEndian: address.sin_port))
    }
}
