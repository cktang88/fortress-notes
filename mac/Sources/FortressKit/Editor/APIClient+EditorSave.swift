import Foundation

extension APIClient {
    /// Applies the editor's block edits and answers with the note's metadata only
    /// (`?response=meta`: revision, title, timestamps, undo state, no `children`). The
    /// editor already holds the content, so a save in a 2,000-block note moves ~500 bytes
    /// instead of megabytes. The cached full copy is now stale, so it is dropped (the next
    /// open reads the server).
    func applyReturningMeta(_ transaction: BlockTransaction, to documentId: String) async throws -> BlockDocument {
        let body = try JSONEncoder().encode(transaction)
        let request = try makeRequest("POST", "api/block-documents/\(documentId)/transactions",
                                      query: ["response": "meta"], body: body, kind: .mutation)
        do {
            let document: BlockDocument = try await asMutation {
                let (data, response) = try await load(request)
                try check(response, body: data)
                return try Self.decode(BlockDocument.self, from: data)
            }
            store.invalidateDocument(documentId)
            return document
        } catch let error as APIError {
            if error.status != 422 { store.invalidateDocument(documentId) }
            throw error
        }
    }
}
