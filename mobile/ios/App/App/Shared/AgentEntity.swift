import AppIntents
import Foundation

/// An agent as Siri, the Shortcuts app and a widget's configuration see it.
/// Resolved from the roster the app publishes (`AgentRoster`), so no network
/// is needed to say "Aurelius" or to pick an agent for a widget.
@available(iOS 16.0, *)
struct AgentEntity: AppEntity {
    static var typeDisplayRepresentation: TypeDisplayRepresentation = "Agent"
    static var defaultQuery = AgentQuery()

    /// The agent's handle (its folder name on the deployment).
    var id: String
    /// What is said and shown: the display name, or the handle.
    var display: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(display)")
    }

    init(id: String, display: String) {
        self.id = id
        self.display = display.isEmpty ? id : display
    }

    init(_ entry: AgentRoster.Entry) {
        self.init(id: entry.name, display: entry.display)
    }
}

@available(iOS 16.0, *)
struct AgentQuery: EntityStringQuery {
    /// Every agent, the chosen default first (Settings, Siri and widgets).
    private func all() -> [AgentEntity] {
        let rows = AgentRoster.entries
        guard let chosen = DefaultAgent.name, let index = rows.firstIndex(where: { $0.name == chosen }), index > 0 else {
            return rows.map(AgentEntity.init)
        }
        var ordered = rows
        ordered.insert(ordered.remove(at: index), at: 0)
        return ordered.map(AgentEntity.init)
    }

    func entities(for identifiers: [String]) async throws -> [AgentEntity] {
        all().filter { identifiers.contains($0.id) }
    }

    /// A name Siri heard that no phrase matched exactly: the agent whose
    /// display name or handle contains it.
    func entities(matching string: String) async throws -> [AgentEntity] {
        let wanted = string.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !wanted.isEmpty else { return all() }
        return all().filter { $0.display.lowercased().contains(wanted) || $0.id.lowercased().contains(wanted) }
    }

    func suggestedEntities() async throws -> [AgentEntity] {
        all()
    }

    func defaultResult() async -> AgentEntity? {
        all().first
    }
}
