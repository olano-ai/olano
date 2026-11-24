// Pure bookkeeping for the terminal store's per-agent tab sets.
//
// A terminal session belongs to exactly one agent — the socket is opened with
// `?agent=` and lands in that agent's workspace or sandbox — so the tab strip
// shows one agent's sessions at a time and the store keeps one active tab per
// agent. These helpers are the arithmetic behind that: which sessions belong
// to an agent, how many each agent has, and which tab becomes active after one
// is closed. They are DOM-free and unit-tested (`npm run test:unit`); the store
// wraps them around the live xterm/WebSocket objects.

export interface AgentOwned {
  id: string;
  agent: string;
}

/** The sessions that belong to `agent`, in list order. */
export function tabsFor<T extends AgentOwned>(list: readonly T[], agent: string): T[] {
  return list.filter((s) => s.agent === agent);
}

/** How many sessions each agent has — every tab, whatever its status: a
 *  closed or unavailable tab still occupies a slot in that agent's strip. */
export function countsByAgent(list: readonly AgentOwned[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const s of list) out[s.agent] = (out[s.agent] ?? 0) + 1;
  return out;
}

/**
 * The tab to activate after closing the one at `killedIndex` within an agent's
 * OWN list (`ids` is that list BEFORE the removal). The neighbour that slides
 * into the closed tab's place wins, clamped to the last tab; `""` when the
 * agent has no tab left — a legal state, rendered as the "Open terminal" empty
 * state rather than respawned (a respawn would often open an invisible shell
 * for an agent that is not the one on screen).
 */
export function nextActiveAfterKill(ids: readonly string[], killedIndex: number): string {
  const rest = ids.filter((_, i) => i !== killedIndex);
  if (rest.length === 0) return "";
  return rest[Math.min(killedIndex, rest.length - 1)];
}
