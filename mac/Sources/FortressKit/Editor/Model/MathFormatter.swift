import Foundation

/// A light LaTeX reader for showing math blocks without a math engine: Greek letters and
/// operators become their symbols, `^`/`_` become raised and lowered pieces, `\frac`
/// and `\sqrt` become a slash and a radical. Anything it doesn't know is shown as
/// written, so the formula always reads.
public enum MathFormatter {
    public enum Piece: Hashable, Sendable {
        case text(String)
        case sup([Piece])
        case sub([Piece])
    }

    public static func format(_ latex: String) -> [Piece] {
        var parser = Parser(Array(latex.trimmingCharacters(in: .whitespacesAndNewlines)))
        return merge(parser.sequence(until: nil))
    }

    /// The pieces as plain text (raised and lowered parts in ^() and _() when there is
    /// no Unicode form), for accessibility.
    public static func plain(_ pieces: [Piece]) -> String {
        pieces.map { piece in
            switch piece {
            case .text(let text): text
            case .sup(let inner): "^" + group(plain(inner))
            case .sub(let inner): "_" + group(plain(inner))
            }
        }.joined()
    }

    private static func group(_ text: String) -> String { text.count == 1 ? text : "(\(text))" }

    static func merge(_ pieces: [Piece]) -> [Piece] {
        var output: [Piece] = []
        for piece in pieces {
            if case .text(let text) = piece, case .text(let last)? = output.last {
                output[output.count - 1] = .text(last + text)
            } else if case .text(let text) = piece, text.isEmpty {
                continue
            } else {
                output.append(piece)
            }
        }
        return output
    }

    static let symbols: [String: String] = [
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
        "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
        "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
        "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
        "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
        "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
        "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝", "infty": "∞",
        "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "partial": "∂", "nabla": "∇", "in": "∈", "notin": "∉",
        "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "cup": "∪", "cap": "∩", "emptyset": "∅", "forall": "∀",
        "exists": "∃", "neg": "¬", "land": "∧", "lor": "∨", "to": "→", "rightarrow": "→", "leftarrow": "←",
        "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔", "mapsto": "↦",
        "ldots": "…", "cdots": "⋯", "dots": "…", "degree": "°", "circ": "∘", "angle": "∠", "perp": "⊥",
        "parallel": "∥", "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
        "quad": "  ", "qquad": "    ", ",": " ", ";": " ", ":": " ", "!": "", " ": " ", "{": "{", "}": "}",
        "%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "\\": "\n",
        "sin": "sin", "cos": "cos", "tan": "tan", "log": "log", "ln": "ln", "exp": "exp", "lim": "lim",
        "max": "max", "min": "min", "det": "det",
        "left": "", "right": "", "displaystyle": "", "textstyle": "", "limits": "",
    ]

    /// Commands whose one argument is shown as is (`\text{…}`, `\mathbf{…}`, …).
    static let passThrough: Set<String> = [
        "text", "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal", "mathbb", "operatorname",
        "textbf", "textit", "boldsymbol", "vec", "hat", "bar", "overline", "underline", "tilde", "dot",
    ]

    struct Parser {
        let chars: [Character]
        var index = 0

        init(_ chars: [Character]) { self.chars = chars }

        mutating func sequence(until terminator: Character?) -> [Piece] {
            var output: [Piece] = []
            while index < chars.count {
                let char = chars[index]
                if let terminator, char == terminator {
                    index += 1
                    return output
                }
                switch char {
                case "{":
                    index += 1
                    output += sequence(until: "}")
                case "^":
                    index += 1
                    output.append(.sup(MathFormatter.merge(argument())))
                case "_":
                    index += 1
                    output.append(.sub(MathFormatter.merge(argument())))
                case "\\":
                    index += 1
                    output += command()
                case "~":
                    index += 1
                    output.append(.text(" "))
                default:
                    index += 1
                    output.append(.text(String(char)))
                }
            }
            return output
        }

        /// One braced group, one command, or one character.
        mutating func argument() -> [Piece] {
            while index < chars.count, chars[index] == " " { index += 1 }
            guard index < chars.count else { return [] }
            let char = chars[index]
            index += 1
            switch char {
            case "{": return sequence(until: "}")
            case "\\": return command()
            default: return [.text(String(char))]
            }
        }

        mutating func command() -> [Piece] {
            guard index < chars.count else { return [.text("\\")] }
            var name = ""
            if chars[index].isLetter {
                while index < chars.count, chars[index].isLetter {
                    name.append(chars[index])
                    index += 1
                }
            } else {
                name = String(chars[index])
                index += 1
            }
            switch name {
            case "frac", "dfrac", "tfrac":
                let numerator = MathFormatter.merge(argument())
                let denominator = MathFormatter.merge(argument())
                return [.sup(numerator), .text("⁄"), .sub(denominator)]
            case "sqrt":
                var degree: [Piece] = []
                if index < chars.count, chars[index] == "[" {
                    index += 1
                    degree = sequence(until: "]")
                }
                let radicand = MathFormatter.merge(argument())
                let wrapped: [Piece] = radicand.count == 1 && MathFormatter.isShort(radicand[0]) ? radicand : [.text("(")] + radicand + [.text(")")]
                return (degree.isEmpty ? [] : [.sup(degree)]) + [.text("√")] + wrapped
            default:
                if let symbol = MathFormatter.symbols[name] { return [.text(symbol)] }
                if MathFormatter.passThrough.contains(name) { return argument() }
                return [.text("\\" + name)]
            }
        }
    }

    static func isShort(_ piece: Piece) -> Bool {
        if case .text(let text) = piece { return text.count <= 2 }
        return false
    }
}
