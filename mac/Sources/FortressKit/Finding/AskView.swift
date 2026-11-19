import SwiftUI

/// "Ask your notes": a question answered from the notes, streamed sentence by sentence,
/// every sentence with chips citing the passages it came from. Follow-ups stack below.
///
/// Opened without a question (the toolbar's Ask button, ⇧⌘A) it shows a short intro and
/// waits in its question field; if the backend has no model key it says how to add one.
struct AskView: View {
    @Environment(AppModel.self) private var model
    @State private var turns: [AskTurn]
    @State private var followUp = ""
    /// Whether the backend has a model key (`ask_configured` in /api/health); nil until known.
    @State private var askConfigured: Bool?
    @FocusState private var isComposerFocused: Bool
    private let question: String
    /// Preset turns are shown as they are (previews and snapshots); otherwise the
    /// question is asked when the view appears.
    private let startsAsking: Bool

    init(question: String) {
        self.question = question
        self.startsAsking = true
        let question = question.trimmingCharacters(in: .whitespacesAndNewlines)
        _turns = State(initialValue: question.isEmpty ? [] : [AskTurn(question: question)])
    }

    init(question: String, turns: [AskTurn], askConfigured: Bool? = nil) {
        self.question = question
        self.startsAsking = false
        _turns = State(initialValue: turns)
        _askConfigured = State(initialValue: askConfigured)
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 44) {
                    if turns.isEmpty {
                        AskIntro(askConfigured: askConfigured)
                    }
                    ForEach(turns) { turn in
                        AskTurnView(turn: turn, retry: { retry(turn) })
                            .id(turn.id)
                    }
                }
                .frame(maxWidth: 640, alignment: .leading)
                .padding(.horizontal, 48)
                .padding(.top, 36)
                .padding(.bottom, 120)
                .frame(maxWidth: .infinity)
            }
            .scrollEdgeEffectStyle(.soft, for: .top)
            .onChange(of: turns.count) { _, _ in
                if let last = turns.last { withAnimation(.smooth(duration: 0.25)) { proxy.scrollTo(last.id, anchor: .top) } }
            }
        }
        .themedContent()
        .safeAreaInset(edge: .bottom, spacing: 0) { composer }
        .navigationTitle("Ask")
        .environment(\.openURL, OpenURLAction { url in openCitation(url) })
        .task {
            guard startsAsking else { return }
            if let first = turns.first {
                if first.state.isStreaming { first.start(model.api.ask(first.question)) }
            } else if askConfigured == nil {
                askConfigured = try? await model.api.health()["ask_configured"]?.boolValue
            }
        }
        // Opened empty, the caret waits in the question field; an answer asked from the
        // search field leaves the caret where it was.
        .onAppear { if turns.isEmpty { takeDraft() } }
        .onChange(of: model.askFocusRequest) { _, _ in takeDraft() }
        .onDisappear { turns.forEach { $0.stop() } }
    }

    /// Puts the caret in the question field, with the text Ask was opened with (if any).
    private func takeDraft() {
        if !model.askDraft.isEmpty {
            followUp = model.askDraft
            model.askDraft = ""
        }
        if startsAsking { isComposerFocused = true }
    }

    // MARK: Follow-up composer (glass, floating above the bottom edge)

    private var composer: some View {
        let isStreaming = turns.last?.state.isStreaming ?? false
        let canSend = !followUp.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        return GlassEffectContainer {
            HStack(alignment: .bottom, spacing: 8) {
                TextField(turns.isEmpty ? "Ask a question about your notes" : "Ask a follow-up",
                          text: $followUp, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 14))
                    .lineLimit(1...5)
                    .focused($isComposerFocused)
                    .onSubmit(send)
                    .padding(.vertical, 9)
                    .padding(.leading, 16)
                if isStreaming {
                    Button { turns.last?.stop() } label: {
                        Image(systemName: "stop.fill").font(.system(size: 11, weight: .bold))
                            .frame(width: 26, height: 26)
                    }
                    .buttonStyle(.glass)
                    .buttonBorderShape(.circle)
                    .keyboardShortcut(.cancelAction)
                    .help("Stop")
                    .accessibilityLabel("Stop Answering")
                    .padding(5)
                } else {
                    Button(action: send) {
                        Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold))
                            .frame(width: 26, height: 26)
                    }
                    .buttonStyle(.glassProminent)
                    .buttonBorderShape(.circle)
                    .disabled(!canSend)
                    .help("Ask")
                    .accessibilityLabel("Ask")
                    .padding(5)
                }
            }
            .frame(minHeight: 40)
            .floatingGlass(in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .frame(maxWidth: 560)
        .padding(.horizontal, 32)
        .padding(.bottom, 16)
    }

    private func send() {
        let text = followUp.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        // Asking something else stops the answer still streaming.
        turns.last?.stop()
        followUp = ""
        let turn = AskTurn(question: text)
        turns.append(turn)
        turn.start(model.api.ask(text))
    }

    private func retry(_ turn: AskTurn) {
        guard let index = turns.firstIndex(where: { $0.id == turn.id }) else { return }
        turn.stop()
        let fresh = AskTurn(question: turn.question)
        turns[index] = fresh
        fresh.start(model.api.ask(fresh.question))
    }

    /// Citation chips are links: fortress-cite://<document id>/<block id>.
    private func openCitation(_ url: URL) -> OpenURLAction.Result {
        guard let target = AskTurnView.citation(from: url) else { return .systemAction }
        model.open(noteId: target.documentId, blockId: target.blockId)
        return .handled
    }
}

