// Whether an agent can answer right now, and where that puts its row in a
// sidebar list. Shared by both shells' agent lists (views/nav/BotNav.tsx and
// views/simple/BotRail.tsx), so the two can never disagree about who counts as
// offline.
//
// This is the pure half: no React, no DOM, so the rules run under Node's test
// runner exactly as they run in the browser.
//
// Why a list needs more than "running or not". Status is live state, while the
// sidebar's sections (Pinned, the reader's own groups, All agents) are the
// reader's arrangement, so status never MOVES an agent between sections. It
// only decides, inside All agents, whether a row sits in the visible band or in
// the folded "N offline" tail. And offline is several different things that
// must not look alike:
//
//   - online      booted and not paused: answers right away
//   - starting /  booting or shutting down: visible, because a row that hides
//     stopping    and reappears seconds later is worse than an amber pip
//   - error       a boot that failed: visible, with a red pip, because it
//                 needs someone to look at it
//   - blocked     refused by the deployment (agent cap, feature off): visible,
//                 amber, with the reason
//   - paused      someone pressed Stop on purpose: folded away
//   - offline     configured but not running: folded away
//
// Only the last two go behind the fold. Everything else either answers or
// wants attention.

/** The states a row can show. */
export type Presence = "online" | "starting" | "stopping" | "error" | "blocked" | "paused" | "offline";

/** The fields of a roster row this module reads. */
export interface PresenceInput {
  running?: boolean;
  paused?: boolean;
  status?: string;
  error?: string;
  blocked?: boolean;
  blocked_reason?: string;
}

/**
 * Classify one roster row.
 *
 * Order matters. A deployment block or a failed boot outranks everything,
 * because that is what someone has to act on. Paused outranks running: a booted
 * agent that was paused still reports `running: true` on the roster, and it
 * will not answer.
 */
export function presenceOf(a: PresenceInput): Presence {
  // Read defensively: a roster row is server JSON, and a field of the wrong
  // type must cost one row its state, never the whole list a render.
  const status = typeof a.status === "string" ? a.status.toLowerCase() : "";
  if (a.blocked === true || status === "blocked") return "blocked";
  if (status === "error") return "error";
  if (a.paused || status === "paused") return "paused";
  if (status === "starting") return "starting";
  if (status === "stopping") return "stopping";
  if (a.running === true) return "online";
  return "offline";
}

/** Folded into the "N offline" tail: paused on purpose, or simply not running. */
export function isAsleep(p: Presence): boolean {
  return p === "paused" || p === "offline";
}

/** The pip's colour class. Undefined = the plain grey pip. */
export function pipTone(p: Presence): "online" | "warn" | "error" | undefined {
  switch (p) {
    case "online":
      return "online";
    case "error":
      return "error";
    case "starting":
    case "stopping":
    case "blocked":
      return "warn";
    default:
      return undefined;
  }
}

/**
 * Split a section's members, already in the reader's order, into the band that
 * always shows and the asleep tail. Both keep their relative order, so a row
 * never changes place inside its band.
 *
 * `keepVisible` names rows that stay in the visible band whatever their state:
 * the selected agent (its unfolded chat list must not vanish into a fold) and
 * any agent with unread replies (something the reader has not seen yet).
 */
export function splitAsleep(
  members: readonly string[],
  presence: (name: string) => Presence,
  keepVisible: (name: string) => boolean = () => false,
): { awake: string[]; asleep: string[] } {
  const awake: string[] = [];
  const asleep: string[] = [];
  for (const n of members) (isAsleep(presence(n)) && !keepVisible(n) ? asleep : awake).push(n);
  return { awake, asleep };
}

/**
 * Whether the asleep tail is drawn.
 *
 * The reader's stored choice decides, with two exceptions that both exist so
 * the fold can never hide the thing being looked for:
 *
 *   - nothing above the fold (the box is booting, or every agent in the
 *     section is stopped): an All agents list with only a "5 offline" row in
 *     it reads as broken, so the rows are drawn flat;
 *   - a search filter is on: typing a name must always find that agent.
 */
export function asleepShown({
  storedOpen,
  awakeCount,
  filtering,
}: {
  storedOpen: boolean;
  awakeCount: number;
  filtering: boolean;
}): boolean {
  return storedOpen || awakeCount === 0 || filtering;
}

/**
 * Whether the "N offline" disclosure row is drawn at all. Not when there is
 * nothing to disclose, and not when the tail is forced open by one of the
 * guards above: a toggle that toggles nothing is a broken control.
 */
export function asleepToggleShown({
  asleepCount,
  awakeCount,
  filtering,
}: {
  asleepCount: number;
  awakeCount: number;
  filtering: boolean;
}): boolean {
  return asleepCount > 0 && awakeCount > 0 && !filtering;
}

/**
 * Rewrite a reader's ordering after moving `name` to `index` within the list
 * as displayed (awake band, then the asleep tail), keeping every row in its
 * band.
 *
 * The display re-sorts by band on every render, so an order that interleaves
 * the two would show a move as "nothing happened" or as a jump. Instead the
 * move lands in the combined list and the result is stably re-partitioned: a
 * row dragged past the end of its band comes to rest at that band's edge.
 */
export function moveWithinBands(
  awake: readonly string[],
  asleep: readonly string[],
  name: string,
  index: number,
): string[] {
  const combined = [...awake, ...asleep];
  const from = combined.indexOf(name);
  const without = combined.filter((n) => n !== name);
  const raw = from !== -1 && from < index ? index - 1 : index;
  const at = Math.max(0, Math.min(without.length, raw));
  const moved = [...without.slice(0, at), name, ...without.slice(at)];
  const sleepy = new Set(asleep);
  return [...moved.filter((n) => !sleepy.has(n)), ...moved.filter((n) => sleepy.has(n))];
}

/** The band a row is in, for a one-step move up or down inside it. */
export function bandOf(
  awake: readonly string[],
  asleep: readonly string[],
  name: string,
): readonly string[] | null {
  if (awake.includes(name)) return awake;
  if (asleep.includes(name)) return asleep;
  return null;
}
