import AppIntents
import Foundation
import WidgetKit

/// What a widget's configuration asks for, and what its buttons do. The
/// buttons' intents run in the extension's own process with the shared
/// session (OlanoShared.swift); none of them opens the app.

// MARK: - Configuration

/// "Which agent?" for the Agent card and the Control Center controls.
struct AgentConfigIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Agent"
    static var description = IntentDescription("Which agent the widget shows.")

    @Parameter(title: "Agent")
    var agent: AgentEntity?

    init() {}

    init(agent: AgentEntity?) {
        self.agent = agent
    }
}

/// The Quick asks widget: an agent and the questions its buttons send.
struct QuickAsksConfigIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Quick asks"
    static var description = IntentDescription("An agent and up to three questions sent with one tap.")

    @Parameter(title: "Agent")
    var agent: AgentEntity?

    @Parameter(title: "First question", default: "Give me a short briefing of what matters right now.")
    var first: String

    @Parameter(title: "Second question", default: "What is waiting on me?")
    var second: String

    @Parameter(title: "Third question", default: "Summarize what you did today.")
    var third: String

    init() {}

    /// The questions as typed, empty ones left out, each with a short label for its button.
    var questions: [(label: String, text: String)] {
        [first, second, third]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .map { (label: QuickAsksConfigIntent.label(for: $0), text: $0) }
    }

    /// A question's first words, enough to tell the buttons apart.
    static func label(for question: String) -> String {
        let words = question.split(separator: " ").prefix(4).joined(separator: " ")
        let trimmed = words.trimmingCharacters(in: CharacterSet(charactersIn: ".,;:!?"))
        return trimmed.count < question.count ? trimmed + "…" : trimmed
    }
}

// MARK: - Buttons

/// A quick ask: sends the question and keeps the reply for the widget.
struct QuickAskIntent: AppIntent {
    static var title: LocalizedStringResource = "Quick ask"
    static var description = IntentDescription("Sends one of the widget's questions to the agent.")
    static var isDiscoverable = false

    @Parameter(title: "Agent")
    var agent: String

    @Parameter(title: "Question")
    var question: String

    init() {}

    init(agent: String, question: String) {
        self.agent = agent
        self.question = question
    }

    func perform() async throws -> some IntentResult {
        let display = AgentRoster.display(of: agent)
        var record = QuickAskRecord(agent: agent, question: question, reply: nil, pending: true, at: Date())
        record.save()
        WidgetCenter.shared.reloadTimelines(ofKind: QuickAsksWidget.kind)
        let turn = SiriTurn(agent: agent, display: display, message: question)
        switch await turn.run(within: SiriTurn.patience) {
        case .replied(let reply):
            record.reply = Speakable.snippet(reply, maxChars: 600)
            record.pending = false
        case .stillWorking:
            record.reply = nil
            record.pending = true
        case .refused(let why):
            record.reply = why
            record.pending = false
        }
        record.at = Date()
        record.save()
        return .result()
    }
}

/// Start or stop an agent (admins).
struct ToggleAgentIntent: AppIntent {
    static var title: LocalizedStringResource = "Start or stop an agent"
    static var isDiscoverable = false

    @Parameter(title: "Agent")
    var agent: String

    @Parameter(title: "Start")
    var start: Bool

    init() {}

    init(agent: String, start: Bool) {
        self.agent = agent
        self.start = start
    }

    func perform() async throws -> some IntentResult {
        if start {
            _ = await Deployment.start(agent: agent)
        } else {
            _ = await Deployment.stop(agent: agent)
        }
        // A boot takes a moment; the snapshot after it says "starting" and the
        // timeline's next redraw catches "running".
        if let fresh = await SnapshotLoader.fetch() { fresh.save() }
        return .result()
    }
}

/// Approve or decline an Approvals inbox item.
struct ResolveApprovalIntent: AppIntent {
    static var title: LocalizedStringResource = "Answer an approval"
    static var isDiscoverable = false

    @Parameter(title: "Approval")
    var id: String

    @Parameter(title: "Approve")
    var approve: Bool

    init() {}

    init(id: String, approve: Bool) {
        self.id = id
        self.approve = approve
    }

    func perform() async throws -> some IntentResult {
        _ = await Deployment.resolve(approval: id, approved: approve)
        if let fresh = await SnapshotLoader.fetch() { fresh.save() }
        return .result()
    }
}

// MARK: - Controls (Control Center, the Lock Screen, the Action button)

/// Opens the app in Hands-Free Mode with the agent (src/native/siri.ts reads
/// the link). A control's action cannot speak to the page directly, so the
/// link is the one route that works from whichever process runs it.
struct OpenTalkIntent: AppIntent {
    static var title: LocalizedStringResource = "Talk to an Agent"
    static var description = IntentDescription("Opens Olano listening to an agent, hands-free.")
    static var isDiscoverable = false

    @Parameter(title: "Agent")
    var agent: String

    init() {}

    init(agent: String) {
        self.agent = agent
    }

    func perform() async throws -> some IntentResult & OpensIntent {
        .result(opensIntent: OpenURLIntent(DeepLink.talk(agent: agent)))
    }
}

/// Opens the app on a new conversation with the agent.
struct OpenAskIntent: AppIntent {
    static var title: LocalizedStringResource = "Message an Agent"
    static var description = IntentDescription("Opens a new conversation with an agent in Olano.")
    static var isDiscoverable = false

    @Parameter(title: "Agent")
    var agent: String

    init() {}

    init(agent: String) {
        self.agent = agent
    }

    func perform() async throws -> some IntentResult & OpensIntent {
        var parts = URLComponents()
        parts.scheme = "olano"
        parts.host = "open"
        parts.queryItems = [URLQueryItem(name: "agent", value: agent), URLQueryItem(name: "thread", value: "new")]
        return .result(opensIntent: OpenURLIntent(parts.url ?? DeepLink.conversation(agent: agent, thread: nil)))
    }
}

/// "Which agent?" for a control.
struct AgentControlConfigIntent: ControlConfigurationIntent {
    static var title: LocalizedStringResource = "Agent"
    static var description = IntentDescription("Which agent the control reaches.")

    @Parameter(title: "Agent")
    var agent: AgentEntity?

    init() {}
}

struct AgentControlValue {
    let agent: String
    let display: String
}

struct AgentControlProvider: AppIntentControlValueProvider {
    func previewValue(configuration: AgentControlConfigIntent) -> AgentControlValue {
        resolve(configuration)
    }

    func currentValue(configuration: AgentControlConfigIntent) async throws -> AgentControlValue {
        resolve(configuration)
    }

    private func resolve(_ configuration: AgentControlConfigIntent) -> AgentControlValue {
        if let agent = configuration.agent {
            return AgentControlValue(agent: agent.id, display: agent.display)
        }
        if let chosen = DefaultAgent.entry {
            return AgentControlValue(agent: chosen.name, display: chosen.display)
        }
        return AgentControlValue(agent: "", display: "an agent")
    }
}
