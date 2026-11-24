import AppIntents
import SwiftUI
import WidgetKit

/// Control Center, the Lock Screen's bottom buttons, and the Action button:
/// one press reaches an agent. "Talk" opens the app listening to it;
/// "Message" opens a new conversation with the keyboard up. Which agent is
/// chosen when the control is added.
struct TalkControl: ControlWidget {
    static let kind = "ai.olano.app.widgets.talk"

    var body: some ControlWidgetConfiguration {
        AppIntentControlConfiguration(kind: Self.kind, provider: AgentControlProvider()) { value in
            ControlWidgetButton(action: OpenTalkIntent(agent: value.agent)) {
                Label("Talk to \(value.display)", systemImage: "waveform")
            }
        }
        .displayName("Talk to an Agent")
        .description("Opens Olano listening to an agent, hands-free.")
    }
}

struct AskControl: ControlWidget {
    static let kind = "ai.olano.app.widgets.ask"

    var body: some ControlWidgetConfiguration {
        AppIntentControlConfiguration(kind: Self.kind, provider: AgentControlProvider()) { value in
            ControlWidgetButton(action: OpenAskIntent(agent: value.agent)) {
                Label("Message \(value.display)", systemImage: "bubble.left.and.text.bubble.right")
            }
        }
        .displayName("Message an Agent")
        .description("Opens a new conversation with an agent.")
    }
}
