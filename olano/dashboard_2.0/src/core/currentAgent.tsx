import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "./api";
import { subscribeStream } from "./agentStream";
import type { Agent } from "./types";
import { usePersistentState } from "./usePersistentState";
import { useToast } from "../ui/toast";
import { useI18n } from "./i18n";
import { agentSwitchTarget, urlAgent } from "./routeUrls";

// The single, app-wide "current agent" selection. One picker (in the sidebar)
// drives every agent-scoped view (Chat, Files, Memory, Cortex …). The agent
// roster is fetched once here and refreshed live on roster changes.
//
// Where the selection lives:
//   • On a per-agent page (/chat/<agent>, /cortex/<agent>, …) the URL IS the
//     selection (core/routeUrls.ts). Reading it from the location rather than
//     from state is what lets a reload, a copied link and Back/Forward land on
//     the right agent in the same render. A thread id opened under the wrong
//     agent for even one render would 404.
//   • Everywhere else it is the last agent chosen, kept for the SPA session
//     and in localStorage, so a bare /chat after a reload opens the agent you
//     were on instead of the first in the roster.
//
// setAgent() on a per-agent page navigates (a history entry, so Back returns
// to the previous agent); elsewhere it only changes the remembered choice.

export type AgentShell = "dashboard" | "simple";

export interface SetAgentOptions {
  /** false: change the selection without moving to the new agent's URL. Only
   *  for a caller about to navigate somewhere itself (one history entry). */
  navigate?: boolean;
}

interface CurrentAgentApi {
  agents: Agent[];
  agent: string;
  setAgent: (name: string, opts?: SetAgentOptions) => void;
  current: Agent | undefined;
  loading: boolean;
  /** true once a roster fetch has succeeded and none is in flight. A URL's
   *  agent is judged against the roster only while this holds. */
  settled: boolean;
  reload: () => void;
  /** The agent a URL naming an unknown one falls back to ("" when none). */
  fallback: string;
}

const Ctx = createContext<CurrentAgentApi | null>(null);

const LAST_AGENT_KEY = "olano.lastAgent";