/// One question with its streamed answer, sources and trace.
struct AskTurnView: View {
    nonisolated static let citationScheme = "fortress-cite"

    /// "fortress-cite:<document id>/<block id>" → the ids (ULIDs keep their case).
    nonisolated static func citation(from url: URL) -> (documentId: String, blockId: String?)? {
        let string = url.absoluteString
        let prefix = citationScheme + ":"
        guard string.hasPrefix(prefix) else { return nil }
        let parts = string.dropFirst(prefix.count).split(separator: "/", maxSplits: 1).map(String.init)
        guard let documentId = parts.first, !documentId.isEmpty else { return nil }
        return (documentId, parts.count > 1 ? parts[1] : nil)
    }

    let turn: AskTurn
    var retry: () -> Void = {}
    @Environment(AppModel.self) private var model

    var body: some View {
        let state = turn.state
        VStack(alignment: .leading, spacing: 14) {
            Text(turn.question)
                .font(.system(size: 22, weight: .semibold))
                .lineSpacing(3)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)

            AskTrace(turn: turn)

            if !state.sentences.isEmpty {
                answer(state)
                    .transition(.opacity)
            }
            outcome(state)

            if !state.citations.isEmpty, !state.isStreaming {
                sources(state)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: state.sentences.count)
        .animation(.smooth(duration: 0.25), value: state.phase)
    }

