import AppKit
import Carbon.HIToolbox
import SwiftUI

/// The global Quick Capture shortcut, chosen in Settings ▸ General (default ⇧⌘Space).
/// Stored in `UserDefaults` as "keyCode:modifiers:key"; "none" turns it off.
public struct QuickCaptureShortcut: Equatable, @unchecked Sendable {  // plain values
    public static let defaultsKey = "QuickCaptureShortcut"
    public static let standard = QuickCaptureShortcut(keyCode: UInt16(kVK_Space), modifiers: [.command, .shift], key: " ")

    /// The key's virtual key code (what Carbon registers).
    public var keyCode: UInt16
    public var modifiers: NSEvent.ModifierFlags
    /// The key's character, for the menu item and the display.
    public var key: String

    public init(keyCode: UInt16, modifiers: NSEvent.ModifierFlags, key: String) {
        self.keyCode = keyCode
        self.modifiers = modifiers.intersection([.command, .shift, .option, .control])
        self.key = key.lowercased()
    }

    /// From a key press while recording. A global shortcut needs ⌘, ⌥ or ⌃ (⇧ alone
    /// would steal typing in every app).
    public init?(event: NSEvent) {
        let modifiers = event.modifierFlags.intersection([.command, .shift, .option, .control])
        guard !modifiers.intersection([.command, .option, .control]).isEmpty,
              let key = event.charactersIgnoringModifiers, !key.isEmpty else { return nil }
        self.init(keyCode: event.keyCode, modifiers: modifiers, key: key)
    }

    public init?(storage: String) {
        let parts = storage.split(separator: ":", maxSplits: 2, omittingEmptySubsequences: false)
        guard parts.count == 3, let code = UInt16(parts[0]), let raw = UInt(parts[1]) else { return nil }
        self.init(keyCode: code, modifiers: NSEvent.ModifierFlags(rawValue: raw), key: String(parts[2]))
    }

    public var storage: String { "\(keyCode):\(modifiers.rawValue):\(key)" }
    public static let noneStorage = "none"

    /// The saved shortcut; nil when turned off. Unset means the standard one.
    public static func saved(in defaults: UserDefaults = .standard) -> QuickCaptureShortcut? {
        guard let stored = defaults.string(forKey: defaultsKey) else { return .standard }
        return QuickCaptureShortcut(storage: stored)
    }

    var carbonModifiers: UInt32 {
        var flags: UInt32 = 0
        if modifiers.contains(.command) { flags |= UInt32(cmdKey) }
        if modifiers.contains(.shift) { flags |= UInt32(shiftKey) }
        if modifiers.contains(.option) { flags |= UInt32(optionKey) }
        if modifiers.contains(.control) { flags |= UInt32(controlKey) }
        return flags
    }

    private var eventModifiers: SwiftUI.EventModifiers {
        var flags: SwiftUI.EventModifiers = []
        if modifiers.contains(.command) { flags.insert(.command) }
        if modifiers.contains(.shift) { flags.insert(.shift) }
        if modifiers.contains(.option) { flags.insert(.option) }
        if modifiers.contains(.control) { flags.insert(.control) }
        return flags
    }

    private var keyEquivalent: KeyEquivalent? {
        if keyCode == UInt16(kVK_Space) { return .space }
        guard key.count == 1, let character = key.first else { return nil }
        return KeyEquivalent(character)
    }

    /// For the Quick Capture menu item (so the menu shows the shortcut).
    public var keyboardShortcut: KeyboardShortcut? {
        keyEquivalent.map { KeyboardShortcut($0, modifiers: eventModifiers) }
    }

    public var display: String {
        guard let keyEquivalent else { return AppShortcut.display(key: "?", modifiers: eventModifiers) }
        return AppShortcut.display(key: keyEquivalent, modifiers: eventModifiers)
    }

    /// What macOS or a standard command already does with this combination, if anything
    /// (Spotlight, input sources, emoji, screenshots…). Such a choice is refused.
    public var systemMeaning: String? {
        guard let meaning = Shortcuts.systemReserved[display] else { return nil }
        return meaning
    }

    /// One of the app's own menu shortcuts (it would never reach the menu).
    public var appMeaning: String? {
        Shortcuts.all.first { $0.display == display }?.title
    }
}

/// Settings control: shows the shortcut; click, then press keys to change it (esc
/// cancels, ⌫ turns it off). Refuses combinations macOS or the app already use, and
/// says when another app holds the chosen one.
struct QuickCaptureShortcutRecorder: View {
    @AppStorage(QuickCaptureShortcut.defaultsKey) private var stored = QuickCaptureShortcut.standard.storage
    @State private var isRecording = false
    @State private var monitor: Any?
    @State private var refusal: String?
    let state: QuickCaptureState

    var body: some View {
        let shortcut = stored == QuickCaptureShortcut.noneStorage ? nil : QuickCaptureShortcut(storage: stored)
        LabeledContent("Quick Capture") {
            VStack(alignment: .trailing, spacing: 4) {
                HStack(spacing: 8) {
                    Button(isRecording ? "Type Shortcut…" : (shortcut?.display ?? "None")) {
                        isRecording ? stopRecording() : startRecording()
                    }
                    .monospacedDigit()
                    .accessibilityLabel(isRecording ? "Recording shortcut" : "Quick Capture shortcut: \(shortcut?.display ?? "None")")
                    .accessibilityHint("Press to record a new shortcut")
                    if shortcut != QuickCaptureShortcut.standard {
                        Button("Reset") { set(.standard) }
                    }
                }
                if let refusal {
                    Text(refusal).font(.caption).foregroundStyle(.themeSecondary)
                } else if state.isShortcutTaken, shortcut != nil {
                    Label("Another app is using this shortcut. Choose another one.", systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.themeSecondary)
                }
            }
        }
        .onDisappear { stopRecording() }
    }

    private func startRecording() {
        refusal = nil
        isRecording = true
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            MainActor.assumeIsolated { handle(event) }
            return nil
        }
    }

    private func stopRecording() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        isRecording = false
    }

    private func handle(_ event: NSEvent) {
        let modifiers = event.modifierFlags.intersection([.command, .shift, .option, .control])
        if event.keyCode == UInt16(kVK_Escape), modifiers.isEmpty { stopRecording(); return }
        if event.keyCode == UInt16(kVK_Delete), modifiers.isEmpty {
            stored = QuickCaptureShortcut.noneStorage
            QuickCapture.shared.updateShortcut()
            stopRecording()
            return
        }
        guard let candidate = QuickCaptureShortcut(event: event) else {
            refusal = "Use at least one of ⌘, ⌥ or ⌃."
            return
        }
        if let meaning = candidate.systemMeaning {
            refusal = "\(candidate.display) is macOS’s “\(meaning)”. Choose another one."
            return
        }
        if let title = candidate.appMeaning {
            refusal = "\(candidate.display) is Fortress’s “\(title)”. Choose another one."
            return
        }
        set(candidate)
        stopRecording()
    }

    private func set(_ shortcut: QuickCaptureShortcut) {
        refusal = nil
        stored = shortcut.storage
        QuickCapture.shared.updateShortcut()
    }
}
