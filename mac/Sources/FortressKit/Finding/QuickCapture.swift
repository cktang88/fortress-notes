import AppKit
import Carbon.HIToolbox
import Observation
import SwiftUI

/// Quick Capture: ⇧⌘Space from anywhere opens a small glass panel over whatever app is
/// in front (without switching to Fortress). ↩ saves to the inbox, ⇧↩ adds a line,
/// ⌘↩ saves and opens the note, esc puts it away (the draft is kept).
@MainActor
public final class QuickCapture {
    public static let shared = QuickCapture()

    let state = QuickCaptureState()
    private var panel: QuickCapturePanel?
    private weak var model: AppModel?
    private var hotKey: GlobalHotKey?
    private var monitor: Any?
    private var draftExpiry: Date?
    private var hideGeneration = 0

    /// Registers the global shortcut (⇧⌘Space). Safe to call more than once.
    public func install(model: AppModel) {
        self.model = model
        guard hotKey == nil else { return }
        hotKey = GlobalHotKey(keyCode: UInt32(kVK_Space), modifiers: UInt32(cmdKey | shiftKey)) { [weak self] in
            self?.toggle()
        }
    }

    public var isShown: Bool { (panel?.isVisible ?? false) && monitor != nil }

    public func toggle() {
        isShown ? hide() : show()
    }

    public func show() {
        hideGeneration += 1
        if let expiry = draftExpiry, expiry < .now { state.text = "" }
        draftExpiry = nil
        state.phase = .editing
        let panel = self.panel ?? makePanel()
        self.panel = panel
        position(panel)
        panel.alphaValue = 0
        panel.makeKeyAndOrderFront(nil)
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            panel.animator().alphaValue = 1
        }
        state.focusRequest += 1
        startMonitoring()
    }

    public func hide() {
        guard let panel, panel.isVisible else { return }
        stopMonitoring()
        if !state.text.isEmpty { draftExpiry = Date.now.addingTimeInterval(600) }  // Keep a draft for 10 minutes.
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.12
            panel.animator().alphaValue = 0
        }
        hideGeneration += 1
        let generation = hideGeneration
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(130))
            guard let self, generation == self.hideGeneration, self.monitor == nil else { return }
            self.panel?.orderOut(nil)
        }
    }

    func save(andOpen: Bool) {
        let text = state.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, state.phase != .saving else { return }
        let api = model?.api ?? APIClient()
        state.phase = .saving
        Task {
            do {
                let result = try await api.capture(text)
                state.text = ""
                state.phase = .saved(title: result.title, folder: result.folder)
                await model?.refreshNavigation()
                if andOpen {
                    hide()
                    open(result.documentId)
                } else {
                    try? await Task.sleep(for: .milliseconds(900))
                    if case .saved = state.phase { hide() }
                }
            } catch {
                state.phase = .failed(AskTurn.message(for: error))
            }
        }
    }

    private func open(_ documentId: String) {
        model?.open(noteId: documentId)
        NSApp.activate()
        NSApp.windows.first { $0.canBecomeMain && !($0 is NSPanel) }?.makeKeyAndOrderFront(nil)
    }

    private func makePanel() -> QuickCapturePanel {
        let panel = QuickCapturePanel(
            contentRect: NSRect(x: 0, y: 0, width: QuickCaptureView.width + 48, height: 220))
        let view = QuickCaptureView(
            state: state,
            onSave: { [weak self] in self?.save(andOpen: false) },
            onCancel: { [weak self] in self?.hide() })
            .padding(24)  // Room for the panel's shadow.
            .tint(.fortressEmber)
        panel.contentView = NSHostingView(rootView: view)
        return panel
    }

    /// Centered horizontally, its top at 22% of the screen with the pointer.
    private func position(_ panel: NSPanel) {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { $0.frame.contains(mouse) } ?? NSScreen.main
        guard let frame = screen?.visibleFrame else { return }
        let size = panel.frame.size
        let top = frame.maxY - frame.height * 0.22 + 24
        panel.setFrameOrigin(NSPoint(x: frame.midX - size.width / 2, y: top - size.height))
    }

    private func startMonitoring() {
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            let handled = MainActor.assumeIsolated { self?.handleKey(event) ?? false }
            return handled ? nil : event
        }
    }

    private func stopMonitoring() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }

    private func handleKey(_ event: NSEvent) -> Bool {
        guard let panel, event.window === panel else { return false }
        let modifiers = KeyCode.modifiers(event)
        if event.keyCode == KeyCode.escape {
            hide()
            return true
        }
        if KeyCode.isReturn(event) {
            if modifiers.isEmpty { save(andOpen: false); return true }
            if modifiers == .command { save(andOpen: true); return true }
        }
        return false  // ⇧↩ and everything else go to the text.
    }
}