    /// The answer in paragraphs; each sentence is followed by its chips. Paragraphs that
    /// are done never change, so while an answer streams only the last one is laid out again.
    private func answer(_ state: AskState) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(state.paragraphs) { paragraph in
                AnswerParagraph(sentences: paragraph.sentences.map { index in
                    let sentence = state.sentences[index]
                    var seen = Set<Int>()
                    let chips = sentence.citations.compactMap { citation -> AnswerParagraph.Chip? in
                        guard let number = state.number(of: citation), seen.insert(number).inserted else { return nil }
                        return AnswerParagraph.Chip(number: number, citation: citation)
                    }
                    return AnswerParagraph.Sentence(text: sentence.text, chips: chips)
                })
                .equatable()
            }
        }
    }

    @ViewBuilder private func outcome(_ state: AskState) -> some View {
        switch state.phase {
        case .streaming, .finished(.answered):
            EmptyView()
        case .stopped:
            if state.sentences.isEmpty {
                calm("Stopped.", detail: nil, symbol: "stop.circle", action: ("Ask Again", retry))
            }
        case .finished(.notFound):
            calm("I couldn’t find anything in your notes about that.", detail: nil,
                 symbol: "text.magnifyingglass", action: ("Search Instead", { model.query = turn.question }))
        case .finished(.notConfigured):
            calm("Answers need an OpenRouter API key.",
                 detail: AskIntro.setupDetail + (state.sources.isEmpty ? "" : " Meanwhile, these passages look closest:"),
                 symbol: "key", action: nil)
            closestPassages(state.sources)
        case .finished(.timeout):
            calm("That took too long to answer.",
                 detail: state.sources.isEmpty ? nil : "These passages look closest:",
                 symbol: "clock", action: ("Try Again", retry))
            closestPassages(state.sources)
        case .finished(.unavailable):
            calm("Ask isn’t available right now.", detail: "The model didn’t respond.",
                 symbol: "bolt.horizontal.circle", action: ("Try Again", retry))
            closestPassages(state.sources)
        case .failed(let message):
            if state.sentences.isEmpty {
                calm("Ask isn’t available.", detail: message, symbol: "bolt.horizontal.circle", action: ("Try Again", retry))
            } else {
                // What arrived stays readable above; say it's incomplete.
                calm("The answer was cut off.", detail: message, symbol: "exclamationmark.bubble", action: ("Try Again", retry))
            }
        }
    }

    private func calm(_ title: String, detail: String?, symbol: String, action: (String, () -> Void)?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(.themeSecondary)
                .font(.system(size: 14))
            VStack(alignment: .leading, spacing: 6) {
                Text(title)
                    .font(.system(size: 15))
                    .foregroundStyle(.primary)
                if let detail {
                    Text(detail).font(.callout).foregroundStyle(.themeSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let action {
                    Button(action.0, action: action.1)
                        .controlSize(.small)
                        .padding(.top, 2)
                }
            }
        }
    }

    @ViewBuilder private func closestPassages(_ sources: [AskSource]) -> some View {
        if !sources.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(sources.prefix(5))) { source in
                    PassageCard(title: source.documentTitle, text: source.text, number: nil) {
                        model.open(noteId: source.documentId, blockId: source.blockId)
                    }
                }
            }
        }
    }

    private func sources(_ state: AskState) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Sources")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.themeSecondary)
            ForEach(Array(state.citations.enumerated()), id: \.element.blockId) { index, citation in
                PassageCard(title: citation.documentTitle, text: citation.quote, number: index + 1) {
                    model.open(noteId: citation.documentId, blockId: citation.blockId)
                }
            }
        }
        .padding(.top, 10)
    }
}

/// What Ask shows before the first question: what it does, how to ask, and (when the
/// backend has no model key) how to set one up.
struct AskIntro: View {
    let askConfigured: Bool?

    static let setupDetail = "Add the line OPENROUTER_API_KEY=your-key to backend/.env (that file stays on this Mac and is never committed), then quit and reopen Fortress Notes. Search works without a key."

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Ask Your Notes")
                .font(.system(size: 22, weight: .semibold))
            Text("Ask a question in your own words. The answer is written only from your notes, and every sentence links to the passage it came from.")
                .font(.system(size: 15))
                .foregroundStyle(.themeSecondary)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 6) {
                tip("Type below and press Return, or type a question in the search field: one that ends in “?” or starts with who, what, when, how… is asked when you press Return.")
                tip("From any search, ⌘↩ asks instead of searching. \(Shortcuts.askNotes.display) comes back here.")
            }
            .padding(.top, 4)
            if askConfigured == false {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Image(systemName: "key")
                        .foregroundStyle(.themeSecondary)
                        .font(.system(size: 14))
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Ask needs an OpenRouter API key")
                            .font(.system(size: 15, weight: .medium))
                        Text(Self.setupDetail)
                            .font(.callout)
                            .foregroundStyle(.themeSecondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(ThemeStyle(.surface), in: .rect(cornerRadius: 10, style: .continuous))
                .padding(.top, 8)
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func tip(_ text: String) -> some View {
        Label {
            Text(text).fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: "sparkle.magnifyingglass").foregroundStyle(.tint)
        }
        .font(.callout)
        .foregroundStyle(.themeSecondary)
    }
}

/// One paragraph of an answer: sentences with their citation chips, as one `Text`.
/// Equatable, so SwiftUI skips paragraphs that didn't change while the answer streams.
struct AnswerParagraph: View, Equatable {
    struct Chip: Equatable {
        var number: Int
        var citation: AskCitation
    }

