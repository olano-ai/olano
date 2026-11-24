import Foundation
#if canImport(WidgetKit)
import WidgetKit
#endif

/// What the app and its widget extension share (compiled into both targets).
///
/// - `SharedStore`: where the app leaves what an extension reads. The App
///   Group's UserDefaults when the build has one; otherwise the keychain,
///   which every build can share: the extension lists the app's identifier
///   in its `keychain-access-groups`, a capability a personal Apple ID team
///   is granted where App Groups are not. A build installed from Xcode on
///   such a team draws its widgets all the same.
/// - `SharedSession`: the deployment's address and the session cookie, kept
///   there (the cookie always in the keychain, under the App Group's access
///   group when there is one). The app copies them over
///   (`OlanoShellPlugin.syncWidgets`) when it signs in, comes to the front and
///   whenever something a widget shows has changed; nothing in an extension
///   ever signs in on its own.
/// - `AgentRoster` and `DefaultAgent`: the agents Siri, the widgets'
///   configuration and the launcher may name, and the one they use when none
///   was picked (Settings, Siri and widgets).
/// - `Deployment`: the handful of reads and writes the widgets and the Siri
///   actions make, as plain `URLSession` calls with the shared cookie.
/// - `SiriTurn`: one chat turn asked from outside a conversation, its reply
///   gathered from the stream.
///
/// What only one process needs (a widget's own cache, a quick ask's record,
/// a talk request on its way to the screens) stays in `AppGroup.defaults`,
/// which without an App Group is the process's own UserDefaults.

enum AppGroup {
    static let id = "group.ai.olano.app"

    /// The shared defaults, or the process's own when there is no App Group.
    static var defaults: UserDefaults {
        UserDefaults(suiteName: id) ?? .standard
    }

    static var isAvailable: Bool {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: id) != nil
    }
}

/// Generic-password items, the one keychain shape used here. With no access
/// group named, an item lands in the process's default group: the app's own
/// identifier, which the widget extension's entitlements list first, so the
/// extension reads what the app wrote without an App Group.
enum Keychain {
    static func read(service: String, account: String, accessGroup: String? = nil) -> Data? {
        var query = query(service: service, account: account, accessGroup: accessGroup)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data, !data.isEmpty else { return nil }
        return data
    }

    /// Replaces any earlier item. False when the keychain refused.
    @discardableResult
    static func write(_ data: Data, service: String, account: String, accessGroup: String? = nil) -> Bool {
        let base = query(service: service, account: account, accessGroup: accessGroup)
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = data
        // A widget redraws while the phone is locked in a pocket.
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
    }

    static func delete(service: String, account: String, accessGroup: String? = nil) {
        SecItemDelete(query(service: service, account: account, accessGroup: accessGroup) as CFDictionary)
    }

    private static func query(service: String, account: String, accessGroup: String?) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }
}

/// Small values the app writes for its extensions: in the App Group's
/// defaults when there is one, else one keychain item per key.
enum SharedStore {
    private static let service = "ai.olano.app.shared"

    static func data(forKey key: String) -> Data? {
        if AppGroup.isAvailable { return AppGroup.defaults.data(forKey: key) }
        return Keychain.read(service: service, account: key)
    }

    static func string(forKey key: String) -> String? {
        guard let data = data(forKey: key), let text = String(data: data, encoding: .utf8), !text.isEmpty else { return nil }
        return text
    }

    static func bool(forKey key: String) -> Bool {
        string(forKey: key) == "1"
    }

    static func set(_ data: Data?, forKey key: String) {
        if AppGroup.isAvailable {
            if let data {
                AppGroup.defaults.set(data, forKey: key)
            } else {
                AppGroup.defaults.removeObject(forKey: key)
            }
            return
        }
        if let data {
            Keychain.write(data, service: service, account: key)
        } else {
            Keychain.delete(service: service, account: key)
        }
    }

    static func set(_ string: String?, forKey key: String) {
        let text = (string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        set(text.isEmpty ? nil : text.data(using: .utf8), forKey: key)
    }

    static func set(_ flag: Bool, forKey key: String) {
        set(flag ? "1" : nil, forKey: key)
    }
}

// MARK: - The session an extension may use

enum SharedSession {
    static let originKey = "olano.shared.origin"
    static let adminKey = "olano.shared.admin"
    private static let service = "ai.olano.app.session"
    private static let account = "cookie"

