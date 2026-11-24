import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { agentDetailPath, chatPath } from "../../core/routeUrls";
import {
  AlertTriangle,
  Bot,
  ChevronLeft,
  ChevronRight,
  MessageSquare,
  Pause,
  Play,
  Plus,
  Power,
  RotateCw,
  Trash2,
  Zap,
  Clock,
  Cat,
  type LucideIcon,
} from "lucide-react";
import { api } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useCapability } from "../../core/capabilities";
import { subscribeStream } from "../../core/agentStream";
import { usePersistentState } from "../../core/usePersistentState";
import { usePathAgent } from "../../core/usePathAgent";
import { useCurrentAgent } from "../../core/currentAgent";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import { ActivityLog } from "./ActivityLog";
import { AgentCard } from "./AgentCard";
import { ApprovalsSection } from "./ApprovalsSection";
import { NotificationsSection } from "../notifications/NotificationsSection";
import { HighlightsSection } from "./HighlightsSection";
import { InsightsTeaser } from "./InsightsTeaser";
import { AgentDetailPanel } from "./AgentDetailPanel";
import { CreditsCard } from "./CreditsCard";
import { ServicesUsageCard } from "./ServicesUsageCard";
import { DiskCard } from "./DiskCard";
import { MemoryCard } from "./MemoryCard";
import { StatCard } from "./StatCard";
import { STR } from "./strings";
import { ensureOverviewStyles } from "./styles";
import {
  isBootLine,
  MAX_LOGS,
  PAGE_SIZE,
  sortRunningFirst,
  WORKING_WINDOW_MS,
  formatUptime,
} from "./helpers";
import type {
  Agent,
  AgentAction,
  AgentActivity,
  SchedulerTask,
  StatusResponse,
  StreamEvent,
  SystemAction,
} from "./types";

// Translation key pairs [idle, busy] per system action.
const SYS_KEYS: Record<SystemAction, [string, string]> = {
  start: ["sys.start", "sys.start.busy"],
  stop: ["sys.stop", "sys.stop.busy"],
  restart: ["sys.restart", "sys.restart.busy"],
};

// One server-control button (Start / Stop / Restart). Shows a progress label
// while its own action is in flight; disabled while any action runs.
function SysBtn({
  action,
  Icon,
  busy,
  onClick,
}: {
  action: SystemAction;
  Icon: LucideIcon;
  busy: SystemAction | null;
  onClick: () => void;
}) {
  const t = useT(STR);
  const isBusy = busy === action;
  const cls = action === "stop" ? "btn danger" : action === "start" ? "btn primary" : "btn";
  return (
    <button
      className={cls}
      disabled={busy !== null}
      onClick={onClick}
      style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
    >
      {isBusy ? <span className="spinner" /> : <Icon size={15} strokeWidth={1.75} />}
      {t(SYS_KEYS[action][isBusy ? 1 : 0])}
    </button>
  );
}

// Small status dot, replaces the former "●" emoji-ish glyph in stat subs.
function StatusDot({ color }: { color: string }) {
  return (
    <span
      aria-hidden
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        background: color,
        flex: "0 0 auto",
        display: "inline-block",
      }}
    />
  );
}

/** Always-visible quarantine notice: the deployment's recovery surface must
 * not depend on the Admin tab (a signed settings artifact can hide tabs, and
 * Overview is the one view that is never gated). Renders nothing unless the
 * runtime reports the quarantined posture. */
function QuarantineBanner() {
  const quarantined = useCapability("deployment_quarantined");
  const t = useT(STR);
  if (quarantined !== true) return null;
  return (
    <div
      className="card"
      role="alert"
      style={{
        marginTop: 16,
        border: "1px solid var(--danger, #b91c1c)",
        background: "color-mix(in srgb, var(--danger, #b91c1c) 8%, transparent)",
      }}
    >
      <strong>{t("quarantine.title")}</strong>
      <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
        {t("quarantine.body")}
      </p>
    </div>
  );
}

