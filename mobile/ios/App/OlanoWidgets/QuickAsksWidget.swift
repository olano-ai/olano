import AppIntents
import SwiftUI
import WidgetKit

/// Questions sent with one tap. A widget cannot take typed text, so the
/// questions are set when the widget is added ("Give me a short briefing"),
/// and each button sends one to the agent; the reply shows in the widget
/// when it arrives. Medium shows the buttons; large keeps the reply too.
struct QuickAsksWidget: Widget {
    static let kind = "ai.olano.app.widgets.asks"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: Self.kind, intent: QuickAsksConfigIntent.self, provider: QuickAsksProvider()) { entry in
            QuickAsksView(entry: entry)
        }
        .configurationDisplayName("Quick asks")
        .description("Up to three questions an agent answers with one tap.")
        .supportedFamilies([.systemMedium, .systemLarge])
    }
}

struct QuickAsksEntry: TimelineEntry {
    let date: Date
    let agent: AgentSnapshot?
    let questions: [(label: String, text: String)]
    let record: QuickAskRecord?
    let signedOut: Bool
}

struct QuickAsksProvider: AppIntentTimelineProvider {
    typealias Entry = QuickAsksEntry
    typealias Intent = QuickAsksConfigIntent

    func placeholder(in context: Context) -> QuickAsksEntry {
        QuickAsksEntry(
            date: Date(), agent: AgentSnapshot.sample[0],
            questions: [
                (label: "Give me a short…", text: "Give me a short briefing of what matters right now."),
                (label: "What is waiting…", text: "What is waiting on me?"),
                (label: "Summarize what you…", text: "Summarize what you did today."),
            ],
            record: QuickAskRecord(agent: "anna", question: "Give me a short briefing of what matters right now.",
                                   reply: "Two things: the Meridian order is waiting on your approval, and Jane's competitor report is ready.", pending: false, at: Date().addingTimeInterval(-300)),
            signedOut: false
        )
    }

    func snapshot(for configuration: QuickAsksConfigIntent, in context: Context) async -> QuickAsksEntry {
        if context.isPreview { return placeholder(in: context) }
        return await entry(for: configuration)
    }

    func timeline(for configuration: QuickAsksConfigIntent, in context: Context) async -> Timeline<QuickAsksEntry> {
        let entry = await entry(for: configuration)
        // A pending ask is checked again soon: its reply lands in the record
        // when the turn ends, or the deployment's answer comes with the push.
        let next = entry.record?.pending == true ? 60 : SnapshotLoader.refreshAfter
        return Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(next)))
    }

    private func entry(for configuration: QuickAsksConfigIntent) async -> QuickAsksEntry {
        switch await SnapshotLoader.load() {
        case .signedOut:
            return QuickAsksEntry(date: Date(), agent: nil, questions: configuration.questions, record: nil, signedOut: true)
        case .snapshot(let snapshot):
            let agent = snapshot.agent(named: configuration.agent?.id)
            var record = agent.flatMap { QuickAskRecord.load(for: $0.name) }
            // An ask let go after 20 s: its reply is the agent's latest, once it came.
            if let pending = record, pending.pending, let agent, let at = agent.lastReplyAt, at > pending.at, let reply = agent.lastReply {
                record?.reply = reply
                record?.pending = false
                record?.save()
            }
            return QuickAsksEntry(date: Date(), agent: agent, questions: configuration.questions, record: record, signedOut: false)
        }
    }
}

struct QuickAsksView: View {
    @Environment(\.widgetFamily) private var family
    let entry: QuickAsksEntry

    var body: some View {
        if let agent = entry.agent {
            let tint = Brand.agentColor(agent.color, name: agent.name)
            Group {
                if family == .systemLarge {
                    large(agent, tint: tint)
                } else {
                    medium(agent, tint: tint)
                }
            }
            .olanoCard(tint: tint)
            .widgetURL(DeepLink.conversation(agent: agent.name, thread: agent.lastThread))
        } else {
            SignedOutView(line: entry.signedOut ? "Open the app and sign in; the widget then sends your questions." : "No agent to ask yet. Open the app once.")
                .olanoCard()
                .widgetURL(DeepLink.tab("chats"))
        }
    }

    private func header(_ agent: AgentSnapshot, tint: Color) -> some View {
        HStack(spacing: 10) {
            AvatarView(name: agent.name, display: agent.display, color: tint, image: agent.avatar, size: 36, state: agent.state)
            VStack(alignment: .leading, spacing: 1) {
                Text(agent.display).font(.headline).lineLimit(1)
                Text("Quick asks").font(.caption2).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }

    private func buttons(_ agent: AgentSnapshot, tint: Color, compact: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if entry.questions.isEmpty {
                Text("Edit the widget to add questions.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            ForEach(Array(entry.questions.enumerated()), id: \.offset) { item in
                let asking = entry.record?.pending == true && entry.record?.question == item.element.text
                Button(intent: QuickAskIntent(agent: agent.name, question: item.element.text)) {
                    HStack(spacing: 6) {
                        Image(systemName: asking ? "ellipsis.circle" : "paperplane.fill")
                            .font(.caption2)
                        Text(item.element.label)
                            .font(.caption.weight(.semibold))
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, compact ? 6 : 8)
                    .foregroundStyle(asking ? Color.secondary : tint)
                    .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(tint.opacity(asking ? 0.08 : 0.14)))
                    .widgetAccentable()
                }
                .buttonStyle(.plain)
                .disabled(asking)
            }
        }
    }

    private func reply(tint: Color, lines: Int) -> some View {
        Group {
            if let record = entry.record {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(record.question)
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        AgoText(date: record.at)
                    }
                    if record.pending {
                        HStack(spacing: 6) {
                            Image(systemName: "ellipsis.circle").foregroundStyle(tint).widgetAccentable()
                            Text("Working on it…").font(.subheadline).foregroundStyle(.secondary)
                        }
                    } else if let text = record.reply, !text.isEmpty {
                        QuoteView(text: text, tint: tint, lines: lines)
                    }
                }
            } else {
                Text("Tap a question. The reply shows here.")
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private func medium(_ agent: AgentSnapshot, tint: Color) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 8) {
                header(agent, tint: tint)
                reply(tint: tint, lines: 3)
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            buttons(agent, tint: tint, compact: true)
                .frame(width: 128)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func large(_ agent: AgentSnapshot, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            header(agent, tint: tint)
            buttons(agent, tint: tint, compact: false)
            Divider()
            reply(tint: tint, lines: 8)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
