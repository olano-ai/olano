// The org chart's channel vocabulary, with no React in it.
//
// An agent card shows one small chip per messaging connector the agent is
// configured with. This module decides which mark a configured `type` wears,
// what the chip is called, how many chips fit a card of a given width, and
// whether a live turn (a `thread_activity` frame's `channel`) arrived on that
// connector. `ChannelChip.tsx` draws it; split out so node's `--test` can
// import it (it strips types but does not compile JSX).

import { CHANNEL_NAMES } from "../chat/channelMeta.ts";

/** Wire states from `olano/runtime/channel_status.py`. */
export type ChannelStatus = "connected" | "connecting" | "error" | "stopped";

/** One row of an agent's `channels` in `GET /api/org-chart`. */
export interface OrgChannel {
  type: string;
  status: ChannelStatus | string;
  detail?: string | null;
}

/**
 * The marks the chip can draw. Messengers people recognise by their logo get
 * a brand chip; the rest share a neutral chip with a descriptive icon, where a
 * made-up logo would mislead more than it helps.
 */
export type ChannelBrand =
  | "whatsapp"
  | "telegram"
  | "discord"
  | "slack"
  | "signal"
  | "imessage"
  | "wechat"
  | "email"
  | "sms"
  | "matrix"
  | "mattermost"
  | "wecom"
  | "feishu"
  | "dingtalk"
  | "qq"
  | "homeassistant"
  | "webhook"
  | "api"
  | "debox"
  | "generic";

const PLATFORM_PREFIX = "platform:";

/** `platform:telegram` → `telegram`; a legacy `telegram` stays as it is. */
export function channelSlug(type: string): string {
  const t = (type || "").trim().toLowerCase();
  return t.startsWith(PLATFORM_PREFIX) ? t.slice(PLATFORM_PREFIX.length) : t;
}

const BRAND_BY_SLUG: Record<string, ChannelBrand> = {
  whatsapp: "whatsapp",
  // The two official Business API routes reach the same WhatsApp client.
  whatsapp_twilio: "whatsapp",
  whatsapp_meta_api: "whatsapp",
  telegram: "telegram",
  discord: "discord",
  slack: "slack",
  signal: "signal",
  bluebubbles: "imessage",
  imessage: "imessage",
  weixin: "wechat",
  email: "email",
  imap: "email",
  sms: "sms",
  matrix: "matrix",
  mattermost: "mattermost",
  wecom: "wecom",
  wecom_callback: "wecom",
  feishu: "feishu",
  dingtalk: "dingtalk",
  qqbot: "qq",
  homeassistant: "homeassistant",
  webhook: "webhook",
  api_server: "api",
  debox: "debox",
};

/** The mark a configured channel type wears; `generic` for anything unknown. */
export function channelBrand(type: string): ChannelBrand {
  return BRAND_BY_SLUG[channelSlug(type)] ?? "generic";
}

// Names `CHANNEL_NAMES` leaves to a capitalize but the chart wants spelled out.
const EXTRA_NAMES: Record<string, string> = {
  imap: "Email (IMAP)",
  feishu: "Feishu / Lark",
};

/** The product name a chip's tooltip and the drawer print, e.g. "WhatsApp". */
export function channelDisplayName(type: string): string {
  const slug = channelSlug(type);
  if (!slug) return "";
  return CHANNEL_NAMES[slug] || EXTRA_NAMES[slug] || slug.charAt(0).toUpperCase() + slug.slice(1);
}

/**
 * Whether a live turn on `activityChannel` (the `channel` of a
 * `thread_activity` frame, a bare platform value like `whatsapp_twilio`)
 * arrived through the configured connector `type`. Exact slug match on
 * purpose: a Twilio WhatsApp turn must not pulse the Baileys WhatsApp chip.
 */
export function activityMatchesChannel(activityChannel: string, type: string): boolean {
  const ch = (activityChannel || "").trim().toLowerCase();
  if (!ch) return false;
  const slug = channelSlug(type);
  return slug === ch || (slug === "imap" && ch === "email");
}

/** Normalised status: an unknown wire value reads as `connected` (no badge). */
export function channelStatus(c: OrgChannel): ChannelStatus {
  const s = c.status;
  return s === "connecting" || s === "error" || s === "stopped" ? s : "connected";
}

const STATUS_RANK: Record<ChannelStatus, number> = { connected: 0, stopped: 1, connecting: 2, error: 3 };

/**
 * The status most worth a look among `channels` (`error` > `connecting` >
 * `stopped` > `connected`). The "+N" chip wears it, so a broken connector that
 * did not fit on a narrow card still shows its dot.
 */
export function worstStatus(channels: OrgChannel[]): ChannelStatus {
  let worst: ChannelStatus = "connected";
  for (const c of channels) {
    const s = channelStatus(c);
    if (STATUS_RANK[s] > STATUS_RANK[worst]) worst = s;
  }
  return worst;
}

// Chip geometry, shared with the card CSS in OrgChartView so the overflow
// arithmetic and what the browser draws can never disagree.
export const CHIP_SIZE = 18;
export const CHIP_GAP = 4;
/** Vertical space the chip row adds to a card: its top margin plus one chip. */
export const CHIP_ROW_H = 5 + CHIP_SIZE;
/** Horizontal chrome of an agent card: 10px padding each side + 1px borders. */
export const CARD_CHROME_X = 22;

/**
 * How many of `count` chips a card `cardWidth` wide shows, and how many fold
 * into a trailing "+N" chip. The "+N" takes a slot of its own, so when the row
 * overflows one fewer real chip is shown. Never shows fewer than one slot.
 */
export function chipLayout(count: number, cardWidth: number): { shown: number; overflow: number } {
  if (count <= 0) return { shown: 0, overflow: 0 };
  const inner = Math.max(0, cardWidth - CARD_CHROME_X);
  const slots = Math.max(1, Math.floor((inner + CHIP_GAP) / (CHIP_SIZE + CHIP_GAP)));
  if (count <= slots) return { shown: count, overflow: 0 };
  const shown = Math.max(0, slots - 1);
  return { shown, overflow: count - shown };
}
