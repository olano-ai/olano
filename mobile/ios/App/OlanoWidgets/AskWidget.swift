import AppIntents
import SwiftUI
import WidgetKit

/// The simplest widget: a way in. Small is one pill, "Ask Anna", that opens
/// a new conversation with the keyboard up. Medium adds the compose bar's
/// buttons: Message, Photo, Photos and Talk (Hands-Free Mode), each a link
/// into the app. It needs no session to draw (the roster is enough for the
/// name), so it works the moment the app has been opened once; with a session
/// it shows the agent's picture and whether it is running.
///
/// A small widget has one tap target (its `widgetURL`), which is why the
/// buttons are on the medium one only.
struct AskWidget: Widget {
    static let kind = "ai.olano.app.widgets.askbar"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: Self.kind, intent: AgentConfigIntent.self, provider: AskProvider()) { entry in
            AskView(entry: entry)
        }
        .configurationDisplayName("Ask")
        .description("Start a message, a photo or a talk with an agent, in one tap.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular, .accessoryInline])
    }
}

struct AskEntry: TimelineEntry {
    let date: Date
    /// The agent's handle, or empty when the app has not published any yet.
    let agent: String
    let display: String
    /// Its picture and status, when a session has drawn them.
    let snapshot: AgentSnapshot?

    var hasAgent: Bool { !agent.isEmpty }
}

struct AskProvider: AppIntentTimelineProvider {
    typealias Entry = AskEntry
    typealias Intent = AgentConfigIntent

    func placeholder(in context: Context) -> AskEntry {
        AskEntry(date: Date(), agent: "anna", display: "Anna", snapshot: AgentSnapshot.sample[0])
    }

    func snapshot(for configuration: AgentConfigIntent, in context: Context) async -> AskEntry {
        if context.isPreview {
            return placeholder(in: context)
        }
        return await entry(for: configuration)
    }

    func timeline(for configuration: AgentConfigIntent, in context: Context) async -> Timeline<AskEntry> {
        let entry = await entry(for: configuration)
        return Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(SnapshotLoader.refreshAfter)))
    }

    private func entry(for configuration: AgentConfigIntent) async -> AskEntry {
        // The roster names the agent without a session; the snapshot, when
        // there is one, adds its picture and status.
        let name = DefaultAgent.resolve(configuration.agent?.id) ?? ""
        if case .snapshot(let loaded) = await SnapshotLoader.load(),
           let found = loaded.agent(named: name.isEmpty ? nil : name),
           name.isEmpty || found.name == name {
            return AskEntry(date: Date(), agent: found.name, display: found.display, snapshot: found)
        }
        let display = configuration.agent?.display ?? AgentRoster.display(of: name)
        return AskEntry(date: Date(), agent: name, display: display.isEmpty ? name : display, snapshot: nil)
    }
}

struct AskView: View {
    @Environment(\.widgetFamily) private var family
    let entry: AskEntry

    private var tint: Color {
        guard entry.hasAgent else { return Brand.accent }
        return Brand.agentColor(entry.snapshot?.color, name: entry.agent)
    }

    var body: some View {
        switch family {
        case .accessoryCircular:
            circular
        case .accessoryInline:
            Text(entry.hasAgent ? "Ask \(entry.display)" : "Open Olano")
                .widgetURL(openURL(mode: "text"))
        case .systemSmall:
            small
        default:
            medium
        }
    }

    /// Where a tap goes: the composer with the agent, or the app when there is no agent yet.
    private func openURL(mode: String) -> URL {
        entry.hasAgent ? DeepLink.compose(agent: entry.agent, mode: mode) : DeepLink.tab("chats")
    }

    private var who: some View {
        HStack(spacing: 8) {
            if let snapshot = entry.snapshot {
                AvatarView(name: snapshot.name, display: snapshot.display, color: tint, image: snapshot.avatar, size: 30, state: snapshot.state)
            } else if entry.hasAgent {
                AvatarView(name: entry.agent, display: entry.display, color: tint, image: nil, size: 30)
            } else {
                OlanoMark(size: 30)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(entry.hasAgent ? entry.display : "Olano")
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                if let snapshot = entry.snapshot {
                    StatusLine(state: snapshot.state)
                } else {
                    Text(entry.hasAgent ? "Agent" : "Open the app once")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
    }

    /// The pill: "Ask Anna" with the send mark, like the app's own composer.
    private func pill(hint: Bool) -> some View {
        HStack(spacing: 8) {
            Text(entry.hasAgent ? (hint ? "Ask \(entry.display)…" : "Ask \(entry.display)") : "Open Olano")
                .font(.subheadline.weight(hint ? .regular : .medium))
                .foregroundStyle(hint ? Color.secondary : Color.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 4)
            Image(systemName: "arrow.up.circle.fill")
                .font(.system(size: 22))
                .foregroundStyle(tint)
                .widgetAccentable()
        }
        .padding(.leading, 14)
        .padding(.trailing, 6)
        .padding(.vertical, 6)
        .background(Capsule().fill(.fill.tertiary))
    }

    // MARK: Home Screen

    private var small: some View {
        VStack(alignment: .leading, spacing: 0) {
            who
            Spacer(minLength: 0)
            pill(hint: false)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard(tint: tint)
        .widgetURL(openURL(mode: "text"))
    }

    private var medium: some View {
        VStack(alignment: .leading, spacing: 10) {
            who
            Link(destination: openURL(mode: "text")) {
                pill(hint: true)
            }
            HStack(spacing: 4) {
                Link(destination: openURL(mode: "text")) {
                    RoundAction(title: "Message", symbol: "keyboard", tint: tint)
                }
                Link(destination: openURL(mode: "camera")) {
                    RoundAction(title: "Photo", symbol: "camera.fill", tint: tint)
                }
                Link(destination: openURL(mode: "photos")) {
                    RoundAction(title: "Photos", symbol: "photo.on.rectangle", tint: tint)
                }
                Link(destination: entry.hasAgent ? DeepLink.talk(agent: entry.agent) : DeepLink.tab("chats")) {
                    RoundAction(title: "Talk", symbol: "waveform", tint: tint, filled: true)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard(tint: tint)
        .widgetURL(openURL(mode: "text"))
    }

    // MARK: Lock Screen

    private var circular: some View {
        ZStack {
            AccessoryWidgetBackground()
            if entry.hasAgent {
                Text(String(entry.display.prefix(1)).uppercased())
                    .font(.system(size: 22, weight: .bold, design: .rounded))
            } else {
                Image(systemName: "sparkle")
                    .font(.system(size: 20, weight: .bold))
            }
        }
        .widgetURL(openURL(mode: "text"))
    }
}
