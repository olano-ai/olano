import Foundation
import UIKit
import WidgetKit

/// What the widgets draw, loaded from the deployment with the shared session
/// and remembered in the App Group so a redraw with no network (or no
/// session) still shows the last known state. Avatars are downsized once,
/// here, so a timeline stays small.
struct AgentSnapshot: Codable, Identifiable {
    var name: String
    var display: String
    var title: String
    var state: String
    var color: String?
    var avatar: Data?
    var lastReply: String?
    var lastReplyAt: Date?
    var lastThread: String?
    var lastThreadTitle: String?
    var approvals: Int
    /// Where the picture came from, so a changed avatar is fetched again.
    var avatarSource: String? = nil

    var id: String { name }

    static let sample: [AgentSnapshot] = [
        AgentSnapshot(name: "anna", display: "Anna", title: "Operations", state: "running", color: "#ff6a3d", avatar: nil,
                      lastReply: "Both suppliers confirmed. Meridian is cheapest at 4.10 a unit, so I have drafted the order for your approval.",
                      lastReplyAt: Date().addingTimeInterval(-14 * 60), lastThread: nil, lastThreadTitle: "Spring order", approvals: 1),
        AgentSnapshot(name: "jane", display: "Jane", title: "Research", state: "running", color: "#7c3aed", avatar: nil,
                      lastReply: "Three competitors raised prices this quarter. Summary and sources are in the report.",
                      lastReplyAt: Date().addingTimeInterval(-55 * 60), lastThread: nil, lastThreadTitle: "Market watch", approvals: 0),
        AgentSnapshot(name: "aurelius", display: "Aurelius", title: "Finance", state: "stopped", color: "#2563eb", avatar: nil,
                      lastReply: "Invoices for September are reconciled.", lastReplyAt: Date().addingTimeInterval(-26 * 3600),
                      lastThread: nil, lastThreadTitle: "Month end", approvals: 0),
        AgentSnapshot(name: "hannah", display: "Hannah", title: "Support", state: "running", color: "#0ea5e9", avatar: nil,
                      lastReply: "Two tickets answered, one waiting on the customer.", lastReplyAt: Date().addingTimeInterval(-3 * 3600),
                      lastThread: nil, lastThreadTitle: "Tickets", approvals: 0),
    ]
}

struct ApprovalSnapshot: Codable, Identifiable {
    var id: String
    var agent: String
    var agentDisplay: String
    var action: String
    var description: String
    var risk: String
    var createdAt: Date?
    var expiresAt: Date?

    static let sample = ApprovalSnapshot(
        id: "sample", agent: "anna", agentDisplay: "Anna", action: "gmail_send",
        description: "Send the order confirmation to Meridian Supplies", risk: "medium",
        createdAt: Date().addingTimeInterval(-90), expiresAt: Date().addingTimeInterval(210)
    )
}

struct PulseSnapshot: Codable, Identifiable {
    var id: String
    var title: String
    var detail: String
    var agent: String
    var agentDisplay: String
    var priority: String
    var createdAt: Date?

    static let sample = PulseSnapshot(
        id: "sample", title: "Bundle shipping with the spring order", detail: "Both suppliers ship from the same port this week; one consignment saves about 12%.",
        agent: "anna", agentDisplay: "Anna", priority: "normal", createdAt: Date().addingTimeInterval(-40 * 60)
    )
}

struct TaskSnapshot: Codable, Identifiable {
    var id: String
    var name: String
    var agent: String
    var agentDisplay: String
    var nextRun: Date?

    static let sample = [
        TaskSnapshot(id: "a", name: "Morning briefing", agent: "anna", agentDisplay: "Anna", nextRun: Date().addingTimeInterval(2 * 3600 + 600)),
        TaskSnapshot(id: "b", name: "Competitor watch", agent: "jane", agentDisplay: "Jane", nextRun: Date().addingTimeInterval(7 * 3600)),
        TaskSnapshot(id: "c", name: "Reconcile invoices", agent: "aurelius", agentDisplay: "Aurelius", nextRun: Date().addingTimeInterval(26 * 3600)),
    ]
}

