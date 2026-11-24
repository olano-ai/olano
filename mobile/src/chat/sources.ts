/**
 * Where a conversation came from, as the coloured badges the dashboard's
 * conversation list shows: SCHEDULED, AGENT, CORTEX... for how it started,
 * TELEGRAM, WHATSAPP... for the messenger it arrived on, MOBILE for a
 * conversation had in this app, and DASHBOARD for one in the dashboard.
 *
 * The channel vocabulary (names, which channels are messengers, their tones)
 * is the dashboard's own `channelMeta.ts`, imported rather than copied, so a
 * platform added there gets its badge here too. Pure, for `node --test`.
 */

import { CHANNEL_NAMES, CHANNEL_TONES, isPlatformChannel } from "../shared/dashboard.ts";

export interface SourceBadge {
  label: string;
  /** Picks the colour: `.olano-source-<tone>` in app.css. */
  tone: string;
}

/** The origins the dashboard badges, with its words and colours. */
const ORIGINS: Record<string, SourceBadge> = {
  scheduled: { label: "Scheduled", tone: "scheduled" },
  heartbeat: { label: "Heartbeat", tone: "scheduled" },
  peer: { label: "Agent", tone: "peer" },
  autonomous: { label: "Autonomous", tone: "autonomous" },
  cortex: { label: "Cortex", tone: "cortex" },
  external: { label: "External", tone: "external" },
};

/** The surfaces that are not messengers, by the runtime's channel names. */
const SURFACES: Record<string, string> = {
  dashboard: "Dashboard",
  api: "Dashboard",
  cli: "Terminal",
  scheduler: "Scheduler",
  webhook: "Webhook",
};

function channelName(channel: string): string {
  return CHANNEL_NAMES[channel] || channel.charAt(0).toUpperCase() + channel.slice(1);
}

/**
 * The badges for one conversation: its origin when it did not start as a
 * chat, then the messenger it arrived on; a plain chat says Dashboard.
 */
export function sourceBadges(thread: { origin?: string; channel?: string }): SourceBadge[] {
  const out: SourceBadge[] = [];
  const origin = ORIGINS[(thread.origin || "").toLowerCase()];
  if (origin) out.push(origin);
  const channel = (thread.channel || "").toLowerCase();
  if (isPlatformChannel(channel)) {
    out.push({ label: channelName(channel), tone: CHANNEL_TONES[channel] });
  } else if (!origin) {
    out.push({ label: SURFACES[channel] || (channel ? channelName(channel) : "Dashboard"), tone: "neutral" });
  }
  return out;
}
