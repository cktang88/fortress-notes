import Foundation

/// What `POST /api/markdown-import/files` answers.
public struct MarkdownImportResult: Codable, Hashable, Sendable {
    public struct Imported: Codable, Hashable, Sendable {
        public var name: String
        public var id: String
        public var title: String
    }
    public struct Failure: Codable, Hashable, Sendable {
        public var name: String
        public var detail: String
    }
    public var imported: [Imported]
    public var errors: [Failure]
}

/// Endpoints only the Shell uses. They build their own requests so `APIClient.swift`
/// stays untouched by this area.
extension APIClient {
    /// Moves a folder under another folder (nil = top level).
    public func moveFolder(id: String, parentId: String?) async throws {
        let body = try JSONEncoder().encode(["parent_id": parentId.map(JSONValue.string) ?? .null])
        _ = try await shellSend("POST", "api/folders/\(id)/move", body: body, contentType: "application/json")
    }

    /// The note as Markdown, the same text the backend mirrors to disk.
    public func markdown(documentId: String) async throws -> String {
        var request = URLRequest(url: markdownURL(documentId: documentId))
        request.timeoutInterval = 30
        return String(decoding: try await shellPerform(request), as: UTF8.self)
    }

    /// Creates one note per Markdown file, in `folderId` (nil = top level).
    public func importMarkdown(files: [(name: String, data: Data)], folderId: String?) async throws -> MarkdownImportResult {
        let boundary = "FortressBoundary-\(UUID().uuidString)"
        let body = Self.multipartBody(files: files, folderId: folderId, boundary: boundary)
        let data = try await shellSend(
            "POST", "api/markdown-import/files", body: body,
            contentType: "multipart/form-data; boundary=\(boundary)")
        return try JSONDecoder().decode(MarkdownImportResult.self, from: data)
    }

    static func multipartBody(files: [(name: String, data: Data)], folderId: String?, boundary: String) -> Data {
        var body = Data()
        func line(_ string: String) { body.append(Data((string + "\r\n").utf8)) }
        for file in files {
            let name = file.name.replacingOccurrences(of: "\"", with: "'")
            line("--\(boundary)")
            line("Content-Disposition: form-data; name=\"files\"; filename=\"\(name)\"")
            line("Content-Type: text/markdown")
            line("")
            body.append(file.data)
            line("")
        }
        if let folderId {
            line("--\(boundary)")
            line("Content-Disposition: form-data; name=\"folder_id\"")
            line("")
            line(folderId)
        }
        line("--\(boundary)--")
        return body
    }

    private func shellSend(_ method: String, _ path: String, body: Data, contentType: String) async throws -> Data {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method
        request.timeoutInterval = 60
        request.httpBody = body
        request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        return try await shellPerform(request)
    }

    private func shellPerform(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            var message = HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            if let detail = try? JSONDecoder().decode([String: JSONValue].self, from: data)["detail"]?.stringValue {
                message = detail
            }
            throw APIError(status: http.statusCode, message: message)
        }
        return data
    }
}