export function OverviewView() {
  const toast = useToast();
  const navigate = useNavigate();
  const t = useT(STR);
  const { setAgent } = useCurrentAgent();
  // System lifecycle + process-memory diagnostics are admin-only server-side
  // (/api/system, /api/diagnostics) — hide those cards from members entirely.
  const isAdmin = !!useAuth().user?.is_admin;

  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [taskCount, setTaskCount] = useState({ total: 0, enabled: 0 });
  const [logs, setLogs] = useState<string[]>([]);
  const [activity, setActivity] = useState<Record<string, AgentActivity>>({});
  const [page, setPage] = usePersistentState("overview.page", 0);
  // The open agent panel is the address (/overview/<agent>): a reload or a
  // copied link reopens it, opening one is a Back step and Back closes it.
  const [selectedName, setSelectedName] = usePathAgent("overview", "overview.selected", "push");
  const selected = selectedName || null;
  const setSelected = setSelectedName;
  // The agents grid collapses to a single header row — collapsed by default so
  // Overview stays a compact snapshot; the choice persists for the SPA session.
  const [agentsCollapsed, setAgentsCollapsed] = usePersistentState("overview.agentsCollapsed", true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [, forceTick] = useState(0); // re-render so uptime + glow stay fresh
  const [busyAgent, setBusyAgent] = useState<Record<string, boolean>>({});
  const [busySystem, setBusySystem] = useState<SystemAction | null>(null);

  // Inject the view-local animations/styles once (can't touch globals.css).
  useEffect(() => {
    ensureOverviewStyles();
  }, []);

  // Refetch just the agent list (used after lifecycle actions + SSE boots).
  const reloadAgents = useCallback(() => {
    api.invalidate("/api/agents");
    api
      .cachedGet<Agent[]>("/api/agents")
      .then(setAgents)
      .catch(() => toast.push(t("err.refreshAgents"), "error"));
  }, [toast, t]);

  // Same refetch, coalesced — for the log stream, where a server boot emits a
  // burst of per-agent lines. Undebounced, each one invalidated the roster
  // cache and issued its own /api/agents round-trip, so the very moment the
  // runtime is busiest was also when the dashboard hammered it hardest.
  const reloadTimer = useRef<number | undefined>(undefined);
  const reloadAgentsSoon = useCallback(() => {
    window.clearTimeout(reloadTimer.current);
    reloadTimer.current = window.setTimeout(reloadAgents, 400);
  }, [reloadAgents]);
  useEffect(() => () => window.clearTimeout(reloadTimer.current), []);

  // Fetch status + agents + scheduler tasks in parallel. Shared by the initial
  // load and the error-card retry. `isAlive` guards against setState-after-
  // unmount on the initial mount; the retry passes a constant `true`.
  const loadAll = useCallback((isAlive: () => boolean) => {
    setLoadError(null);
    Promise.all([
      // cachedGet: core/capabilities.ts reads the same payload for its tab
      // flags, so on a cold landing the two share one round-trip instead of
      // racing duplicate ones. A retry after a failure still refetches —
      // rejections are never cached.
      api.cachedGet<StatusResponse>("/api/status"),
      api.cachedGet<Agent[]>("/api/agents"),
      api.get<SchedulerTask[]>("/api/scheduler/tasks").catch(() => [] as SchedulerTask[]),
    ])
      .then(([st, ag, tasks]) => {
        if (!isAlive()) return;
        setStatus(st);
        setAgents(ag);
        setTaskCount({ total: tasks.length, enabled: tasks.filter((t) => t.enabled).length });
      })
      .catch((e: unknown) => {
        if (!isAlive()) return;
        setAgents([]);
        setLoadError(e instanceof Error ? e.message : t("err.unreachable.default"));
      });
  }, [t]);

  // ── initial load ─────────────────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    loadAll(() => alive);
    return () => {
      alive = false;
    };
  }, [loadAll]);

  // ── tick every second so uptime label + working-glow timeouts stay live ──
  useEffect(() => {
    const id = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  // ── shared real-time stream (EventSource auto-reconnects) ────────────────
  useEffect(() => {
    return subscribeStream((frame) => {
      const parsed = frame as StreamEvent;
      switch (parsed.type) {
        case "log": {
          const line = typeof parsed.data === "string" ? parsed.data : JSON.stringify(parsed.data);
          setLogs((prev) => {
            const next = [...prev, line];
            return next.length > MAX_LOGS ? next.slice(next.length - MAX_LOGS) : next;
          });
          if (isBootLine(line)) reloadAgentsSoon();
          break;
        }
        case "agent_activity": {
          const d = parsed.data as { agent?: string; status?: string } | null;
          if (!d?.agent) break;
          const now = Date.now();
          const working = d.status === "thinking" || d.status === "talking";
          setActivity((prev) => {
            const prevAct = prev[d.agent as string];
            return {
              ...prev,
              [d.agent as string]: {
                status: d.status ?? "idle",
                updatedAt: now,
                lastWorkingAt: working ? now : (prevAct?.lastWorkingAt ?? now - WORKING_WINDOW_MS),
              },
            };
          });
          break;
        }
        case "agent_started":
        case "agent_stopped":
        case "agent_deleted":
        case "agent_avatar_updated":
        case "agent_sprites_ready":
        case "agent_sprites_failed":
          // Coalesced: a boot fires agent_started once per agent, and each
          // refetch invalidates the roster cache — 26 agents meant 26
          // round-trips. 400ms is invisible for a status refresh.
          reloadAgentsSoon();
          break;
        case "scheduler_update":
          api
            .get<SchedulerTask[]>("/api/scheduler/tasks")
            .then((tasks) =>
              setTaskCount({ total: tasks.length, enabled: tasks.filter((t) => t.enabled).length }),
            )
            .catch(() => {});
          break;
        default:
          break;
      }
    });
  }, [reloadAgentsSoon]);

  // ── derived view data ────────────────────────────────────────────────────
  const sortedAgents = useMemo(() => sortRunningFirst(agents ?? []), [agents]);
  const runningCount = useMemo(() => (agents ?? []).filter((a) => a.running).length, [agents]);
  const idleCount = (agents?.length ?? 0) - runningCount;

  // Connectors are counted twice over: `configured` is what the humans set up
  // (the headline — a connector they created must never read as 0), `live` is
  // how many adapters actually attached. A gap between them is a connector that
  // failed to come up, so the sub-line reports "1 of 2 active" rather than
  // quietly dropping the broken one. Runtimes that predate `channels_configured`
  // fall back to the live list, which is what the card always showed.
  const channelCount = useMemo(() => {
    const live = status?.channels.length ?? 0;
    return { live, configured: status?.channels_configured ?? live };
  }, [status]);

  const totalPages = Math.max(1, Math.ceil(sortedAgents.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pageAgents = useMemo(
    () => sortedAgents.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE),
    [sortedAgents, safePage],
  );

  // The selected card expands into a detail panel below the grid. Resolve it
  // against the live list so it stays in sync after reloads / SSE updates, and
  // clears itself if the agent disappears.
  const selectedAgent = useMemo(
    () => (selected ? (agents ?? []).find((a) => a.name === selected) ?? null : null),
    [agents, selected],
  );

  // Effective expansion of the agents section. Collapsed by default, but always
  // expanded when there are no agents so the "create your first agent" onboarding
  // CTA is never buried behind a collapsed header.
  const agentsExpanded = !agentsCollapsed || (agents?.length ?? 0) === 0;

  // ── actions ──────────────────────────────────────────────────────────────
  async function systemAction(action: SystemAction) {
    setBusySystem(action);
    try {
      await api.post(`/api/system/${action}`);
      const okKey =
        action === "start"
          ? "toast.systemStarted"
          : action === "stop"
            ? "toast.systemStopped"
            : "toast.systemRestarted";
      toast.push(t(okKey));
      reloadAgents();
    } catch (e) {
      const failKey =
        action === "start"
          ? "toast.systemStartFailed"
          : action === "stop"
            ? "toast.systemStopFailed"
            : "toast.systemRestartFailed";
      toast.push(t(failKey, { err: e instanceof Error ? e.message : t("err.unknown") }), "error");
    } finally {
      setBusySystem(null);
    }
  }

  async function agentAction(name: string, action: AgentAction) {
    setBusyAgent((b) => ({ ...b, [name]: true }));
    try {
      await api.post(`/api/agents/${encodeURIComponent(name)}/${action}`);
      const okKey =
        action === "start"
          ? "toast.agentStarted"
          : action === "stop"
            ? "toast.agentStopped"
            : "toast.agentRestarted";
      toast.push(t(okKey, { name }));
      reloadAgents();
    } catch (e) {
      const failKey =
        action === "start"
          ? "toast.agentStartFailed"
          : action === "stop"
            ? "toast.agentStopFailed"
            : "toast.agentRestartFailed";
      toast.push(
        t(failKey, { name, err: e instanceof Error ? e.message : t("err.unknown") }),
        "error",
      );
    } finally {
      setBusyAgent((b) => ({ ...b, [name]: false }));
    }
  }

  function openChat(name: string) {
    navigate(chatPath(name));
  }

  function editAgent(name: string) {
    navigate(agentDetailPath(name));
  }

  function createAgent() {
    navigate("/agents");
  }

  const uptimeLabel = status ? t("stat.uptime", { value: formatUptime(status.uptime) }) : "—";

  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          <Link className="btn" to="/agents">
            {t("btn.manageAgents")}
          </Link>
          <Link
            className="btn primary"
            to="/chat"
            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <MessageSquare size={15} strokeWidth={1.75} /> {t("btn.openChat")}
          </Link>
        </div>
      </div>

      <div className="page">
        {/* ── header + system actions ──────────────────────────────────── */}
        <div>
          <h1>{t("title")}</h1>
          <p className="muted">{t("subtitle")}</p>
        </div>

        <QuarantineBanner />

        {agents === null ? (
          <div className="spinner" style={{ marginTop: 24 }} />
        ) : loadError ? (
          <div
            className="card"
            style={{
              marginTop: 18,
              padding: 28,
              textAlign: "center",
              borderColor: "color-mix(in srgb, var(--danger) 35%, var(--border))",
            }}
          >
            <div style={{ display: "flex", justifyContent: "center", color: "var(--danger)" }} aria-hidden>
              <AlertTriangle size={30} strokeWidth={1.5} />
            </div>
            <strong style={{ display: "block", fontSize: 15, marginTop: 8, color: "var(--danger)" }}>
              {t("err.unreachable")}
            </strong>
            <p className="muted" style={{ marginTop: 6, maxWidth: 460, marginInline: "auto" }}>
              {loadError}
            </p>
            <button
              className="btn primary"
              style={{ marginTop: 10, display: "inline-flex", alignItems: "center", gap: 6 }}
              onClick={() => {
                setAgents(null);
                loadAll(() => true);
              }}
            >
              <RotateCw size={15} strokeWidth={1.75} /> {t("btn.retry")}
            </button>
          </div>
        ) : (
          <>
            {/* ── insights teaser (links to the full /insights view) ───── */}
            <InsightsTeaser />

            {/* ── stats grid ──────────────────────────────────────────── */}
            <div
              style={{
                display: "grid",
                gap: 12,
                marginTop: 18,
                gridTemplateColumns: "repeat(auto-fill,minmax(220px,1fr))",
              }}
            >
              <StatCard
                label={t("stat.agents")}
                icon={<Bot size={16} strokeWidth={1.75} />}
                value={agents.length}
                to="/agents"
                linkLabel={t("stat.agents.open")}
                sub={
                  <span
                    style={{
                      color: runningCount > 0 ? "var(--success)" : undefined,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                    }}
                  >
                    <StatusDot color={runningCount > 0 ? "var(--success)" : "var(--text-4)"} />
                    {t("stat.running", { n: String(runningCount) })}
                    {idleCount > 0 ? t("stat.idle", { n: String(idleCount) }) : ""}
                  </span>
                }
              />
              <StatCard
                label={t("stat.channels")}
                icon={<Zap size={16} strokeWidth={1.75} />}
                value={channelCount.configured}
                sub={
                  channelCount.configured === 0 ? (
                    t("stat.noConnectors")
                  ) : (
                    <span
                      style={{
                        color: channelCount.live > 0 ? "var(--success)" : "var(--warn)",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                      }}
                    >
                      <StatusDot color={channelCount.live > 0 ? "var(--success)" : "var(--warn)"} />
                      {channelCount.live === channelCount.configured
                        ? t("stat.active", { n: String(channelCount.live) })
                        : t("stat.activeOf", {
                            n: String(channelCount.live),
                            total: String(channelCount.configured),
                          })}
                    </span>
                  )
                }
              />
              <StatCard
                label={t("stat.scheduledTasks")}
                icon={<Clock size={16} strokeWidth={1.75} />}
                value={taskCount.total}
                sub={
                  taskCount.enabled > 0
                    ? t("stat.enabled", { n: String(taskCount.enabled) })
                    : t("stat.noneEnabled")
                }
              />
              <StatCard
                label={t("stat.status")}
                icon={<Power size={16} strokeWidth={1.75} />}
                value={t("stat.status.live")}
                sub={uptimeLabel}
              />
            </div>

            {/* ── system control (admin-only API) ─────────────────────── */}
            {isAdmin && (
            <div className="card" style={{ marginTop: 14, padding: 16 }}>
              <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>{t("sys.title")}</div>
                  <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5, maxWidth: 560 }}>
                    {status ? t("sys.desc.live") : t("sys.desc.stopped")}
                  </p>
                </div>
                <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  {status ? (
                    <>
                      <SysBtn action="stop" Icon={Pause} busy={busySystem} onClick={() => void systemAction("stop")} />
                      <SysBtn
                        action="restart"
                        Icon={RotateCw}
                        busy={busySystem}
                        onClick={() => void systemAction("restart")}
                      />
                    </>
                  ) : (
                    <SysBtn action="start" Icon={Play} busy={busySystem} onClick={() => void systemAction("start")} />
                  )}
                </div>
              </div>
            </div>

            )}

            {/* ── daily work highlights (human narrative of what the fleet
                 did — conversations, scheduled tasks, cortex activity). The
                 narrative is scoped server-side to the viewer's agents;
                 generation stays admin-only inside the section. ─────────── */}
            <HighlightsSection />

            {/* ── approvals inbox (moved here from the standalone tab) ─── */}
            <NotificationsSection />
            <ApprovalsSection />

            {/* ── agents section (collapsible; collapsed by default) ──── */}
            <div className="row" style={{ justifyContent: "space-between", marginTop: 28 }}>
              {agents.length === 0 ? (
                <div className="row" style={{ gap: 8 }}>
                  <h2 style={{ fontSize: 16, margin: 0 }}>{t("agents.heading")}</h2>
                </div>
              ) : (
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setAgentsCollapsed((c) => !c)}
                  aria-expanded={agentsExpanded}
                  title={agentsExpanded ? t("agents.collapse") : t("agents.expand")}
                  style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "2px 6px", margin: "-2px -6px" }}
                >
                  <ChevronRight
                    size={16}
                    strokeWidth={1.75}
                    style={{
                      transition: "transform var(--t-fast)",
                      transform: agentsExpanded ? "rotate(90deg)" : "none",
                    }}
                  />
                  <h2 style={{ fontSize: 16, margin: 0 }}>{t("agents.heading")}</h2>
                  <span className="badge">{agents.length}</span>
                </button>
              )}
              <div className="row">
                {agentsExpanded && totalPages > 1 && (
                  <div className="row" style={{ gap: 6 }}>
                    <button
                      className="btn"
                      aria-label={t("agents.prevPage")}
                      style={{ padding: "5px 9px", display: "inline-flex", alignItems: "center" }}
                      disabled={safePage === 0}
                      onClick={() => setPage((p) => Math.max(0, p - 1))}
                    >
                      <ChevronLeft size={15} strokeWidth={1.75} />
                    </button>
                    <span className="muted" style={{ fontSize: 12, minWidth: 32, textAlign: "center" }}>
                      {safePage + 1}/{totalPages}
                    </span>
                    <button
                      className="btn"
                      aria-label={t("agents.nextPage")}
                      style={{ padding: "5px 9px", display: "inline-flex", alignItems: "center" }}
                      disabled={safePage >= totalPages - 1}
                      onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                    >
                      <ChevronRight size={15} strokeWidth={1.75} />
                    </button>
                  </div>
                )}
                <Link
                  to="/agents"
                  className="muted"
                  style={{ fontSize: 13, display: "inline-flex", alignItems: "center", gap: 2 }}
                >
                  {t("agents.viewAll")} <ChevronRight size={14} strokeWidth={1.75} />
                </Link>
              </div>
            </div>

            {agentsExpanded && (
              <>
                {agents.length === 0 ? (
                  <div className="card" style={{ marginTop: 12, padding: 32, textAlign: "center" }}>
                    <div style={{ display: "flex", justifyContent: "center", color: "var(--text-3)" }} aria-hidden>
                      <Cat size={32} strokeWidth={1.5} />
                    </div>
                    <strong style={{ display: "block", marginTop: 8, fontSize: 15 }}>
                      {t("agents.empty.title")}
                    </strong>
                    <p className="muted" style={{ marginTop: 6, maxWidth: 460, marginInline: "auto" }}>
                      {t("agents.empty.desc")}
                    </p>
                    <button
                      className="btn primary"
                      style={{ marginTop: 6, display: "inline-flex", alignItems: "center", gap: 6 }}
                      onClick={createAgent}
                    >
                      <Plus size={15} strokeWidth={1.75} /> {t("agents.empty.create")}
                    </button>
                  </div>
                ) : (
                  <div
                    style={{
                      display: "grid",
                      gap: 12,
                      marginTop: 12,
                      gridTemplateColumns: "repeat(auto-fill,minmax(240px,1fr))",
                    }}
                  >
                    {pageAgents.map((a) => (
                      <AgentCard
                        key={a.name}
                        agent={a}
                        activity={activity[a.name]}
                        busy={!!busyAgent[a.name]}
                        selected={selected === a.name}
                        onSelect={() => {
                          // Selecting a card in the Overview also promotes that
                          // agent to the app-wide "current agent" so the sidebar
                          // master picker (and every agent-scoped view) follows.
                          setAgent(a.name);
                          setSelected(selected === a.name ? "" : a.name);
                        }}
                        onAction={(action) => void agentAction(a.name, action)}
                        onChat={() => openChat(a.name)}
                        onEdit={() => editAgent(a.name)}
                      />
                    ))}
                  </div>
                )}

                {/* ── selected agent detail panel ─────────────────────── */}
                {selectedAgent && (
                  <AgentDetailPanel
                    agent={selectedAgent}
                    busy={!!busyAgent[selectedAgent.name]}
                    onClose={() => setSelected("")}
                    onAction={(action) => void agentAction(selectedAgent.name, action)}
                    onChat={() => openChat(selectedAgent.name)}
                    onEdit={() => editAgent(selectedAgent.name)}
                  />
                )}
              </>
            )}

            {/* ── live process memory + monitor toggle (collapsible card,
                 collapsed by default — sits below the agents grid; the
                 diagnostics API behind it is admin-only) ────────────────── */}
            {isAdmin && <MemoryCard />}

            {/* ── olano credits (managed deployments; hides itself on
                 unmanaged installs — {available:false}) ────────────────── */}
            {isAdmin && <CreditsCard />}
            {isAdmin && <ServicesUsageCard />}

            {/* ── disk usage + automatic cleanup (collapsible card, same
                 pattern; auto-clean toggle + "clean now" without leaving
                 the dashboard) ─────────────────────────────────────────── */}
            {isAdmin && <DiskCard />}

            {/* ── recent activity (raw runtime log lines — the SSE stream
                 only delivers them to admins, so hide the card for members) */}
            {isAdmin && (
              <>
                <div className="row" style={{ justifyContent: "space-between", marginTop: 28 }}>
                  <div className="row" style={{ gap: 8 }}>
                    <h2 style={{ fontSize: 16, margin: 0 }}>{t("activity.heading")}</h2>
                    {logs.length > 0 && <span className="badge">{logs.length}</span>}
                  </div>
                  <div className="row">
                    <button
                      className="btn"
                      title={t("activity.clear.title")}
                      disabled={logs.length === 0}
                      style={{ padding: "5px 11px", fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 }}
                      onClick={() => setLogs([])}
                    >
                      <Trash2 size={14} strokeWidth={1.75} /> {t("activity.clear")}
                    </button>
                    <Link
                      to="/logs"
                      className="muted"
                      style={{ fontSize: 13, display: "inline-flex", alignItems: "center", gap: 2 }}
                    >
                      {t("activity.allLogs")} <ChevronRight size={14} strokeWidth={1.75} />
                    </Link>
                  </div>
                </div>
                <ActivityLog logs={logs} />
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}