    /// The deployment's origin (`https://host[:port]`), or nil before sign-in.
    static var origin: String? {
        get { SharedStore.string(forKey: originKey) }
        set { SharedStore.set(newValue, forKey: originKey) }
    }

    /// Whether the signed-in person administers the deployment (Start / Stop).
    static var isAdmin: Bool {
        get { SharedStore.bool(forKey: adminKey) }
        set { SharedStore.set(newValue, forKey: adminKey) }
    }

    /// The `Cookie:` header value for the deployment, from the keychain.
    static var cookieHeader: String? {
        guard let data = Keychain.read(service: service, account: account, accessGroup: cookieAccessGroup),
              let text = String(data: data, encoding: .utf8), !text.isEmpty else { return nil }
        return text
    }

    static var isSignedIn: Bool {
        origin != nil && cookieHeader != nil
    }

    /// Keeps the cookie header (replacing any earlier one). False when the
    /// keychain refused it.
    @discardableResult
    static func saveCookieHeader(_ header: String) -> Bool {
        guard let data = header.data(using: .utf8) else { return false }
        return Keychain.write(data, service: service, account: account, accessGroup: cookieAccessGroup)
    }

    static func clear() {
        origin = nil
        isAdmin = false
        Keychain.delete(service: service, account: account, accessGroup: cookieAccessGroup)
    }

    /// The App Group's access group when there is one (an App Group id is a
    /// keychain access group too); the process's default group otherwise.
    private static var cookieAccessGroup: String? {
        AppGroup.isAvailable ? AppGroup.id : nil
    }

    /// A request to the deployment with the shared cookie, or nil before the
    /// address is known. Without a shared cookie the process's own cookie jar
    /// is used, which inside the app holds the session (an extension's is empty).
    static func request(_ path: String, method: String = "GET", json: [String: Any]? = nil, timeout: TimeInterval = 20) -> URLRequest? {
        guard let origin, let url = URL(string: origin + path) else { return nil }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = method
        if let cookie = cookieHeader {
            request.httpShouldHandleCookies = false
            request.setValue(cookie, forHTTPHeaderField: "Cookie")
        } else {
            request.httpShouldHandleCookies = true
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: json)
        }
        return request
    }
}

// MARK: - The agents the phone may name

enum AgentRoster {
    static let key = "olano.siri.agents"

    struct Entry: Codable {
        let name: String
        let display: String
    }

    static var entries: [Entry] {
        guard let data = SharedStore.data(forKey: key),
              let rows = try? JSONDecoder().decode([Entry].self, from: data) else { return [] }
        return rows
    }

    static func save(_ rows: [Entry]) {
        if let data = try? JSONEncoder().encode(rows) {
            SharedStore.set(data, forKey: key)
        }
    }

    static func display(of name: String) -> String {
        entries.first { $0.name == name }?.display ?? name
    }
}

/// The agent Siri, a control or a widget reaches when none was picked:
/// chosen in the app (Settings, Siri and widgets), published with the roster
/// (`OlanoShell.setAgents`), the first of the roster when nothing was chosen.
enum DefaultAgent {
    static let key = "olano.default.agent"

    static var name: String? {
        get { SharedStore.string(forKey: key) }
        set { SharedStore.set(newValue, forKey: key) }
    }

    static var entry: AgentRoster.Entry? {
        let rows = AgentRoster.entries
        if let name, let chosen = rows.first(where: { $0.name == name }) { return chosen }
        return rows.first
    }

    /// Whether `candidate` (a handle, or nil for "whichever") means this agent.
    static func resolve(_ candidate: String?) -> String? {
        if let candidate, !candidate.isEmpty { return candidate }
        return entry?.name
    }
}

/// The conversation Siri's asks continue. A follow-up soon after the last ask
/// ("and tomorrow?") belongs to the same conversation; after a while a fresh
/// one starts, so an old exchange never colours a new question.
enum SiriThreads {
    static let continueWithin: TimeInterval = 30 * 60

    private static func key(_ agent: String) -> String { "olano.siri.thread.\(agent)" }

    static func current(for agent: String) -> String? {
        guard let row = AppGroup.defaults.dictionary(forKey: key(agent)),
              let id = row["id"] as? String, !id.isEmpty,
              let at = row["at"] as? Double else { return nil }
        return Date().timeIntervalSince1970 - at <= continueWithin ? id : nil
    }

