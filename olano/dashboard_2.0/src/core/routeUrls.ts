// The dashboard's URL grammar, in one pure module (no React, no window) so node's
// --test can pin every rule and both shells read URLs the same way.
//
//   /<view>[/<agent>][/<section>]?<selection>
//
// The URL says WHERE you are: which agent, which sub-tab, which open item. It
// never holds what you are typing. The agent sits in the path whenever a view
// has one:
//
//   AGENT_VIEWS   the whole page is one agent's (Chat, Memory, Cortex, ...). The
//                 segment is the app-wide agent selection itself, and a bare
//                 `/chat` is canonicalised to `/chat/<agent>`.
//   FILTER_VIEWS  the page spans the fleet and an agent only narrows it (Files,
//                 Scheduler, ...). The segment is optional: no segment = all.
//   /agents/:name/:tab, /insights/:tab[/:agent], /calendar/:mode, /groups/:id
//                 the remaining shapes, each spelled out in parseRoute().
//
// Views read their agent/section through parseRoute() on the LOCATION rather
// than through useParams(): the simple shell mounts some of these views as
// panels with no route table above them, where useParams() is empty.

/** Views about one agent. The agent segment is the app-wide selection. */
export const AGENT_VIEWS = ["chat", "memory", "notes", "knowledge", "terminal", "cortex"] as const;

/** Fleet-wide views an agent segment only narrows. No segment means "all agents". */
export const FILTER_VIEWS = ["files", "connections", "scheduler", "logs", "overview"] as const;

export type AgentView = (typeof AGENT_VIEWS)[number];

export function isAgentView(view: string): view is AgentView {
  return (AGENT_VIEWS as readonly string[]).includes(view);
}

export function isFilterView(view: string): boolean {
  return (FILTER_VIEWS as readonly string[]).includes(view);
}

export interface RouteParts {
  /** First path segment ("" for "/"). */
  view: string;
  /** The agent the path names, decoded, or null. */
  agent: string | null;
  /** The sub-tab / section / mode the path names, or null. */
  section: string | null;
}

function decodeSeg(seg: string | undefined): string | null {
  if (!seg) return null;
  try {
    return decodeURIComponent(seg);
  } catch {
    // A malformed escape is not a name anyone has; treat it as absent rather
    // than throwing out of a render.
    return null;
  }
}

function segments(pathname: string): string[] {
  return pathname.split("?")[0].split("#")[0].split("/").filter(Boolean);
}

export function parseRoute(pathname: string): RouteParts {
  const segs = segments(pathname);
  const view = segs[0] ?? "";
  if (isAgentView(view) || isFilterView(view) || view === "agents") {
    return { view, agent: decodeSeg(segs[1]), section: decodeSeg(segs[2]) };
  }
  if (view === "insights") {
    // The tab comes first: /insights/drill/<agent> is the drill-down narrowed
    // to one agent, /insights/overview has no agent at all.
    return { view, agent: decodeSeg(segs[2]), section: decodeSeg(segs[1]) };
  }
  return { view, agent: null, section: decodeSeg(segs[1]) };
}

const enc = encodeURIComponent;

/** Build a path in the grammar above. Search/hash are the caller's to add. */
export function viewPath(view: string, opts: { agent?: string | null; section?: string | null } = {}): string {
  const agent = opts.agent || null;
  const section = opts.section || null;
  if (view === "insights") {
    if (!section) return "/insights";
    return agent ? `/insights/${enc(section)}/${enc(agent)}` : `/insights/${enc(section)}`;
  }
  if (isAgentView(view) || isFilterView(view) || view === "agents") {
    if (!agent) return `/${view}`;
    return section ? `/${view}/${enc(agent)}/${enc(section)}` : `/${view}/${enc(agent)}`;
  }
  return section ? `/${view}/${enc(section)}` : `/${view}`;
}

/** A chat link: the agent in the path, the thread (opaque, per-agent) in the query. */
export function chatPath(agent: string, threadId?: string | null): string {
  const base = viewPath("chat", { agent });
  return threadId ? `${base}?thread=${enc(threadId)}` : base;
}

/** An agent's configuration page, optionally on one of its tabs. */
export function agentDetailPath(name: string, tab?: string | null): string {
  return viewPath("agents", { agent: name, section: tab && tab !== "overview" ? tab : null });
}

/** The Files view, optionally opened on one path inside one agent's workspace. */
export function filesPath(agent?: string | null, path?: string | null): string {
  const base = viewPath("files", { agent });
  const p = (path ?? "").replace(/^\/+|\/+$/g, "");
  return agent && p ? `${base}?path=${enc(p).replace(/%2F/g, "/")}` : base;
}

/**
 * The key a route's page identity is remembered under: the shell remounts its
 * error boundary, restores scroll and closes the mobile drawer when THIS
 * changes, not when the raw pathname does.
 *
 * It is exactly the pathname each view had before agents and sections moved
 * into the path, so those three behaviours are unchanged: switching agent on
 * /chat never remounted the chat (the store holds every conversation), and an
 * agent page remounted per agent but never per tab. Keying on the raw
 * pathname would remount a page on every sub-tab click and reset its scroll.
 */
export function shellKey(pathname: string): string {
  const segs = segments(pathname);
  if (!segs.length) return "/";
  const [view, second] = segs;
  if (view === "agents") return second ? `/agents/${second}` : "/agents";
  if (view === "groups") return second ? `/groups/${second}` : "/groups";
  return `/${view}`;
}

