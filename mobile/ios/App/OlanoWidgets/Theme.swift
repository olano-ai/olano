import SwiftUI
import WidgetKit

/// The widgets' look: Olano's accent, each agent's own colour, a status
/// language shared by every widget, and the small views they are built from.
/// Everything reads in light and dark, in the Home Screen's tinted mode
/// (`widgetAccentable` marks what takes the tint) and in StandBy.
enum Brand {
    /// The app's accent (`--ion-color-primary`).
    static let accent = Color(red: 1.0, green: 0.416, blue: 0.239)
    /// Cortex's violet (`--olano-cortex`).
    static let cortex = Color(red: 0.486, green: 0.227, blue: 0.929)
    static let running = Color(red: 0.20, green: 0.78, blue: 0.47)
    static let starting = Color(red: 0.98, green: 0.72, blue: 0.20)
    static let stopped = Color.secondary
    static let error = Color(red: 0.93, green: 0.30, blue: 0.30)

    /// An agent's colour from the deployment (`#rrggbb`), or a colour drawn
    /// from its name so two agents never share one by accident.
    static func agentColor(_ hex: String?, name: String) -> Color {
        if let hex, let color = Color(hex: hex) { return color }
        var hash: UInt32 = 2166136261
        for byte in name.utf8 {
            hash ^= UInt32(byte)
            hash = hash &* 16777619
        }
        let hue = Double(hash % 360) / 360
        return Color(hue: hue, saturation: 0.55, brightness: 0.78)
    }

    static func statusColor(_ state: String) -> Color {
        switch state {
        case "running": return running
        case "starting": return starting
        case "error", "blocked": return error
        case "paused": return starting
        default: return stopped
        }
    }

    static func statusWord(_ state: String) -> String {
        switch state {
        case "running": return "Running"
        case "starting": return "Starting"
        case "error": return "Could not start"
        case "blocked": return "Held back"
        case "paused": return "Paused"
        default: return "Stopped"
        }
    }
}

extension Color {
    init?(hex: String) {
        var text = hex.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("#") { text.removeFirst() }
        guard text.count == 6 || text.count == 3, let value = UInt32(text.count == 3 ? text.map { "\($0)\($0)" }.joined() : text, radix: 16) else { return nil }
        self.init(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}

/// The deep links the app opens (src/native/siri.ts handles them).
enum DeepLink {
    static func conversation(agent: String, thread: String?) -> URL {
        var parts = URLComponents()
        parts.scheme = "olano"
        parts.host = "open"
        parts.queryItems = [URLQueryItem(name: "agent", value: agent)]
        if let thread, !thread.isEmpty { parts.queryItems?.append(URLQueryItem(name: "thread", value: thread)) }
        return parts.url ?? URL(string: "olano://open")!
    }

    static func talk(agent: String) -> URL {
        var parts = URLComponents()
        parts.scheme = "olano"
        parts.host = "talk"
        parts.queryItems = [URLQueryItem(name: "agent", value: agent)]
        return parts.url ?? URL(string: "olano://talk")!
    }

    /// A new conversation with the composer ready: `text` (the keyboard up),
    /// `camera` (taking a photo) or `photos` (choosing some).
    static func compose(agent: String, mode: String) -> URL {
        var parts = URLComponents()
        parts.scheme = "olano"
        parts.host = "open"
        parts.queryItems = [
            URLQueryItem(name: "agent", value: agent),
            URLQueryItem(name: "thread", value: "new"),
            URLQueryItem(name: "compose", value: mode),
        ]
        return parts.url ?? URL(string: "olano://open")!
    }

    static func tab(_ name: String) -> URL {
        URL(string: "olano://open?tab=\(name)") ?? URL(string: "olano://open")!
    }
}

// MARK: - Small views

/// The agent's picture, or its initials on its colour.
struct AvatarView: View {
    let name: String
    let display: String
    let color: Color
    let image: Data?
    var size: CGFloat = 44
    var state: String? = nil

    private var initials: String {
        let parts = display.split(separator: " ").prefix(2)
        let letters = parts.compactMap { $0.first }.map { String($0).uppercased() }
        return letters.isEmpty ? String(display.prefix(1)).uppercased() : letters.joined()
    }

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            Group {
                if let image, let ui = UIImage(data: image) {
                    Image(uiImage: ui)
                        .resizable()
                        .scaledToFill()
                } else {
                    LinearGradient(colors: [color, color.opacity(0.65)], startPoint: .topLeading, endPoint: .bottomTrailing)
                        .overlay(
                            Text(initials)
                                .font(.system(size: size * 0.38, weight: .semibold, design: .rounded))
                                .foregroundStyle(.white)
                        )
                }
            }
            .frame(width: size, height: size)
            .clipShape(Circle())
            if let state {
                Circle()
                    .fill(Brand.statusColor(state))
                    .frame(width: size * 0.27, height: size * 0.27)
                    .overlay(Circle().strokeBorder(.background, lineWidth: 1.5))
                    .offset(x: 1, y: 1)
            }
        }
        .widgetAccentable(false)
    }
}

