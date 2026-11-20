// swift-tools-version: 6.2
import PackageDescription

// Swift 6 language mode (complete concurrency checking) and no warnings: a warning is a
// build error, so data-race diagnostics can't pile up unseen.
let strict: [SwiftSetting] = [.treatAllWarnings(as: .error)]

let package = Package(
    name: "FortressNotes",
    defaultLocalization: "en",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "FortressNotes", targets: ["FortressNotes"]),
        .library(name: "FortressKit", targets: ["FortressKit"]),
    ],
    targets: [
        // Models, the API client and every view; the app target is just the entry point.
        .target(name: "FortressKit", swiftSettings: strict),
        .executableTarget(name: "FortressNotes", dependencies: ["FortressKit"], swiftSettings: strict),
        .testTarget(
            name: "FortressKitTests", dependencies: ["FortressKit"],
            resources: [.copy("Fixtures")], swiftSettings: strict),
    ],
    swiftLanguageModes: [.v6]
)
