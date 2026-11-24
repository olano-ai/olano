// Agents view — gallery of every agent the caller owns or can access, with
// search, lifecycle controls, create, and a full detail drawer (overview /
// subagents / connectors / mcp / skills / config). Parity port of the original
// dashboard's loadAgents / renderAgentsGrid / agent detail chain, rebuilt on
// the Aurora design system (gradient buttons, glassy cards, token colours).
//
// Endpoints (olano/runtime/api.py):
//   GET    /api/agents                       list (auth-filtered server-side)
//   GET    /api/agents/{name}                full config + resolved subagents
//   POST   /api/agents                       create
//   PUT    /api/agents/{name}                update (partial patch)
//   DELETE /api/agents/{name}                delete
//   POST   /api/agents/{name}/{start|stop|restart}   lifecycle
//   plus nested subagents / channels / mcp CRUD (see AgentDetail).
// Live refresh comes from the global /api/stream "agents_update" frame.

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { agentDetailPath, chatPath } from "../../core/routeUrls";
import { usePersistentState } from "../../core/usePersistentState";
import { importChunk } from "../../core/chunkRecovery";
import { useCurrentAgent } from "../../core/currentAgent";
import { Search, RotateCw, Plus, MessageSquare, Square, Play, Bot } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import { ErrorState } from "../../ui/ErrorState";
import type { Agent } from "../../core/types";
import { agentLabel, agentStatus, statusColor, statusLabelKey, pluralKey } from "./helpers";
// The create modal drags in CatalogPicker + the connector catalog — ~28 KB gz,
// most of this route's payload, for a dialog that only opens when the operator
// clicks "New agent". Split out so listing agents doesn't pay for creating one.
const AgentCreateModal = lazy(() =>
  importChunk(() => import("./AgentForm")).then((m) => ({ default: m.AgentCreateModal })),
);
import { STR } from "./strings";
import { subscribeStream } from "../../core/agentStream";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