    static func remember(_ id: String, for agent: String) {
        AppGroup.defaults.set(["id": id, "at": Date().timeIntervalSince1970], forKey: key(agent))
    }
}

/// "Talk to an Agent", on its way to the screens (Siri, a Control Center
/// control, the Action button).
enum TalkRequest {
    static let key = "olano.talk.agent"
    /// Posted after the request is kept; OlanoShellPlugin forwards it to the
    /// page when the page is listening, and the page asks `takeTalk()` otherwise.
    static let posted = Notification.Name("ai.olano.app.talk")

    static func post(_ agent: String) {
        AppGroup.defaults.set(agent, forKey: key)
        NotificationCenter.default.post(name: posted, object: nil)
    }

    static func take() -> String? {
        let agent = AppGroup.defaults.string(forKey: key)
        AppGroup.defaults.removeObject(forKey: key)
        return (agent ?? "").isEmpty ? nil : agent
    }
}

/// Asks the system to redraw every widget (a no-op where WidgetKit is absent).
enum WidgetRefresh {
    static func reloadAll() {
        #if canImport(WidgetKit)
        if #available(iOS 14.0, *) {
            WidgetCenter.shared.reloadAllTimelines()
        }
        #endif
    }
}

// MARK: - What the deployment answers

/// The reads and writes widgets and the Siri actions make. Every call returns
/// nil (or false) rather than throwing: a widget draws what it has.
enum Deployment {
    struct Agent: Decodable {
        let name: String
        let display_name: String?
        let title: String?
        let emoji: String?
        let running: Bool?
        let paused: Bool?
        let status: String?
        let error: String?
        let blocked: Bool?
        let blocked_reason: String?
        let avatar_url: String?
        let color: String?

        var display: String {
            let text = (display_name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return text.isEmpty ? name : text
        }

        /// "running", "starting", "stopped", "error", "blocked" or "paused".
        var state: String {
            if blocked == true { return "blocked" }
            if let status, !status.isEmpty { return status == "configured" ? "stopped" : status }
            if paused == true { return "paused" }
            return running == true ? "running" : "stopped"
        }
    }

    struct Thread: Decodable {
        let id: String
        let name: String?
        let title: String?
        let preview: String?
        let origin: String?
        let channel: String?
        let updated_at: String?
        let running: Bool?

        /// A conversation a person had with the agent, by the rule the
        /// dashboard and the app share (`thread_pages.is_conversation`):
        /// never a scheduled or heartbeat run, another agent's thread, a
        /// Cortex thread or a call from another system.
        var isConversation: Bool {
            !["scheduled", "heartbeat", "peer", "autonomous", "cortex", "external"].contains(origin ?? "")
                && !["mcp", "a2a"].contains(channel ?? "")
        }
    }

    private struct ThreadPage: Decodable {
        let threads: [Thread]
    }

    struct ThreadRow: Decodable {
        let role: String
        let content: String?
        let ts: String?
    }

    struct ThreadDetail: Decodable {
        let id: String
        let messages: [ThreadRow]
    }

    struct Approval: Decodable {
        let id: String
        let agent: String
        let action: String
        let description: String?
        let risk: String?
        let created_at: String?
        let timeout_seconds: Int?
    }

    struct PulseItem: Decodable {
        let id: String
        let lane: String?
        let priority: String?
        let title: String?
        let content: String?
        let why_now: String?
        let created_at: String?
        let agent: String?
    }

    struct ScheduledTask: Decodable {
        let id: String
        let name: String
        let enabled: Bool?
        let agent_name: String?
        let next_run: String?
        let last_run: String?
    }

    struct CreditsDisplay: Decodable {
        let credits_enabled: Bool?
        let unit_name: String?
        let unit_name_plural: String?
        let decimals: Int?
    }

    struct CreditsBalance: Decodable {
        let available: Bool
        let balance: Double?
        let included_remaining: Double?
        let monthly_included: Double?
        let monthly_used: Double?
        let period_end: String?
    }

    private struct Me: Decodable {
        struct User: Decodable {
            let is_admin: Bool?
        }
        let user: User
    }

    private struct Pending<T: Decodable>: Decodable {
        let pending: [T]
    }

    private struct Items<T: Decodable>: Decodable {
        let items: [T]
    }

