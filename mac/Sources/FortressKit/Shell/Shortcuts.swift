import SwiftUI

/// A menu command's keyboard shortcut, shown the Mac way ("⌥⌘I").
public struct AppShortcut: @unchecked Sendable {  // KeyEquivalent and EventModifiers are values
    public let title: String
    public let key: KeyEquivalent
    public let modifiers: EventModifiers

    public init(_ title: String, _ key: KeyEquivalent, _ modifiers: EventModifiers = .command) {
        self.title = title
        self.key = key
        self.modifiers = modifiers
    }

    public var keyboardShortcut: KeyboardShortcut { KeyboardShortcut(key, modifiers: modifiers) }

    /// Modifiers in the Mac order ⌃⌥⇧⌘, then the key.
    public var display: String { Self.display(key: key, modifiers: modifiers) }

    /// A comparable identity ("⌥⌘I") for conflict checks.
    public var chord: String { display }

    public static func display(key: KeyEquivalent, modifiers: EventModifiers) -> String {
        var text = ""
        if modifiers.contains(.control) { text += "⌃" }
        if modifiers.contains(.option) { text += "⌥" }
        if modifiers.contains(.shift) { text += "⇧" }
        if modifiers.contains(.command) { text += "⌘" }
        return text + keyName(key)
    }

    static func keyName(_ key: KeyEquivalent) -> String {
        switch key {
        case .return: "↩"
        case .delete: "⌫"
        case .space: "Space"
        case .upArrow: "↑"
        case .downArrow: "↓"
        case .escape: "esc"
        case .tab: "⇥"
        default: String(key.character).uppercased()
        }
    }
}

/// Every keyboard shortcut the app defines, in one table (DESIGN b.12). The menu bar,
/// the ⌘K palette's hints and the tests read it, so they can't disagree.
///
/// Rule (principle of least surprise): no shortcut here takes a key combination that
/// macOS, the HIG's standard table or Notes/TextEdit/Pages give a different meaning;
/// `ShortcutTests` checks this table against `systemReserved`.
public enum Shortcuts {
    // File
    public static let newNote = AppShortcut("New Note", "n")
    public static let newFolder = AppShortcut("New Folder", "n", [.option, .command])
    public static let importMarkdown = AppShortcut("Import Markdown Files…", "i", [.shift, .command])
    public static let exportMarkdown = AppShortcut("Export as Markdown…", "e", [.shift, .command])
    // Edit
    public static let searchAllNotes = AppShortcut("Search All Notes", "f", [.option, .command])
    // View
    public static let toggleInspector = AppShortcut("Show Inspector", "i", [.option, .command])
    public static let focusMode = AppShortcut("Enter Focus Mode", .return, [.control, .command])
    public static let commandPalette = AppShortcut("Open Quickly…", "o", [.shift, .command])
    public static let askNotes = AppShortcut("Ask Your Notes…", "a", [.shift, .command])
    public static let reloadNotes = AppShortcut("Reload Notes", "r")
    // Note
    public static let moveToTrash = AppShortcut("Move to Trash", .delete)
    // Go
    public static let allNotes = AppShortcut("All Notes", "1")
    public static let rough = AppShortcut("Rough", "2")
    public static let polished = AppShortcut("Polished", "3")
    public static let recent = AppShortcut("Recent", "4")
    // Format (the editor handles the same keys while it has focus)
    public static let bold = AppShortcut("Bold", "b")
    public static let italic = AppShortcut("Italic", "i")
    public static let underline = AppShortcut("Underline", "u")
    public static let strikethrough = AppShortcut("Strikethrough", "x", [.shift, .command])
    public static let inlineCode = AppShortcut("Inline Code", "e", [.option, .command])
    public static let highlight = AppShortcut("Highlight", "h", [.shift, .command])
    public static let superscript = AppShortcut("Superscript", "=", [.control, .command])
    public static let `subscript` = AppShortcut("Subscript", "-", [.control, .command])
    public static let addLink = AppShortcut("Add Link…", "k")
    public static let body = AppShortcut("Body", "0", [.option, .command])
    public static let heading1 = AppShortcut("Heading 1", "1", [.option, .command])
    public static let heading2 = AppShortcut("Heading 2", "2", [.option, .command])
    public static let heading3 = AppShortcut("Heading 3", "3", [.option, .command])
    public static let bulletedList = AppShortcut("Bulleted List", "7", [.shift, .command])
    public static let numberedList = AppShortcut("Numbered List", "8", [.shift, .command])
    public static let checklist = AppShortcut("Checklist", "l", [.shift, .command])
    public static let quote = AppShortcut("Quote", "'", [.option, .command])
    public static let codeBlock = AppShortcut("Code Block", "c", [.control, .option, .command])
    public static let toggleCheckbox = AppShortcut("Mark as Checked", "u", [.shift, .command])
    public static let indent = AppShortcut("Indent", "]")
    public static let outdent = AppShortcut("Outdent", "[")
    public static let moveBlockUp = AppShortcut("Move Up", .upArrow, [.control, .command])
    public static let moveBlockDown = AppShortcut("Move Down", .downArrow, [.control, .command])
    /// Duplicates the selected blocks (the HIG's Duplicate, for blocks; Notion's ⌘D too).
    public static let duplicateBlock = AppShortcut("Duplicate", "d")
    // View
    public static let showBlocks = AppShortcut("Show Blocks", "b", [.option, .command])