/// "Running", with its dot.
struct StatusLine: View {
    let state: String
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(Brand.statusColor(state)).frame(width: 6, height: 6)
            Text(Brand.statusWord(state))
        }
        .font(.caption2.weight(.medium))
        .foregroundStyle(.secondary)
    }
}

/// A small count, as on the app's Inbox tab.
struct CountBadge: View {
    let count: Int
    var color: Color = Brand.accent
    var body: some View {
        if count > 0 {
            Text(count > 99 ? "99+" : "\(count)")
                .font(.caption2.weight(.bold).monospacedDigit())
                .foregroundStyle(.white)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Capsule().fill(color))
                .widgetAccentable()
        }
    }
}

/// A capsule button, for a widget's interactive intents.
struct PillLabel: View {
    let title: String
    let symbol: String
    var tint: Color = Brand.accent
    var filled = false
    var body: some View {
        Label(title, systemImage: symbol)
            .font(.caption.weight(.semibold))
            .labelStyle(.titleAndIcon)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .foregroundStyle(filled ? Color.white : tint)
            .background(Capsule().fill(filled ? tint : tint.opacity(0.14)))
            .widgetAccentable()
    }
}

/// A reply's first lines with an accent bar, like a quoted message.
struct QuoteView: View {
    let text: String
    let tint: Color
    var lines = 3
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(tint)
                .frame(width: 3)
                .widgetAccentable()
            Text(text)
                .font(.subheadline)
                .foregroundStyle(.primary)
                .lineLimit(lines)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// "4 min ago", keeping itself current.
struct AgoText: View {
    let date: Date?
    var body: some View {
        if let date {
            Text(date, style: .relative)
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.tertiary)
        }
    }
}

/// Olano's mark: a rounded square in the accent with a spark, the way the
/// app's icon reads at a glance.
struct OlanoMark: View {
    var size: CGFloat = 28
    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
            .fill(Brand.accent)
            .frame(width: size, height: size)
            .overlay(
                Image(systemName: "sparkle")
                    .font(.system(size: size * 0.5, weight: .bold))
                    .foregroundStyle(.white)
            )
            .widgetAccentable()
    }
}

/// A round button with a symbol, as on a compose bar; its label sits under it.
struct RoundAction: View {
    let title: String
    let symbol: String
    var tint: Color = Brand.accent
    var filled = false
    var size: CGFloat = 40
    var body: some View {
        VStack(spacing: 5) {
            Circle()
                .fill(filled ? tint : tint.opacity(0.14))
                .frame(width: size, height: size)
                .overlay(
                    Image(systemName: symbol)
                        .font(.system(size: size * 0.42, weight: .semibold))
                        .foregroundStyle(filled ? Color.white : tint)
                )
                .widgetAccentable()
            Text(title)
                .font(.caption2.weight(.medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity)
    }
}

/// A widget that has nothing to read yet: the mark, and one line saying why.
struct SignedOutView: View {
    let line: String
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                OlanoMark(size: 26)
                Text("Olano")
                    .font(.headline)
            }
            Spacer(minLength: 0)
            Text(line)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

extension View {
    /// The widget's card: the system background with a faint wash of the
    /// agent's colour in one corner, so a Home Screen of them stays calm.
    func olanoCard(tint: Color = Brand.accent) -> some View {
        containerBackground(for: .widget) {
            LinearGradient(
                colors: [tint.opacity(0.12), tint.opacity(0.0)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        }
    }
}
