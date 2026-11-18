import SwiftUI
import UniformTypeIdentifiers

extension UTType {
    /// Markdown text (`.md`). Declared by filename extension, which works without the app
    /// bundle importing the type.
    public static let markdownText = UTType(filenameExtension: "md", conformingTo: .plainText) ?? .plainText
}

/// A note's Markdown, for the save panel.
public struct MarkdownFile: FileDocument, Hashable, Sendable {
    public static var readableContentTypes: [UTType] { [.markdownText, .plainText] }

    public var text: String
    public var filename: String

    public init(text: String, filename: String) {
        self.text = text
        self.filename = filename
    }

    public init(configuration: ReadConfiguration) throws {
        text = String(decoding: configuration.file.regularFileContents ?? Data(), as: UTF8.self)
        filename = configuration.file.filename ?? "Note.md"
    }

    public func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: Data(text.utf8))
    }
}

public enum MarkdownFiles {
    static let extensions: Set<String> = ["md", "markdown", "mdown", "mkd", "txt"]

    /// The Markdown files among `urls`, looking inside folders, in a stable order.
    public static func collect(_ urls: [URL], fileManager: FileManager = .default) -> [URL] {
        var found: [URL] = []
        for url in urls {
            let scoped = url.startAccessingSecurityScopedResource()
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            var isDirectory: ObjCBool = false
            guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) else { continue }
            if isDirectory.boolValue {
                let enumerator = fileManager.enumerator(
                    at: url, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles, .skipsPackageDescendants])
                var inside: [URL] = []
                while let next = enumerator?.nextObject() as? URL {
                    if isMarkdown(next) { inside.append(next) }
                }
                found += inside.sorted { $0.path.localizedStandardCompare($1.path) == .orderedAscending }
            } else if isMarkdown(url) {
                found.append(url)
            }
        }
        var seen = Set<String>()
        return found.filter { seen.insert($0.resolvingSymlinksInPath().path).inserted }
    }

    public static func isMarkdown(_ url: URL) -> Bool {
        extensions.contains(url.pathExtension.lowercased())
    }

    /// A file name for an exported note: the title without characters Finder rejects.
    public static func filename(for title: String) -> String {
        let cleaned = title
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
            .trimmingCharacters(in: CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: ".")))
        return (cleaned.isEmpty ? "Note" : String(cleaned.prefix(120))) + ".md"
    }
}