export function AgentsView() {
  const toast = useToast();
  const t = useT(STR);
  const navigate = useNavigate();
  const { setAgent: setCurrentAgent, reload: reloadRoster } = useCurrentAgent();
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Mirror for the load callback: lets a failed *refresh* (rows already on
  // screen) fall back to a toast without threading `agents` into load's deps
  // (which would re-run the mount effect on every roster change).
  const agentsRef = useRef(agents);
  agentsRef.current = agents;
  const [search, setSearch] = usePersistentState<string>("agents.search", "");
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null); // "name:action" while a lifecycle call runs

  const openDetail = useCallback((name: string) => navigate(agentDetailPath(name)), [navigate]);

  const load = useCallback(() => {
    // This view drives agent lifecycle — always refetch fresh and repopulate
    // the shared roster cache so other views don't read a stale list after a
    // create/delete here.
    api.invalidate("/api/agents");
    api
      .cachedGet<Agent[]>("/api/agents")
      .then((list) => {
        setAgents(list);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        // Keep any rows already on screen; a failed refresh must not turn the
        // roster into a lying "No agents yet" empty state.
        setLoadError(e instanceof Error ? e.message : t("toast.loadFailed"));
        if (agentsRef.current !== null) toast.push(t("toast.loadFailed"), "error");
      });
  }, [toast, t]);

  useEffect(load, [load]);

  // Live refresh: the runtime pushes agents_update on create / lifecycle / edit.
  // Coalesced: a server boot emits one agent_status per agent, and each load()
  // invalidates the roster cache and refetches — so an un-debounced handler
  // turned a restart into one /api/agents round-trip per agent, all of them
  // racing the view's own first load.
  useEffect(() => {
    let timer: number | undefined;
    const unsubscribe = subscribeStream((frame) => {
      if (
        frame.type !== "agents_update" &&
        frame.type !== "agent_status" &&
        frame.type !== "agent_joined"
      )
        return;
      window.clearTimeout(timer);
      timer = window.setTimeout(load, 250);
    });
    return () => {
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, [load]);

  const filtered = useMemo(() => {
    const list = agents ?? [];
    const q = search.trim().toLowerCase();
    if (!q) return list;
    return list.filter((a) =>
      `${a.name} ${a.display_name || ""} ${a.title || ""} ${a.description || ""} ${a.model || ""} ${(a.channels || []).join(" ")}`
        .toLowerCase()
        .includes(q),
    );
  }, [agents, search]);

  const counts = useMemo(() => {
    const list = agents ?? [];
    return {
      total: list.length,
      running: list.filter((a) => agentStatus(a) === "running").length,
    };
  }, [agents]);

  async function lifecycle(name: string, action: "start" | "stop" | "restart") {
    setBusy(`${name}:${action}`);
    try {
      await api.post(`/api/agents/${encodeURIComponent(name)}/${action}`);
      toast.push(t(`toast.${action === "start" ? "started" : action === "stop" ? "stopped" : "restarted"}`, { name }));
      load();
    } catch (e) {
      toast.push(errText(e, t("toast.lifecycleFailed", { action: t(`verb.${action}`), name })), "error");
    } finally {
      setBusy(null);
    }
  }

  function goChat(name: string) {
    setCurrentAgent(name, { navigate: false }); // keep the sidebar in step; the URL opens Chat on it
    navigate(chatPath(name));
  }

  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row" style={{ gap: 8 }}>
          <div style={{ position: "relative" }}>
            <input
              className="input"
              placeholder={t("search.placeholder")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              // Shrinks below 220px on narrow phones so the topbar never gets
              // wider than the viewport.
              style={{ width: "min(220px, 45vw)", paddingLeft: 30 }}
            />
            <span
              style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "var(--text-4)", display: "inline-flex" }}
            >
              <Search size={14} strokeWidth={1.75} />
            </span>
          </div>
          <button className="btn" onClick={load} title={t("btn.refresh")} aria-label={t("btn.refresh")}>
            <RotateCw size={15} strokeWidth={1.75} />
          </button>
          <button className="btn primary row" style={{ gap: 6 }} onClick={() => setCreating(true)}>
            <Plus size={15} strokeWidth={1.75} /> {t("btn.newAgent")}
          </button>
        </div>
      </div>

      <div className="page">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end" }}>
          <div>
            <h1>{t("title")}</h1>
            <p className="muted" style={{ margin: 0 }}>
              {t("subtitle")}
            </p>
          </div>
          {agents && agents.length > 0 && (
            <div className="row" style={{ gap: 8 }}>
              <span className="badge">{t(pluralKey(counts.total, "agent"), { n: String(counts.total) })}</span>
              <span className="badge" style={{ color: "var(--success)" }}>
                {t("badge.running", { n: String(counts.running) })}
              </span>
            </div>
          )}
        </div>

        {agents === null && loadError ? (
          <ErrorState message={t("toast.loadFailed")} detail={loadError} onRetry={load} />
        ) : agents === null ? (
          <div className="spinner" style={{ marginTop: 24 }} />
        ) : filtered.length === 0 ? (
          <EmptyState
            search={search.trim()}
            onCreate={() => setCreating(true)}
            onClear={() => setSearch("")}
          />
        ) : (
          <div
            style={{
              display: "grid",
              gap: 14,
              marginTop: 20,
              gridTemplateColumns: "repeat(auto-fill,minmax(280px,1fr))",
            }}
          >
            {filtered.map((a) => (
              <AgentCard
                key={a.name}
                agent={a}
                busy={busy}
                onOpen={() => openDetail(a.name)}
                onLifecycle={lifecycle}
                onChat={goChat}
              />
            ))}
          </div>
        )}
      </div>

      {creating && (
        <Suspense fallback={null}>
          <AgentCreateModal
            onClose={() => setCreating(false)}
            // Refresh both this grid AND the sidebar agent picker immediately —
            // the picker is driven by the currentAgent context, so reload it
            // directly rather than waiting on the agents_update SSE frame.
            onCreated={() => {
              load();
              reloadRoster();
            }}
          />
        </Suspense>
      )}
    </>
  );
}