    struct Sentence: Equatable {
        var text: String
        var chips: [Chip]
    }

    let sentences: [Sentence]

    var body: some View {
        var parts: [Text] = []
        parts.reserveCapacity(sentences.count * 3)
        for (index, sentence) in sentences.enumerated() {
            parts.append(Text(verbatim: (index == 0 ? "" : " ") + sentence.text))
            for chip in sentence.chips {
                parts.append(Text(verbatim: "\u{2009}"))
                parts.append(Self.chip(chip))
            }
        }
        return Text.joined(parts)
            .font(.system(size: 16))
            .lineSpacing(9)
            .foregroundStyle(.primary)
            .textRenderer(MarkRenderer())
            .fixedSize(horizontal: false, vertical: true)
    }

    private static func chip(_ chip: Chip) -> Text {
        var link = AttributedString("\u{2002}\(chip.number)\u{2002}")
        link.link = URL(string: "\(AskTurnView.citationScheme):\(chip.citation.documentId)/\(chip.citation.blockId)")
        link.swiftUI.font = .system(size: 11, weight: .semibold).monospacedDigit()
        link.swiftUI.baselineOffset = 2
        return Text(link)
            .customAttribute(TextMark(style: .citation))
    }
}

/// "Searched for “ryokan|hotel” · Read “Kyoto trip” · 0.8 s", with a pulse while working.
struct AskTrace: View {
    let turn: AskTurn

    var body: some View {
        let state = turn.state
        if state.isStreaming || !state.steps.isEmpty || (state.elapsedMs ?? 0) >= 100 {
            content(state)
        }
    }

    private func content(_ state: AskState) -> some View {
        HStack(spacing: 7) {
            if state.isStreaming {
                PulsingDot()
            } else {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 10, weight: .semibold))  // HIG minimum
            }
            TimelineView(.periodic(from: turn.started, by: 0.1)) { context in
                Text(verbatim: line(state, now: context.date))
                    .monospacedDigit()
            }
            .lineLimit(1)
            .truncationMode(.middle)
        }
        .font(.subheadline)
        .foregroundStyle(.themeTertiary)
    }

    private func line(_ state: AskState, now: Date) -> String {
        var parts: [String] = []
        let trace = state.trace
        if !trace.isEmpty { parts.append(trace) }
        if state.isStreaming {
            if trace.isEmpty { parts.append(state.sentences.isEmpty ? "Reading your notes" : "Writing") }
            parts.append(Self.seconds(now.timeIntervalSince(turn.started)))
        } else {
            if trace.isEmpty { parts.append("Answered") }
            if let ms = state.elapsedMs { parts.append(Self.seconds(Double(ms) / 1000)) }
        }
        return parts.joined(separator: " · ")
    }

    static func seconds(_ interval: TimeInterval) -> String {
        // "12.3s" in the locale's digits and unit (not a fixed "%.1f s").
        Duration.milliseconds(Int64((max(0, interval) * 1000).rounded()))
            .formatted(.units(allowed: [.seconds], width: .narrow, fractionalPart: .show(length: 1)))
    }
}

/// A 6pt accent dot that breathes while an answer streams.
struct PulsingDot: View {
    @State private var dim = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Circle()
            .fill(.tint)
            .frame(width: 6, height: 6)
            .opacity(dim ? 0.35 : 1)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true)) { dim = true }
            }
    }
}

/// A quoted passage from a note: optional number, note title, two lines of text. Click opens it.
struct PassageCard: View {
    let title: String
    let text: String
    let number: Int?
    let action: () -> Void
    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                if let number {
                    Text(verbatim: "\(number)")
                        .font(.system(size: 11, weight: .semibold).monospacedDigit())
                        .foregroundStyle(.tint)
                        .frame(width: 18, height: 18)
                        .background(.tint.opacity(0.15), in: .circle)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(title.isEmpty ? "Untitled" : title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.themeSecondary)
                        .lineLimit(1)
                    Text(text)
                        .font(.callout)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                ThemeStyle(isHovered ? .border : .surface),
                in: .rect(cornerRadius: 8, style: .continuous))
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
    }
}