/// Everything the widgets show, in one record, so one fetch feeds them all.
struct WidgetSnapshot: Codable {
    var agents: [AgentSnapshot]
    var approvals: [ApprovalSnapshot]
    var pulseApprovals: Int
    var pulse: [PulseSnapshot]
    var tasks: [TaskSnapshot]
    var isAdmin: Bool
    var fetchedAt: Date

    static let sample = WidgetSnapshot(
        agents: AgentSnapshot.sample, approvals: [ApprovalSnapshot.sample], pulseApprovals: 1,
        pulse: [PulseSnapshot.sample], tasks: TaskSnapshot.sample, isAdmin: true, fetchedAt: Date()
    )

    static let cacheKey = "olano.widget.snapshot"

    static func cached() -> WidgetSnapshot? {
        guard let data = AppGroup.defaults.data(forKey: cacheKey) else { return nil }
        return try? JSONDecoder().decode(WidgetSnapshot.self, from: data)
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            AppGroup.defaults.set(data, forKey: Self.cacheKey)
        }
    }

    /// The agent a widget shows: the one it was configured with, else the
    /// default chosen in the app, else the first.
    func agent(named name: String?) -> AgentSnapshot? {
        guard let name = DefaultAgent.resolve(name) else { return agents.first }
        return agents.first { $0.name == name } ?? agents.first
    }
}

/// How the widgets come by their snapshot.
enum SnapshotLoader {
    /// The next redraw the system is asked for, when nothing else wakes the widget.
    static let refreshAfter: TimeInterval = 15 * 60

    enum Result {
        case signedOut
        case snapshot(WidgetSnapshot)
    }

    /// The freshest snapshot there is: the deployment's when it answers, else
    /// the remembered one, else nothing (signed out, or never synced).
    static func load() async -> Result {
        guard SharedSession.isSignedIn else {
            return WidgetSnapshot.cached().map { .snapshot($0) } ?? .signedOut
        }
        if let fresh = await fetch() {
            fresh.save()
            return .snapshot(fresh)
        }
        return WidgetSnapshot.cached().map { .snapshot($0) } ?? .signedOut
    }

    /// One round trip per list, the agents' details in parallel.
    static func fetch() async -> WidgetSnapshot? {
        guard let agents = await Deployment.agents() else { return nil }
        let previous = WidgetSnapshot.cached()
        async let approvalsCall = Deployment.approvals()
        async let pulseApprovalsCall = Deployment.pulseApprovals()
        async let pulseCall = Deployment.pulse()
        async let tasksCall = Deployment.tasks()
        async let adminCall = Deployment.isAdmin()

        let snapshots: [AgentSnapshot] = await withTaskGroup(of: (Int, AgentSnapshot).self) { group in
            for (index, agent) in agents.enumerated() {
                group.addTask {
                    (index, await detail(of: agent, previous: previous?.agents.first { $0.name == agent.name }))
                }
            }
            var ordered: [(Int, AgentSnapshot)] = []
            for await item in group { ordered.append(item) }
            return ordered.sorted { $0.0 < $1.0 }.map { $0.1 }
        }

        let approvals = (await approvalsCall ?? []).map { approval -> ApprovalSnapshot in
            let created = Deployment.parseDate(approval.created_at)
            let expires = created.flatMap { c in approval.timeout_seconds.map { c.addingTimeInterval(TimeInterval($0)) } }
            return ApprovalSnapshot(
                id: approval.id, agent: approval.agent, agentDisplay: snapshots.first { $0.name == approval.agent }?.display ?? approval.agent,
                action: approval.action, description: approval.description ?? "", risk: approval.risk ?? "low",
                createdAt: created, expiresAt: expires
            )
        }
        var byAgent: [String: Int] = [:]
        for approval in approvals { byAgent[approval.agent, default: 0] += 1 }
        let withCounts = snapshots.map { snapshot -> AgentSnapshot in
            var copy = snapshot
            copy.approvals = byAgent[snapshot.name] ?? 0
            return copy
        }
        let pulse = (await pulseCall ?? []).map { item in
            PulseSnapshot(
                id: item.id, title: item.title ?? "Untitled", detail: item.why_now ?? item.content ?? "",
                agent: item.agent ?? "", agentDisplay: snapshots.first { $0.name == item.agent }?.display ?? (item.agent ?? ""),
                priority: item.priority ?? "normal", createdAt: Deployment.parseDate(item.created_at)
            )
        }
        let tasks = (await tasksCall ?? [])
            .filter { $0.enabled ?? true }
            .map { task in
                TaskSnapshot(
                    id: task.id, name: task.name, agent: task.agent_name ?? "",
                    agentDisplay: snapshots.first { $0.name == task.agent_name }?.display ?? (task.agent_name ?? ""),
                    nextRun: Deployment.parseDate(task.next_run)
                )
            }
            .filter { $0.nextRun != nil }
            .sorted { ($0.nextRun ?? .distantFuture) < ($1.nextRun ?? .distantFuture) }
        let admin = await adminCall ?? previous?.isAdmin ?? false
        SharedSession.isAdmin = admin
        return WidgetSnapshot(
            agents: withCounts, approvals: approvals, pulseApprovals: (await pulseApprovalsCall ?? []).count,
            pulse: pulse, tasks: tasks, isAdmin: admin, fetchedAt: Date()
        )
    }

