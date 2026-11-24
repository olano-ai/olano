// Where a chat came from, as a FILTER — the answer to a rail drowning in
// scheduled runs. Shared by both shells: the simple rail and its all-chats
// panel, and the dashboard sidebar's per-agent chat list.
//
// Every persisted thread already carries `origin` / `channel` derived
// server-side (core/thread_labels.py); this module folds that pair into ONE
// exclusive bucket per chat, so the buckets partition the list and their
// counts add up to the total:
//
//   scheduled — origin "scheduled" | "heartbeat" (a run the scheduler started,
//               wherever it was delivered — a scheduled Telegram fan-out files
//               here, not under Telegram, because "hide the scheduler spam" is
//               the whole reason this filter exists)
//   agents    — origin "peer" | "autonomous" (another bot, or the bot's own
//               background thinking)
//   ch:<x>    — a messaging platform conversation (Telegram, WhatsApp, …)
//   chats     — everything else: conversations a person typed here or in the
//               dashboard (drafts with no thread yet included)
//
// The chip row lives in ThreadFilterChips.tsx so this model stays JSX-free and
// testable under Node.

import { channelLabel, isPlatformChannel, type T } from "./channelMeta.ts";

/** "all" | "chats" | "scheduled" | "agents" | "ch:<channel>" */
export type ThreadFilterId = string;

export const FILTER_ALL: ThreadFilterId = "all";

/** The one exclusive bucket a chat belongs to. Origin outranks channel. */
export function threadBucket(origin: string, channel: string): ThreadFilterId {
  if (origin === "scheduled" || origin === "heartbeat") return "scheduled";
  if (origin === "peer" || origin === "autonomous") return "agents";
  if (isPlatformChannel(channel)) return `ch:${channel}`;
  return "chats";
}

export interface ThreadFilterOption {
  id: ThreadFilterId;
  count: number;
}

/**
 * The chips worth drawing for this list: "All" plus every bucket that actually
 * holds something, in a fixed order (chats · platforms A→Z · scheduled ·
 * agents) so the row never reshuffles as chats arrive.
 */
export function buildFilterOptions(
  rows: { origin: string; channel: string }[],
): ThreadFilterOption[] {
  const counts = new Map<ThreadFilterId, number>();
  for (const row of rows) {
    const id = threadBucket(row.origin, row.channel);
    counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  const platforms = [...counts.keys()]
    .filter((id) => id.startsWith("ch:"))
    .sort((a, b) => a.localeCompare(b));
  const ordered: ThreadFilterId[] = ["chats", ...platforms, "scheduled", "agents"];
  const options: ThreadFilterOption[] = [{ id: FILTER_ALL, count: rows.length }];
  for (const id of ordered) {
    const count = counts.get(id);
    if (count) options.push({ id, count });
  }
  return options;
}

/** Apply a filter. "all" (or an id the list no longer holds) hides nothing. */
export function filterThreadRows<R extends { origin: string; channel: string }>(
  rows: R[],
  filter: ThreadFilterId,
): R[] {
  if (filter === FILTER_ALL) return rows;
  return rows.filter((r) => threadBucket(r.origin, r.channel) === filter);
}

// ── persistence ─────────────────────────────────────────────────────────────
// Per bot, per browser: the bot whose scheduler is chatty is the one whose
// filter you set, and that choice should survive a reload without following
// the account to another device — same storage posture as pins and the theme.
// One key for both shells on purpose: it is a preference about the BOT, and a
// person who quiets one agent's scheduler in the simple view expects it quiet
// in the dashboard too. (The name predates the dashboard's list; kept so an
// existing choice survives the upgrade.)
const FILTER_KEY = "lc.simple.threadFilter";

export function readThreadFilter(bot: string): ThreadFilterId {
  try {
    const raw = localStorage.getItem(FILTER_KEY);
    const map = raw ? (JSON.parse(raw) as unknown) : null;
    if (map && typeof map === "object") {
      const v = (map as Record<string, unknown>)[bot];
      if (typeof v === "string" && v) return v;
    }
  } catch {
    // storage disabled or corrupt — everyone starts on "all".
  }
  return FILTER_ALL;
}

export function writeThreadFilter(bot: string, filter: ThreadFilterId): void {
  try {
    const raw = localStorage.getItem(FILTER_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    const map: Record<string, string> =
      parsed && typeof parsed === "object" ? { ...(parsed as Record<string, string>) } : {};
    if (filter === FILTER_ALL) delete map[bot];
    else map[bot] = filter;
    localStorage.setItem(FILTER_KEY, JSON.stringify(map));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

// ── labels ──────────────────────────────────────────────────────────────────
// `t` is the chat-strings translator (views/chat/strings.ts): the `filter.*`
// keys and the channel product names live there, shared by both shells.

/** Visible label for one chip. Platform chips carry their product name. */
export function filterLabel(id: ThreadFilterId, t: T): string {
  if (id.startsWith("ch:")) return channelLabel(id.slice(3), t);
  return t(`filter.${id}`);
}

/** The hover explanation for one chip — every control explains itself. */
export function filterHelp(id: ThreadFilterId, t: T): string {
  if (id.startsWith("ch:")) return t("filter.platformHelp", { channel: channelLabel(id.slice(3), t) });
  return t(`filter.${id}Help`);
}
