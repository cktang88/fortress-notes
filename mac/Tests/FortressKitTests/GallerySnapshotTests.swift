import SwiftUI
import Testing

@testable import FortressKit

/// Screenshots of real windows for eyeballing in CI (artifact "snapshots"); not diffed.
/// Each area adds its own scenes here with fixture data, no backend needed.
@MainActor
@Suite(.serialized) struct GallerySnapshotTests {
    @Test func mainWindowEmpty() throws {
        let model = AppModel()
        try snapshot(MainWindow().environment(model), named: "main-empty-light")
        try snapshot(MainWindow().environment(model), named: "main-empty-dark", appearance: .darkAqua)
    }
}
