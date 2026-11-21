import Foundation
import Testing

@testable import FortressKit

/// `BlockSchema.bundled` is a hand copy of the backend's registry; the backend's tests
/// write `Fixtures/block-schema.json` from `block_schema.py`, so a change there that
/// isn't copied here fails this test.
@Test func bundledBlockSchemaMatchesTheBackendsRegistry() throws {
    struct Registry: Decodable {
        struct TypeInfo: Decodable {
            var category: String
            var children: [String]
            var change_group: String?
        }
        var flow: [String]
        var types: [String: TypeInfo]
    }
    let url = try #require(Bundle.module.url(forResource: "block-schema.json", withExtension: nil, subdirectory: "Fixtures"))
    let registry = try JSONDecoder().decode(Registry.self, from: Data(contentsOf: url))
    let bundled = BlockSchema.bundled
    #expect(bundled.flow.sorted() == registry.flow)
    #expect(Set(bundled.types.keys) == Set(registry.types.keys))
    for (name, expected) in registry.types {
        let info = try #require(bundled.types[name], "\(name) is missing")
        #expect(info.category == expected.category, "\(name)")
        #expect(info.children.sorted() == expected.children, "\(name)")
        #expect(info.changeGroup == expected.change_group, "\(name)")
    }
}
