import Foundation
import os

/// Signpost intervals for the moments the app is judged by (open a note, first palette
/// result, first Ask token, save), visible in Instruments' Points of Interest and
/// os_signpost tracks under the subsystem `app.fortress.notes`.
enum Signposts {
    static let signposter = OSSignposter(subsystem: "app.fortress.notes", category: .pointsOfInterest)

    static func begin(_ name: StaticString) -> OSSignpostIntervalState {
        signposter.beginInterval(name, id: signposter.makeSignpostID())
    }

    static func end(_ name: StaticString, _ state: OSSignpostIntervalState) {
        signposter.endInterval(name, state)
    }

    static func event(_ name: StaticString) {
        signposter.emitEvent(name)
    }
}

/// Time spent in the editor's own text processing, for the performance tests' breakdowns
/// (milliseconds since `reset`).
@MainActor
enum EditorMetrics {
    static var normalize = 0.0
    static var restyle = 0.0
    /// The editor's own work after an edit (textDidChange, selection change callbacks).
    static var callbacks = 0.0

    static func reset() {
        normalize = 0
        restyle = 0
        callbacks = 0
    }

    static func now() -> UInt64 { DispatchTime.now().uptimeNanoseconds }
    static func since(_ start: UInt64) -> Double { Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000 }
}
