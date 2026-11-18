import AppKit
import Foundation

// MARK: Offline outbox

/// Captures that couldn't reach the backend (offline, server down), kept on disk and
/// sent in order once it answers again. Nothing typed into Quick Capture is lost.
@MainActor
final class CaptureOutbox {
    struct Item: Codable, Equatable, Sendable {
        var id: UUID
        var text: String
        var createdAt: Date
    }

    typealias Send = @Sendable (String) async throws -> CaptureResult

    private(set) var items: [Item] = []
    let fileURL: URL?
    /// Called (on the main actor) for each capture that finally went through.
    var onSent: (@MainActor (CaptureResult) -> Void)?
    /// Retry delays after consecutive failures; the last repeats.
    var backoff: [Duration] = [.seconds(2), .seconds(5), .seconds(15), .seconds(30), .seconds(60)]

    private var send: Send?
    private var retryTask: Task<Void, Never>?
    private var isFlushing = false
    private var failures = 0

    init(fileURL: URL? = CaptureOutbox.defaultURL) {
        self.fileURL = fileURL
        if let fileURL, let data = try? Data(contentsOf: fileURL),
           let saved = try? JSONDecoder().decode([Item].self, from: data) {
            items = saved
        }
    }

    isolated deinit {
        retryTask?.cancel()
    }

    static var defaultURL: URL? {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appending(path: "FortressNotes", directoryHint: .isDirectory)
            .appending(path: "capture-outbox.json")
    }

    /// Where captures go; sending starts as soon as there is somewhere to send.
    func connect(_ send: @escaping Send) {
        self.send = send
        if !items.isEmpty { scheduleRetry(after: .zero) }
    }

    func enqueue(_ text: String, at date: Date = .now) {
        items.append(Item(id: UUID(), text: text, createdAt: date))
        persist()
        scheduleRetry(after: currentDelay)
    }

    /// Something suggests the backend is back (the app became active, the Mac woke): try now.
    func nudge() {
        guard !items.isEmpty else { return }
        failures = 0
        scheduleRetry(after: .zero)
    }

    /// Sends what's waiting, oldest first, stopping at the first failure. Returns how many went.
    @discardableResult
    func flush() async -> Int {
        guard let send, !isFlushing else { return 0 }
        isFlushing = true
        defer { isFlushing = false }
        var sent = 0
        while let item = items.first {
            do {
                let result = try await send(item.text)
                items.removeAll { $0.id == item.id }
                persist()
                sent += 1
                failures = 0
                onSent?(result)
            } catch {
                if Self.isRetryable(error) {
                    failures += 1
                    scheduleRetry(after: currentDelay)
                } else {
                    // The server refused this one (4xx): it will never go through. Keep it
                    // out of the way, but don't throw the text away.
                    items.removeAll { $0.id == item.id }
                    items.append(item)
                    persist()
                    failures += 1
                    scheduleRetry(after: currentDelay)
                }
                break
            }
        }
        return sent
    }

    private var currentDelay: Duration {
        backoff[min(failures, backoff.count - 1)]
    }

    private func scheduleRetry(after delay: Duration) {
        guard send != nil, !items.isEmpty else { return }
        retryTask?.cancel()
        retryTask = Task { [weak self] in
            if delay > .zero { try? await Task.sleep(for: delay) }
            guard !Task.isCancelled else { return }
            await self?.flush()
        }
    }

    private func persist() {
        guard let fileURL else { return }
        do {
            try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if items.isEmpty {
                try? FileManager.default.removeItem(at: fileURL)
            } else {
                try JSONEncoder().encode(items).write(to: fileURL, options: .atomic)
            }
        } catch {
            // Disk full or similar: the items are still in memory and retried this session.
        }
    }

    /// Worth trying again later: no connection, a timeout, or the server failing.
    nonisolated static func isRetryable(_ error: Error) -> Bool {
        if error is URLError { return true }
        if let error = error as? APIError { return error.status >= 500 || error.status == 408 || error.status == 429 }
        return false
    }
}

// MARK: Global shortcut that survives conflicts and sleep

/// Keeps a global hot key registered: registers when asked, again after the Mac wakes
/// (some setups drop Carbon hot keys across sleep) and, while another app holds the
/// shortcut, each time Fortress becomes active, so it works as soon as the other app lets go.
@MainActor
final class HotKeyKeeper {
    enum Status: Equatable { case unregistered, registered, conflict }

    private(set) var status: Status = .unregistered
    private(set) var registrations = 0
    /// Called when registering fails (another app owns the shortcut).
    var onConflict: (@MainActor () -> Void)?

    private let register: @MainActor () -> AnyObject?
    private var token: AnyObject?
    private var observers: [(NotificationCenter, NSObjectProtocol)] = []

    /// `register` returns a token that keeps the hot key registered while it's alive, or nil.
    init(register: @escaping @MainActor () -> AnyObject?,
         workspaceCenter: NotificationCenter = NSWorkspace.shared.notificationCenter,
         appCenter: NotificationCenter = .default) {
        self.register = register
        observe(workspaceCenter, NSWorkspace.didWakeNotification) { $0.reregister() }
        observe(workspaceCenter, NSWorkspace.sessionDidBecomeActiveNotification) { $0.reregister() }
        observe(appCenter, NSApplication.didBecomeActiveNotification) { keeper in
            if keeper.status != .registered { keeper.activate() }
        }
    }

    isolated deinit {
        for (center, observer) in observers { center.removeObserver(observer) }
    }

    private func observe(_ center: NotificationCenter, _ name: Notification.Name, _ action: @escaping @MainActor @Sendable (HotKeyKeeper) -> Void) {
        // Delivered synchronously when posted on the main thread (as AppKit does), else hopped there.
        let observer = center.addObserver(forName: name, object: nil, queue: nil) { [weak self] _ in
            if Thread.isMainThread {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    action(self)
                }
            } else {
                Task { @MainActor in
                    guard let self else { return }
                    action(self)
                }
            }
        }
        observers.append((center, observer))
    }

    /// Registers unless already registered.
    func activate() {
        guard token == nil else { return }
        attempt()
    }

    /// Drops the registration and makes it again.
    func reregister() {
        token = nil
        attempt()
    }

    private func attempt() {
        registrations += 1
        token = register()
        let previous = status
        status = token == nil ? .conflict : .registered
        if status == .conflict && previous != .conflict { onConflict?() }
    }
}

// MARK: Draft

/// The unsaved Quick Capture text, kept across closing the panel and quitting the app.
struct CaptureDraftStore: @unchecked Sendable {
    var defaults: UserDefaults = .standard
    var key = "QuickCaptureDraft"

    func load() -> String { defaults.string(forKey: key) ?? "" }

    func save(_ text: String) {
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            defaults.removeObject(forKey: key)
        } else {
            defaults.set(text, forKey: key)
        }
    }
}
