import SwiftUI

/// The backend's optional picture descriptions (backend/app/captions.py): a small local
/// model writes a sentence about each picture so search finds photos by what they show.
/// Off by default; turned on in Settings, which this Mac remembers and tells the backend
/// whenever it connects (`PUT /api/descriptions`).
public enum PictureDescriptions {
    public static let enabledKey = "describesPictures"
}

/// `GET`/`PUT /api/descriptions`.
public struct PictureDescriptionStatus: Decodable, Hashable, Sendable {
    public var enabled: Bool
    /// off, downloading, loading, describing, idle (all described), failed.
    public var state: String
    public var model: String?
    /// The model's download size, and how much of it is on disk.
    public var downloadBytes: Int64?
    public var downloadedBytes: Int64?
    public var error: String?
    /// Pictures still to describe, described, given up on.
    public var queued: Int
    public var done: Int?
    public var failed: Int?

    public init(enabled: Bool, state: String, model: String? = nil, downloadBytes: Int64? = nil,
                downloadedBytes: Int64? = nil, error: String? = nil, queued: Int = 0, done: Int? = nil,
                failed: Int? = nil) {
        self.enabled = enabled
        self.state = state
        self.model = model
        self.downloadBytes = downloadBytes
        self.downloadedBytes = downloadedBytes
        self.error = error
        self.queued = queued
        self.done = done
        self.failed = failed
    }

    /// Something is moving: poll often.
    public var isBusy: Bool { ["downloading", "loading", "describing"].contains(state) }

    /// One line for Settings.
    public var summary: String {
        switch state {
        case "downloading":
            let total = (downloadBytes ?? 0).formatted(.byteCount(style: .file))
            let done = (downloadedBytes ?? 0).formatted(.byteCount(style: .file))
            return String(localized: "Downloading the model · \(done) of \(total)")
        case "loading":
            return String(localized: "Loading the model…")
        case "failed":
            return error ?? String(localized: "The model couldn’t be loaded.")
        case "off":
            return String(localized: "Off")
        default:
            if queued > 0 { return String(localized: "Describing pictures · \(queued.formatted()) left") }
            let described = done ?? 0
            return described == 1 ? String(localized: "1 picture described")
                : String(localized: "\(described.formatted()) pictures described")
        }
    }

    enum CodingKeys: String, CodingKey {
        case enabled, state, model, error, queued, done, failed
        case downloadBytes = "download_bytes"
        case downloadedBytes = "downloaded_bytes"
    }
}

extension APIClient {
    public func pictureDescriptions() async throws -> PictureDescriptionStatus {
        try await get("api/descriptions")
    }

    /// Turns the descriptions on or off. Not a "mutation" for the client's caches: no
    /// note changes.
    @discardableResult
    public func setPictureDescriptions(enabled: Bool) async throws -> PictureDescriptionStatus {
        let body = try JSONEncoder().encode(["enabled": enabled])
        let (data, response) = try await load(try makeRequest("PUT", "api/descriptions", body: body))
        try check(response, body: data)
        return try Self.decode(PictureDescriptionStatus.self, from: data)
    }
}

/// Tells the backend whether to describe pictures whenever it connects or the setting
/// changes (a backend the app starts begins with them off).
struct PictureDescribing: ViewModifier {
    @Environment(AppModel.self) private var model
    @AppStorage(PictureDescriptions.enabledKey) private var isEnabled = false

    private struct Key: Equatable {
        var connected: Bool
        var enabled: Bool
        var backend: String
    }

    func body(content: Content) -> some View {
        content.task(id: Key(connected: model.connectsOnAppear && model.isConnected, enabled: isEnabled,
                             backend: model.api.baseURL.absoluteString)) {
            guard model.connectsOnAppear && model.isConnected else { return }
            _ = try? await model.api.setPictureDescriptions(enabled: isEnabled)
        }
    }
}

/// Settings › General › Picture Descriptions: the switch, the model's size, and the
/// download and describing progress while it is on.
struct PictureDescriptionSettings: View {
    @Environment(AppModel.self) private var model
    @AppStorage(PictureDescriptions.enabledKey) private var isEnabled = false
    @State private var status: PictureDescriptionStatus?

    var body: some View {
        Section {
            Toggle("Describe pictures so search finds them by what they show", isOn: $isEnabled)
            if isEnabled {
                LabeledContent("Status") {
                    HStack(spacing: 6) {
                        if let status, status.isBusy { ProgressView().controlSize(.mini) }
                        Text(statusText)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                if let status, status.state == "downloading", let total = status.downloadBytes, total > 0 {
                    ProgressView(value: Double(status.downloadedBytes ?? 0), total: Double(total))
                }
            }
        } header: {
            Text("Picture Descriptions")
        } footer: {
            Text("A small model (\(downloadSize) download, once) writes a short description of each picture in the background, so a search like “dog on the beach” finds the photo. Descriptions are searchable but never added to your notes, and they can be wrong. Turning this off stops the work.")
                .foregroundStyle(.secondary)
        }
        .task(id: Key(enabled: isEnabled, connected: model.isConnected)) {
            guard model.isConnected else { status = nil; return }
            while !Task.isCancelled {
                status = try? await model.api.pictureDescriptions()
                try? await Task.sleep(for: .seconds(status?.isBusy == true ? 1 : 5))
            }
        }
    }

    private struct Key: Equatable {
        var enabled: Bool
        var connected: Bool
    }

    private var statusText: String {
        guard model.isConnected else { return String(localized: "Not connected") }
        guard let status else { return String(localized: "Starting…") }
        // The backend hears about the switch a moment after it moves.
        if !status.enabled { return String(localized: "Starting…") }
        return status.summary
    }

    private var downloadSize: String {
        Int64(status?.downloadBytes ?? Self.knownDownloadBytes).formatted(.byteCount(style: .file))
    }

    /// The pinned model's size, shown before the backend has answered.
    static let knownDownloadBytes: Int64 = 1_000_000_000
}
