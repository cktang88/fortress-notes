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
    guard ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] != nil else { return }
    let window = HostWindow(view, size: size, appearance: appearance)
    try window.capture(named: name)
    window.close()
}

/// A SwiftUI view in a real on-screen window, for tests that look at the AppKit views
/// SwiftUI made (a `List`'s table rows) and for snapshots.
@MainActor
final class HostWindow {
    let window: NSWindow

    init<V: View>(_ view: V, size: CGSize = CGSize(width: 1000, height: 660), appearance: NSAppearance.Name = .aqua) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        app.appearance = NSAppearance(named: appearance)
        window = NSWindow(
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
        settle()
    }

    /// Runs the main run loop for about `seconds`, so SwiftUI applies model changes.
    func settle(_ seconds: Double = 1) {
        let steps = max(1, Int(seconds / 0.05))
        for _ in 0..<steps { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
    }

    /// Every table view in the window (a SwiftUI `List` on macOS is one), top to bottom.
    var tableViews: [NSTableView] {
        func walk(_ view: NSView) -> [NSTableView] {
            (view as? NSTableView).map { [$0] } ?? view.subviews.flatMap(walk)
        }
        return window.contentView.map(walk) ?? []
    }

    /// Writes `<name>.png` to $SNAPSHOT_DIR (nothing when that isn't set).
    func capture(named name: String) throws {
        guard let directory = ProcessInfo.processInfo.environment["SNAPSHOT_DIR"] else { return }
        try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        let url = URL(fileURLWithPath: directory).appendingPathComponent("\(name).png")
        let capture = Process()
        capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        capture.arguments = ["-x", "-o", "-l", String(window.windowNumber), url.path]
        try capture.run()
        capture.waitUntilExit()
    }

    func close() { window.close() }
}