@MainActor
@Observable
final class QuickCaptureState {
    enum Phase: Equatable {
        case editing, saving
        case saved(title: String, folder: String)
        case failed(String)
    }

    var text = ""
    var phase: Phase = .editing
    /// Bumped each time the panel opens, to put the caret in the text.
    var focusRequest = 0

    init(text: String = "", phase: Phase = .editing) {
        self.text = text
        self.phase = phase
    }
}

/// A borderless floating panel that takes typing without activating the app.
final class QuickCapturePanel: NSPanel {
    init(contentRect: NSRect) {
        super.init(
            contentRect: contentRect,
            styleMask: [.nonactivatingPanel, .borderless, .fullSizeContentView],
            backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false  // The SwiftUI view draws its own soft shadow.
        isMovableByWindowBackground = true
        hidesOnDeactivate = false
        isReleasedWhenClosed = false
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// The capture panel's content: a few lines of text on glass, with a quiet footer.
struct QuickCaptureView: View {
    static let width: CGFloat = 560

    let state: QuickCaptureState
    var onSave: () -> Void = {}
    var onCancel: () -> Void = {}
    @FocusState private var isFocused: Bool

    var body: some View {
        @Bindable var state = state
        GlassEffectContainer {
            VStack(alignment: .leading, spacing: 0) {
                ZStack(alignment: .topLeading) {
                    if state.text.isEmpty {
                        Text("Capture a thought…")
                            .foregroundStyle(.tertiary)
                            .padding(.leading, 5)
                            .allowsHitTesting(false)
                    }
                    TextEditor(text: $state.text)
                        .textEditorStyle(.plain)
                        .scrollContentBackground(.hidden)
                        .scrollIndicators(.never)
                        .focused($isFocused)
                        .disabled(state.phase == .saving)
                }
                .font(.system(size: 15))
                .lineSpacing(4)
                .frame(height: 88)
                .padding(.horizontal, 14)
                .padding(.top, 16)

                footer
                    .padding(.horizontal, 18)
                    .frame(height: 38)
            }
            .frame(width: Self.width)
            .glassEffect(.regular, in: .rect(cornerRadius: 22))
        }
        .shadow(color: .black.opacity(0.18), radius: 18, y: 8)
        .onChange(of: state.focusRequest, initial: true) { _, _ in isFocused = true }
    }

    @ViewBuilder private var footer: some View {
        HStack(spacing: 14) {
            switch state.phase {
            case .saved(let title, let folder):
                Label {
                    Text("Saved to \(folder) · \(Text(verbatim: title).foregroundStyle(.secondary))")
                        .lineLimit(1)
                } icon: {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.tint)
                }
                .transition(.opacity)
            case .failed(let message):
                Label(message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            case .saving:
                Label("Saving…", systemImage: "bolt").foregroundStyle(.secondary)
            case .editing:
                Label("New note", systemImage: "square.and.pencil")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if case .saved = state.phase {} else {
                hint("Save", key: "↩")
                hint("Save & Open", key: "⌘↩")
                hint("Close", key: "esc")
            }
        }
        .font(.subheadline)
        .animation(.smooth(duration: 0.2), value: state.phase)
    }

    private func hint(_ label: String, key: String) -> some View {
        HStack(spacing: 4) {
            Text(label).foregroundStyle(.secondary)
            Text(key).font(.caption.weight(.medium)).foregroundStyle(.tertiary)
        }
    }
}

// MARK: Global hot key (Carbon; needs no accessibility permission)

@MainActor
final class GlobalHotKey {
    private static var actions: [UInt32: @MainActor () -> Void] = [:]
    private static var nextID: UInt32 = 1
    private static var handlerInstalled = false
    private var reference: EventHotKeyRef?
    private let id: UInt32

    init?(keyCode: UInt32, modifiers: UInt32, action: @escaping @MainActor () -> Void) {
        Self.installHandler()
        id = Self.nextID
        Self.nextID += 1
        let hotKeyID = EventHotKeyID(signature: OSType(0x464E_5143), id: id)  // "FNQC"
        let status = RegisterEventHotKey(keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &reference)
        guard status == noErr else { return nil }
        Self.actions[id] = action
    }

    isolated deinit {
        if let reference { UnregisterEventHotKey(reference) }
        Self.actions[id] = nil
    }

    private static func installHandler() {
        guard !handlerInstalled else { return }
        handlerInstalled = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var hotKeyID = EventHotKeyID()
            let status = GetEventParameter(
                event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID)
            guard status == noErr else { return status }
            let id = hotKeyID.id
            MainActor.assumeIsolated { GlobalHotKey.actions[id]?() }
            return noErr
        }, 1, &spec, nil, nil)
    }
}
