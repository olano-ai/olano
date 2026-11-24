// How the sidebar's bot list is arranged: pins, hand-made groups, the order
// inside them, and which sections are folded shut.
//
// This is the pure half — no React, no DOM — so the rules below run under
// Node's test runner exactly as they run in the browser. `useBotArrangement`
// (useBotArrangement.ts) wraps it in state and localStorage.
//
// Three reserved sections come from the simple shell's rail and keep its
// semantics: PINNED (`pins`, in pin order), ALL BOTS (every unpinned,
// unassigned bot, in the hand-arranged `order` with alphabetical fallback) and
// GROUP CHATS (not bots at all — a list of group conversations, owned by
// groupsStore). New here are the hand-made groups a person creates from the
// right-click menu. A bot lives in exactly one place — pinned, in one group, or
// under All bots — which is the simple rail's rule ("two sections, one
// gesture") extended to n sections: moving a bot somewhere always takes it out
// of wherever it was.
//
// Why localStorage and not the account: the arrangement is a per-device
// convenience, like the theme, the sidebar width and the simple rail's own
// pins (`lc.simple.*`). It is deliberately NOT shared with the simple rail's
// keys — the two shells have different sections, and one key describing both
// would drift the day either one changed.

export const RESERVED_SECTIONS = ["pinned", "all", "chats"] as const;
export type ReservedSection = (typeof RESERVED_SECTIONS)[number];

/** A section a bot can live in: pinned, a hand-made group (by id), or "all". */
export type BotHome = "pinned" | "all" | { group: string };

export interface CustomGroup {
  /** Stable id, minted on creation. Never the name — names are renamed. */
  id: string;
  name: string;
}

export interface Arrangement {
  /** Pinned bots, in pin order. */
  pins: string[];
  /** Hand-arranged order of the unpinned bots. Empty = alphabetical. A bot
   *  missing from it (created since the last move) sorts alphabetically after
   *  the ones that are in it. Shared by "All bots" and every group: one
   *  ranking, filtered per section. */
  order: string[];
  /** Hand-made groups, in the order they are shown. */
  groups: CustomGroup[];
  /** Bot name → group id. Absent = "All bots" (or pinned, if in `pins`). */
  assign: Record<string, string>;
  /** Section ids folded shut — a reserved id or a group id. */
  folded: string[];
  /**
   * Whether All agents shows its "N offline" tail (views/agents/presence.ts).
   * Its own flag rather than an id in `folded`, because its default is the
   * opposite: the tail starts CLOSED, and an id in a list of folded sections
   * cannot say "closed until someone opens it" without inverting the list's
   * meaning for one entry.
   */
  offlineOpen: boolean;
}

export const EMPTY_ARRANGEMENT: Arrangement = {
  pins: [],
  order: [],
  groups: [],
  assign: {},
  folded: [],
  offlineOpen: false,
};

/** The longest a group name can be. Long enough for "Customer success, EU". */
export const GROUP_NAME_MAX = 40;

/**
 * Move `name` to `index` in `list`, inserting it if it isn't there yet.
 *
 * `index` is an insertion point measured in the list the reader is LOOKING at
 * — the one that still contains the moved row. So when the row is moving down
 * within its own list, pulling it out first shifts every later slot up by one
 * and the raw index would land it one place too far; that is the `from <
 * index` correction, and dropping it is the classic "drag down by one does
 * nothing" bug. (Same helper as the simple rail's, kept identical on purpose.)
 */
export function moveInto(list: readonly string[], name: string, index: number): string[] {
  const from = list.indexOf(name);
  const without = list.filter((n) => n !== name);
  const raw = from !== -1 && from < index ? index - 1 : index;
  const at = Math.max(0, Math.min(without.length, raw));
  return [...without.slice(0, at), name, ...without.slice(at)];
}

/** Where a bot currently lives. */
export function homeOf(a: Arrangement, name: string): BotHome {
  if (a.pins.includes(name)) return "pinned";
  const g = a.assign[name];
  if (g && a.groups.some((x) => x.id === g)) return { group: g };
  return "all";
}

/**
 * Every unpinned bot in the reader's arrangement: hand-ranked first, then
 * alphabetical for anything the ranking has never seen. Sections filter this
 * one list, so a bot keeps its place when it moves between them.
 */