    /// The app's own shortcuts (Quick Capture's global one is configurable and separate).
    public static let all: [AppShortcut] = [
        newNote, newFolder, importMarkdown, exportMarkdown, searchAllNotes, toggleInspector, focusMode,
        commandPalette, askNotes, reloadNotes, moveToTrash, allNotes, rough, polished, recent,
        bold, italic, underline, strikethrough, inlineCode, highlight, superscript, `subscript`, addLink,
        body, heading1, heading2, heading3, bulletedList, numberedList, checklist, quote, codeBlock,
        toggleCheckbox, indent, outdent, moveBlockUp, moveBlockDown, duplicateBlock, showBlocks,
    ]

    /// Standard macOS shortcuts (HIG "Standard keyboard shortcuts", the system, and the
    /// text system) with their meaning. An app shortcut may use one only for that same
    /// meaning (`sameMeaning`).
    public static let systemReserved: [String: String] = [
        "⌘A": "Select All", "⌘B": "Bold", "⌘C": "Copy", "⌘D": "Duplicate", "⌘E": "Use Selection for Find",
        "⌘F": "Find", "⌘G": "Find Next", "⇧⌘G": "Find Previous", "⌘H": "Hide", "⌥⌘H": "Hide Others",
        "⌘I": "Italic", "⌘J": "Jump to Selection", "⌘K": "Add Link", "⌘M": "Minimize", "⌥⌘M": "Minimize All",
        "⌘N": "New", "⌘O": "Open", "⌘P": "Print", "⇧⌘P": "Page Setup", "⌘Q": "Quit", "⌘S": "Save",
        "⇧⌘S": "Save As / Duplicate", "⌘T": "Show Fonts", "⌥⌘T": "Show/Hide Toolbar", "⌘U": "Underline",
        "⌘V": "Paste", "⌥⇧⌘V": "Paste and Match Style", "⌥⌘V": "Paste Style", "⌥⌘C": "Copy Style",
        "⇧⌘C": "Show Colors", "⌘W": "Close", "⌥⌘W": "Close All", "⇧⌘W": "Close File", "⌘X": "Cut",
        "⌘Z": "Undo", "⇧⌘Z": "Redo", "⌘,": "Settings", "⌘?": "Help", "⇧⌘/": "Help", "⌘`": "Cycle Windows",
        "⌘.": "Cancel", "⌘Space": "Spotlight", "⌥⌘Space": "Finder Search", "⌃Space": "Input Source",
        "⌃⌥Space": "Input Source", "⌃⌘Space": "Emoji & Symbols", "⌃⌘F": "Full Screen", "⌃⌘S": "Show/Hide Sidebar",
        "⌥⌘I": "Show Inspector", "⌥⌘F": "Search Field", "⌘[": "Shift Left", "⌘]": "Shift Right",
        "⌘;": "Next Misspelling", "⌘:": "Spelling and Grammar", "⇧⌘?": "Help", "⌃⌘D": "Look Up",
        "⇧⌘3": "Screenshot", "⇧⌘4": "Screenshot", "⇧⌘5": "Screenshot", "⌃⇧⌘3": "Screenshot",
        "⌃⇧⌘4": "Screenshot", "⌘=": "Bigger", "⌘+": "Bigger", "⌘-": "Smaller", "⇧⌘=": "Bigger",
        "⌃⌘=": "Superscript", "⌃⌘-": "Subscript", "⌘↑": "Beginning of Document", "⌘↓": "End of Document",
        "⌘⌫": "Delete to Line Start / Move to Trash", "⌘↩": "Done", "⌘Tab": "App Switcher",
        "⇧⌘Q": "Log Out", "⌃⌘Q": "Lock Screen", "⌥⌘D": "Show/Hide Dock", "⌥⌘Esc": "Force Quit",
        // The text system's own keys (NSStandardKeyBindingResponding).
        "⌥⇧↑": "Select to Paragraph Start", "⌥⇧↓": "Select to Paragraph End", "⌥esc": "Complete",
        "⇧↑": "Extend Selection Up", "⇧↓": "Extend Selection Down",
    ]

    /// App shortcuts that do take a standard combination, for its standard meaning.
    public static let sameMeaning: Set<String> = [
        "⌘N", "⌘B", "⌘I", "⌘U", "⌘K", "⌥⌘I", "⌥⌘F", "⌘[", "⌘]", "⌃⌘=", "⌃⌘-", "⌘⌫", "⌘D",
    ]
}
