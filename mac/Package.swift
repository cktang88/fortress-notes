// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "FortressNotes",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "FortressNotes", targets: ["FortressNotes"]),
        .library(name: "FortressKit", targets: ["FortressKit"]),
    ],
    targets: [
        // Models, the API client and every view; the app target is just the entry point.
        .target(name: "FortressKit"),
        .executableTarget(name: "FortressNotes", dependencies: ["FortressKit"]),
        .testTarget(
            name: "FortressKitTests", dependencies: ["FortressKit"],
            resources: [.copy("Fixtures")]),
    ]
)