    /// The body of a successful GET, or nil for anything else.
    private static func data(_ path: String) async -> Data? {
        guard let request = SharedSession.request(path) else { return nil }
        guard let result = try? await URLSession.shared.data(for: request),
              let status = (result.1 as? HTTPURLResponse)?.statusCode, (200..<300).contains(status) else { return nil }
        return result.0
    }

    private static func get<T: Decodable>(_ path: String, as type: T.Type) async -> T? {
        guard let body = await data(path) else { return nil }
        return try? JSONDecoder().decode(type, from: body)
    }

    private static func post(_ path: String, json: [String: Any]) async -> Int? {
        guard let request = SharedSession.request(path, method: "POST", json: json) else { return nil }
        guard let result = try? await URLSession.shared.data(for: request) else { return nil }
        return (result.1 as? HTTPURLResponse)?.statusCode
    }

    static func agents() async -> [Agent]? {
        await get("/api/agents", as: [Agent].self)
    }

    static func isAdmin() async -> Bool? {
        guard let me = await get("/api/auth/me", as: Me.self) else { return nil }
        return me.user.is_admin ?? false
    }

    /// The conversations a person had with the agent, newest first, at most
    /// `limit` of them: what tapping the agent in the app's Chats opens, so a
    /// widget's tap lands where the app's own would. Empty when the agent has
    /// none (only scheduled runs, say); nil when the deployment did not
    /// answer. Asked as one page (`?limit=&kind=conversation`); a runtime
    /// from before paging answers with every thread as a list, and the rule
    /// is applied here.
    static func conversations(of agent: String, limit: Int = 1) async -> [Thread]? {
        guard let body = await data("/api/agents/\(encode(agent))/threads?limit=\(limit)&kind=conversation") else { return nil }
        if let page = try? JSONDecoder().decode(ThreadPage.self, from: body) {
            return page.threads
        }
        guard let all = try? JSONDecoder().decode([Thread].self, from: body) else { return nil }
        return Array(
            all.filter(\.isConversation)
                .sorted { (parseDate($0.updated_at) ?? .distantPast) > (parseDate($1.updated_at) ?? .distantPast) }
                .prefix(limit)
        )
    }

    /// The last thing the agent said in a conversation, with when.
    static func lastReply(of agent: String, thread: String) async -> (text: String, at: Date?)? {
        guard let detail = await get("/api/agents/\(encode(agent))/threads/\(encode(thread))", as: ThreadDetail.self) else { return nil }
        guard let row = detail.messages.last(where: { $0.role == "assistant" && !($0.content ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else { return nil }
        return (row.content ?? "", parseDate(row.ts))
    }

    static func approvals() async -> [Approval]? {
        await get("/api/hitl/pending", as: Pending<Approval>.self)?.pending
    }

    static func pulseApprovals() async -> [PulseItem]? {
        await get("/api/pulse?lane=approval&status=pending", as: Items<PulseItem>.self)?.items
    }

    static func pulse() async -> [PulseItem]? {
        await get("/api/pulse?lane=pulse&status=pending", as: Items<PulseItem>.self)?.items
    }

    static func tasks() async -> [ScheduledTask]? {
        await get("/api/scheduler/tasks", as: [ScheduledTask].self)
    }

    static func creditsDisplay() async -> CreditsDisplay? {
        await get("/api/credits", as: CreditsDisplay.self)
    }

    static func creditsBalance() async -> CreditsBalance? {
        await get("/api/olano/credits", as: CreditsBalance.self)
    }

    /// The avatar's bytes (the image needs the cookie, so a plain URL cannot show it).
    static func image(at path: String) async -> Data? {
        guard !path.isEmpty, let request = SharedSession.request(path) else { return nil }
        guard let result = try? await URLSession.shared.data(for: request),
              let status = (result.1 as? HTTPURLResponse)?.statusCode, (200..<300).contains(status), !result.0.isEmpty else { return nil }
        return result.0
    }

    static func start(agent: String) async -> Bool {
        guard let status = await post("/api/agents/\(encode(agent))/start", json: [:]) else { return false }
        return (200..<300).contains(status)
    }

    static func stop(agent: String) async -> Bool {
        guard let status = await post("/api/agents/\(encode(agent))/stop", json: [:]) else { return false }
        return (200..<300).contains(status)
    }

    /// Answers an Approvals inbox item. True when the deployment took the answer
    /// (a 404 or 409 means it was already answered, which counts as done).
    static func resolve(approval id: String, approved: Bool) async -> Bool {
        guard let status = await post("/api/hitl/resolve", json: ["id": id, "approved": approved]) else { return false }
        return (200..<300).contains(status) || status == 404 || status == 409
    }

    static func encode(_ segment: String) -> String {
        segment.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? segment
    }

    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let iso: ISO8601DateFormatter = ISO8601DateFormatter()
    private static let longFraction: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 0)
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXXXX"
        return f
    }()

    /// The deployment's timestamps: ISO 8601, with or without fractions, with
    /// or without a zone (a bare one is UTC).
    static func parseDate(_ text: String?) -> Date? {
        guard var text = text?.trimmingCharacters(in: .whitespaces), !text.isEmpty else { return nil }
        if !text.hasSuffix("Z") && !text.contains("+") && text.range(of: #"-\d\d:\d\d$"#, options: .regularExpression) == nil {
            text += "Z"
        }
        if text.contains(" ") { text = text.replacingOccurrences(of: " ", with: "T") }
        return isoFractional.date(from: text) ?? iso.date(from: text) ?? longFraction.date(from: text)
    }
}

// MARK: - A turn asked from outside a conversation

/// One chat turn: `POST /api/chat/stream` with the shared cookie, the reply's
/// text gathered from the stream until `done`. Siri's "Ask an Agent" and the
/// widgets' quick asks both run on it.
struct SiriTurn {
    /// How long a caller is kept waiting before the turn is let go. The turn
    /// finishes on its own; with nobody watching, its reply pushes.
    static let patience: TimeInterval = 20