export interface CanonicalContext {
  /** The agent a bare per-agent URL should land on ("" while unknown). */
  agent: string;
  /** Whether `name` is in the roster; null while the roster is still loading. */
  known: ((name: string) => boolean) | null;
  /** Where an unknown agent falls back to ("" when the roster is empty). */
  fallback: string;
}

export interface CanonicalResult {
  to: string;
  /** Set when the URL named an agent that is not in the roster. */
  unknownAgent?: string;
}

function withQuery(path: string, params: URLSearchParams, hash: string): string {
  // Slashes stay readable (?path=reports/q3.md, as filesPath() writes them);
  // a query value may carry them unescaped and every reader decodes both.
  const q = params.toString().replace(/%2F/gi, "/");
  return `${path}${q ? `?${q}` : ""}${hash}`;
}

/**
 * Where a URL should be instead, or null when it is already canonical.
 *
 * Every link already in circulation keeps working through this: notification
 * hrefs the runtime stored (`/agents/<a>?tab=mcp`, `/cortex?agent=<a>`,
 * `/files?agent=<a>&path=…`, `/connections?agent=<a>`), copied
 * `/chat?thread=…` links, bookmarks. It is applied before the view mounts, so
 * no view ever runs against a non-canonical URL.
 */
export function canonicalTarget(
  pathname: string,
  search: string,
  hash: string,
  ctx: CanonicalContext,
): CanonicalResult | null {
  const parts = parseRoute(pathname);
  const params = new URLSearchParams(search);
  const { view } = parts;

  if (view === "agents") {
    const legacyAgent = params.get("agent");
    const legacyTab = params.get("tab");
    if (!parts.agent && legacyAgent) {
      // Both legacy keys at once, so /agents?agent=a&tab=t is one hop.
      params.delete("agent");
      params.delete("tab");
      return { to: withQuery(agentDetailPath(legacyAgent, legacyTab), params, hash) };
    }
    if (parts.agent && legacyTab !== null && !parts.section) {
      params.delete("tab");
      return { to: withQuery(agentDetailPath(parts.agent, legacyTab), params, hash) };
    }
    return null;
  }

  if (!isAgentView(view) && !isFilterView(view)) return null;

  // The pre-path spelling: ?agent=<name>. Moves into the path, everything else
  // (thread, history, path, focus, share-target fields) rides along untouched.
  const legacyAgent = params.get("agent");
  if (!parts.agent && legacyAgent) {
    params.delete("agent");
    return { to: withQuery(viewPath(view, { agent: legacyAgent, section: parts.section }), params, hash) };
  }

  if (parts.agent && ctx.known && !ctx.known(parts.agent)) {
    // Deleted, renamed, or not visible to this member. Drop the selection
    // along with it: a thread, note or item id belongs to the agent it came
    // from and would 404 under any other.
    const to = isAgentView(view) && ctx.fallback ? viewPath(view, { agent: ctx.fallback, section: parts.section }) : `/${view}`;
    return { to, unknownAgent: parts.agent };
  }

  if (isAgentView(view) && !parts.agent && ctx.agent) {
    return { to: withQuery(viewPath(view, { agent: ctx.agent, section: parts.section }), params, hash) };
  }
  return null;
}

/**
 * Where switching the app-wide agent to `name` should take you from here, or
 * null when this page has no agent of its own (the selection then changes
 * without navigating). On a per-agent page the page stays and follows the
 * agent; its sub-tab is kept, its open item is not (ids are per agent).
 */
export function agentSwitchTarget(pathname: string, name: string, shell: "dashboard" | "simple"): string | null {
  const parts = parseRoute(pathname);
  if (isAgentView(parts.view)) return viewPath(parts.view, { agent: name, section: parts.section });
  if (shell === "simple" && parts.view === "") return `/?agent=${enc(name)}`;
  return null;
}

/**
 * The agent a URL names as the app-wide selection: the path segment of a
 * per-agent view, its pre-path `?agent=` spelling, and in the simple shell the
 * conversation's own `/?agent=`.
 */
export function urlAgent(pathname: string, search: string, shell: "dashboard" | "simple"): string | null {
  const parts = parseRoute(pathname);
  if (isAgentView(parts.view)) return parts.agent ?? (new URLSearchParams(search).get("agent") || null);
  if (shell === "simple" && parts.view === "") return new URLSearchParams(search).get("agent") || null;
  return null;
}

/**
 * Where the simple shell sends a dashboard URL it has no panel for. It shows
 * one thing outside its panels, the conversation at "/", so the agent and
 * thread a dashboard chat link names must survive the trip: /chat/<a>?thread=x
 * becomes /?agent=<a>&thread=x. Everything else in the query rides along.
 */
export function simpleShellFallback(pathname: string, search: string): string {
  const parts = parseRoute(pathname);
  const params = new URLSearchParams(search);
  if (isAgentView(parts.view) && parts.agent && !params.has("agent")) {
    // `agent` first, then whatever the link carried, so the result reads as
    // "this agent, this thread".
    const next = new URLSearchParams({ agent: parts.agent });
    params.forEach((v, k) => next.append(k, v));
    return `/?${next.toString()}`;
  }
  const q = params.toString();
  return q ? `/?${q}` : "/";
}
