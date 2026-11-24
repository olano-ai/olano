import SwiftUI
import WidgetKit

/// Two glances at what the agents do on their own: the newest Pulse card
/// (Cortex), and the next scheduled run with a live countdown.

// MARK: - Pulse

struct PulseWidget: Widget {
    static let kind = "ai.olano.app.widgets.pulse"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: Self.kind, provider: PulseProvider()) { entry in
            PulseView(entry: entry)
        }
        .configurationDisplayName("Pulse")
        .description("The newest finding your agents posted: news, research or an idea worth a look.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular])
    }
}

struct PulseEntry: TimelineEntry {
    let date: Date
    let items: [PulseSnapshot]
    let agents: [AgentSnapshot]
    let signedOut: Bool
}

struct PulseProvider: TimelineProvider {
    func placeholder(in context: Context) -> PulseEntry {
        PulseEntry(date: Date(), items: [PulseSnapshot.sample], agents: AgentSnapshot.sample, signedOut: false)
    }

    func getSnapshot(in context: Context, completion: @escaping (PulseEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(await entry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<PulseEntry>) -> Void) {
        Task {
            completion(Timeline(entries: [await entry()], policy: .after(Date().addingTimeInterval(SnapshotLoader.refreshAfter))))
        }
    }

    private func entry() async -> PulseEntry {
        switch await SnapshotLoader.load() {
        case .signedOut: return PulseEntry(date: Date(), items: [], agents: [], signedOut: true)
        case .snapshot(let snapshot):
            let newest = snapshot.pulse.sorted { ($0.createdAt ?? .distantPast) > ($1.createdAt ?? .distantPast) }
            return PulseEntry(date: Date(), items: newest, agents: snapshot.agents, signedOut: false)
        }
    }
}

struct PulseView: View {
    @Environment(\.widgetFamily) private var family
    let entry: PulseEntry

    private var first: PulseSnapshot? { entry.items.first }

    var body: some View {
        Group {
            if family == .accessoryRectangular {
                VStack(alignment: .leading, spacing: 2) {
                    Label(first == nil ? "Pulse: nothing new" : "Pulse", systemImage: "brain")
                        .font(.headline)
                    if let first {
                        Text(first.title).font(.caption).lineLimit(2)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                card
            }
        }
        .widgetURL(DeepLink.tab("cortex"))
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("Pulse", systemImage: "brain")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Brand.cortex)
                    .widgetAccentable()
                Spacer()
                CountBadge(count: entry.items.count, color: Brand.cortex)
            }
            if entry.signedOut {
                Text("Open the app to sign in.").font(.caption).foregroundStyle(.secondary)
            } else if let first {
                let who = entry.agents.first { $0.name == first.agent }
                Text(first.title)
                    .font(family == .systemSmall ? .subheadline.weight(.semibold) : .headline)
                    .lineLimit(family == .systemSmall ? 3 : 2)
                    .fixedSize(horizontal: false, vertical: true)
                if family != .systemSmall, !first.detail.isEmpty {
                    Text(first.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                HStack(spacing: 6) {
                    if let who {
                        AvatarView(name: who.name, display: who.display, color: Brand.agentColor(who.color, name: who.name), image: who.avatar, size: 16)
                    }
                    Text(first.agentDisplay).font(.caption2.weight(.medium)).foregroundStyle(.secondary).lineLimit(1)
                    if first.priority == "urgent" {
                        Text("Urgent").font(.system(size: 9, weight: .bold)).foregroundStyle(Brand.error)
                    }
                    Spacer()
                    AgoText(date: first.createdAt)
                }
            } else {
                Spacer(minLength: 0)
                Text("Quiet for now").font(.subheadline.weight(.medium))
                Text("Agents post what they find here: news, research, ideas.").font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard(tint: Brand.cortex)
    }
}

// MARK: - Next run

struct NextRunWidget: Widget {
    static let kind = "ai.olano.app.widgets.nextrun"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: Self.kind, provider: NextRunProvider()) { entry in
            NextRunView(entry: entry)
        }
        .configurationDisplayName("Next run")
        .description("The next scheduled task, counting down, and the ones after it.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular, .accessoryInline])
    }
}

struct NextRunEntry: TimelineEntry {
    let date: Date
    let tasks: [TaskSnapshot]
    let signedOut: Bool
}

struct NextRunProvider: TimelineProvider {
    func placeholder(in context: Context) -> NextRunEntry {
        NextRunEntry(date: Date(), tasks: TaskSnapshot.sample, signedOut: false)
    }

    func getSnapshot(in context: Context, completion: @escaping (NextRunEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(await entry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<NextRunEntry>) -> Void) {
        Task {
            let entry = await entry()
            // Redraw right after the next run, so the list moves on by itself.
            let next = entry.tasks.first?.nextRun.map { $0.addingTimeInterval(30) } ?? Date().addingTimeInterval(SnapshotLoader.refreshAfter)
            completion(Timeline(entries: [entry], policy: .after(max(next, Date().addingTimeInterval(60)))))
        }
    }

    private func entry() async -> NextRunEntry {
        switch await SnapshotLoader.load() {
        case .signedOut: return NextRunEntry(date: Date(), tasks: [], signedOut: true)
        case .snapshot(let snapshot): return NextRunEntry(date: Date(), tasks: snapshot.tasks, signedOut: false)
        }
    }
}

struct NextRunView: View {
    @Environment(\.widgetFamily) private var family
    let entry: NextRunEntry

    private var next: TaskSnapshot? { entry.tasks.first }

    var body: some View {
        Group {
            switch family {
            case .accessoryInline:
                if let next, let at = next.nextRun {
                    Text("\(next.name) in \(at, style: .relative)")
                } else {
                    Text("No scheduled runs")
                }
            case .accessoryRectangular:
                VStack(alignment: .leading, spacing: 2) {
                    Label("Next run", systemImage: "clock.arrow.circlepath").font(.headline)
                    if let next, let at = next.nextRun {
                        Text(next.name).font(.caption).lineLimit(1)
                        Text(at, style: .relative).font(.caption2)
                    } else {
                        Text("Nothing scheduled").font(.caption)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            default:
                card
            }
        }
        .widgetURL(DeepLink.tab("scheduled"))
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("Next run", systemImage: "clock.arrow.circlepath")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
            }
            if entry.signedOut {
                Text("Open the app to sign in.").font(.caption).foregroundStyle(.secondary)
            } else if let next, let at = next.nextRun {
                Text(at, style: .relative)
                    .font(.system(size: family == .systemSmall ? 26 : 30, weight: .bold, design: .rounded).monospacedDigit())
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                Text(next.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                Text(next.agentDisplay).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                if family != .systemSmall, entry.tasks.count > 1 {
                    Divider()
                    ForEach(entry.tasks.dropFirst().prefix(2)) { task in
                        HStack {
                            Text(task.name).font(.caption).lineLimit(1)
                            Spacer()
                            if let at = task.nextRun {
                                Text(at, style: .relative).font(.caption2.monospacedDigit()).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            } else {
                Spacer(minLength: 0)
                Text("Nothing scheduled").font(.subheadline.weight(.medium))
                Text("Scheduled tasks appear here.").font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard()
    }
}
