// The channel vocabulary, with no React in it.
//
// `channels.tsx` draws the pills; this module is the data and the predicates
// underneath them, split out so the pure thread logic that depends on them
// (`threadFilters.ts`, `threadPick.ts`) can run under Node's test runner, which
// strips types but does not compile JSX. `channels.tsx` re-exports everything
// here, so existing importers see one module.

/** Translator signature, matching ChatView's own local alias. */
export type T = (key: string, vars?: Record<string, string>) => string;

/**
 * The values match `olano/platforms/config.py:Platform` plus the four surfaces
 * that aren't messaging platforms: `dashboard`, `scheduler`, `cli`, `webhook`.
 */

/** Product names carry their own casing — a naive capitalize gives "Whatsapp". */
export const CHANNEL_NAMES: Record<string, string> = {
  whatsapp: "WhatsApp",
  // The official Business API via Twilio Conversations: the customer is on
  // the same WhatsApp client, so it reads as WhatsApp with the route named.
  whatsapp_twilio: "WhatsApp (Twilio)",
  // The official Business API through Meta's own Cloud API.
  whatsapp_meta_api: "WhatsApp (Meta Cloud API)",
  imessage: "iMessage",
  bluebubbles: "BlueBubbles",
  wecom: "WeCom",
  wecom_callback: "WeCom",
  weixin: "WeChat",
  homeassistant: "Home Assistant",
  dingtalk: "DingTalk",
  qqbot: "QQ Bot",
  api_server: "API",
  sms: "SMS",
  // The inbound protocols — acronyms, so a naive capitalize gives "Mcp".
  mcp: "MCP",
  a2a: "A2A",
};

/**
 * Channels that earn a coloured pill: the messaging platforms.
 *
 * `dashboard`, `cli`, `scheduler` and `webhook` are deliberately absent from
 * the RAIL (see `channelPillTone`) — an ordinary dashboard chat is the baseline
 * every other row is read against, the same reasoning that keeps `interactive`
 * out of the origin pills. They DO get a tone here because inside a
 * conversation a "this one turn came from the dashboard" badge is exactly the
 * information the reader wants.
 */
export const CHANNEL_TONES: Record<string, string> = {
  telegram: "telegram",
  whatsapp: "whatsapp",
  whatsapp_twilio: "whatsapp",
  whatsapp_meta_api: "whatsapp",
  slack: "slack",
  discord: "discord",
  weixin: "weixin",
  wecom: "wecom",
  wecom_callback: "wecom",
  feishu: "feishu",
  dingtalk: "dingtalk",
  signal: "signal",
  matrix: "matrix",
  mattermost: "mattermost",
  email: "email",
  sms: "sms",
  bluebubbles: "bluebubbles",
  imessage: "bluebubbles",
  qqbot: "qqbot",
  debox: "debox",
  homeassistant: "homeassistant",
  webhook: "webhook",
  // Inbound MCP / A2A calls get a coloured pill like a platform does: which
  // protocol an outside system used is exactly what the row must say.
  mcp: "mcp",
  a2a: "a2a",
  // The Olano mobile app (core/thread_labels.CHANNEL_MOBILE): a conversation
  // a person had on their phone. A platform pill, so it gets its own filter
  // chip, and a human conversation for `pickLatestThread`.
  mobile: "mobile",
  api_server: "neutral",
  local: "neutral",
  cli: "neutral",
  dashboard: "neutral",
};

/**
 * The inbound protocols. They wear a platform-style pill (the protocol is what
 * the row must say) but they are not messengers: nobody is waiting on the
 * other end of an MCP call the way a person waits on WhatsApp, which is why
 * `pickLatestThread` ranks them below a real conversation.
 */
export const PROTOCOL_CHANNELS: ReadonlySet<string> = new Set(["mcp", "a2a"]);

/** True when this channel is a messaging platform the rail should badge. */
export function isPlatformChannel(channel: string): boolean {
  const tone = CHANNEL_TONES[channel];
  return !!tone && tone !== "neutral";
}

/** A messaging platform a PERSON talks on — a platform channel minus the protocols. */
export function isMessengerChannel(channel: string): boolean {
  return isPlatformChannel(channel) && !PROTOCOL_CHANNELS.has(channel);
}

/**
 * Channels keep their own product name rather than being translated;
 * "dashboard"/"scheduler"/"cli"/"webhook" are ours, so they get real strings.
 *
 * The membership check matters: `useT` renders a missing key as the raw key
 * string, so calling `t()` on an arbitrary channel would print
 * "thread.source.telegram" into the UI.
 */
export function channelLabel(channel: string, t: T): string {
  if (!channel) return "";
  if (channel === "dashboard" || channel === "scheduler" || channel === "cli" || channel === "webhook")
    return t(`thread.source.${channel}`);
  return CHANNEL_NAMES[channel] || channel.charAt(0).toUpperCase() + channel.slice(1);
}
