import AppIntents
import SwiftUI
import WidgetKit

/// What is waiting on you: the oldest approval with Approve and Decline on
/// it, and how many more there are. An approval expires on its own after a
/// few minutes, which the widget counts down. A tap opens the Inbox.
struct ApprovalsWidget: Widget {
    static let kind = "ai.olano.app.widgets.approvals"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: Self.kind, provider: ApprovalsProvider()) { entry in
            ApprovalsView(entry: entry)
        }
        .configurationDisplayName("Approvals")
        .description("Approve or decline what an agent is waiting on, from the Home Screen.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular, .accessoryRectangular])
    }
}

struct ApprovalsEntry: TimelineEntry {
    let date: Date
    let approvals: [ApprovalSnapshot]
    let pulseApprovals: Int
    let agents: [AgentSnapshot]
    let signedOut: Bool

    var total: Int { approvals.count + pulseApprovals }
}

struct ApprovalsProvider: TimelineProvider {
    func placeholder(in context: Context) -> ApprovalsEntry {
        ApprovalsEntry(date: Date(), approvals: [ApprovalSnapshot.sample], pulseApprovals: 1, agents: AgentSnapshot.sample, signedOut: false)
    }

    func getSnapshot(in context: Context, completion: @escaping (ApprovalsEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(await entry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ApprovalsEntry>) -> Void) {
        Task {
            let entry = await entry()
            // An approval that expires soon is shown gone when it is.
            let soonest = entry.approvals.compactMap(\.expiresAt).min()
            let refresh = min(soonest ?? .distantFuture, Date().addingTimeInterval(entry.total > 0 ? 5 * 60 : SnapshotLoader.refreshAfter))
            completion(Timeline(entries: [entry], policy: .after(max(refresh, Date().addingTimeInterval(60)))))
        }
    }

    private func entry() async -> ApprovalsEntry {
        switch await SnapshotLoader.load() {
        case .signedOut:
            return ApprovalsEntry(date: Date(), approvals: [], pulseApprovals: 0, agents: [], signedOut: true)
        case .snapshot(let snapshot):
            let live = snapshot.approvals.filter { ($0.expiresAt ?? .distantFuture) > Date() }
            return ApprovalsEntry(date: Date(), approvals: live, pulseApprovals: snapshot.pulseApprovals, agents: snapshot.agents, signedOut: false)
        }
    }
}

struct ApprovalsView: View {
    @Environment(\.widgetFamily) private var family
    let entry: ApprovalsEntry

    private var oldest: ApprovalSnapshot? {
        entry.approvals.sorted { ($0.createdAt ?? .distantPast) < ($1.createdAt ?? .distantPast) }.first
    }

    private func agent(_ name: String) -> AgentSnapshot? {
        entry.agents.first { $0.name == name }
    }

    var body: some View {
        Group {
            switch family {
            case .accessoryCircular:
                ZStack {
                    AccessoryWidgetBackground()
                    VStack(spacing: 0) {
                        Image(systemName: "hand.raised.fill").font(.system(size: 14))
                        Text("\(entry.total)").font(.system(size: 16, weight: .bold, design: .rounded))
                    }
                }
            case .accessoryRectangular:
                VStack(alignment: .leading, spacing: 2) {
                    Label(entry.total == 0 ? "Nothing waiting" : (entry.total == 1 ? "1 approval" : "\(entry.total) approvals"), systemImage: "hand.raised.fill")
                        .font(.headline)
                    if let first = oldest {
                        Text("\(first.agentDisplay): \(first.description.isEmpty ? first.action : first.description)")
                            .font(.caption)
                            .lineLimit(2)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            case .systemSmall:
                small
            default:
                medium
            }
        }
        .widgetURL(DeepLink.tab("inbox"))
    }

    private var small: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: "hand.raised.fill")
                    .foregroundStyle(Brand.accent)
                    .widgetAccentable()
                Text("Approvals").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            if entry.signedOut {
                Text("Open the app to sign in").font(.caption).foregroundStyle(.secondary)
            } else if entry.total == 0 {
                Spacer(minLength: 0)
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 28))
                    .foregroundStyle(Brand.running)
                    .widgetAccentable()
                Text("Nothing waiting").font(.subheadline.weight(.medium))
            } else {
                Text("\(entry.total)")
                    .font(.system(size: 40, weight: .bold, design: .rounded))
                    .contentTransition(.numericText())
                if let first = oldest {
                    Text("\(first.agentDisplay) · \(first.action)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard()
    }

    private var medium: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("Approvals", systemImage: "hand.raised.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                CountBadge(count: entry.total)
            }
            if entry.signedOut {
                SignedOutView(line: "Open the app and sign in; approvals then wait here.")
            } else if let first = oldest {
                let who = agent(first.agent)
                let tint = Brand.agentColor(who?.color, name: first.agent)
                HStack(alignment: .top, spacing: 10) {
                    AvatarView(name: first.agent, display: first.agentDisplay, color: tint, image: who?.avatar, size: 36)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text(first.agentDisplay).font(.subheadline.weight(.semibold)).lineLimit(1)
                            Text(first.action)
                                .font(.caption2.monospaced())
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                            if first.risk == "high" || first.risk == "medium" {
                                Text(first.risk.capitalized)
                                    .font(.system(size: 9, weight: .bold))
                                    .padding(.horizontal, 5).padding(.vertical, 1)
                                    .background(Capsule().fill((first.risk == "high" ? Brand.error : Brand.starting).opacity(0.18)))
                                    .foregroundStyle(first.risk == "high" ? Brand.error : Brand.starting)
                            }
                        }
                        Text(first.description.isEmpty ? "Wants to run \(first.action)." : first.description)
                            .font(.subheadline)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
                HStack(spacing: 8) {
                    if first.risk == "high" {
                        Text("Open the app to see exactly what it will run.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    } else {
                        Button(intent: ResolveApprovalIntent(id: first.id, approve: true)) {
                            PillLabel(title: "Approve", symbol: "checkmark", tint: Brand.running, filled: true)
                        }
                        .buttonStyle(.plain)
                        Button(intent: ResolveApprovalIntent(id: first.id, approve: false)) {
                            PillLabel(title: "Decline", symbol: "xmark", tint: Brand.error)
                        }
                        .buttonStyle(.plain)
                    }
                    Spacer()
                    if let expires = first.expiresAt {
                        HStack(spacing: 3) {
                            Image(systemName: "timer").font(.caption2)
                            Text(expires, style: .timer).font(.caption2.monospacedDigit())
                        }
                        .foregroundStyle(.tertiary)
                    }
                }
            } else if entry.pulseApprovals > 0 {
                Spacer(minLength: 0)
                Label(entry.pulseApprovals == 1 ? "1 Cortex card waits for your OK" : "\(entry.pulseApprovals) Cortex cards wait for your OK", systemImage: "brain")
                    .font(.subheadline)
                    .foregroundStyle(Brand.cortex)
                Text("Open the Inbox to read them.").font(.caption).foregroundStyle(.secondary)
            } else {
                Spacer(minLength: 0)
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill").font(.title2).foregroundStyle(Brand.running).widgetAccentable()
                    VStack(alignment: .leading) {
                        Text("Nothing waiting").font(.subheadline.weight(.semibold))
                        Text("Your agents are not blocked on you.").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .olanoCard()
    }
}
