/**
 * An agent's conversation list, filtered by where each one came from: the
 * dashboard's own buckets (views/chat/threadFilters.ts), so a thread counts
 * under exactly one chip in both places. A scheduled run delivered on
 * Telegram files under Scheduled; a chat in this app under Mobile.
 *
 * Pure apart from the remembered choice, so `node --test` covers it.
 */

import { buildFilterOptions, CHANNEL_NAMES, FILTER_ALL, type ThreadFilterId } from "../shared/dashboard.ts";

export interface FilterChip {
  id: ThreadFilterId;
  label: string;
  count: number;
}

const LABELS: Record<string, string> = {
  [FILTER_ALL]: "All",
  // The dashboard calls this bucket "Chats"; here, beside a Mobile chip, it
  // is the conversations had in the dashboard.
  chats: "Dashboard",
  scheduled: "Scheduled",
  agents: "Agents",
};

export function chipLabel(id: ThreadFilterId): string {
  if (id.startsWith("ch:")) {
    const channel = id.slice(3);
    return CHANNEL_NAMES[channel] || channel.charAt(0).toUpperCase() + channel.slice(1);
  }
  return LABELS[id] ?? id;
}

/** The chips worth showing: All, then every bucket that holds something. */
export function filterChips(threads: readonly { origin?: string; channel?: string }[]): FilterChip[] {
  const rows = threads.map((t) => ({ origin: t.origin ?? "", channel: t.channel ?? "" }));
  return buildFilterOptions(rows).map((option) => ({ ...option, label: chipLabel(option.id) }));
}
