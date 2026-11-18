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