export function rankedUnpinned(a: Arrangement, agents: readonly string[]): string[] {
  const pinned = new Set(a.pins);
  const rank = new Map(a.order.map((n, i) => [n, i] as const));
  return agents
    .filter((n) => !pinned.has(n))
    .sort(
      (x, y) =>
        (rank.get(x) ?? Number.MAX_SAFE_INTEGER) - (rank.get(y) ?? Number.MAX_SAFE_INTEGER) ||
        x.localeCompare(y),
    );
}

/** The bots a section shows, in order. `agents` is the live roster. */
export function membersOf(a: Arrangement, home: BotHome, agents: readonly string[]): string[] {
  if (home === "pinned") {
    const live = new Set(agents);
    return a.pins.filter((n) => live.has(n));
  }
  const want = home === "all" ? undefined : home.group;
  return rankedUnpinned(a, agents).filter((n) => {
    const g = a.assign[n];
    const inGroup = g !== undefined && a.groups.some((x) => x.id === g);
    return want === undefined ? !inGroup : g === want;
  });
}

// ── pins ───────────────────────────────────────────────────────────────────

export function togglePin(a: Arrangement, name: string): Arrangement {
  if (a.pins.includes(name)) return { ...a, pins: a.pins.filter((n) => n !== name) };
  // Pinning takes the bot out of its group: one home per bot.
  const { [name]: _gone, ...assign } = a.assign;
  return { ...a, pins: [...a.pins, name], assign };
}

// ── groups ─────────────────────────────────────────────────────────────────

export type GroupNameError = "empty" | "duplicate" | "reserved";

/** Trim and validate a group name. Returns the clean name or the reason. */
export function cleanGroupName(
  a: Arrangement,
  raw: string,
  exceptId?: string,
): { name: string } | { error: GroupNameError } {
  const name = raw.trim().slice(0, GROUP_NAME_MAX);
  if (!name) return { error: "empty" };
  const lower = name.toLowerCase();
  // The reserved sections' *labels* vary by language, so the ids are the only
  // stable thing to refuse — a group literally named "pinned" would still
  // render, it would just be a poor name. Nothing to protect there.
  if (a.groups.some((g) => g.id !== exceptId && g.name.toLowerCase() === lower)) {
    return { error: "duplicate" };
  }
  return { name };
}

function mintGroupId(a: Arrangement): string {
  // Time-ordered and unique within one browser, which is the only scope this
  // arrangement has. Not a name — names are renamed.
  let id = `g-${Date.now().toString(36)}`;
  let n = 0;
  while (a.groups.some((g) => g.id === id)) id = `g-${Date.now().toString(36)}-${++n}`;
  return id;
}

export function createGroup(
  a: Arrangement,
  raw: string,
): { next: Arrangement; group: CustomGroup } | { error: GroupNameError } {
  const clean = cleanGroupName(a, raw);
  if ("error" in clean) return clean;
  const group = { id: mintGroupId(a), name: clean.name };
  return { next: { ...a, groups: [...a.groups, group] }, group };
}

export function renameGroup(
  a: Arrangement,
  id: string,
  raw: string,
): { next: Arrangement } | { error: GroupNameError | "missing" } {
  if (!a.groups.some((g) => g.id === id)) return { error: "missing" };
  const clean = cleanGroupName(a, raw, id);
  if ("error" in clean) return clean;
  return { next: { ...a, groups: a.groups.map((g) => (g.id === id ? { ...g, name: clean.name } : g)) } };
}

export type DeleteGroupError = "missing" | "not_empty" | "reserved";

/**
 * Delete a group. Refused while it still holds a bot that EXISTS: emptiness
 * is measured against the live roster, so a stale assignment left behind by a
 * deleted bot never locks a group the reader can see is empty. The reserved
 * sections are refused outright — they are not groups, they are the rail.
 */
export function deleteGroup(
  a: Arrangement,
  id: string,
  agents: readonly string[],
): { next: Arrangement } | { error: DeleteGroupError } {
  if ((RESERVED_SECTIONS as readonly string[]).includes(id)) return { error: "reserved" };
  if (!a.groups.some((g) => g.id === id)) return { error: "missing" };
  if (membersOf(a, { group: id }, agents).length > 0) return { error: "not_empty" };
  // Drop the stale assignments too, so nothing dangles.
  const assign: Record<string, string> = {};
  for (const [n, g] of Object.entries(a.assign)) if (g !== id) assign[n] = g;
  return {
    next: {
      ...a,
      groups: a.groups.filter((g) => g.id !== id),
      assign,
      folded: a.folded.filter((f) => f !== id),
    },
  };
}

