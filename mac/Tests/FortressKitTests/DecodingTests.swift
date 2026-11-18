import Foundation
import Testing

@testable import FortressKit

/// Every fixture is a real response captured from the backend.
private func fixture(_ name: String) throws -> Data {
    let url = try #require(Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}

private func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
    try JSONDecoder().decode(T.self, from: try fixture(name))
}

@Test func decodesNavigationTree() throws {
    let navigation = try decode(NavigationResponse.self, "navigation.json")
    let folders = navigation.items.compactMap { if case .folder(let f) = $0 { f } else { nil } }
    #expect(folders.map(\.name).contains("Travel"))
    #expect(folders.flatMap(\.children).contains { if case .document(let d) = $0 { d.title == "Kyoto trip" } else { false } })
    #expect(!navigation.recent.isEmpty)
}

@Test func decodesDocumentAndKeepsAttributeKeysVerbatim() throws {
    let document = try decode(BlockDocument.self, "document.json")
    #expect(document.title == "Kyoto trip")
    #expect(document.children.first?.type == "heading")
    #expect(document.children.contains { $0.text == "We booked a ryokan in Gion." })
    // Free-form keys must round-trip untouched (no snake/camel conversion).
    let encoded = try JSONEncoder().encode(BlockOperation(operation: .setAttrs, blockId: "b", attrs: ["textAlignment": .string("left")]))
    #expect(String(decoding: encoded, as: UTF8.self).contains("\"textAlignment\""))
    #expect(String(decoding: encoded, as: UTF8.self).contains("\"block_id\""))
}

@Test func decodesSearchAndLookups() throws {
    let matches = try decode([SearchMatch].self, "search.json")
    #expect(matches.contains { $0.documentTitle == "Kyoto trip" && !$0.highlights.isEmpty })
    _ = try decode([RelatedResult].self, "related.json")
    _ = try decode([Backlink].self, "backlinks.json")
    #expect(try decode([BlockLinkTarget].self, "link-targets.json").contains { $0.text.contains("ryokan") })
}

@Test func decodesCaptureTrashAndBackups() throws {
    #expect(try decode(CaptureResult.self, "capture.json").folder == "Uncategorized")
    #expect(try decode([TrashedDocument].self, "trash.json").first?.title == "Packing list")
    #expect(try decode([Backup].self, "backups.json").first?.kind == "manual")
}

@Test func decodesStreamedAskEvents() throws {
    for name in ["ask.ndjson", "ask-answered.ndjson"] {
        let lines = String(decoding: try fixture(name), as: UTF8.self).split(separator: "\n")
        let events = try lines.compactMap { try APIClient.askEvent(from: Data($0.utf8)) }
        guard case .done(let result) = events.last else {
            Issue.record("\(name) has no done event")
            continue
        }
        if name == "ask-answered.ndjson" {
            #expect(result.status == .answered)
            #expect(events.contains { if case .sentence(let s) = $0 { s.citations.first?.quote == "ryokan in Gion" } else { false } })
        } else {
            #expect(result.status == .notConfigured)
        }
    }
}

@Test func parsesBackendTimestamps() {
    #expect(Timestamp.date("2026-11-18T00:11:49.055171+00:00") != nil)
    #expect(Timestamp.date("2026-11-18T00:11:49+00:00") != nil)
}
