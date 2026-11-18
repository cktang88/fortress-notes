import AppKit
import SwiftUI

/// Renders a SwiftUI view in an offscreen window (so AppKit-backed views like List and
/// NavigationSplitView draw) and writes a PNG to $SNAPSHOT_DIR, for eyeballing in CI.
@MainActor
func snapshot<V: View>(
    _ view: V, named name: String, size: CGSize = CGSize(width: 1200, height: 760),
    appearance: NSAppearance.Name = .aqua
) throws {
    guard let directory = ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] else { return }
    let host = NSHostingView(rootView: view.frame(width: size.width, height: size.height))
    host.frame = CGRect(origin: .zero, size: size)
    let window = NSWindow(
        contentRect: host.frame, styleMask: [.titled, .fullSizeContentView],
        backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: appearance)
    window.contentView = host
    host.layoutSubtreeIfNeeded()
    // Let SwiftUI finish a few layout/render passes before capturing.
    for _ in 0..<5 { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
    host.cacheDisplay(in: host.bounds, to: rep)
    let data = rep.representation(using: .png, properties: [:])!
    let url = URL(fileURLWithPath: directory).appendingPathComponent("\(name).png")
    try FileManager.default.createDirectory(
        atPath: directory, withIntermediateDirectories: true)
    try data.write(to: url)
}