    enum Outcome {
        case replied(String)
        case stillWorking
        case refused(String)
    }

    let agent: String
    let display: String
    let message: String

    func run(within seconds: TimeInterval) async -> Outcome {
        let streaming = Task { await stream() }
        let patience = Task {
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            streaming.cancel()
        }
        let outcome = await streaming.value
        patience.cancel()
        return outcome
    }

    private func stream() async -> Outcome {
        var body: [String: Any] = ["agent": agent, "message": message, "client": "mobile"]
        if let thread = SiriThreads.current(for: agent) {
            body["thread_id"] = thread
        }
        guard var request = SharedSession.request("/api/chat/stream", method: "POST", json: body, timeout: 60) else {
            return .refused("Open Olano and sign in to your deployment first.")
        }
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")

        var reply = ""
        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard (200..<300).contains(status) else { return .refused(refusal(status)) }
            for try await line in bytes.lines {
                guard line.hasPrefix("data:") else { continue }
                let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                guard let raw = json.data(using: .utf8),
                      let frame = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
                      let type = frame["type"] as? String else { continue }
                switch type {
                case "start":
                    if let id = frame["thread_id"] as? String, !id.isEmpty {
                        SiriThreads.remember(id, for: agent)
                    }
                case "text":
                    reply += frame["text"] as? String ?? ""
                case "error":
                    let why = (frame["message"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    return .refused(why.isEmpty ? "Something went wrong. Open Olano to see what." : why)
                case "done":
                    return .replied(reply)
                default:
                    break
                }
            }
            // The stream ended without saying so: whatever came is the answer.
            return .replied(reply)
        } catch is CancellationError {
            return .stillWorking
        } catch let error as URLError where error.code == .cancelled {
            return .stillWorking
        } catch {
            return .refused("The deployment could not be reached. Open Olano to try again.")
        }
    }

    private func refusal(_ status: Int) -> String {
        switch status {
        case 401: return "Olano needs you to sign in again. Open it to continue."
        case 403: return "You cannot chat with \(display) on this deployment."
        case 404: return "\(display) is not on this deployment any more."
        case 409: return "\(display) is busy with another message. Try again in a moment."
        case 429: return "\(display) is getting too many messages right now. Try again in a moment."
        default: return "Something went wrong. Open Olano to try again."
        }
    }
}

/// A reply as words to read aloud, the way the app reads one (src/chat/speakable.ts):
/// Markdown marks go, a link is read as its text and an address as its site,
/// a table row as its cells, and a code block as nothing at all. Widgets use
/// it for a reply's first lines too.
enum Speakable {
    /// A text-to-speech service's own limit is 4096 characters.
    static let maxChars = 4000

