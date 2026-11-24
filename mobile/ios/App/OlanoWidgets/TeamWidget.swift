import SwiftUI
import WidgetKit

/// The whole team: every agent with its status and when it last spoke.
/// Medium shows four as tiles, large lists up to seven. Each one opens its
/// latest conversation.
struct TeamWidget: Widget {
    static let kind = "ai.olano.app.widgets.team"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: Self.kind, provider: TeamProvider()) { entry in
            TeamView(entry: entry)
        }
        .configurationDisplayName("Team")
        .description("Every agent, its status and its latest word.")
        .supportedFamilies([.systemMedium, .systemLarge])
    }
}

struct TeamEntry: TimelineEntry {
    let date: Date
    let agents: [AgentSnapshot]
    let signedOut: Bool
}

struct TeamProvider: TimelineProvider {
    func placeholder(in context: Context) -> TeamEntry {
        TeamEntry(date: Date(), agents: AgentSnapshot.sample, signedOut: false)
    }

    func getSnapshot(in context: Context, completion: @escaping (TeamEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(await entry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<TeamEntry>) -> Void) {
        Task {
            completion(Timeline(entries: [await entry()], policy: .after(Date().addingTimeInterval(SnapshotLoader.refreshAfter))))
        }
    }

    private func entry() async -> TeamEntry {
        switch await SnapshotLoader.load() {
        case .signedOut: return TeamEntry(date: Date(), agents: [], signedOut: true)
        case .snapshot(let snapshot): return TeamEntry(date: Date(), agents: snapshot.agents, signedOut: false)
        }
    }
}

struct TeamView: View {
    @Environment(\.widgetFamily) private var family
    let entry: TeamEntry

    var body: some View {
        if entry.agents.isEmpty {
            SignedOutView(line: entry.signedOut ? "Open the app and sign in; your team then shows here." : "No agents yet.")
                .olanoCard()
                .widgetURL(DeepLink.tab("chats"))
        } else if family == .systemLarge {
            list
        } else {
            tiles
        }
    }

    private var running: Int { entry.agents.filter { $0.state == "running" }.count }

    private var header: some View {
        HStack {
            Label("Team", systemImage: "person.2.fill")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Spacer()
            Text("\(running) of \(entry.agents.count) running")
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.tertiary)
        }
    }

    private var tiles: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            let shown = Array(entry.agents.prefix(4))
            let columns = [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)]
            LazyVGrid(columns: columns, spacing: 8) {
                ForEach(shown) { agent in
                    let tint = Brand.agentColor(agent.color, name: agent.name)
                    Link(destination: DeepLink.conversation(agent: agent.name, thread: agent.lastThread)) {
                        HStack(spacing: 8) {
                            AvatarView(name: agent.name, display: agent.display, color: tint, image: agent.avatar, size: 28, state: agent.state)
                            VStack(alignment: .leading, spacing: 0) {
                                Text(agent.display).font(.caption.weight(.semibold)).lineLimit(1)
                                Text(Brand.statusWord(agent.state)).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                            }
                            Spacer(minLength: 0)
                            CountBadge(count: agent.approvals)
                        }
                        .padding(6)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(.fill.quaternary))
                    }
                }
            }
            if entry.agents.count > 4 {
                Text("and \(entry.agents.count - 4) more")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard()
        .widgetURL(DeepLink.tab("chats"))
    }

    private var list: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            ForEach(Array(entry.agents.prefix(7))) { agent in
                let tint = Brand.agentColor(agent.color, name: agent.name)
                Link(destination: DeepLink.conversation(agent: agent.name, thread: agent.lastThread)) {
                    HStack(spacing: 10) {
                        AvatarView(name: agent.name, display: agent.display, color: tint, image: agent.avatar, size: 34, state: agent.state)
                        VStack(alignment: .leading, spacing: 1) {
                            HStack(spacing: 6) {
                                Text(agent.display).font(.subheadline.weight(.semibold)).lineLimit(1)
                                if !agent.title.isEmpty {
                                    Text(agent.title).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                }
                            }
                            Text(agent.lastReply ?? Brand.statusWord(agent.state))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        Spacer(minLength: 4)
                        VStack(alignment: .trailing, spacing: 2) {
                            CountBadge(count: agent.approvals)
                            AgoText(date: agent.lastReplyAt)
                        }
                    }
                }
                if agent.id != entry.agents.prefix(7).last?.id {
                    Divider()
                }
            }
            if entry.agents.count > 7 {
                Text("and \(entry.agents.count - 7) more in the app")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard()
        .widgetURL(DeepLink.tab("chats"))
    }
}
