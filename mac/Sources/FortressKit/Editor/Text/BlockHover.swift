import AppKit
import SwiftUI

/// The quiet way to see where a block came from: hovering the left margin of a block
/// shows a small handle; hovering it (or clicking) opens a popover with "Edited 3 days
/// ago · From OneNote › Work › Standups · Created Mar 2024" (spec §7a).
@MainActor
final class BlockHover {
    private weak var controller: BlockEditorController?
    private let handle: NSButton
    private var blockRange: NSRange?
    private var popover: NSPopover?
    private var openTimer: Timer?

    init(controller: BlockEditorController) {
        self.controller = controller
        handle = NSButton()
        handle.isBordered = false
        handle.image = NSImage(systemSymbolName: "circle.grid.2x1.fill", accessibilityDescription: "Block info")?
            .withSymbolConfiguration(.init(pointSize: 9, weight: .regular))
        handle.contentTintColor = .tertiaryLabelColor
        handle.isHidden = true
        handle.toolTip = "Block info"
        handle.target = self
        handle.action = #selector(showInfo)
        handle.setAccessibilityLabel("Block info")
    }

    /// The mouse moved to `point` (text view coordinates), or left the view.
    func hover(at point: NSPoint?) {
        guard let controller, let point else { hide(); return }
        let textView = controller.textView
        if handle.superview !== textView { textView.addSubview(handle) }
        let origin = textView.textContainerOrigin
        guard point.x < origin.x + 4, point.x > origin.x - 72,
              let (range, frame) = controller.blockFrame(at: CGPoint(x: origin.x + 1, y: point.y))
        else {
            if !handle.frame.insetBy(dx: -6, dy: -6).contains(point) { hide() }
            return
        }
        let size: CGFloat = 18
        handle.frame = CGRect(x: frame.minX - size - 22, y: frame.minY + (min(frame.height, 26) - size) / 2, width: size, height: size)
        handle.isHidden = false
        if blockRange != range {
            blockRange = range
            popover?.close()
            popover = nil
        }
        openTimer?.invalidate()
        if handle.frame.insetBy(dx: -4, dy: -4).contains(point) {
            openTimer = Timer.scheduledTimer(withTimeInterval: 0.45, repeats: false) { [weak self] _ in
                MainActor.assumeIsolated { self?.showInfo() }
            }
        }
    }

    private func hide() {
        openTimer?.invalidate()
        handle.isHidden = true
        blockRange = nil
    }

    @objc func showInfo() {
        guard let controller, let range = blockRange, popover == nil,
              let info = controller.storage.blockInfo(at: range.location) else { return }
        let popover = NSPopover()
        popover.behavior = .transient
        let view = BlockInfoView(details: BlockDetails(info: info, newlyTyped: info.origin == nil)) { url in
            controller.open(url)
        }
        let hosting = NSHostingController(rootView: view)
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        popover.show(relativeTo: handle.bounds, of: handle, preferredEdge: .minX)
        self.popover = popover
    }
}

/// What the info popover says about a block.
struct BlockDetails: Equatable {
    var edited: Date?
    var created: Date?
    var source: String?
    var sourceURL: URL?
    var inherited = false
    var newlyTyped = false

    init(info: BlockInfo, newlyTyped: Bool) {
        self.newlyTyped = newlyTyped
        let origin = info.origin?.origin
        edited = origin.flatMap { Timestamp.date($0.updatedAt) }
        created = origin.flatMap { Timestamp.date($0.createdAt) }
        let provenance = origin?.provenance ?? info.newProvenance
        inherited = provenance?["inherited"]?.boolValue ?? false
        source = Self.describe(provenance)
        if let string = provenance?["source_url"]?.stringValue { sourceURL = URL(string: string) }
        if let sourceCreated = provenance?["source_created_at"]?.stringValue.flatMap(Timestamp.date) {
            created = sourceCreated
        }
    }

    /// "From OneNote › Work › Standups", "Pasted from example.com", "Folded in from Plans".
    static func describe(_ provenance: JSONValue?) -> String? {
        guard let provenance else { return nil }
        if let origin = provenance["origin"], let operation = origin["operation"]?.stringValue {
            let title = origin["document_title"]?.stringValue ?? String(localized: "another note", comment: "Block provenance: a note whose title is unknown")
            switch operation {
            case "move_into": return String(localized: "Folded in from \(title)")
            case "duplicate": return String(localized: "Duplicated from \(title)")
            case "split": return String(localized: "Split from \(title)")
            default: break
            }
        }
        let path = provenance["source_path"]?.stringValue
        let host = provenance["source_url"]?.stringValue.flatMap { URL(string: $0)?.host() }
        func with(_ base: String) -> String {
            guard let path, !path.isEmpty else { return base }
            return String(localized: "\(base) › \(path)", comment: "Block provenance: source, then the path inside it")
        }
        switch provenance["source"]?.stringValue ?? "" {
        case "manual", "": return String(localized: "Written here")
        case "capture": return host.map { String(localized: "Captured from \($0)") } ?? String(localized: "Quick Capture")
        case "onenote_import": return with(String(localized: "From OneNote"))
        case "markdown_import": return with(String(localized: "Imported from Markdown"))
        case "paste": return host.map { String(localized: "Pasted from \($0)") } ?? String(localized: "Pasted")
        case "web": return host.map { String(localized: "From \($0)") } ?? String(localized: "From the web")
        case "ai": return String(localized: "Written with AI")
        case let other: return with(String(localized: "From \(other.replacingOccurrences(of: "_", with: " "))"))
        }
    }
}

private struct BlockInfoView: View {
    let details: BlockDetails
    let open: (URL) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if details.newlyTyped && details.edited == nil {
                row("pencil", String(localized: "Written just now"))
            }
            if let edited = details.edited {
                row("pencil", EditorDates.edited(edited, now: Date()))
            }
            if let source = details.source {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.down.forward.circle").frame(width: 14)
                    Text(source)
                    if let url = details.sourceURL {
                        Button { open(url) } label: { Image(systemName: "arrow.up.forward.square") }
                            .buttonStyle(.plain).foregroundStyle(.tint).help(url.absoluteString)
                    }
                }
            }
            if let created = details.created {
                row("calendar", String(localized: "Created \(created.formatted(.dateTime.month(.abbreviated).year()))"))
            }
        }
        .font(.callout)
        .foregroundStyle(.secondary)
        .padding(12)
        .fixedSize()
    }

    private func row(_ symbol: String, _ text: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: symbol).frame(width: 14)
            Text(text)
        }
    }
}
