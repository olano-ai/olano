/**
 * The app's own links (`olano://`), opened by the widgets, the Control
 * Center controls and Siri's "Talk to an Agent":
 *
 * - `olano://talk?agent=<name>`: a new conversation with the agent in
 *   Hands-Free Mode.
 * - `olano://open?agent=<name>[&thread=<id>][&compose=<mode>]`: the agent's
 *   conversation: the one named, a new one for `thread=new`, and with no
 *   thread named what tapping the agent in Chats opens (the last
 *   conversation the person had with it, else a new one: an `agent` action,
 *   resolved by App.tsx since it takes a read of the deployment). `compose`
 *   readies the composer on arrival: `text` puts the keyboard up, `camera`
 *   opens the camera, `photos` the photo picker (the Ask widget's buttons).
 * - `olano://open?tab=<chats|inbox|cortex|scheduled|settings>`: a screen.
 *
 * `olano://share` belongs to the share extension (src/native/shareIn.ts).
 * Pure: no DOM, no network, so `node --test` covers it.
 */

export type ComposeMode = "text" | "camera" | "photos";

export type DeepLinkAction =
  | { kind: "talk"; agent: string }
  | { kind: "agent"; agent: string }
  | { kind: "route"; route: string };

const TABS: Record<string, string> = {
  chats: "/chats",
  inbox: "/inbox",
  cortex: "/cortex/pulse",
  scheduled: "/settings/scheduled",
  settings: "/settings",
};

const COMPOSE_MODES: ComposeMode[] = ["text", "camera", "photos"];

function parse(raw: string): URL | null {
  try {
    const url = new URL(raw);
    return url.protocol === "olano:" ? url : null;
  } catch {
    return null;
  }
}

/** The host of `olano://host?...`, which some parsers put in the pathname. */
function hostOf(url: URL): string {
  return (url.host || url.pathname.replace(/^\/+/, "").split("/")[0] || "").toLowerCase();
}

/** The compose mode a query string names, or null for none or an unknown one. */
export function composeModeOf(search: string): ComposeMode | null {
  const value = (new URLSearchParams(search).get("compose") ?? "").trim().toLowerCase();
  return COMPOSE_MODES.includes(value as ComposeMode) ? (value as ComposeMode) : null;
}

export function deepLinkAction(raw: string): DeepLinkAction | null {
  const url = parse(raw);
  if (!url) return null;
  const host = hostOf(url);
  const agent = (url.searchParams.get("agent") ?? "").trim();
  if (host === "talk") return agent ? { kind: "talk", agent } : null;
  if (host !== "open") return null;
  if (agent) {
    const thread = (url.searchParams.get("thread") ?? "").trim();
    const compose = composeModeOf(url.search);
    // No thread named: the agent as Chats opens it. With a compose mode a new
    // conversation, since the composer can only be readied in one.
    if (!thread && !compose) return { kind: "agent", agent };
    const path = `/chats/${encodeURIComponent(agent)}/${thread ? encodeURIComponent(thread) : "new"}`;
    return { kind: "route", route: compose ? `${path}?compose=${compose}` : path };
  }
  const tab = (url.searchParams.get("tab") ?? "").trim().toLowerCase();
  const route = TABS[tab];
  return route ? { kind: "route", route } : null;
}
