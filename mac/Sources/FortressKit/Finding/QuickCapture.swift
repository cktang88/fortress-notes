import AppKit
import Carbon.HIToolbox
import Observation
import SwiftUI

/// Quick Capture: ⇧⌘Space from anywhere opens a small glass panel over whatever app is
/// in front (without switching to Fortress). ↩ saves to the inbox, ⇧↩ adds a line,
/// ⌘↩ saves and opens the note, esc puts it away.
///
/// Nothing typed is lost: esc (or quitting) keeps the draft until it is saved, and a
/// capture made while the backend is unreachable waits in an on-disk outbox and is sent
/// when it answers again. The shortcut is re-registered after sleep, and if another app
/// owns it the person is told (the menu item still works) and it is retried later.
@MainActor
public final class QuickCapture {
    public static let shared = QuickCapture()

    let state: QuickCaptureState
    let outbox: CaptureOutbox
    private let drafts: CaptureDraftStore
    private var panel: QuickCapturePanel?
    private weak var model: AppModel?
    private var keeper: HotKeyKeeper?
    private var monitor: Any?
    private var hideGeneration = 0
    private var terminationObserver: NSObjectProtocol?
    private var isInstalled = false

    init(outbox: CaptureOutbox = CaptureOutbox(), drafts: CaptureDraftStore = CaptureDraftStore()) {
        self.outbox = outbox
        self.drafts = drafts
        state = QuickCaptureState(text: drafts.load())
        outbox.onSent = { [weak self] _ in
            guard let self else { return }
            Task { await self.model?.refreshNavigation() }
        }
    }

    /// Registers the global shortcut (Settings ▸ General; ⇧⌘Space unless changed). Safe
    /// to call more than once.
    public func install(model: AppModel) {
        attach(model)
        guard !isInstalled else { return }
        isInstalled = true
        updateShortcut()
        let drafts = self.drafts
        let state = self.state
        terminationObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { _ in
            MainActor.assumeIsolated { drafts.save(state.text) }
        }
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.outbox.nudge() }
        }
    }

    /// Registers the shortcut saved in Settings (again), dropping the previous one.
    public func updateShortcut() {
        keeper = nil
        state.isShortcutTaken = false
        guard let shortcut = QuickCaptureShortcut.saved() else { return }
        let keeper = HotKeyKeeper { [weak self] in
            GlobalHotKey(keyCode: UInt32(shortcut.keyCode), modifiers: shortcut.carbonModifiers) { self?.toggle() }
        }
        keeper.onConflict = { [weak self] in
            guard let self else { return }
            self.state.isShortcutTaken = true
            self.model?.notice = Notice(message: "Another app is using \(shortcut.display), so Quick Capture can’t use it. Choose another shortcut in Settings; Quick Capture is still in the View menu.")
        }
        keeper.onRegistered = { [weak self] in self?.state.isShortcutTaken = false }
        self.keeper = keeper
        keeper.activate()
    }

    /// Where captures are sent (also used by tests, without the global shortcut).
    func attach(_ model: AppModel) {
        self.model = model
        outbox.connect { [weak model] text in
            let api = await MainActor.run { model?.api } ?? APIClient()
            return try await api.capture(text)
        }
    }

    /// Whether the global shortcut is registered (false while another app holds it).
    public var isShortcutRegistered: Bool { keeper?.status == .registered }

    public var isShown: Bool { (panel?.isVisible ?? false) && monitor != nil }

    public func toggle() {
        isShown ? hide() : show()
    }

    public func show() {
        hideGeneration += 1
        if state.phase != .saving { state.phase = .editing }
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
        drafts.save(state.text)  // Kept until saved, across launches too.
        guard let panel, panel.isVisible else { return }
        stopMonitoring()
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

    @discardableResult
    func save(andOpen: Bool) -> Task<Void, Never>? {
        let text = state.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, state.phase != .saving else { return nil }
        let api = model?.api ?? APIClient()
        state.phase = .saving
        drafts.save(state.text)  // Until the server has it.
        return Task {
            do {
                let result = try await api.capture(text)
                state.text = ""
                drafts.save("")
                state.phase = .saved(title: result.title, folder: result.folder)
                await model?.refreshNavigation()
                if andOpen {
                    hide()
                    open(result.documentId)
                } else {
                    try? await Task.sleep(for: .milliseconds(900))
                    if case .saved = state.phase { hide() }
                }
            } catch where CaptureOutbox.isRetryable(error) {
                // Offline or the server is down: keep it on disk and send it later.
                outbox.enqueue(text)
                state.text = ""
                drafts.save("")
                state.phase = .queued
                try? await Task.sleep(for: .milliseconds(1400))
                if state.phase == .queued { hide() }
            } catch {
                state.phase = .failed(AskTurn.message(for: error))  // The text stays, as a draft.
            }
        }
    }

    private func open(_ documentId: String) {
        NSApp.activate()
        if let window = model?.library.activeWindowModel {
            window.open(noteId: documentId)
            NSApp.windows.first { $0.canBecomeMain && !($0 is NSPanel) && $0.isVisible }?.makeKeyAndOrderFront(nil)
        } else {
            model?.library.openNote(documentId)  // No window open: opens one.
        }
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

extension QuickCaptureState.Phase {
    var isDone: Bool {
        switch self {
        case .saved, .queued: true
        default: false
        }
    }
}

@MainActor
@Observable
final class QuickCaptureState {
    enum Phase: Equatable {
        case editing, saving
        case saved(title: String, folder: String)
        /// Saved on this Mac; sent when the backend answers again.
        case queued
        case failed(String)
    }

    var text = ""
    var phase: Phase = .editing
    /// Bumped each time the panel opens, to put the caret in the text.
    var focusRequest = 0
    /// Another app holds the global shortcut (Settings says so next to it).
    var isShortcutTaken = false

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
            .floatingGlass(in: RoundedRectangle(cornerRadius: 22, style: .continuous))
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
            case .queued:
                Label("Saved on this Mac · it’ll be added when Fortress is back", systemImage: "tray.and.arrow.down")
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            case .editing:
                Label("New note", systemImage: "square.and.pencil")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if state.phase.isDone {} else {
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
