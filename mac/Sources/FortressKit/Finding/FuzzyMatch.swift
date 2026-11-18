import Foundation
import Synchronization

// MARK: Folded text

/// Text prepared once for fuzzy matching: one code per Swift character (lowercased,
/// accents removed), whether it is a letter or digit, and a 64-bit summary of which
/// codes occur, so most candidates are rejected with a single AND.
///
/// Titles are folded when the palette's index is built (once per sidebar change), never
/// per keystroke.
struct FoldedText: Sendable, Equatable {
    /// One code per character of the original: the folded character's scalar, or an
    /// interned id for folded characters made of several scalars (emoji sequences).
    let units: [UInt32]
    /// Whether each character is a letter or a number (a word character).
    let isWord: [Bool]
    /// Bits for the codes that occur (see `bit(_:)`).
    let mask: UInt64

    init(_ text: String) {
        var units: [UInt32] = []
        var isWord: [Bool] = []
        units.reserveCapacity(text.utf8.count)
        isWord.reserveCapacity(text.utf8.count)
        var mask: UInt64 = 0
        for character in text {
            let folded = Self.fold(character)
            units.append(folded.unit)
            isWord.append(folded.isWord)
            mask |= Self.bit(folded.unit)
        }
        self.units = units
        self.isWord = isWord
        self.mask = mask
    }

    var count: Int { units.count }
    var isEmpty: Bool { units.isEmpty }

    static let space = UInt32(UInt8(ascii: " "))

    /// a–z and 0–9 get a bit each; everything else shares the remaining 28.
    static func bit(_ unit: UInt32) -> UInt64 {
        switch unit {
        case 97...122: 1 << UInt64(unit - 97)
        case 48...57: 1 << UInt64(26 + unit - 48)
        default: 1 << UInt64(36 + unit % 28)
        }
    }

    private struct FoldCache: Sendable {
        var folded: [Character: (unit: UInt32, isWord: Bool)] = [:]
        var interned: [Character: UInt32] = [:]
    }

    private static let cache = Mutex(FoldCache())

    /// Lowercased with accents removed, the same as Foundation's case- and
    /// diacritic-insensitive folding of the character on its own. ASCII takes a fast path;
    /// other characters are folded once and remembered.
    static func fold(_ character: Character) -> (unit: UInt32, isWord: Bool) {
        if let ascii = character.asciiValue {
            switch ascii {
            case 65...90: return (UInt32(ascii) + 32, true)
            case 97...122, 48...57: return (UInt32(ascii), true)
            default: return (UInt32(ascii), false)
            }
        }
        return cache.withLock { state in
            if let known = state.folded[character] { return known }
            if state.folded.count > 20_000 { state.folded = [:] }  // Bounded; ids stay stable.
            let folded = String(character).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil).first ?? character
            let scalars = folded.unicodeScalars
            let unit: UInt32
            if scalars.count == 1, let scalar = scalars.first {
                unit = scalar.value
            } else if let id = state.interned[folded] {
                unit = id
            } else {
                unit = 0x20_0000 + UInt32(state.interned.count)
                state.interned[folded] = unit
            }
            let result = (unit: unit, isWord: folded.isLetter || folded.isNumber)
            state.folded[character] = result
            return result
        }
    }
}

// MARK: Fuzzy matching

/// How well a query matches a name. Tiers come first, so a command whose name *contains*
/// the query always beats a note that only matches scattered letters.
enum FuzzyMatch {
    struct Result: Equatable {
        var score: Int
        /// Character offsets in the candidate that matched (for bolding).
        var indices: [Int]
    }

    /// A query folded once per keystroke.
    struct Query: Sendable {
        let needle: [UInt32]
        /// The query's words, for "trip kyo".
        let words: [[UInt32]]
        /// The query without spaces, for scattered letters.
        let letters: [UInt32]
        /// Bits of every letter: a candidate missing one can't match at all.
        let mask: UInt64

        init(_ text: String) {
            let folded = FoldedText(text.trimmingCharacters(in: .whitespaces))
            needle = folded.units
            words = folded.units.split(separator: FoldedText.space).map(Array.init)
            letters = folded.units.filter { $0 != FoldedText.space }
            mask = letters.reduce(0) { $0 | FoldedText.bit($1) }
        }

        var isEmpty: Bool { needle.isEmpty }
    }

