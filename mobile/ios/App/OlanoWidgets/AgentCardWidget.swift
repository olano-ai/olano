import AppIntents
import SwiftUI
import WidgetKit

/// One agent at a glance: who it is, whether it is running, what it last
/// said and whether it is waiting on you. Small and medium on the Home
/// Screen, every Lock Screen shape, and StandBy. A tap opens its latest
/// conversation; Talk opens it hands-free; admins can start and stop it.
struct AgentCardWidget: Widget {
    static let kind = "ai.olano.app.widgets.agent"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: Self.kind, intent: AgentConfigIntent.self, provider: AgentCardProvider()) { entry in
            AgentCardView(entry: entry)
        }
        .configurationDisplayName("Agent")
        .description("An agent's status, its latest reply and what it is waiting on.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular, .accessoryRectangular, .accessoryInline])
    }
}

struct AgentCardEntry: TimelineEntry {
    let date: Date
    let agent: AgentSnapshot?
    let signedOut: Bool
    let isAdmin: Bool
}

struct AgentCardProvider: AppIntentTimelineProvider {
    typealias Entry = AgentCardEntry
    typealias Intent = AgentConfigIntent

    func placeholder(in context: Context) -> AgentCardEntry {
        AgentCardEntry(date: Date(), agent: AgentSnapshot.sample[0], signedOut: false, isAdmin: true)
    }

    func snapshot(for configuration: AgentConfigIntent, in context: Context) async -> AgentCardEntry {
        if context.isPreview {
            return placeholder(in: context)
        }
        return await entry(for: configuration)
    }

    func timeline(for configuration: AgentConfigIntent, in context: Context) async -> Timeline<AgentCardEntry> {
        let entry = await entry(for: configuration)
        return Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(SnapshotLoader.refreshAfter)))
    }

    private func entry(for configuration: AgentConfigIntent) async -> AgentCardEntry {
        switch await SnapshotLoader.load() {
        case .signedOut:
            return AgentCardEntry(date: Date(), agent: nil, signedOut: true, isAdmin: false)
        case .snapshot(let snapshot):
            return AgentCardEntry(date: Date(), agent: snapshot.agent(named: configuration.agent?.id), signedOut: false, isAdmin: snapshot.isAdmin)
        }
    }
}

struct AgentCardView: View {
    @Environment(\.widgetFamily) private var family
    let entry: AgentCardEntry

    var body: some View {
        if let agent = entry.agent {
            let tint = Brand.agentColor(agent.color, name: agent.name)
            switch family {
            case .accessoryCircular:
                circular(agent, tint: tint)
            case .accessoryRectangular:
                rectangular(agent, tint: tint)
            case .accessoryInline:
                Text("\(agent.display): \(Brand.statusWord(agent.state))")
                    .widgetURL(DeepLink.conversation(agent: agent.name, thread: agent.lastThread))
            case .systemSmall:
                small(agent, tint: tint)
            default:
                medium(agent, tint: tint)
            }
        } else {
            empty
        }
    }

    private var empty: some View {
        Group {
            switch family {
            case .accessoryCircular:
                ZStack {
                    AccessoryWidgetBackground()
                    Image(systemName: "person.crop.circle.badge.exclamationmark")
                }
            case .accessoryRectangular, .accessoryInline:
                Text("Olano: open the app to sign in")
            default:
                SignedOutView(line: entry.signedOut ? "Open the app and sign in to your deployment; the widget then shows your agent." : "No agent to show yet. Open the app once.")
                    .olanoCard()
            }
        }
        .widgetURL(DeepLink.tab("chats"))
    }

    // MARK: Home Screen

    private func small(_ agent: AgentSnapshot, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top) {
                AvatarView(name: agent.name, display: agent.display, color: tint, image: agent.avatar, size: 40, state: agent.state)
                Spacer(minLength: 0)
                CountBadge(count: agent.approvals)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(agent.display)
                    .font(.headline)
                    .lineLimit(1)
                StatusLine(state: agent.state)
            }
            if let reply = agent.lastReply, !reply.isEmpty {
                Text(reply)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("No replies yet")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
            AgoText(date: agent.lastReplyAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard(tint: tint)
        .widgetURL(DeepLink.conversation(agent: agent.name, thread: agent.lastThread))
    }

    private func medium(_ agent: AgentSnapshot, tint: Color) -> some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                AvatarView(name: agent.name, display: agent.display, color: tint, image: agent.avatar, size: 48, state: agent.state)
                VStack(alignment: .leading, spacing: 2) {
                    Text(agent.display)
                        .font(.headline)
                        .lineLimit(1)
                    if !agent.title.isEmpty {
                        Text(agent.title)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    StatusLine(state: agent.state)
                }
                Spacer(minLength: 0)
                Link(destination: DeepLink.talk(agent: agent.name)) {
                    PillLabel(title: "Talk", symbol: "waveform", tint: tint, filled: true)
                }
            }
            .frame(width: 104, alignment: .leading)

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(agent.lastThreadTitle ?? "Latest")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    if agent.approvals > 0 {
                        HStack(spacing: 4) {
                            Image(systemName: "hand.raised.fill").font(.caption2)
                            Text(agent.approvals == 1 ? "1 approval" : "\(agent.approvals) approvals")
                        }
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Brand.accent)
                        .widgetAccentable()
                    }
                }
                if let reply = agent.lastReply, !reply.isEmpty {
                    QuoteView(text: reply, tint: tint, lines: 4)
                } else {
                    Text("Nothing said yet. Tap to start a conversation.")
                        .font(.subheadline)
                        .foregroundStyle(.tertiary)
                }
                Spacer(minLength: 0)
                HStack {
                    AgoText(date: agent.lastReplyAt)
                    Spacer()
                    if entry.isAdmin, agent.state != "blocked" {
                        let running = agent.state == "running" || agent.state == "starting"
                        Button(intent: ToggleAgentIntent(agent: agent.name, start: !running)) {
                            PillLabel(title: running ? "Stop" : "Start", symbol: running ? "stop.fill" : "play.fill", tint: running ? Brand.error : Brand.running)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard(tint: tint)
        .widgetURL(DeepLink.conversation(agent: agent.name, thread: agent.lastThread))
    }

    // MARK: Lock Screen

    private func circular(_ agent: AgentSnapshot, tint: Color) -> some View {
        ZStack {
            AccessoryWidgetBackground()
            Circle()
                .strokeBorder(Brand.statusColor(agent.state), lineWidth: 2.5)
                .padding(2)
            VStack(spacing: 0) {
                Text(String(agent.display.prefix(1)).uppercased())
                    .font(.system(size: 22, weight: .bold, design: .rounded))
                if agent.approvals > 0 {
                    Text("\(agent.approvals)")
                        .font(.system(size: 9, weight: .semibold))
                }
            }
        }
        .widgetURL(DeepLink.conversation(agent: agent.name, thread: agent.lastThread))
    }

    private func rectangular(_ agent: AgentSnapshot, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: agent.state == "running" ? "circle.fill" : "circle")
                    .font(.system(size: 7))
                Text(agent.display)
                    .font(.headline)
                    .lineLimit(1)
                if agent.approvals > 0 {
                    Image(systemName: "hand.raised.fill").font(.caption2)
                }
            }
            Text(agent.lastReply ?? Brand.statusWord(agent.state))
                .font(.caption)
                .lineLimit(2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .widgetURL(DeepLink.conversation(agent: agent.name, thread: agent.lastThread))
    }
}