    static func words(_ text: String) -> String {
        var lines: [String] = []
        var inFence = false
        for rawLine in text.components(separatedBy: .newlines) {
            let line = String(rawLine)
            if matches(line, #"^\s*(```|~~~)"#) {
                inFence.toggle()
                continue
            }
            if inFence { continue }
            if matches(line, #"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$"#) { continue }
            var out = line
            // A table row says its cells.
            if matches(out, #"^\s*\|.*\|\s*$"#) {
                out = out.trimmingCharacters(in: .whitespaces)
                out = String(out.dropFirst().dropLast())
                out = out.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }.joined(separator: ", ")
            }
            out = replace(out, #"^\s{0,3}#{1,6}\s+"#, with: "")
            out = replace(out, #"^\s*>\s?"#, with: "")
            out = replace(out, #"^\s*([-*+]|\d+[.)])\s+"#, with: "")
            out = replace(out, #"!\[[^\]]*\]\([^)]*\)"#, with: "")
            out = replace(out, #"\[([^\]]+)\]\([^)]*\)"#, with: "$1")
            out = replaceURLs(out)
            out = replace(out, #"<br\s*/?>"#, with: " ")
            out = replace(out, #"</?[a-zA-Z][^>]*>"#, with: "")
            out = replace(out, #"`([^`]+)`"#, with: "$1")
            out = replace(out, #"(\*\*|__)(\S.*?\S|\S)\1"#, with: "$2")
            out = replace(out, #"(^|[^\w*])\*(\S.*?\S|\S)\*(?!\w)"#, with: "$1$2")
            out = replace(out, #"~~(.+?)~~"#, with: "$1")
            out = replace(out, #"^\s*([-*_]\s*){3,}$"#, with: "")
            lines.append(out.trimmingCharacters(in: .whitespaces))
        }
        let joined = lines.filter { !$0.isEmpty }.joined(separator: "\n")
        let collapsed = replace(joined, #"[ \t]{2,}"#, with: " ")
        if collapsed.count <= maxChars { return collapsed }
        return String(collapsed.prefix(maxChars))
    }

    /// The first line or two, for a card.
    static func snippet(_ text: String, maxChars: Int = 160) -> String {
        let plain = words(text).replacingOccurrences(of: "\n", with: " ").trimmingCharacters(in: .whitespaces)
        if plain.count <= maxChars { return plain }
        let cut = plain.index(plain.startIndex, offsetBy: maxChars)
        let head = plain[..<cut]
        if let space = head.lastIndex(of: " "), head.distance(from: head.startIndex, to: space) > maxChars / 2 {
            return String(head[..<space]) + "…"
        }
        return String(head) + "…"
    }

    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }

    private static func replace(_ text: String, _ pattern: String, with template: String) -> String {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.anchorsMatchLines]) else { return text }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.stringByReplacingMatches(in: text, options: [], range: range, withTemplate: template)
    }

    /// A bare web address is read as the name of its site.
    private static func replaceURLs(_ text: String) -> String {
        guard let regex = try? NSRegularExpression(pattern: #"<?(https?://[^\s)<>]+)>?"#) else { return text }
        var out = text
        let matches = regex.matches(in: text, range: NSRange(text.startIndex..<text.endIndex, in: text)).reversed()
        for match in matches {
            guard let whole = Range(match.range, in: out), let inner = Range(match.range(at: 1), in: out) else { continue }
            let host = URL(string: String(out[inner]))?.host ?? "a link"
            out.replaceSubrange(whole, with: host.hasPrefix("www.") ? String(host.dropFirst(4)) : host)
        }
        return out
    }
}

// MARK: - What a widget remembers between redraws

/// A quick ask a widget sent and what came back, kept so the widget can show
/// it at once and after the phone redraws it.
struct QuickAskRecord: Codable {
    var agent: String
    var question: String
    var reply: String?
    var pending: Bool
    var at: Date

    private static func key(_ agent: String) -> String { "olano.widget.ask.\(agent)" }

    static func load(for agent: String) -> QuickAskRecord? {
        guard let data = AppGroup.defaults.data(forKey: key(agent)) else { return nil }
        return try? JSONDecoder().decode(QuickAskRecord.self, from: data)
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            AppGroup.defaults.set(data, forKey: Self.key(agent))
        }
    }
}
