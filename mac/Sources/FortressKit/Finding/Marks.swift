import AppKit
import SwiftUI

// MARK: Highlight ranges

/// Turns the backend's highlight ranges (UTF-16 offsets into a passage, like a browser
/// slices strings) into ranges of Swift characters.
enum PassageHighlights {
    /// Sorted, merged ranges, each widened to whole characters (so an emoji or a letter
    /// with a combining accent is never cut in half). Out-of-bounds or empty pairs are dropped.
    static func ranges(in text: String, utf16 offsets: [[Int]]) -> [Range<String.Index>] {
        let length = text.utf16.count
        var ranges: [Range<String.Index>] = []
        for pair in offsets {
            guard pair.count == 2 else { continue }
            let start = max(0, pair[0])
            let end = min(length, pair[1])
            guard start < end else { continue }
            let lower = String.Index(utf16Offset: start, in: text)
            let upper = String.Index(utf16Offset: end, in: text)
            ranges.append(text.rangeOfComposedCharacterSequences(for: lower..<upper))
        }
        ranges.sort { $0.lowerBound < $1.lowerBound }
        var merged: [Range<String.Index>] = []
        for range in ranges {
            if let last = merged.last, range.lowerBound <= last.upperBound {
                merged[merged.count - 1] = last.lowerBound..<max(last.upperBound, range.upperBound)
            } else {
                merged.append(range)
            }
        }
        return merged
    }

    /// The passage cut into plain and highlighted pieces, in order.
    static func segments(of text: String, utf16 offsets: [[Int]]) -> [(text: Substring, isMatch: Bool)] {
        var segments: [(Substring, Bool)] = []
        var cursor = text.startIndex
        for range in ranges(in: text, utf16: offsets) {
            if cursor < range.lowerBound { segments.append((text[cursor..<range.lowerBound], false)) }
            segments.append((text[range], true))
            cursor = range.upperBound
        }
        if cursor < text.endIndex { segments.append((text[cursor...], false)) }
        return segments
    }
}

// MARK: Drawing marks behind text

/// Marks a run of `Text` to get a shape drawn behind it by `MarkRenderer`.
struct TextMark: TextAttribute {
    enum Style: Hashable { case highlight, citation }
    var style: Style
}

/// Draws find-style highlights (rounded, radius 3) and citation chips (capsules) behind
/// marked runs; everything else draws as usual.
struct MarkRenderer: TextRenderer {
    var highlight: Color = Color(nsColor: .findHighlightColor)

    func draw(layout: Text.Layout, in context: inout GraphicsContext) {
        for line in layout {
            for run in line {
                if let mark = run[TextMark.self] {
                    let bounds = run.typographicBounds.rect
                    switch mark.style {
                    case .highlight:
                        let rect = bounds.insetBy(dx: -1, dy: 0)
                        context.fill(RoundedRectangle(cornerRadius: 3).path(in: rect), with: .color(highlight))
                    case .citation:
                        let rect = bounds.insetBy(dx: -2, dy: -0.5)
                        // The window's tint (Fortress Ember), faint.
                        context.fill(Capsule().path(in: rect), with: .style(.tint.opacity(0.15)))
                    }
                }
                context.draw(run)
            }
        }
    }
}

extension Text {
    /// Joins pieces of styled text (string interpolation keeps each piece's attributes).
    static func joined(_ parts: [Text]) -> Text {
        parts.reduce(Text(verbatim: "")) { Text("\($0)\($1)") }
    }

    /// A passage with its matched words highlighted like Find (yellow, dark text, semibold).
    static func highlighted(_ text: String, ranges: [[Int]]) -> Text {
        joined(PassageHighlights.segments(of: text, utf16: ranges).map { segment in
            if segment.isMatch {
                Text(verbatim: String(segment.text))
                    .fontWeight(.semibold)
                    .foregroundStyle(Color.black.opacity(0.88))
                    .customAttribute(TextMark(style: .highlight))
            } else {
                Text(verbatim: String(segment.text))
            }
        })
    }
}

// MARK: Keyboard

/// Watches key presses in this app while the view is on screen. The handler returns
/// true when it handled the key (the event is then swallowed).
struct LocalKeyMonitor: ViewModifier {
    let handler: @MainActor (NSEvent) -> Bool
    @State private var box = MonitorBox()

    func body(content: Content) -> some View {
        box.handler = handler
        return content
            .onAppear { box.start() }
            .onDisappear { box.stop() }
    }

    @MainActor
    final class MonitorBox {
        var handler: (@MainActor (NSEvent) -> Bool)?
        private var monitor: Any?

        func start() {
            guard monitor == nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                let handled = MainActor.assumeIsolated { self?.handler?(event) ?? false }
                return handled ? nil : event
            }
        }

        func stop() {
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
        }
    }
}

extension View {
    func onLocalKeyDown(_ handler: @escaping @MainActor (NSEvent) -> Bool) -> some View {
        modifier(LocalKeyMonitor(handler: handler))
    }
}

enum KeyCode {
    static let returnKey: UInt16 = 36
    static let keypadEnter: UInt16 = 76
    static let tab: UInt16 = 48
    static let escape: UInt16 = 53
    static let up: UInt16 = 126
    static let down: UInt16 = 125

    static func isReturn(_ event: NSEvent) -> Bool {
        event.keyCode == returnKey || event.keyCode == keypadEnter
    }

    /// Command/Shift/Option/Control held for this event.
    static func modifiers(_ event: NSEvent) -> NSEvent.ModifierFlags {
        event.modifierFlags.intersection([.command, .shift, .option, .control])
    }
}