    static func match(_ query: String, in candidate: String) -> Result? {
        match(Query(query), in: FoldedText(candidate))
    }

    /// The match with the offsets to bold.
    static func match(_ query: Query, in hay: FoldedText) -> Result? {
        evaluate(query, hay, collect: true)
    }

    /// Only the score: no allocation, for ranking thousands of titles per keystroke.
    static func score(_ query: Query, in hay: FoldedText) -> Int? {
        evaluate(query, hay, collect: false)?.score
    }

    private static func evaluate(_ query: Query, _ hay: FoldedText, collect: Bool) -> Result? {
        guard !query.isEmpty else { return Result(score: 1, indices: []) }
        guard !hay.isEmpty, query.mask & ~hay.mask == 0 else { return nil }
        return hay.units.withUnsafeBufferPointer { units in
            hay.isWord.withUnsafeBufferPointer { isWord in
                evaluate(query, units, isWord, collect: collect)
            }
        }
    }

    private static func evaluate(
        _ query: Query, _ hay: UnsafeBufferPointer<UInt32>, _ isWord: UnsafeBufferPointer<Bool>, collect: Bool
    ) -> Result? {
        let count = hay.count
        @inline(__always) func atWordStart(_ index: Int) -> Bool { index == 0 || !isWord[index - 1] }

        if let start = query.needle.withUnsafeBufferPointer({ find($0, in: hay, isWord: isWord, wordStartOnly: false) }) {
            let length = query.needle.count
            let base: Int
            if start == 0 && length == count {
                base = 10_000  // Exact.
            } else if start == 0 {
                base = 9_000  // Prefix.
            } else if atWordStart(start) {
                base = 8_000  // Starts a word.
            } else {
                base = 7_000  // Somewhere inside.
            }
            // Earlier and shorter candidates rank higher within a tier.
            return Result(score: base - min(start, 200) * 4 - min(count, 400),
                          indices: collect ? Array(start..<(start + length)) : [])
        }

        // Several words in any order, each found at a word start: "trip kyo".
        if query.words.count > 1 {
            var indices: [Int] = []
            var found = true
            for word in query.words {
                guard let start = word.withUnsafeBufferPointer({ find($0, in: hay, isWord: isWord, wordStartOnly: true) }) else {
                    found = false
                    break
                }
                if collect { indices += start..<(start + word.count) }
            }
            if found {
                return Result(score: 6_000 - min(count, 400), indices: collect ? indices.sorted() : [])
            }
        }

        // Scattered letters in order ("kt" → "Kyoto trip"); fewer gaps and word starts score higher.
        var indices: [Int] = []
        var first = -1
        var last = -1
        var matched = 0
        var wordStarts = 0
        var position = 0
        for letter in query.letters {
            // Prefer the next occurrence at a word start, else the next occurrence.
            var found = -1
            var firstPlain = -1
            var index = position
            while index < count {
                if hay[index] == letter {
                    if firstPlain < 0 { firstPlain = index }
                    if atWordStart(index) { found = index; break }
                }
                index += 1
            }
            let hit = found >= 0 ? found : firstPlain
            guard hit >= 0 else { return nil }
            if first < 0 { first = hit }
            last = hit
            matched += 1
            if atWordStart(hit) { wordStarts += 1 }
            if collect { indices.append(hit) }
            position = hit + 1
        }
        let span = matched == 0 ? 1 : last - first + 1
        let gaps = span - matched
        let score = 3_000 + wordStarts * 150 - gaps * 25 - max(first, 0) * 5 - min(count, 400)
        return Result(score: max(1, min(score, 5_999)), indices: indices)
    }

    private static func find(
        _ needle: UnsafeBufferPointer<UInt32>, in hay: UnsafeBufferPointer<UInt32>, isWord: UnsafeBufferPointer<Bool>,
        wordStartOnly: Bool
    ) -> Int? {
        let length = needle.count
        guard length > 0, length <= hay.count else { return nil }
        let head = needle[0]
        var start = 0
        let lastStart = hay.count - length
        while start <= lastStart {
            if hay[start] == head, !wordStartOnly || start == 0 || !isWord[start - 1] {
                var offset = 1
                while offset < length && hay[start + offset] == needle[offset] { offset += 1 }
                if offset == length { return start }
            }
            start += 1
        }
        return nil
    }
}