function readLastAgent(): string {
  try {
    return localStorage.getItem(LAST_AGENT_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeLastAgent(name: string): void {
  try {
    if (name) localStorage.setItem(LAST_AGENT_KEY, name);
  } catch {
    // Private window / blocked storage: the in-session choice still works.
  }
}

// Drop the remembered agent: the next person to sign in on this browser starts
// from their own roster rather than the previous person's choice.
export function forgetLastAgent(): void {
  try {
    localStorage.removeItem(LAST_AGENT_KEY);
  } catch {
    // Blocked storage: nothing was remembered.
  }
}

export function CurrentAgentProvider({
  children,
  shell = "dashboard",
}: {
  children: ReactNode;
  shell?: AgentShell;
}) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  // Roster fetches in flight. A name the URL carries can only be judged
  // unknown against a settled roster: right after "create agent" the sidebar
  // links to /chat/<new> while the refetch that will contain it is still out.
  const [inflight, setInflight] = useState(0);
  // Whether any roster fetch has ever succeeded. A failed fetch is not a
  // roster: judging a URL's agent against an empty list after a transient
  // error would evict the reader from /chat/<agent> with a "not in your
  // roster" notice and drop the thread. The last good roster stays in place,
  // and until there is one the URL is trusted as given.
  const [rosterKnown, setRosterKnown] = useState(false);
  const [stored, setStored] = usePersistentState<string>("global.agent", readLastAgent);
  const toast = useToast();
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();

  const reload = useCallback(() => {
    // Authoritative refresh (SSE-triggered): drop the shared roster cache and
    // refetch, repopulating it for the passive cachedGet readers in the views.
    api.invalidate("/api/agents");
    setInflight((n) => n + 1);
    api
      .cachedGet<Agent[]>("/api/agents")
      .then((list) => {
        setAgents(list);
        setRosterKnown(true);
        // Keep the remembered choice if it still exists; otherwise fall back to
        // the first agent so agent-scoped views always have a valid target.
        setStored((a) => (a && list.some((x) => x.name === a) ? a : list[0]?.name ?? ""));
      })
      .catch(() => {
        // Keep whatever roster was last known (initially none).
      })
      .finally(() => {
        setLoading(false);
        setInflight((n) => n - 1);
      });
  }, [setStored]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Live roster refresh (create / lifecycle / edit broadcast agents_update; a
  // brand-new teammate also broadcasts agent_joined). Shares the app-wide
  // multiplexed /api/stream connection (see core/agentStream.ts) instead of
  // opening its own EventSource. On agent_joined we also pop a welcome toast so
  // every human in the dashboard sees the new team member arrive.
  //
  // Reloads are coalesced: the runtime broadcasts a frame per agent lifecycle
  // transition, so a restart walks ~3 frames × every agent through here, and
  // each reload() invalidates the shared roster cache and refetches. Per-frame
  // reloading would mean dozens of /api/agents round-trips against a runtime
  // that is already busy booting. The toast stays immediate — it is per-agent
  // and must not be collapsed.
  //
  // `connected` is a reload trigger too. SSE has no replay, so frames emitted
  // while the stream was down (an idle-timeout proxy, a laptop waking, the
  // runtime itself restarting) are simply lost and the picker would sit on a
  // pre-restart roster indefinitely. The server emits `connected` at the top of
  // every (re)connection, which makes it a free resync point. The first one is
  // skipped — it lands right after the mount fetch and would only duplicate it.
  useEffect(() => {
    let timer: number | undefined;
    let seenConnected = false;
    const unsubscribe = subscribeStream((f) => {
      let resync =
        f.type === "agents_update" || f.type === "agent_status" || f.type === "agent_joined";
      if (f.type === "connected") {
        resync = seenConnected; // the first one just mirrors the mount fetch
        seenConnected = true;
      }
      if (resync) {
        window.clearTimeout(timer);
        timer = window.setTimeout(reload, 300);
      }
      if (f.type === "agent_joined") {
        const d = (f.data ?? {}) as { name?: string; emoji?: string };
        if (d.name) toast.push(t("toast.agentJoined", { emoji: d.emoji || "🦁", name: d.name }));
      }
    });
    return () => {
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, [reload, toast, t]);

  // The URL's answer wins whenever it has one. While the roster is loading it
  // is trusted as given (the route canonicaliser swaps an unknown one out once
  // the roster answers); after that only a name in the roster counts.
  const settled = !loading && inflight === 0 && rosterKnown;
  const fromUrl = urlAgent(location.pathname, location.search, shell);
  const urlValid = !!fromUrl && (!settled || agents.some((a) => a.name === fromUrl));
  const agent = urlValid ? (fromUrl as string) : stored;

  // Leaving a per-agent page must not snap back to an older choice: the agent
  // the URL showed becomes the remembered one.
  useEffect(() => {
    if (urlValid && settled && fromUrl !== stored) setStored(fromUrl as string);
  }, [urlValid, settled, fromUrl, stored, setStored]);
  useEffect(() => {
    writeLastAgent(agent);
  }, [agent]);

  // The latest location and navigate() for setAgent, which must not change
  // identity on every navigation (half the app lists it as an effect
  // dependency). navigate() itself is in the ref because under <BrowserRouter>
  // react-router memoises it on the current pathname, so listing it as a
  // dependency would hand out a new setAgent per navigation.
  const locationRef = useRef(location);
  locationRef.current = location;
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const agentRef = useRef(agent);
  agentRef.current = agent;

  const setAgent = useCallback(
    (name: string, opts?: SetAgentOptions) => {
      setStored(name);
      if (opts?.navigate === false) return;
      const here = locationRef.current;
      // Re-picking the agent already on screen is not a move: navigating would
      // only drop the thread or item open under it.
      if ((urlAgent(here.pathname, here.search, shell) ?? agentRef.current) === name) return;
      const to = agentSwitchTarget(here.pathname, name, shell);
      if (to) navigateRef.current(to);
    },
    [setStored, shell],
  );
  const current = agents.find((a) => a.name === agent);
  const fallback = agents.some((a) => a.name === stored) ? stored : agents[0]?.name ?? "";

  return (
    <Ctx.Provider value={{ agents, agent, setAgent, current, loading, settled, reload, fallback }}>
      {children}
    </Ctx.Provider>
  );
}

export function useCurrentAgent(): CurrentAgentApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useCurrentAgent must be used within <CurrentAgentProvider>");
  return ctx;
}