    /// One agent: the last conversation the person had with it (the one its
    /// tap opens, as in the app's Chats) and that conversation's last reply,
    /// and its picture (kept from last time when the address has not changed).
    private static func detail(of agent: Deployment.Agent, previous: AgentSnapshot?) async -> AgentSnapshot {
        var snapshot = AgentSnapshot(
            name: agent.name, display: agent.display, title: agent.title ?? "", state: agent.state, color: agent.color,
            avatar: previous?.avatar, lastReply: previous?.lastReply, lastReplyAt: previous?.lastReplyAt,
            lastThread: previous?.lastThread, lastThreadTitle: previous?.lastThreadTitle, approvals: 0
        )
        async let avatarCall = avatar(for: agent, previous: previous)
        if let conversations = await Deployment.conversations(of: agent.name) {
            if let latest = conversations.first {
                snapshot.lastThread = latest.id
                snapshot.lastThreadTitle = latest.name ?? latest.title
                if let reply = await Deployment.lastReply(of: agent.name, thread: latest.id) {
                    snapshot.lastReply = Speakable.snippet(reply.text, maxChars: 240)
                    snapshot.lastReplyAt = reply.at ?? Deployment.parseDate(latest.updated_at)
                } else if let preview = latest.preview, !preview.isEmpty {
                    snapshot.lastReply = Speakable.snippet(preview, maxChars: 240)
                    snapshot.lastReplyAt = Deployment.parseDate(latest.updated_at)
                }
            } else {
                // No conversation yet (or none any more): a tap starts one,
                // and nothing remembered from an earlier read may name a
                // thread the app would not open.
                snapshot.lastThread = nil
                snapshot.lastThreadTitle = nil
                snapshot.lastReply = nil
                snapshot.lastReplyAt = nil
            }
        }
        snapshot.avatar = await avatarCall ?? previous?.avatar
        snapshot.avatarSource = agent.avatar_url
        return snapshot
    }

    /// The agent's picture, kept from last time while its address is unchanged.
    private static func avatar(for agent: Deployment.Agent, previous: AgentSnapshot?) async -> Data? {
        guard let path = agent.avatar_url, !path.isEmpty else { return nil }
        if let previous, previous.avatar != nil, previous.avatarSource == path { return previous.avatar }
        guard let data = await Deployment.image(at: path) else { return nil }
        return downsized(data)
    }

    /// A 96 pt picture is the largest any widget draws; the source can be a megabyte.
    static func downsized(_ data: Data) -> Data? {
        guard let image = UIImage(data: data) else { return nil }
        let side: CGFloat = 192
        let thumb = image.preparingThumbnail(of: CGSize(width: side, height: side)) ?? image
        return thumb.jpegData(compressionQuality: 0.85) ?? thumb.pngData()
    }
}