// ── Agent card ───────────────────────────────────────────────────────────
function AgentCard({
  agent,
  busy,
  onOpen,
  onLifecycle,
  onChat,
}: {
  agent: Agent;
  busy: string | null;
  onOpen: () => void;
  onLifecycle: (name: string, action: "start" | "stop" | "restart") => void;
  onChat: (name: string) => void;
}) {
  const t = useT(STR);
  const status = agentStatus(agent);
  const running = status === "running" || !!agent.running;
  const isBusy = (action: string) => busy === `${agent.name}:${action}`;

  // Quick-action buttons stop propagation so they don't open the drawer.
  const stop = (fn: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation();
    fn();
  };

  const badges: { label: string; key: string }[] = [];
  if (agent.subagent_count)
    badges.push({ key: "sub", label: t(pluralKey(agent.subagent_count, "subagent"), { n: String(agent.subagent_count) }) });
  if (agent.tool_count) badges.push({ key: "tool", label: t(pluralKey(agent.tool_count, "tool"), { n: String(agent.tool_count) }) });
  if ((agent.channels || []).length)
    badges.push({
      key: "ch",
      label: t(pluralKey((agent.channels || []).length, "channel"), { n: String((agent.channels || []).length) }),
    });

  return (
    <div
      className="card"
      onClick={onOpen}
      style={{
        padding: 16,
        cursor: "pointer",
        position: "relative",
        overflow: "hidden",
        // Running agents get the same green-tinted surface as the Overview cards.
        background: running ? "color-mix(in srgb, var(--success) 9%, var(--surface))" : undefined,
      }}
    >
      {agent.color && (
        <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 3, background: agent.color }} />
      )}

      {/* quick actions (top-right, appear-on-card) */}
      <div className="row" style={{ position: "absolute", top: 12, right: 12, gap: 4 }}>
        {running ? (
          <>
            <IconBtn title={t("card.chat")} onClick={stop(() => onChat(agent.name))}>
              <MessageSquare size={14} strokeWidth={1.75} />
            </IconBtn>
            <IconBtn title={t("card.restart")} busy={isBusy("restart")} onClick={stop(() => onLifecycle(agent.name, "restart"))}>
              <RotateCw size={14} strokeWidth={1.75} />
            </IconBtn>
            <IconBtn title={t("card.stop")} busy={isBusy("stop")} onClick={stop(() => onLifecycle(agent.name, "stop"))}>
              <Square size={14} strokeWidth={1.75} />
            </IconBtn>
          </>
        ) : (
          <IconBtn title={t("card.start")} busy={isBusy("start")} onClick={stop(() => onLifecycle(agent.name, "start"))}>
            <Play size={14} strokeWidth={1.75} />
          </IconBtn>
        )}
      </div>

      <div className="row" style={{ gap: 12, marginBottom: 10, paddingRight: 70 }}>
        <div
          className="avatar-sm"
          style={{
            width: 38,
            height: 38,
            fontSize: 18,
            background: agent.color ? `${agent.color}30` : "var(--accent-gradient)",
            border: agent.color ? `1px solid ${agent.color}66` : "none",
            overflow: "hidden",
          }}
        >
          {agent.avatar_url ? (
            <img src={agent.avatar_url} alt={agent.name} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
          ) : (
            (agent.name[0] || "?").toUpperCase()
          )}
        </div>
        <div style={{ minWidth: 0 }}>
          <div
            style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {agentLabel(agent)}
          </div>
          {/* The position, under the name — the one line that says what
              this agent IS when the name alone ("Hillary") does not. */}
          {!!(agent.title || "").trim() && (
            <div
              style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            >
              {agent.title}
            </div>
          )}
          <div
            className="muted"
            style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {agent.model || "—"}
          </div>
        </div>
      </div>

      <div
        className="muted"
        style={{
          fontSize: 13,
          minHeight: 38,
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {agent.description || t("card.noDescription")}
      </div>

      <div className="row" style={{ gap: 5, marginTop: 12, flexWrap: "wrap" }}>
        <span className="badge" style={{ color: statusColor(status), gap: 5 }}>
          <span
            style={{ width: 7, height: 7, borderRadius: "50%", background: statusColor(status), display: "inline-block" }}
          />
          {t(statusLabelKey(status))}
        </span>
        {badges.map((b) => (
          <span key={b.key} className="badge">
            {b.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function IconBtn({
  title,
  busy,
  onClick,
  children,
}: {
  title: string;
  busy?: boolean;
  onClick: (e: React.MouseEvent) => void;
  children: React.ReactNode;
}) {
  return (
    <button
      className="btn ghost"
      title={title}
      aria-label={title}
      disabled={busy}
      onClick={onClick}
      style={{ padding: "4px 7px", fontSize: 13, lineHeight: 1 }}
    >
      {busy ? <span className="spinner" /> : children}
    </button>
  );
}

// ── Empty state ──────────────────────────────────────────────────────────
function EmptyState({ search, onCreate, onClear }: { search: string; onCreate: () => void; onClear: () => void }) {
  const t = useT(STR);
  return (
    <div
      className="card"
      style={{ marginTop: 24, padding: "48px 24px", textAlign: "center", background: "var(--surface)" }}
    >
      <div style={{ display: "flex", justifyContent: "center", color: "var(--text-4)" }}>
        <Bot size={38} strokeWidth={1.5} />
      </div>
      <div style={{ fontWeight: 600, fontSize: 16, marginTop: 8 }}>{search ? t("empty.noMatches") : t("empty.noAgents")}</div>
      <p className="muted" style={{ marginTop: 4 }}>
        {search ? t("empty.trySearch") : t("empty.createFirst")}
      </p>
      <div className="row" style={{ justifyContent: "center", marginTop: 16, gap: 8 }}>
        {search ? (
          <button className="btn" onClick={onClear}>
            {t("empty.clearSearch")}
          </button>
        ) : (
          <button className="btn primary row" style={{ gap: 6 }} onClick={onCreate}>
            <Plus size={15} strokeWidth={1.75} /> {t("btn.newAgent")}
          </button>
        )}
      </div>
    </div>
  );
}
