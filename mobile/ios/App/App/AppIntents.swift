import AppIntents
import Foundation
import UIKit

/// Siri, the Shortcuts app and the Action button (the app's own intents; the
/// agent entity and everything they run on are in Shared/, compiled into the
/// widget extension too).
///
/// - **Ask an Agent**: "Hey Siri, ask Aurelius in Olano", then the question
///   when Siri asks for it. Siri takes the words, the deployment runs the
///   turn, and Siri speaks the reply, without the app opening. A turn that
///   outlasts Siri's patience (`SiriTurn.patience`) is let go and finishes on
///   its own: the stream is closed, so nobody watches the run and its reply
///   comes as a push, the way a reply nobody is looking at always does.
/// - **Talk to an Agent**: opens the app in Hands-Free Mode with that agent
///   (the Action button, Control Center, a Shortcuts automation). The request
///   is kept in `TalkRequest` and handed to the screens by OlanoShell
///   (`takeTalk()` at launch and on resume, the `talk` event while running).
///
/// Both intents run in the app's own process, so the saved deployment and the
/// session cookie are at hand, as for the notification buttons
/// (NotificationActions.swift). Every phrase names the app, as Apple
/// requires, and a phrase can carry an entity but never free text: the
/// question is always Siri's follow-up. `OlanoShortcuts` publishes the
/// phrases; OlanoShell `setAgents` refreshes the names Siri listens for.

// MARK: - Ask an Agent

@available(iOS 16.0, *)
struct AskAgentIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask an Agent"
    static var description = IntentDescription("Sends a message to one of your Olano agents and speaks its reply.")

    @Parameter(title: "Agent")
    var agent: AgentEntity

    @Parameter(title: "Message", requestValueDialog: IntentDialog("What do you want to ask?"))
    var message: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask \(\.$agent) \(\.$message)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        guard SharedSession.isSignedIn || ServerStore.serverURL != nil else {
            return .result(value: "", dialog: "Open Olano and connect it to your deployment first.")
        }
        let words = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else {
            return .result(value: "", dialog: "What do you want to ask \(agent.display)?")
        }
        // The app may be in the background (or just launched for this): ask
        // the phone for the time the turn needs, as the notification buttons do.
        let task = await MainActor.run {
            UIApplication.shared.beginBackgroundTask(withName: "olano.siri-ask")
        }
        defer {
            Task { @MainActor in UIApplication.shared.endBackgroundTask(task) }
        }
        let turn = SiriTurn(agent: agent.id, display: agent.display, message: words)
        switch await turn.run(within: SiriTurn.patience) {
        case .replied(let reply):
            let spoken = Speakable.words(reply)
            if spoken.isEmpty {
                return .result(value: reply, dialog: "\(agent.display) answered with nothing to read aloud. Open Olano to see it.")
            }
            return .result(value: reply, dialog: IntentDialog(stringLiteral: spoken))
        case .stillWorking:
            return .result(value: "", dialog: "\(agent.display) is still working on it. The answer will be in Olano, and in a notification when those are on.")
        case .refused(let why):
            return .result(value: "", dialog: IntentDialog(stringLiteral: why))
        }
    }
}

// MARK: - Talk to an Agent

@available(iOS 16.0, *)
struct TalkToAgentIntent: AppIntent {
    static var title: LocalizedStringResource = "Talk to an Agent"
    static var description = IntentDescription("Opens Olano in Hands-Free Mode with one of your agents: it listens, sends what you say, and reads the reply aloud.")
    static var openAppWhenRun = true

    @Parameter(title: "Agent")
    var agent: AgentEntity

    static var parameterSummary: some ParameterSummary {
        Summary("Talk to \(\.$agent)")
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        TalkRequest.post(agent.id)
        return .result()
    }
}

// MARK: - What Siri listens for

@available(iOS 16.4, *)
struct OlanoShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskAgentIntent(),
            // A phrase can carry an entity, never free text: the question is
            // always Siri's follow-up ("What do you want to ask?"). Several
            // spellings, because "ask <name>" alone is also how Siri hears a
            // request to text someone, and the app's name is what settles it.
            phrases: [
                "Ask \(\.$agent) in \(.applicationName)",
                "Ask \(\.$agent) on \(.applicationName)",
                "Ask \(\.$agent) with \(.applicationName)",
                "Ask \(\.$agent) a question in \(.applicationName)",
                "\(.applicationName) ask \(\.$agent)",
                "Ask an agent in \(.applicationName)",
                "Ask an agent on \(.applicationName)",
                "Ask a question in \(.applicationName)",
                "Ask \(.applicationName)",
            ],
            shortTitle: "Ask an Agent",
            systemImageName: "bubble.left.and.text.bubble.right"
        )
        AppShortcut(
            intent: TalkToAgentIntent(),
            phrases: [
                "Talk to \(\.$agent) in \(.applicationName)",
                "Talk to \(\.$agent) on \(.applicationName)",
                "Talk to \(\.$agent) with \(.applicationName)",
                "\(.applicationName) talk to \(\.$agent)",
                "Talk to an agent in \(.applicationName)",
                "Talk to an agent on \(.applicationName)",
                "Talk to \(.applicationName)",
            ],
            shortTitle: "Talk to an Agent",
            systemImageName: "waveform"
        )
    }
}