/** Put a bot in a group, under All bots, or in Pinned — leaving wherever it was. */
export function moveTo(a: Arrangement, name: string, home: BotHome): Arrangement {
  const pins = a.pins.filter((n) => n !== name);
  const { [name]: _gone, ...assign } = a.assign;
  if (home === "pinned") return { ...a, pins: [...pins, name], assign };
  if (home === "all") return { ...a, pins, assign };
  if (!a.groups.some((g) => g.id === home.group)) return a;
  return { ...a, pins, assign: { ...assign, [name]: home.group } };
}

// ── order ──────────────────────────────────────────────────────────────────

/**
 * Move a bot one place up or down among its section's siblings. The
 * keyboard/touch route to rearranging — HTML5 drag-and-drop reaches neither a
 * screen reader nor a phone, so a list that can ONLY be arranged by dragging
 * cannot be arranged at all by a good share of the people using it.
 *
 * Unpinned bots share one ranking across All bots and every group, so the
 * move is computed among the section's siblings and then written back into
 * that one list: the bot lands just past (or before) its neighbouring sibling,
 * and every bot in every other section keeps its rank.
 */
export function nudge(
  a: Arrangement,
  name: string,
  delta: -1 | 1,
  agents: readonly string[],
  /**
   * The rows the reader sees beside this one, when that is not the whole
   * section: All agents shows its awake band and its offline tail as two runs
   * (presence.ts), so a step must be measured inside the run the row is in, or
   * a move "up" past a hidden offline agent looks like nothing happened.
   */
  band?: readonly string[],
): Arrangement {
  const home = homeOf(a, name);
  if (home === "pinned") {
    const at = a.pins.indexOf(name);
    if (at === -1) return a;
    return { ...a, pins: moveInto(a.pins, name, at + (delta > 0 ? 2 : -1)) };
  }
  const ranked = rankedUnpinned(a, agents);
  const siblings = band ?? membersOf(a, home, agents);
  const at = siblings.indexOf(name);
  const neighbour = siblings[at + delta];
  if (at === -1 || neighbour === undefined) return a;
  const target = ranked.indexOf(neighbour);
  return { ...a, order: moveInto(ranked, name, target + (delta > 0 ? 1 : 0)) };
}

// ── folds ──────────────────────────────────────────────────────────────────

export function toggleFold(a: Arrangement, id: string): Arrangement {
  return a.folded.includes(id)
    ? { ...a, folded: a.folded.filter((f) => f !== id) }
    : { ...a, folded: [...a.folded, id] };
}

export function setOfflineOpen(a: Arrangement, open: boolean): Arrangement {
  return a.offlineOpen === open ? a : { ...a, offlineOpen: open };
}

// ── (de)serialisation ──────────────────────────────────────────────────────

/** Read anything back into a valid arrangement; junk fields read as empty. */
export function parseArrangement(raw: unknown): Arrangement {
  if (!raw || typeof raw !== "object") return EMPTY_ARRANGEMENT;
  const o = raw as Record<string, unknown>;
  const names = (v: unknown): string[] =>
    Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  const groups: CustomGroup[] = Array.isArray(o.groups)
    ? o.groups
        .filter(
          (g): g is CustomGroup =>
            !!g && typeof g === "object" && typeof (g as CustomGroup).id === "string" &&
            typeof (g as CustomGroup).name === "string",
        )
        .map((g) => ({ id: g.id, name: g.name }))
    : [];
  const assign: Record<string, string> = {};
  if (o.assign && typeof o.assign === "object") {
    for (const [k, v] of Object.entries(o.assign as Record<string, unknown>)) {
      if (typeof v === "string") assign[k] = v;
    }
  }
  return {
    pins: names(o.pins),
    order: names(o.order),
    groups,
    assign,
    folded: names(o.folded),
    // Only an explicit `true` opens it: an arrangement saved before the flag
    // existed (or a junk value) reads as the default, closed.
    offlineOpen: o.offlineOpen === true,
  };
}
