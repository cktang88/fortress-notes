import Foundation

/// The OneNote import endpoints (spec: `POST /api/import/onenote` multipart field `file`,
/// optional `folder_id` → 202 `{job_id, status}`; `GET /api/import/onenote/{job_id}`;
/// `GET /api/import/onenote` for recent jobs).
extension APIClient {
    /// Uploads an export (.zip, .onepkg, .one) and starts the import job. The body is
    /// streamed from a temporary file, so large notebooks never sit in memory.
    public func startOneNoteImport(
        file: URL, folderId: String?, progress: (@Sendable (Double) -> Void)? = nil
    ) async throws -> OneNoteImportJob {
        let boundary = "FortressBoundary-\(UUID().uuidString)"
        let body = try Self.oneNoteMultipartFile(file: file, folderId: folderId, boundary: boundary)
        defer { try? FileManager.default.removeItem(at: body) }
        var request = URLRequest(url: baseURL.appending(path: "api/import/onenote"))
        request.httpMethod = "POST"
        request.timeoutInterval = 600
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let delegate = progress.map(OneNoteUploadProgress.init)
        let (data, response) = try await session.upload(for: request, fromFile: body, delegate: delegate)
        try Self.oneNoteCheck(data, response)
        return try JSONDecoder().decode(OneNoteImportJob.self, from: data)
    }

    public func oneNoteImportJob(id: String) async throws -> OneNoteImportJob {
        var request = URLRequest(url: baseURL.appending(path: "api/import/onenote/\(id)"))
        request.timeoutInterval = 30
        let (data, response) = try await session.data(for: request)
        try Self.oneNoteCheck(data, response)
        return try JSONDecoder().decode(OneNoteImportJob.self, from: data)
    }

    public func recentOneNoteImports() async throws -> [OneNoteImportJob] {
        var request = URLRequest(url: baseURL.appending(path: "api/import/onenote"))
        request.timeoutInterval = 30
        let (data, response) = try await session.data(for: request)
        try Self.oneNoteCheck(data, response)
        return try OneNoteImport.decodeRecent(data)
    }

    /// Stops an import between pages (pages already imported are kept).
    public func cancelOneNoteImport(id: String) async throws {
        var request = URLRequest(url: baseURL.appending(path: "api/import/onenote/\(id)/cancel"))
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        let (data, response) = try await session.data(for: request)
        try Self.oneNoteCheck(data, response)
    }

    // MARK: Download from Microsoft (app/onenote_graph.py)

    /// Whether the backend can run the download, and its latest download job.
    public func oneNoteGraphStatus() async throws -> OneNoteGraphStatus {
        var request = URLRequest(url: baseURL.appending(path: "api/import/onenote-graph"))
        request.timeoutInterval = 30
        let (data, response) = try await session.data(for: request)
        try Self.oneNoteCheck(data, response)
        return try JSONDecoder().decode(OneNoteGraphStatus.self, from: data)
    }

    /// Starts a download (then import) or, with `kind: .notebooks`, a sign-in that lists
    /// the notebooks. `notebooks` nil downloads every notebook.
    public func startOneNoteGraph(
        kind: OneNoteGraphJob.Kind, clientId: String, account: OneNoteGraphAccount,
        notebooks: [String]? = nil, folderId: String? = nil
    ) async throws -> OneNoteGraphJob {
        let path = kind == .notebooks ? "api/import/onenote-graph/notebooks" : "api/import/onenote-graph"
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["client_id": clientId.trimmingCharacters(in: .whitespacesAndNewlines),
                                   "account": account.rawValue]
        if let notebooks { body["notebooks"] = notebooks }
        if let folderId { body["folder_id"] = folderId }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await session.data(for: request)
        try Self.oneNoteCheck(data, response)
        return try JSONDecoder().decode(OneNoteGraphJob.self, from: data)
    }

    public func oneNoteGraphJob(id: String) async throws -> OneNoteGraphJob {
        var request = URLRequest(url: baseURL.appending(path: "api/import/onenote-graph/\(id)"))
        request.timeoutInterval = 30
        let (data, response) = try await session.data(for: request)
        try Self.oneNoteCheck(data, response)
        return try JSONDecoder().decode(OneNoteGraphJob.self, from: data)
    }

    /// Stops a download; what was downloaded stays cached, so the next run resumes.
    public func cancelOneNoteGraph(id: String) async throws {
        var request = URLRequest(url: baseURL.appending(path: "api/import/onenote-graph/\(id)/cancel"))
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        let (data, response) = try await session.data(for: request)
        try Self.oneNoteCheck(data, response)
    }

    /// Writes the multipart body (the file, then `folder_id`) to a temporary file.
    static func oneNoteMultipartFile(file: URL, folderId: String?, boundary: String) throws -> URL {
        let output = FileManager.default.temporaryDirectory.appending(path: "onenote-upload-\(UUID().uuidString)")
        FileManager.default.createFile(atPath: output.path, contents: nil)
        let handle = try FileHandle(forWritingTo: output)
        defer { try? handle.close() }
        func write(_ string: String) throws { try handle.write(contentsOf: Data(string.utf8)) }
        let name = file.lastPathComponent.replacingOccurrences(of: "\"", with: "'")
        let type = file.pathExtension.lowercased() == "zip" ? "application/zip" : "application/octet-stream"
        try write("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(name)\"\r\nContent-Type: \(type)\r\n\r\n")
        let input = try FileHandle(forReadingFrom: file)
        defer { try? input.close() }
        while let chunk = try input.read(upToCount: 1 << 20), !chunk.isEmpty {
            try handle.write(contentsOf: chunk)
        }
        try write("\r\n")
        if let folderId {
            try write("--\(boundary)\r\nContent-Disposition: form-data; name=\"folder_id\"\r\n\r\n\(folderId)\r\n")
        }
        try write("--\(boundary)--\r\n")
        return output
    }

    private static func oneNoteCheck(_ data: Data, _ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) else { return }
        var message = HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
        if let object = try? JSONDecoder().decode([String: JSONValue].self, from: data) {
            if let detail = object["detail"]?.stringValue {
                message = detail
            } else if let detail = object["detail"]?["message"]?.stringValue ?? object["error"]?.stringValue {
                message = detail
            }
        }
        throw APIError(status: http.statusCode, message: message)
    }
}

/// Reports how much of the upload has gone out.
final class OneNoteUploadProgress: NSObject, URLSessionTaskDelegate, Sendable {
    let handler: @Sendable (Double) -> Void

    init(_ handler: @escaping @Sendable (Double) -> Void) {
        self.handler = handler
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        handler(min(1, Double(totalBytesSent) / Double(totalBytesExpectedToSend)))
    }
}
