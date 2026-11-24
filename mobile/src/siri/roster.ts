/**
 * The agents a phone's assistant may name: the roster the app publishes to
 * Siri ("Hey Siri, ask Aurelius in Olano...") and to the Android launcher's
 * "Talk to ..." shortcuts. The display name is what is said and shown; the
 * handle is what the deployment is asked for.
 *
 * The roster is ordered with the chosen default agent first (Settings, Siri
 * and widgets): the launcher's first shortcut, Siri's suggestion and a
 * widget with no agent picked all take the first entry.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

export interface RosterEntry {
  /** The agent's handle (its folder name on the deployment). */
  name: string;
  /** What is said and shown: the display name, or the handle. */
  display: string;
}

/** Every agent once, in the app's order (the default agent first), by the name people use for it. */
export function rosterOf(agents: { name: string; display_name?: string }[], preferred = ""): RosterEntry[] {
  const seen = new Set<string>();
  const roster: RosterEntry[] = [];
  for (const agent of agents) {
    const name = (agent.name || "").trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    roster.push({ name, display: (agent.display_name || "").trim() || name });
  }
  const first = roster.findIndex((entry) => entry.name === preferred.trim());
  if (first > 0) roster.unshift(...roster.splice(first, 1));
  return roster;
}

/** The same roster, as one string, so an unchanged one is not published twice. */
export function rosterKey(roster: RosterEntry[]): string {
  return roster.map((entry) => `${entry.name}\u0000${entry.display}`).join("\n");
}

/** The agent a "talk" request names, or null for an empty one. */
export function talkAgentOf(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const agent = raw.trim();
  return agent ? agent : null;
}
