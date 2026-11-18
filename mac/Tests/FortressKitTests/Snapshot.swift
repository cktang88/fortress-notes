import AppKit
import SwiftUI

/// Shows a SwiftUI view in a real on-screen window and captures it with `screencapture`,
/// so materials, Liquid Glass and lazily drawn lists look exactly as they do in the app.
/// Writes `<name>.png` to $SNAPSHOT_DIR; does nothing when that isn't set.
@MainActor
func snapshot<V: View>(
    _ view: V, named name: String, size: CGSize = CGSize(width: 1000, height: 660),
    appearance: NSAppearance.Name = .aqua
) throws {
    guard let directory = ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] else { return }
    try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    app.appearance = NSAppearance(named: appearance)

    let window = NSWindow(
        contentRect: CGRect(origin: CGPoint(x: 80, y: 80), size: size),
        styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
        backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: appearance)
    window.titlebarAppearsTransparent = true
    window.isReleasedWhenClosed = false
    window.contentView = NSHostingView(rootView: view)
    window.setContentSize(size)
    window.makeKeyAndOrderFront(nil)
    window.orderFrontRegardless()
    app.activate()
    // Let SwiftUI lay out, lists load their rows and glass settle.
    for _ in 0..<20 { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }

    let url = URL(fileURLWithPath: directory).appendingPathComponent("\(name).png")
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-o", "-l", String(window.windowNumber), url.path]
    try capture.run()
    capture.waitUntilExit()
    window.close()
}
