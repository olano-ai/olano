// Agent detail — a full page (route /agents/:name), not a modal. Header with
// lifecycle + delete, tabbed body (overview / subagents / connectors / mcp /
// skills / config). The Overview tab is the single editable surface: every
// field edits in place and auto-saves (selects/toggles on change, text on blur,
// the accent colour debounced). The "Config" tab is a read-only JSON dump.

import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import { agentDetailPath, chatPath, viewPath } from "../../core/routeUrls";
import {
  ArrowLeft,
  MessageSquare,
  RotateCw,
  Square,
  Play,
  Pencil,
  Trash2,
  Plus,
  Bot,
  Plug,
  BookOpen,
  Cable,
  Settings,
  Zap,
  Copy,
  Maximize2,
  Minimize2,
  Info,
  Activity,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Circle,
  Power,
  Code2,
  KeyRound,
  ShieldCheck,
  FileText,
  ChevronDown,
  ChevronUp,
  ChevronRight,
  Search,
  Lock,
} from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useCapability } from "../../core/capabilities";
import { subscribeStream } from "../../core/agentStream";
import { useCurrentAgent } from "../../core/currentAgent";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import { GmailWatchCard } from "./GmailWatchCard";
import { ObservabilityCard } from "./ObservabilityCard";
import { useCatalog, modelOptions, modelReasoningMeta, modelValueLabel, selectedExtraTools, internalOnNames } from "./catalog";
import { ModelOptionGroups } from "./ModelOptions";
import { Toggle, KvRow, OptionNotes, noteItems, type NoteKeys } from "./Toggle";
import { ExternalAccessTab } from "../external/ExternalAccessTab";
import { BrowserTab } from "../browser/BrowserTab";
import { ConfirmModal, Field, ColorPicker, Modal, GeneratePromptButton } from "./Modal";
import {
  SubagentModal,
  SubagentDetailModal,
  SubagentOverridesModal,
  McpModal,
  ChannelModal,
} from "./SubModals";
import { connectorVariantName } from "./connectorCatalog";
import { McpOAuthDialog } from "./McpOAuthDialog";
import { SkillDetailModal, SkillBadges } from "./SkillDetailModal";
import { ToolBrowser } from "./ToolBrowser";
import { AvatarEditor } from "./AvatarEditor";
import { OrgTab } from "./OrgTab";
import { PagesTab } from "./PagesTab";
import { WebhooksTab } from "./WebhooksTab";
import { CommandsTab } from "./CommandsTab";
import { MonetizationTab } from "./MonetizationTab";
import { MultiUserTab } from "./MultiUserTab";
import { VariablesTab } from "./VariablesTab";
import { AdvancedTab } from "./AdvancedTab";
import { useHitlPolicy, effectiveApproval, type ToolApproval } from "./hitlPolicy";
import { STR } from "./strings";
import {
  statusColor,
  statusLabelKey,
  backendLabelKey,
  backendShorthand,
  channelGlyph,
  channelLabel,
  channelSummary,
  pluralKey,
  agentLabel,
  saveAgentTitle,
  ENV_NOTES,
} from "./helpers";
import type {
  AgentConfig,
  ChannelObject,
  ConnectorHealth,
  ConnectorRow,
  DetailTab,
  EnvShorthand,
  Lifecycle,
  McpServer,
  McpServerStatus,
  RateLimitsConfig,
  RateLimitWindows,
  ScheduleEntry,
  SkillEntry,
  SkillsResponse,
  SubagentEntry,
} from "./types";
import { ScheduleBuilder } from "../scheduler/ScheduleBuilder";
import { TaskOverrideFields } from "../scheduler/SchedulerView";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

// Per-option explanations for the Overview selects. An option label alone never
// says what the choice does — "Locked-down — strictest" doesn't tell you which
// tools start asking for approval — so every select pairs its control with one
// of these lists, rendered underneath by <OptionNotes>. ENV_NOTES is shared
// with the create-agent form and lives in helpers.ts.
const MODE_NOTES: NoteKeys[] = [
  ["ov.profileTrusted", "ov.modeOpt.trusted"],
  ["ov.profileSupervised", "ov.modeOpt.supervised"],
  ["ov.profileLocked", "ov.modeOpt.locked"],
];
const VERBOSITY_NOTES: NoteKeys[] = [
  ["ov.verbosityNormal", "ov.verbosityOpt.normal"],
  ["ov.verbosityQuiet", "ov.verbosityOpt.quiet"],
  ["ov.verbosityVerbose", "ov.verbosityOpt.verbose"],
];
const HB_NOTES: NoteKeys[] = [
  ["ov.hbOpt.defaultLabel", "ov.hbOpt.default"],
  ["ov.hbOpt.shortLabel", "ov.hbOpt.short"],
  ["ov.hbOpt.mediumLabel", "ov.hbOpt.medium"],
  ["ov.hbOpt.longLabel", "ov.hbOpt.long"],
];
const REASONING_NOTES: NoteKeys[] = [
  ["form.reasoningOpt.defaultLabel", "form.reasoningOpt.default"],
  ["form.reasoningOpt.noneLabel", "form.reasoningOpt.none"],
  ["form.reasoningOpt.lowLabel", "form.reasoningOpt.low"],
  ["form.reasoningOpt.mediumLabel", "form.reasoningOpt.medium"],
  ["form.reasoningOpt.highLabel", "form.reasoningOpt.high"],
];
const APPROVAL_NOTES: NoteKeys[] = [
  ["ov.approvalDefault", "ov.approvalOpt.default"],
  ["ov.approvalAlways", "ov.approvalOpt.always"],
  ["ov.approvalNever", "ov.approvalOpt.never"],
];

// Route component: reads :name and drives navigation. Renders the full-page
// agent configuration. Back / chat are plain route transitions.
export function AgentDetailPage() {
  const { name = "" } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const { setAgent } = useCurrentAgent();
  const toast = useToast();
  const t = useT(STR);

  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  const [notFound, setNotFound] = useState(false);
  // The tab is the path's last segment (/agents/<name>/<tab>, core/routeUrls.ts),
  // so a reload, a copied link and Back/Forward all land on it, and a tab
  // click is a Back step of its own. The overview is the bare page. Links that
  // still say ?tab=mcp (stored notifications, old bookmarks) are rewritten to
  // the path form before this page mounts (RouteCanonical).
  const { tab: routeTab } = useParams<{ tab: string }>();
  const _allowedTabs: DetailTab[] = ["overview", "subagents", "tools", "connectors", "mcp", "external", "browser", "webhooks", "commands", "monetization", "multiuser", "skills", "org", "pages", "variables", "advanced", "raw"];
  const tab: DetailTab = routeTab && _allowedTabs.includes(routeTab as DetailTab) ? (routeTab as DetailTab) : "overview";
  // An unknown tab falls back to the overview, and the address says so.
  useEffect(() => {
    if (routeTab && tab !== routeTab) navigate(agentDetailPath(name), { replace: true });
  }, [routeTab, tab, name, navigate]);
  const setTab = useCallback(
    (next: DetailTab) => {
      if (next !== tab) navigate(agentDetailPath(name, next));
    },
    [tab, name, navigate],
  );
  const [busyAction, setBusyAction] = useState<Lifecycle | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // Cartoon generation possible at all: Olano credits (managed default) OR an OpenAI key.
  const [imageGen, setImageGen] = useState(false);
  const [spritesPending, setSpritesPending] = useState(false); // sprite sheet generating in background
  // Config edits auto-save to disk but the *running* process keeps the old
  // config until restarted. `dirty` tracks "edited since last (re)start"; when
  // the user leaves the edit view with a running agent we hold the pending
  // navigation in `pendingNav` and pop a restart reminder first.
  const [dirty, setDirty] = useState(false);
  const [pendingNav, setPendingNav] = useState<{ go: () => void } | null>(null);

  // Guards that keep optimistic edits from being clobbered by a stale reload.
  // Every editable field does an optimistic `setCfg` for instant feedback, then
  // a PUT, then a reconciling `load()`. But `load()` also fires on *every*
  // `agents_update` SSE frame — which the runtime broadcasts on any agent/roster
  // activity, not just this agent's own edits — and multiple GETs can resolve
  // out of order. Because `load()` replaces the whole `cfg`, an in-flight or
  // slow GET that lands after an optimistic toggle overwrites it with a stale
  // snapshot: the toggle visibly flips back, and a stale response for one field
  // resets its siblings too (the "press one switch, another changes" bug).
  //   • `loadSeq` — only the newest load's response is applied; superseded ones
  //     are dropped, so out-of-order GETs can't win.
  //   • `pendingSaves` — while any local save is in flight the optimistic `cfg`
  //     is newer than anything a GET can return, so we skip applying reloads
  //     until the save settles and issues its own reconcile.
  const loadSeq = useRef(0);
  const pendingSaves = useRef(0);

  // Keep the selected tab on screen in the scrollable rail (phones). `nearest`
  // on the block axis so bringing a tab into horizontal view never scrolls the
  // page vertically underneath the user.
  const tabstripRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = tabstripRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    el?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [tab]);

  const back = useCallback(() => navigate("/agents"), [navigate]);

  // Runtime capability probe — gates the "Generate cartoon" button.
  useEffect(() => {
    api
      .get<{ capabilities?: { image_gen?: boolean; openai_image_gen?: boolean } }>("/api/status")
      // openai_image_gen is the pre-Olano-image name, still sent as an alias.
      .then((s) => setImageGen(!!(s.capabilities?.image_gen ?? s.capabilities?.openai_image_gen)))
      .catch(() => setImageGen(false));
  }, []);

  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    api
      .get<AgentConfig>(`/api/agents/${encodeURIComponent(name)}`)
      .then((c) => {
        // Drop this response if a newer load has since started, or a local save
        // is mid-flight (its optimistic state supersedes anything on the server).
        if (seq !== loadSeq.current || pendingSaves.current > 0) return;
        setCfg(c);
        setNotFound(false);
      })
      .catch((e) => {
        if (seq !== loadSeq.current || pendingSaves.current > 0) return;
        toast.push(errText(e, t("toast.openFailed")), "error");
        setNotFound(true);
      });
  }, [name, toast, t]);

  useEffect(load, [load]);

  // `savePatch`'s reconcile must target the *currently viewed* agent. An
  // in-flight save closes over the `load` it was created with, so if the
  // :name route param changed mid-save its `finally` would refetch the old
  // agent — and, being the newest load, win the seq race with wrong-agent
  // data. Reconcile through a ref to the latest `load` instead.
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  }, [load]);

  // Live refresh: the runtime pushes agents_update / agent_status on lifecycle
  // and edits (including ones made elsewhere). Keep this page in sync.
  useEffect(() => {
    return subscribeStream((frame) => {
      if (frame.type === "agents_update" || frame.type === "agent_status") load();
      // Avatar/sprite jobs broadcast {name, …}; only react to this agent's.
      else if (frame.type === "agent_avatar_updated" && (frame.data as { name?: string } | undefined)?.name === name) load();
      else if (frame.type === "agent_sprites_ready" && (frame.data as { name?: string } | undefined)?.name === name) {
        setSpritesPending(false);
        load();
      }
    });
  }, [load, name]);

  // Persist a partial config patch (PUT), refresh in place. Optimistic callers
  // pass the already-mutated cfg into setCfg first for instant feedback.
  // The title is `org.title`, which PUT /api/agents/{name} refuses (it is a
  // read-only mirror there); it goes through the org endpoint so the central
  // organization.yaml — which shadows the agent.json5 block — stays canonical.
  const saveTitle = useCallback(
    async (next: string): Promise<void> => {
      pendingSaves.current += 1;
      try {
        await saveAgentTitle(name, next);
        setDirty(true);
      } catch (e) {
        toast.push(errText(e, t("toast.saveFailed")), "error");
      } finally {
        pendingSaves.current -= 1;
        loadRef.current();
      }
    },
    [name, toast, t],
  );

  const savePatch = useCallback(
    async (patch: Partial<AgentConfig>, successMsg?: string): Promise<boolean> => {
      pendingSaves.current += 1;
      try {
        await api.put(`/api/agents/${encodeURIComponent(name)}`, patch);
        if (successMsg) toast.push(successMsg);
        setDirty(true);
        return true;
      } catch (e) {
        toast.push(errText(e, t("toast.saveFailed")), "error");
        return false;
      } finally {
        pendingSaves.current -= 1;
        // Reconcile with the server. If another save is still in flight this
        // reload is dropped by the guards in `load` and that save issues its
        // own reconcile, so the last write always wins.
        loadRef.current();
      }
    },
    [name, toast, t],
  );

  // Reload variant for sub-tabs (subagents / connectors / mcp): every call is
  // the result of an edit, so flag the config dirty too.
  const reloadDirty = useCallback(() => {
    setDirty(true);
    load();
  }, [load]);

  // Gate a navigation away from the edit view: if the agent is running and has
  // unapplied edits, defer the navigation behind the restart reminder.
  const guardExit = useCallback(
    (go: () => void) => {
      if (dirty && !!cfg?._running) setPendingNav({ go });
      else go();
    },
    [dirty, cfg],
  );

  async function lifecycle(action: Lifecycle) {
    setBusyAction(action);
    try {
      await api.post(`/api/agents/${encodeURIComponent(name)}/${action}`);
      toast.push(t(`toast.${action === "start" ? "started" : action === "stop" ? "stopped" : "restarted"}`, { name }));
      // A (re)start boots a fresh process from the on-disk config, so any
      // pending edits are now applied — clear the dirty flag.
      if (action === "restart" || action === "start") setDirty(false);
      load();
    } catch (e) {
      toast.push(errText(e, t("toast.lifecycleFailed", { action: t(`verb.${action}`), name })), "error");
    } finally {
      setBusyAction(null);
    }
  }

  async function doDelete() {
    setDeleting(true);
    try {
      await api.del(`/api/agents/${encodeURIComponent(name)}`);
      toast.push(t("toast.deleted"));
      back();
    } catch (e) {
      toast.push(errText(e, t("toast.deleteFailed")), "error");
    } finally {
      setDeleting(false);
      setConfirmDelete(false);
    }
  }

  function goChat() {
    setAgent(name, { navigate: false });
    navigate(chatPath(name));
  }

  // Deployment settings can disable the raw agent.json5 editor (the API 403s
  // regardless; hiding the tab is the matching UX). Default-on — only an
  // explicit false removes it. MUST be called before the notFound/loading
  // early returns below: hooks may not appear after a conditional return, and
  // calling it later crashed the whole detail page with "Rendered more hooks
  // than during the previous render" as soon as the config finished loading.
  const rawEditorEnabled = useCapability("agent_config_raw_enabled") !== false;
  // Variables (agent-scoped vault overrides) is an owner-or-admin surface —
  // hide the tab from plain members. The API re-checks server-side, so a
  // deep-linked ?tab=variables only ever shows the forbidden notice.
  const { canAdminAgent } = useAuth();
  const showVariables = canAdminAgent(name);
  // For the Tools tab badge — see the count computation below, right where
  // `tabs` is built (same hook the Tools tab body uses for its own count, so
  // the two can no longer disagree).
  const cat = useCatalog();

  if (notFound) {
    return (
      <>
        <div className="topbar">
          <button className="btn ghost row" style={{ gap: 6 }} onClick={back}>
            <ArrowLeft size={15} strokeWidth={1.75} /> {t("detail.back")}
          </button>
        </div>
        <div className="page">
          <Empty icon={<Bot size={34} strokeWidth={1.5} />} title={t("toast.openFailed")} sub={name} />
        </div>
      </>
    );
  }

  if (!cfg) {
    return (
      <>
        <div className="topbar">
          <button className="btn ghost row" style={{ gap: 6 }} onClick={back} aria-label={t("detail.backAria")}>
            <ArrowLeft size={15} strokeWidth={1.75} /> {t("detail.back")}
          </button>
        </div>
        <div className="page">
          <div className="spinner" style={{ marginTop: 24 }} />
        </div>
      </>
    );
  }

  const running = !!cfg._running;
  const status =
    busyAction === "start" ? "starting" : busyAction === "stop" ? "stopping" : running ? "running" : "configured";
  const subs = cfg.subagents_resolved || cfg.subagents || [];
  const channels = cfg.channels || [];
  const mcps = cfg.mcp_servers || [];

  // Total tools this agent actually runs with: extras (config.tools + universal
  // defaults, minus disabled_tools — same computation the Tools tab body uses)
  // unioned with whichever internal catalog rows are currently on. Union BY
  // NAME, not a sum — the universal defaults (and e.g. graph/notes tools) also
  // appear as internal rows, and adding the two lengths double-counted every
  // one of them (badge said 37 while the browser could only show 13).
  const toolsCount = new Set([...selectedExtraTools(cfg, cat.agentDefaults), ...internalOnNames(cfg)])
    .size;

  const tabs: { id: DetailTab; label: string; count?: number }[] = [
    { id: "overview", label: t("tab.overview") },
    { id: "subagents", label: t("tab.subagents"), count: subs.length },
    { id: "tools", label: t("tab.tools"), count: toolsCount },
    { id: "connectors", label: t("tab.connectors"), count: channels.length },
    { id: "mcp", label: t("tab.mcp"), count: mcps.length },
    {
      id: "external",
      label: t("tab.external"),
      count: (cfg.external_access?.mcp ? 1 : 0) + (cfg.external_access?.a2a ? 1 : 0) || undefined,
    },
    { id: "browser", label: t("tab.browser") },
    { id: "webhooks", label: t("tab.webhooks"), count: ((cfg as { webhooks?: unknown[] }).webhooks || []).length },
    { id: "commands", label: t("tab.commands"), count: (cfg.commands?.custom || []).length || undefined },
    { id: "monetization", label: t("tab.monetization") },
    { id: "multiuser", label: t("tab.multiuser") },
    { id: "skills", label: t("tab.skills") },
    { id: "org", label: t("tab.org") },
    { id: "pages", label: t("tab.pages") },
    ...(showVariables ? [{ id: "variables" as DetailTab, label: t("tab.variables") }] : []),
    { id: "advanced", label: t("tab.advanced") },
    ...(rawEditorEnabled ? [{ id: "raw" as DetailTab, label: t("tab.config") }] : []),
  ];

  return (
    <>
      {/* ── topbar: back + lifecycle + delete ─────────────────────────── */}
      <div className="topbar">
        <button className="btn ghost row" style={{ gap: 6 }} onClick={() => guardExit(back)} aria-label={t("detail.backAria")}>
          <ArrowLeft size={15} strokeWidth={1.75} /> {t("detail.back")}
        </button>
        {/* Phones collapse this cluster to a 44px icon toolbar (.topbar-actions
            in globals.css) — the labels stay in the a11y tree and as tooltips,
            so nothing is lost, but the agent's own name stops being pushed
            below the fold by a second row of wrapped buttons. */}
        <div className="topbar-actions">
          {running ? (
            <>
              <button className="btn row" style={{ gap: 6 }} title={t("detail.chat")} onClick={() => guardExit(goChat)}>
                <MessageSquare size={15} strokeWidth={1.75} /> <span className="btn-label">{t("detail.chat")}</span>
              </button>
              <button
                className="btn row"
                style={{ gap: 6 }}
                title={t("detail.restart")}
                disabled={busyAction !== null}
                onClick={() => void lifecycle("restart")}
              >
                {busyAction === "restart" ? <span className="spinner" /> : <RotateCw size={15} strokeWidth={1.75} />}{" "}
                <span className="btn-label">{t("detail.restart")}</span>
              </button>
              <button
                className="btn row"
                style={{ gap: 6 }}
                title={t("detail.stop")}
                disabled={busyAction !== null}
                onClick={() => void lifecycle("stop")}
              >
                {busyAction === "stop" ? <span className="spinner" /> : <Square size={15} strokeWidth={1.75} />}{" "}
                <span className="btn-label">{t("detail.stop")}</span>
              </button>
            </>
          ) : (
            <button
              className="btn primary row"
              style={{ gap: 6 }}
              title={t("detail.start")}
              disabled={busyAction !== null}
              onClick={() => void lifecycle("start")}
            >
              {busyAction === "start" ? <span className="spinner" /> : <Play size={15} strokeWidth={1.75} />}{" "}
              <span className="btn-label">{t("detail.start")}</span>
            </button>
          )}
          <button className="btn danger row" style={{ gap: 6 }} title={t("detail.delete")} onClick={() => setConfirmDelete(true)}>
            <Trash2 size={15} strokeWidth={1.75} /> <span className="btn-label">{t("detail.delete")}</span>
          </button>
        </div>
      </div>

      <div className="page">
        {/* ── identity header ─────────────────────────────────────────── */}
        <div className="row" style={{ alignItems: "flex-start", gap: 16 }}>
          <div
            className="avatar-sm"
            style={{
              width: 56,
              height: 56,
              fontSize: 26,
              background: cfg.color ? `${cfg.color}30` : "var(--accent-gradient)",
              border: cfg.color ? `1px solid ${cfg.color}66` : "none",
              overflow: "hidden",
            }}
          >
            {cfg.avatar_url ? (
              <img src={cfg.avatar_url} alt={cfg.name} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
            ) : (
              (cfg.name[0] || "?").toUpperCase()
            )}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h1 style={{ marginBottom: 2 }}>{agentLabel(cfg)}</h1>
            {/* The position, under the name: "Hillary — Recruitment
                Specialist". A resolved mirror of org.title (central store
                first), edited in Identity below or on the Talk to Agent tab. */}
            {!!(cfg.title || "").trim() && (
              <div style={{ fontSize: 13, marginBottom: 2 }} data-testid="agent-title">
                {cfg.title}
              </div>
            )}
            {/* The handle only earns its own line when it differs from the name
                on display — otherwise it just repeats the heading. */}
            {agentLabel(cfg) !== cfg.name && (
              <div
                className="muted"
                style={{ fontSize: 12, fontFamily: "var(--font-mono, monospace)", marginBottom: 2 }}
              >
                {cfg.name}
              </div>
            )}
            <div className="muted" style={{ fontSize: 13 }}>
              {cfg.description || t("card.noDescription")}
            </div>
            <div className="row" style={{ gap: 6, marginTop: 10, flexWrap: "wrap" }}>
              <span className="badge" style={{ color: statusColor(status), gap: 5 }}>
                <span style={{ width: 7, height: 7, borderRadius: "50%", background: statusColor(status), display: "inline-block" }} />
                {t(statusLabelKey(status))}
              </span>
              <span className="badge" style={{ fontFamily: "var(--font-mono)" }}>
                {cfg.model || "—"}
              </span>
              <span className="badge">{t(backendLabelKey(cfg.backend))}</span>
            </div>
          </div>
        </div>

        {/* ── tabs ─────────────────────────────────────────────────────
            One row that scrolls horizontally on phones (see .tabstrip) rather
            than wrapping 14 items into a ~250px block above the content. The
            active tab is scrolled into view so a deep link (?tab=advanced) or a
            programmatic switch never lands on a tab parked off-screen. */}
        <div className="tabstrip" role="tablist" aria-label={t("detail.tabsAria")} ref={tabstripRef}>
          {tabs.map((tb, i) => (
            <button
              key={tb.id}
              role="tab"
              id={`agent-tab-${tb.id}`}
              aria-selected={tab === tb.id}
              aria-controls="agent-tabpanel"
              // Roving tabindex: the tablist is one stop in the page's tab
              // order and the arrow keys move within it — the pattern a screen
              // reader promises the moment `role="tablist"` is declared.
              tabIndex={tab === tb.id ? 0 : -1}
              className="tabstrip-tab"
              onClick={() => setTab(tb.id)}
              onKeyDown={(e) => {
                const delta = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                let next = -1;
                if (delta) next = (i + delta + tabs.length) % tabs.length;
                else if (e.key === "Home") next = 0;
                else if (e.key === "End") next = tabs.length - 1;
                if (next < 0) return;
                e.preventDefault();
                setTab(tabs[next].id);
                // The scroll-into-view effect runs on `tab`; focus has to
                // follow selection or the arrow keys stop working after one
                // press (the old button keeps focus but is now tabIndex -1).
                requestAnimationFrame(() => document.getElementById(`agent-tab-${tabs[next].id}`)?.focus());
              }}
            >
              {tb.label}
              {tb.count !== undefined && <span className="badge">{tb.count}</span>}
            </button>
          ))}
        </div>

        {/* ── tab body ───────────────────────────────────────────────── */}
        <div style={{ marginTop: 18 }} role="tabpanel" id="agent-tabpanel" aria-labelledby={`agent-tab-${tab}`}>
          {tab === "overview" && (
            <OverviewTab
              cfg={cfg}
              setCfg={setCfg}
              savePatch={savePatch}
              saveTitle={saveTitle}
              agentName={name}
              reload={load}
              imageGen={imageGen}
              spritesPending={spritesPending}
              onSpritesPending={setSpritesPending}
            />
          )}
          {tab === "subagents" && <SubagentsTab cfg={cfg} agentName={name} reload={reloadDirty} />}
          {tab === "tools" && <ToolsTab cfg={cfg} setCfg={setCfg} savePatch={savePatch} />}
          {tab === "connectors" && <ConnectorsTab cfg={cfg} agentName={name} reload={reloadDirty} />}
          {tab === "mcp" && <McpTab cfg={cfg} agentName={name} reload={reloadDirty} />}
          {tab === "external" && (
            <ExternalAccessTab cfg={cfg} agentName={name} savePatch={savePatch} />
          )}
          {tab === "browser" && <BrowserTab agentName={name} />}
          {tab === "webhooks" && <WebhooksTab cfg={cfg} agentName={name} reload={reloadDirty} />}
          {tab === "commands" && <CommandsTab cfg={cfg} setCfg={setCfg} savePatch={savePatch} />}
          {tab === "monetization" && (
            <MonetizationTab cfg={cfg} setCfg={setCfg} savePatch={savePatch} agentName={name} />
          )}
          {tab === "multiuser" && <MultiUserTab cfg={cfg} setCfg={setCfg} savePatch={savePatch} />}
          {tab === "skills" && <SkillsTab cfg={cfg} savePatch={savePatch} />}
          {tab === "org" && <OrgTab agentName={name} />}
          {tab === "pages" && <PagesTab agentName={name} />}
          {tab === "variables" && <VariablesTab agentName={name} />}
          {tab === "advanced" && (
            <AdvancedTab agentName={name} cfg={cfg} savePatch={savePatch} onGoToTab={setTab} />
          )}
          {tab === "raw" && <RawTab name={name} />}
        </div>
      </div>

      {confirmDelete && (
        <ConfirmModal
          title={t("confirm.deleteTitle", { name: cfg.name })}
          message={t("confirm.deleteMsg", { name: cfg.name })}
          confirmLabel={t("confirm.deleteAgent")}
          danger
          busy={deleting}
          onConfirm={() => void doDelete()}
          onClose={() => setConfirmDelete(false)}
        />
      )}

      {/* Restart reminder — shown when leaving the edit view with a running
          agent that has unapplied config edits. "Restart now" applies the
          changes then continues; "Leave without restarting" navigates anyway;
          dismissing (X/Esc/backdrop) stays on the page. */}
      {pendingNav && (
        <Modal
          title={t("confirm.runningTitle", { name: cfg.name })}
          width={440}
          onClose={() => setPendingNav(null)}
          footer={
            <>
              <button
                className="btn"
                disabled={busyAction === "restart"}
                onClick={() => {
                  const go = pendingNav.go;
                  setPendingNav(null);
                  go();
                }}
              >
                {t("confirm.leaveAnyway")}
              </button>
              <button
                className="btn primary"
                disabled={busyAction === "restart"}
                autoFocus
                onClick={async () => {
                  const go = pendingNav.go;
                  await lifecycle("restart");
                  setPendingNav(null);
                  go();
                }}
              >
                {busyAction === "restart" ? t("modal.working") : t("confirm.restartNow")}
              </button>
            </>
          }
        >
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
            {t("confirm.runningMsg")}
          </div>
        </Modal>
      )}
    </>
  );
}

// ── Card shell used across the overview tab ──────────────────────────────
function DCard({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10, gap: 8 }}>
        <strong style={{ fontSize: 13 }}>{title}</strong>
        {action}
      </div>
      {children}
    </div>
  );
}

// A labelled integer setting that auto-saves on blur. Shows the effective value
// (configured value, or the schema default when unset); an out-of-range or
// non-numeric entry is discarded so a bad keystroke can't persist.
function NumberSetting({
  label,
  hint,
  note,
  value,
  def,
  min,
  onSave,
}: {
  label: string;
  hint?: ReactNode;
  note?: ReactNode;
  value: number | undefined;
  def: number;
  min?: number;
  onSave: (n: number) => void;
}) {
  const effective = value ?? def;
  const [draft, setDraft] = useState(String(effective));
  useEffect(() => setDraft(String(effective)), [effective]);
  const commit = () => {
    const n = parseInt(draft, 10);
    if (!Number.isFinite(n) || (min !== undefined && n < min)) {
      setDraft(String(effective));
      return;
    }
    if (n !== effective) onSave(n);
  };
  return (
    <KvRow label={label} hint={hint} note={note}>
      <input
        className="input"
        type="number"
        min={min}
        style={{ width: 120 }}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      />
    </KvRow>
  );
}

// Single-line text field that auto-saves on blur (only when the value actually
// changed). Stays in sync when the agent reloads from the server.
function InlineText({
  value,
  placeholder,
  onCommit,
}: {
  value: string;
  placeholder?: string;
  onCommit: (next: string) => void;
}) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  return (
    <input
      className="input"
      value={v}
      placeholder={placeholder}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => {
        if (v !== value) onCommit(v);
      }}
    />
  );
}

// Multi-line field (system prompt) — auto-saves on blur, with an expand toggle.
function InlinePrompt({ value, onCommit }: { value: string; onCommit: (next: string) => void }) {
  const t = useT(STR);
  const [v, setV] = useState(value);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => setV(value), [value]);
  return (
    <>
      <textarea
        className="input"
        value={v}
        placeholder={t("ov.promptPlaceholder")}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => {
          if (v !== value) onCommit(v);
        }}
        style={{
          minHeight: expanded ? 420 : 150,
          resize: "vertical",
          fontFamily: "var(--font-mono)",
          fontSize: 13,
          lineHeight: 1.5,
        }}
      />
      <div className="row" style={{ justifyContent: "space-between", marginTop: 8 }}>
        <span className="muted" style={{ fontSize: 11 }}>
          {t("ov.autosave")}
        </span>
        <button className="btn ghost row" style={{ padding: "3px 8px", fontSize: 12, gap: 5 }} onClick={() => setExpanded((x) => !x)}>
          {expanded ? <Minimize2 size={13} strokeWidth={1.75} /> : <Maximize2 size={13} strokeWidth={1.75} />}
          {expanded ? t("ov.collapse") : t("ov.expand")}
        </button>
      </div>
    </>
  );
}

// Accent colour picker that debounces saves — typing a hex value or clicking a
// swatch persists ~500ms after the last change, so the cursor never jumps from
// a mid-edit reload and we don't spam the API per keystroke.
function InlineColor({ value, onCommit }: { value: string; onCommit: (next: string) => void }) {
  const [color, setColor] = useState(value);
  const dirty = useRef(false);
  useEffect(() => {
    if (!dirty.current) setColor(value);
  }, [value]);
  useEffect(() => {
    if (!dirty.current) return;
    const id = setTimeout(() => {
      dirty.current = false;
      onCommit(color);
    }, 500);
    return () => clearTimeout(id);
  }, [color, onCommit]);
  return (
    <ColorPicker
      value={color}
      onChange={(c) => {
        dirty.current = true;
        setColor(c);
      }}
    />
  );
}

// ── Overview tab — the single editable surface ───────────────────────────
function OverviewTab({
  cfg,
  setCfg,
  savePatch,
  saveTitle,
  agentName,
  reload,
  imageGen,
  spritesPending,
  onSpritesPending,
}: {
  cfg: AgentConfig;
  setCfg: (c: AgentConfig) => void;
  savePatch: (patch: Partial<AgentConfig>, msg?: string) => Promise<boolean>;
  // The title takes its own writer (PUT …/org); see AgentDetail.saveTitle.
  saveTitle: (next: string) => Promise<void>;
  agentName: string;
  reload: () => void;
  imageGen: boolean;
  spritesPending: boolean;
  onSpritesPending: (pending: boolean) => void;
}) {
  const t = useT(STR);
  const cat = useCatalog();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const { setAgent } = useCurrentAgent();
  const models = modelOptions(cat.providers);
  const mem = cfg.memory || {};
  const sec = cfg.security || {};
  const hb = cfg.heartbeat || {};
  const cog = cfg.cognition || {};
  const aut = cfg.autonomy || {};
  const schedule = cfg.schedule || [];
  const env = backendShorthand(cfg.backend);

  // Optimistic helpers: mutate local state, then persist the whole nested object
  // so each PUT patch is well-formed.
  function setMemFlag(key: string, value: boolean | number) {
    const next = { ...mem, [key]: value };
    setCfg({ ...cfg, memory: next });
    void savePatch({ memory: next });
  }
  function setSecFlag(key: string, value: boolean | string) {
    const next = { ...sec, [key]: value };
    setCfg({ ...cfg, security: next });
    void savePatch({ security: next });
  }
  function setCogFlag(key: string, value: boolean | string | null) {
    const next = { ...cog, [key]: value };
    setCfg({ ...cfg, cognition: next });
    void savePatch({ cognition: next });
  }
  function setAutFlag(key: string, value: boolean | null) {
    const next = { ...aut, [key]: value };
    setCfg({ ...cfg, autonomy: next });
    void savePatch({ autonomy: next });
  }
  function setHbField(key: string, value: boolean | number | null) {
    const next = { ...hb, [key]: value };
    setCfg({ ...cfg, heartbeat: next });
    void savePatch({ heartbeat: next });
  }
  // Top-level scalar fields (access flags, runtime limits) — patch the field
  // directly. Optimistic: reflect in local state, then persist.
  // `string` covers the enum-valued top-level fields (default_verbosity), which
  // save exactly like the boolean flags and number limits around them.
  function setTopFlag(key: string, value: boolean | number | string) {
    setCfg({ ...cfg, [key]: value });
    void savePatch({ [key]: value } as Partial<AgentConfig>);
  }
  // Fleet file access opens every agent's files to this agent's shell, so
  // turning it ON asks first; turning it off never does.
  async function setFleetAccess(next: boolean) {
    if (
      next &&
      !(await confirm({
        title: t("ov.fleetAccessConfirmTitle"),
        message: t("ov.fleetAccessConfirm"),
        confirmLabel: t("ov.fleetAccessConfirmOk"),
        danger: true,
      }))
    ) {
      return;
    }
    setTopFlag("fleet_access", next);
  }

  // ── Inbound message rate limits (per-agent). Empty = unset ⇒ inherit the
  // home-wide default / unlimited. Only positive ints are written; per_tool and
  // the legacy requests_per_minute alias are preserved untouched.
  const rl: RateLimitsConfig = sec.rate_limits || {};
  const rlMessages: RateLimitWindows = rl.messages || {};
  const rlPlatforms: Record<string, RateLimitWindows> = rl.per_platform || {};
  // Per-platform rows the user opened but hasn't filled yet (kept local so we
  // never persist an empty override map).
  const [showRlPlatforms, setShowRlPlatforms] = useState(false);
  const [pendingPlatforms, setPendingPlatforms] = useState<string[]>([]);
  const rlWindowKeys: Array<keyof RateLimitWindows> = ["per_minute", "per_hour", "per_day"];

  function commitRateLimits(next: RateLimitsConfig) {
    const cleaned: RateLimitsConfig = { ...next };
    if (!cleaned.messages || Object.keys(cleaned.messages).length === 0) delete cleaned.messages;
    if (!cleaned.per_platform || Object.keys(cleaned.per_platform).length === 0)
      delete cleaned.per_platform;
    const nextSec = { ...sec, rate_limits: cleaned };
    setCfg({ ...cfg, security: nextSec });
    void savePatch({ security: nextSec });
  }
  // Parse an input string into a positive int, or drop the key (empty/≤0 = unset).
  function withWindow(current: RateLimitWindows, key: keyof RateLimitWindows, raw: string) {
    const n = parseInt(raw, 10);
    const next = { ...current };
    if (raw.trim() === "" || !Number.isFinite(n) || n <= 0) delete next[key];
    else next[key] = n;
    return next;
  }
  function setAgentWindow(key: keyof RateLimitWindows, raw: string) {
    commitRateLimits({ ...rl, messages: withWindow(rlMessages, key, raw) });
  }
  function setPlatformWindow(platform: string, key: keyof RateLimitWindows, raw: string) {
    const windows = withWindow(rlPlatforms[platform] || {}, key, raw);
    const next = { ...rlPlatforms };
    if (Object.keys(windows).length === 0) delete next[platform];
    else next[platform] = windows;
    commitRateLimits({ ...rl, per_platform: next });
  }
  function removePlatform(platform: string) {
    setPendingPlatforms((p) => p.filter((x) => x !== platform));
    if (rlPlatforms[platform]) {
      const next = { ...rlPlatforms };
      delete next[platform];
      commitRateLimits({ ...rl, per_platform: next });
    }
  }
  // Platform namespace = the agent's configured channels (bare, `platform:`
  // stripped) + the dashboard/REST scope. Matches the enforcement keys.
  const channelPlatforms = (cfg.channels || [])
    .map((c) => (typeof c === "string" ? c : c.type))
    .map((s) => String(s || "").replace(/^platform:/, "").toLowerCase())
    .filter(Boolean);
  const rlPlatformRows = Array.from(
    new Set([...Object.keys(rlPlatforms), ...pendingPlatforms]),
  ).sort();
  const rlAddablePlatforms = Array.from(new Set([...channelPlatforms, "dashboard"]))
    .filter((p) => !rlPlatformRows.includes(p))
    .sort();

  // Interval options for the heartbeat poll (minutes). Empty = inherit the
  // system-wide default (hourly).
  const hbIntervals = [5, 15, 30, 60, 120, 360, 720, 1440];
  const hbLabel = (m: number) =>
    m < 60 ? `${m} ${t("ov.hbMinUnit")}` : `${m / 60} ${t("ov.hbHourUnit")}`;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(360px,1fr))", gap: 14 }}>
      <AvatarEditor
        agentName={agentName}
        avatarUrl={cfg.avatar_url}
        sprites={cfg.sprites}
        emoji={cfg.emoji}
        color={cfg.color}
        description={cfg.description}
        openaiImageGen={imageGen}
        spritesPending={spritesPending}
        onSpritesPending={onSpritesPending}
        onChanged={reload}
      />

      <DCard title={t("ov.identity")}>
        <Field label={t("form.model")} hint={t("form.modelHint")}>
          <select
            className="input"
            value={cfg.model || ""}
            onChange={(e) => {
              setCfg({ ...cfg, model: e.target.value });
              void savePatch({ model: e.target.value });
            }}
          >
            {cfg.model && !models.some((m) => m.value === cfg.model) && (
              <option value={cfg.model}>{modelValueLabel(cfg.model)}</option>
            )}
            {models.length === 0 && !cfg.model && <option value="">—</option>}
            <ModelOptionGroups options={models} />
          </select>
        </Field>
        {(() => {
          // Reasoning-effort picker — rendered only when the selected model's
          // catalog metadata advertises effort levels, so it can never offer a
          // setting the model would reject.
          const rMeta = modelReasoningMeta(cat.providers, cfg.model || "");
          if (!rMeta) return null;
          return (
            <Field
              label={t("form.reasoning")}
              hint={
                <>
                  {t("form.reasoningHint")}
                  <OptionNotes items={noteItems(t, REASONING_NOTES)} />
                </>
              }
            >
              <select
                className="input"
                value={cfg.reasoning_effort || ""}
                onChange={(e) => {
                  const v = e.target.value || null;
                  setCfg({ ...cfg, reasoning_effort: v });
                  void savePatch({ reasoning_effort: v });
                }}
              >
                <option value="">
                  {rMeta.defaultEffort
                    ? t("form.reasoningDefault", { level: rMeta.defaultEffort })
                    : t("form.reasoningModelDefault")}
                </option>
                {rMeta.efforts.map((lvl) => (
                  <option key={lvl} value={lvl}>
                    {lvl}
                  </option>
                ))}
              </select>
            </Field>
          );
        })()}
        <Field
          label={t("ov.access")}
          hint={
            <>
              {t("ov.accessHint")}
              <OptionNotes items={noteItems(t, ENV_NOTES)} />
            </>
          }
        >
          <select
            className="input"
            value={env}
            onChange={(e) => {
              const v = e.target.value as EnvShorthand;
              setCfg({ ...cfg, backend: v });
              void savePatch({ backend: v }, t("toast.envUpdated"));
            }}
          >
            <option value="filesystem+shell">{t("env.execute")}</option>
            <option value="filesystem">{t("env.write")}</option>
            <option value="read">{t("env.read")}</option>
            {/* Virtual (no real host filesystem) backends — virtual files, no shell.
                "state" is thread-local/ephemeral; "store" persists across threads. */}
            <option value="state">{t("env.sandbox")}</option>
            <option value="store">{t("env.virtual")}</option>
          </select>
        </Field>
        <Field label={t("form.displayName")} hint={t("form.displayNameHint")}>
          <InlineText
            value={cfg.display_name || ""}
            placeholder={cfg.name}
            onCommit={(next) => {
              setCfg({ ...cfg, display_name: next });
              void savePatch({ display_name: next });
            }}
          />
        </Field>
        <Field label={t("form.title")} hint={t("form.titleHint")}>
          <InlineText
            value={cfg.title || ""}
            placeholder={t("form.titlePlaceholder")}
            onCommit={(next) => {
              setCfg({ ...cfg, title: next });
              void saveTitle(next);
            }}
          />
        </Field>
        <Field label={t("form.description")} hint={t("form.descHint")}>
          <InlineText
            value={cfg.description || ""}
            placeholder={t("form.descPlaceholder")}
            onCommit={(next) => {
              setCfg({ ...cfg, description: next });
              void savePatch({ description: next });
            }}
          />
        </Field>
        <Field label={t("form.accentColor")} hint={t("form.accentHint")}>
          <InlineColor
            value={cfg.color || ""}
            onCommit={(next) => {
              setCfg({ ...cfg, color: next || null });
              void savePatch({ color: next || null });
            }}
          />
        </Field>
      </DCard>

      <DCard
        title={t("ov.systemPrompt")}
        action={
          <GeneratePromptButton
            name={cfg.name}
            description={cfg.description || ""}
            tools={cfg.tools || []}
            model={cfg.model || undefined}
            kind="agent"
            onGenerated={(next) => {
              setCfg({ ...cfg, system_prompt: next });
              void savePatch({ system_prompt: next });
            }}
          />
        }
      >
        <InlinePrompt
          value={cfg.system_prompt || ""}
          onCommit={(next) => {
            setCfg({ ...cfg, system_prompt: next });
            void savePatch({ system_prompt: next }, t("toast.promptSaved"));
          }}
        />
        <p className="muted" style={{ fontSize: 11, margin: "8px 0 0", lineHeight: 1.45 }}>
          {t("form.promptHint")}
        </p>
      </DCard>

      <DCard title={t("ov.memory")}>
        <KvRow label={t("ov.markdown")} hint={t("ov.markdownHint")}>
          <Toggle checked={mem.markdown ?? true} onChange={(v) => setMemFlag("markdown", v)} />
        </KvRow>
        <KvRow label={t("ov.rag")} hint={t("ov.ragHint")}>
          <Toggle checked={mem.rag ?? false} onChange={(v) => setMemFlag("rag", v)} />
        </KvRow>
        <KvRow label={t("ov.selfLearning")} hint={t("ov.selfLearningHint")}>
          <Toggle checked={mem.self_learning ?? true} onChange={(v) => setMemFlag("self_learning", v)} />
        </KvRow>
        <KvRow label={t("ov.interval")} hint={t("ov.intervalHint")}>
          <input
            type="number"
            className="input"
            min={60}
            step={60}
            value={mem.self_learning_interval ?? 3600}
            onChange={(e) => setMemFlag("self_learning_interval", parseInt(e.target.value, 10) || 3600)}
            style={{ width: 90 }}
          />
        </KvRow>
      </DCard>

      <DCard title={t("ov.cortex")}>
        {/* Three-state, not a toggle: `cognition.enabled` is an override, and a
            binary switch has no way to say "inherit". A plain toggle here would
            silently mint a force-on override the moment anyone flipped it back
            and forth — which then survives a later fleet-wide kill-switch. The
            Cortex view's engine row is the same control. */}
        <KvRow label={t("ov.cortexEnabled")} hint={t("ov.cortexEnabledHint")}>
          <div className="seg">
            {[
              { label: t("ov.cortexInherit"), val: null },
              { label: t("ov.cortexOn"), val: true },
              { label: t("ov.cortexOff"), val: false },
            ].map((o) => (
              <button
                key={String(o.val)}
                className={`seg-btn${(cog.enabled ?? null) === o.val ? " active" : ""}`}
                aria-pressed={(cog.enabled ?? null) === o.val}
                style={{ fontSize: 12, padding: "4px 10px" }}
                onClick={() => setCogFlag("enabled", o.val)}
              >
                {o.label}
              </button>
            ))}
          </div>
        </KvRow>
        <p className="muted" style={{ fontSize: 11, margin: "8px 0 0" }}>
          {t("ov.cortexNote")}{" "}
          <button
            onClick={() => {
              setAgent(agentName, { navigate: false });
              navigate(viewPath("cortex", { agent: agentName }));
            }}
            style={{
              border: 0,
              background: "none",
              padding: 0,
              cursor: "pointer",
              color: "var(--accent)",
              font: "inherit",
              textDecoration: "underline",
            }}
          >
            {t("ov.cortexLink")}
          </button>
        </p>
      </DCard>

      <DCard title={t("ov.autonomy")}>
        {/* Same three-state rule as the Cortex card above: each capability is
            an OVERRIDE of the deployment-wide Agent self-configuration block
            (Config, admin-only), and a binary toggle has no way to say
            "inherit" — flipping one back and forth would mint a standing
            per-agent override that survives a later fleet-wide change. */}
        <p className="muted" style={{ fontSize: 11.5, margin: "0 0 10px" }}>
          {t("ov.autonomyLead")}
        </p>
        {(
          [
            ["explore_archetypes", "ov.autExplore", "ov.autExploreHint"],
            ["change_model", "ov.autModel", "ov.autModelHint"],
            ["modify_prompt", "ov.autPrompt", "ov.autPromptHint"],
            ["restart_self", "ov.autRestart", "ov.autRestartHint"],
            ["add_subagents", "ov.autSubagents", "ov.autSubagentsHint"],
            ["connect_mcps", "ov.autMcps", "ov.autMcpsHint"],
            ["connect_platforms", "ov.autPlatforms", "ov.autPlatformsHint"],
            ["manage_skills", "ov.autSkills", "ov.autSkillsHint"],
            ["magic_links", "ov.autMagicLinks", "ov.autMagicLinksHint"],
            ["external_access", "ov.autExternal", "ov.autExternalHint"],
          ] as Array<[keyof typeof aut & string, string, string]>
        ).map(([key, labelKey, hintKey]) => (
          <KvRow key={key} label={t(labelKey)} hint={t(hintKey)}>
            <div className="seg">
              {[
                { label: t("ov.cortexInherit"), val: null },
                { label: t("ov.cortexOn"), val: true },
                { label: t("ov.cortexOff"), val: false },
              ].map((o) => (
                <button
                  key={String(o.val)}
                  className={`seg-btn${(aut[key] ?? null) === o.val ? " active" : ""}`}
                  aria-pressed={(aut[key] ?? null) === o.val}
                  style={{ fontSize: 12, padding: "4px 10px" }}
                  onClick={() => setAutFlag(key, o.val)}
                >
                  {o.label}
                </button>
              ))}
            </div>
          </KvRow>
        ))}
        <p className="muted" style={{ fontSize: 11, margin: "8px 0 0" }}>
          {t("ov.autonomyNote")}
        </p>
      </DCard>

      <DCard title={t("ov.heartbeat")}>
        <KvRow label={t("ov.hbEnabled")} hint={t("ov.hbEnabledHint")}>
          <Toggle checked={hb.enabled ?? true} onChange={(v) => setHbField("enabled", v)} />
        </KvRow>
        <KvRow
          label={t("ov.hbInterval")}
          hint={t("ov.hbIntervalHint")}
          note={<OptionNotes items={noteItems(t, HB_NOTES)} />}
        >
          <select
            className="input"
            value={hb.interval_minutes ?? ""}
            onChange={(e) =>
              setHbField("interval_minutes", e.target.value === "" ? null : parseInt(e.target.value, 10))
            }
            style={{ width: 220 }}
          >
            <option value="">{t("ov.hbSystemDefault")}</option>
            {hb.interval_minutes != null && !hbIntervals.includes(hb.interval_minutes) && (
              <option value={hb.interval_minutes}>{hbLabel(hb.interval_minutes)}</option>
            )}
            {hbIntervals.map((m) => (
              <option key={m} value={m}>
                {hbLabel(m)}
              </option>
            ))}
          </select>
        </KvRow>
      </DCard>

      <GmailWatchCard
        agentName={cfg.name}
        value={cfg.gmail_watch}
        onSave={(next) => {
          setCfg({ ...cfg, gmail_watch: next });
          return savePatch({ gmail_watch: next });
        }}
      />

      <ObservabilityCard
        agentName={cfg.name}
        value={cfg.observability}
        onSave={(next) => {
          setCfg({ ...cfg, observability: next });
          return savePatch({ observability: next });
        }}
      />

      <DCard title={t("ov.oversight")}>
        <KvRow
          label={t("ov.mode")}
          hint={t("ov.modeHint")}
          note={<OptionNotes items={noteItems(t, MODE_NOTES)} />}
        >
          <select
            className="input"
            value={sec.profile || "developer"}
            onChange={(e) => setSecFlag("profile", e.target.value)}
            style={{ width: 200 }}
          >
            <option value="developer">{t("ov.profileTrusted")}</option>
            <option value="safe">{t("ov.profileSupervised")}</option>
            <option value="production">{t("ov.profileLocked")}</option>
          </select>
        </KvRow>
        <KvRow label={t("ov.askWrite")} hint={t("ov.askWriteHint")}>
          <Toggle checked={sec.hitl_write_tools ?? false} onChange={(v) => setSecFlag("hitl_write_tools", v)} />
        </KvRow>
        <KvRow label={t("ov.askShell")} hint={t("ov.askShellHint")}>
          <Toggle checked={sec.hitl_shell ?? false} onChange={(v) => setSecFlag("hitl_shell", v)} />
        </KvRow>
        <KvRow label={t("ov.scanInput")} hint={t("ov.scanInputHint")}>
          <Toggle checked={sec.pii_scan_input ?? false} onChange={(v) => setSecFlag("pii_scan_input", v)} />
        </KvRow>
        <KvRow label={t("ov.scanOutput")} hint={t("ov.scanOutputHint")}>
          <Toggle checked={sec.pii_scan_output ?? false} onChange={(v) => setSecFlag("pii_scan_output", v)} />
        </KvRow>
        <KvRow label={t("ov.hideSecrets")} hint={t("ov.hideSecretsHint")}>
          <Toggle checked={sec.credential_scrub ?? true} onChange={(v) => setSecFlag("credential_scrub", v)} />
        </KvRow>
      </DCard>

      <DCard title={t("ov.accessSafety")}>
        <KvRow label={t("ov.humanOnly")} hint={t("ov.humanOnlyHint")}>
          <Toggle checked={cfg.human_only ?? false} onChange={(v) => setTopFlag("human_only", v)} />
        </KvRow>
        <KvRow label={t("ov.fleetAccess")} hint={t("ov.fleetAccessHint")}>
          <Toggle
            checked={cfg.fleet_access ?? false}
            onChange={(v) => void setFleetAccess(v)}
            ariaLabel={t("ov.fleetAccess")}
          />
        </KvRow>
        <KvRow label={t("ov.hidden")} hint={t("ov.hiddenHint")}>
          <Toggle checked={cfg.hidden ?? false} onChange={(v) => setTopFlag("hidden", v)} />
        </KvRow>
        <KvRow
          label={t("ov.defaultVerbosity")}
          // The hint rides in `note`, not `hint`: next to a dropdown the left
          // column is narrow enough at the mobile breakpoint that a paragraph
          // there renders as a ~175px ribbon beside dead space. `note` spans the
          // row, so the explanation reads at full width like the toggle rows do.
          note={
            <>
              <p className="muted" style={{ fontSize: 11, lineHeight: 1.45, margin: "8px 0 0" }}>
                {t("ov.defaultVerbosityHint")}
              </p>
              <OptionNotes items={noteItems(t, VERBOSITY_NOTES)} />
            </>
          }
        >
          <select
            className="input"
            value={cfg.default_verbosity || "normal"}
            onChange={(e) => setTopFlag("default_verbosity", e.target.value)}
            style={{ width: 140 }}
          >
            <option value="normal">{t("ov.verbosityNormal")}</option>
            <option value="quiet">{t("ov.verbosityQuiet")}</option>
            <option value="verbose">{t("ov.verbosityVerbose")}</option>
          </select>
        </KvRow>
        <NumberSetting
          label={t("ov.iterationBudget")}
          hint={t("ov.iterationBudgetHint")}
          value={cfg.iteration_budget}
          def={0}
          min={0}
          onSave={(n) => setTopFlag("iteration_budget", n)}
        />
        <NumberSetting
          label={t("ov.truncRetry")}
          hint={t("ov.truncRetryHint")}
          value={cfg.truncation_retry_limit}
          def={3}
          min={0}
          onSave={(n) => setTopFlag("truncation_retry_limit", n)}
        />
        <NumberSetting
          label={t("ov.toolRetry")}
          hint={t("ov.toolRetryHint")}
          value={cfg.tool_error_retry_limit}
          def={3}
          min={0}
          onSave={(n) => setTopFlag("tool_error_retry_limit", n)}
        />
      </DCard>

      <DCard title={t("ov.rateLimits")}>
        <p className="muted" style={{ fontSize: 11, margin: "0 0 10px" }}>
          {t("ov.rateLimitsHint")}
        </p>
        <KvRow label={t("ov.rlPerMinute")} hint={t("ov.rlInheritHint")}>
          <input
            type="number"
            className="input"
            min={1}
            placeholder={t("ov.rlUnlimited")}
            value={rlMessages.per_minute ?? ""}
            onChange={(e) => setAgentWindow("per_minute", e.target.value)}
            style={{ width: 120 }}
          />
        </KvRow>
        <KvRow label={t("ov.rlPerHour")}>
          <input
            type="number"
            className="input"
            min={1}
            placeholder={t("ov.rlUnlimited")}
            value={rlMessages.per_hour ?? ""}
            onChange={(e) => setAgentWindow("per_hour", e.target.value)}
            style={{ width: 120 }}
          />
        </KvRow>
        <KvRow label={t("ov.rlPerDay")}>
          <input
            type="number"
            className="input"
            min={1}
            placeholder={t("ov.rlUnlimited")}
            value={rlMessages.per_day ?? ""}
            onChange={(e) => setAgentWindow("per_day", e.target.value)}
            style={{ width: 120 }}
          />
        </KvRow>

        {/* Per-platform overrides — advanced, collapsed by default. */}
        <div style={{ marginTop: 16, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
          <button
            className="btn-ghost"
            onClick={() => setShowRlPlatforms((v) => !v)}
            style={{ fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 6 }}
          >
            <span style={{ color: "var(--text-3)" }}>{showRlPlatforms ? "▾" : "▸"}</span>
            {t("ov.rlPerPlatform")}
          </button>
          {showRlPlatforms && (
            <div style={{ marginTop: 10 }}>
              <p className="muted" style={{ fontSize: 11, margin: "0 0 10px" }}>
                {t("ov.rlPerPlatformHint")}
              </p>
              {rlPlatformRows.length === 0 && rlAddablePlatforms.length === 0 && (
                <p className="muted" style={{ fontSize: 12 }}>
                  {t("ov.rlNoPlatforms")}
                </p>
              )}
              {rlPlatformRows.map((p) => (
                <div
                  key={p}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "6px 0",
                    flexWrap: "wrap",
                  }}
                >
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 13, minWidth: 84 }}>{p}</span>
                  {rlWindowKeys.map((key) => (
                    <input
                      key={key}
                      type="number"
                      className="input"
                      min={1}
                      placeholder={t(`ov.rlPh.${key}` as Parameters<typeof t>[0])}
                      value={rlPlatforms[p]?.[key] ?? ""}
                      onChange={(e) => setPlatformWindow(p, key, e.target.value)}
                      style={{ width: 72 }}
                    />
                  ))}
                  <button
                    className="btn-ghost"
                    onClick={() => removePlatform(p)}
                    title={t("ov.rlRemove")}
                    style={{ color: "var(--text-3)" }}
                  >
                    ✕
                  </button>
                </div>
              ))}
              {rlAddablePlatforms.length > 0 && (
                <select
                  className="input"
                  value=""
                  onChange={(e) => {
                    if (e.target.value) setPendingPlatforms((p) => [...p, e.target.value]);
                  }}
                  style={{ marginTop: 8, width: 220 }}
                >
                  <option value="">{t("ov.rlAddPlatform")}</option>
                  {rlAddablePlatforms.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}
        </div>
      </DCard>

      <ScheduleSection
        entries={schedule}
        agentModel={cfg.model || ""}
        onChange={(next) => {
          setCfg({ ...cfg, schedule: next });
          void savePatch({ schedule: next }, t("ov.schedule.saved"));
        }}
      />
    </div>
  );
}

// Editable agent-level schedule (agent.json5 `schedule`). Same jobs the
// Scheduler view now surfaces with a "Config" badge — edited here they write
// back to the file and apply live (PUT /api/agents/{name} → resync).
function cronOf(s: ScheduleEntry): string {
  if (s.cron && s.cron.trim().split(/\s+/).length === 5) return s.cron.trim();
  return `${s.minute ?? "*"} ${s.hour ?? "*"} ${s.day ?? "*"} ${s.month ?? "*"} ${s.day_of_week ?? "*"}`;
}

function ScheduleSection({
  entries,
  agentModel,
  onChange,
}: {
  entries: ScheduleEntry[];
  // The agent's configured model: what an entry runs on when it names none,
  // and whose effort ladder the entry's effort select offers by default.
  agentModel: string;
  onChange: (next: ScheduleEntry[]) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  // editIdx: number → editing that row; -1 → adding; null → modal closed.
  const [editIdx, setEditIdx] = useState<number | null>(null);
  const [confirmIdx, setConfirmIdx] = useState<number | null>(null);

  function toggle(i: number) {
    // enabled === false → currently paused → resume (true); otherwise pause (false).
    const next = entries.map((e, j) => (j === i ? { ...e, enabled: e.enabled === false } : e));
    onChange(next);
  }

  function remove(i: number) {
    onChange(entries.filter((_, j) => j !== i));
    setConfirmIdx(null);
  }

  function commit(entry: ScheduleEntry) {
    const next = editIdx === -1 ? [...entries, entry] : entries.map((e, j) => (j === editIdx ? entry : e));
    onChange(next);
    setEditIdx(null);
  }

  return (
    <DCard title={t("ov.schedule")}>
      <p className="muted" style={{ fontSize: 12, margin: "0 0 10px" }}>
        {t("ov.schedule.hint")}
      </p>
      {entries.length ? (
        <ul style={{ listStyle: "none", margin: "0 0 10px", padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {entries.map((s, i) => {
            const paused = s.enabled === false;
            return (
              <li
                key={i}
                className="card"
                style={{ padding: "8px 10px", display: "flex", alignItems: "flex-start", gap: 8, opacity: paused ? 0.6 : 1 }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {s.name || s.message || cronOf(s)}
                    {paused ? <span className="muted" style={{ fontWeight: 400 }}> · {t("ov.schedule.paused")}</span> : null}
                  </div>
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                    <code>{cronOf(s)}</code>
                    {s.timezone ? ` · ${s.timezone}` : ""}
                  </div>
                  {s.name && s.message ? (
                    <div className="muted" style={{ fontSize: 12, marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {s.message}
                    </div>
                  ) : null}
                </div>
                <div className="row" style={{ gap: 4 }}>
                  <button className="btn" style={{ padding: "4px 8px" }} title={paused ? t("ov.schedule.resume") : t("ov.schedule.pause")} onClick={() => toggle(i)}>
                    {paused ? t("ov.schedule.resume") : t("ov.schedule.pause")}
                  </button>
                  <button className="btn" style={{ padding: "4px 8px" }} title={t("ov.schedule.edit")} onClick={() => setEditIdx(i)}>
                    <Pencil size={14} strokeWidth={1.75} />
                  </button>
                  <button className="btn danger" style={{ padding: "4px 8px" }} title={t("ov.schedule.delete")} onClick={() => setConfirmIdx(i)}>
                    <Trash2 size={14} strokeWidth={1.75} />
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted" style={{ fontSize: 13, margin: "0 0 10px" }}>
          {t("ov.noSchedule")}
        </p>
      )}

      <button className="btn" onClick={() => setEditIdx(-1)}>
        <Plus size={15} strokeWidth={1.75} /> {t("ov.schedule.add")}
      </button>

      {editIdx !== null ? (
        <ScheduleEntryModal
          entry={editIdx === -1 ? null : entries[editIdx]}
          agentModel={agentModel}
          onClose={() => setEditIdx(null)}
          onSave={commit}
          onError={(msg) => toast.push(msg, "error")}
        />
      ) : null}

      {confirmIdx !== null ? (
        <ConfirmModal
          title={t("ov.schedule.deleteTitle")}
          message={t("ov.schedule.deleteMsg")}
          confirmLabel={t("ov.schedule.delete")}
          danger
          onConfirm={() => remove(confirmIdx)}
          onClose={() => setConfirmIdx(null)}
        />
      ) : null}
    </DCard>
  );
}

function ScheduleEntryModal({
  entry,
  agentModel,
  onClose,
  onSave,
  onError,
}: {
  entry: ScheduleEntry | null;
  agentModel: string;
  onClose: () => void;
  onSave: (e: ScheduleEntry) => void;
  onError: (msg: string) => void;
}) {
  const t = useT(STR);
  const [name, setName] = useState(entry?.name ?? "");
  const [message, setMessage] = useState(entry?.message ?? "");
  const [schedule, setSchedule] = useState(entry ? cronOf(entry) : "");
  const [timezone, setTimezone] = useState(entry?.timezone ?? "");
  // Per-entry model / effort overrides ("" = the agent's own). The selects
  // offer only the ladder of the model the entry runs on, so nothing the
  // runtime would drop can be chosen here.
  const [overrides, setOverrides] = useState({
    model: entry?.model ?? "",
    reasoning_effort: entry?.reasoning_effort ?? "",
  });

  function save() {
    if (!message.trim()) {
      onError(t("ov.schedule.msgRequired"));
      return;
    }
    if (!schedule.trim()) {
      onError(t("ov.schedule.cronRequired"));
      return;
    }
    // Preserve fields the editor doesn't surface (thread_id); canonicalize onto
    // `cron` and drop the split minute/hour/… fields for a single source of truth.
    const next: ScheduleEntry = {
      ...(entry?.thread_id ? { thread_id: entry.thread_id } : {}),
      name: name.trim() || undefined,
      message: message.trim(),
      cron: schedule.trim(),
      timezone: timezone.trim() || undefined,
      enabled: entry?.enabled === false ? false : undefined,
      model: overrides.model || undefined,
      reasoning_effort: overrides.reasoning_effort || undefined,
    };
    onSave(next);
  }

  return (
    <Modal
      title={entry ? t("ov.schedule.edit") : t("ov.schedule.new")}
      onClose={onClose}
      width={560}
      footer={
        <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
          <button className="btn" onClick={onClose}>
            {t("btn.cancel")}
          </button>
          <button className="btn primary" onClick={save}>
            {t("btn.save")}
          </button>
        </div>
      }
    >
      <Field label={t("ov.schedule.name")}>
        <input className="input" value={name} placeholder={t("ov.schedule.namePlaceholder")} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label={t("ov.schedule.message")}>
        <textarea
          className="input"
          rows={4}
          style={{ resize: "vertical" }}
          value={message}
          placeholder={t("ov.schedule.messagePlaceholder")}
          onChange={(e) => setMessage(e.target.value)}
        />
      </Field>
      <ScheduleBuilder
        schedule={schedule}
        runAt=""
        timezone={timezone}
        onChange={(next) => {
          setSchedule(next.schedule);
          setTimezone(next.timezone);
        }}
      />
      <TaskOverrideFields
        model={overrides.model}
        effort={overrides.reasoning_effort}
        agentModel={agentModel || undefined}
        onChange={setOverrides}
      />
    </Modal>
  );
}

// ── Empty-state block ────────────────────────────────────────────────────
function Empty({ icon, title, sub }: { icon: React.ReactNode; title: string; sub: string }) {
  return (
    <div style={{ textAlign: "center", padding: "40px 20px", color: "var(--text-3)" }}>
      <div style={{ display: "flex", justifyContent: "center", color: "var(--text-4)" }}>{icon}</div>
      <div style={{ fontWeight: 600, color: "var(--text-2)", marginTop: 6 }}>{title}</div>
      <div style={{ fontSize: 13, marginTop: 4 }}>{sub}</div>
    </div>
  );
}

// Title + action cluster. Layout lives in .section-head (globals.css): a single
// row on desktop, title-over-a-two-column-button-grid on phones — where the old
// `space-between` row starved the title to its narrowest word and left the
// buttons wrapping over three ragged rows.
function SectionHead({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="section-head">
      <strong className="section-head-title">{title}</strong>
      <div className="section-head-actions">{children}</div>
    </div>
  );
}

function Row({
  glyph,
  title,
  sub,
  meta,
  actions,
}: {
  glyph: React.ReactNode;
  title: React.ReactNode;
  sub?: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="card" style={{ padding: "12px 14px", marginBottom: 8 }}>
      <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
        <div className="avatar-sm" style={{ background: "var(--surface-3)", color: "var(--text-2)" }}>
          {glyph}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>{title}</div>
          {sub ? (
            <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>
              {sub}
            </div>
          ) : null}
          {meta ? (
            <div className="row" style={{ gap: 5, marginTop: 6, flexWrap: "wrap" }}>
              {meta}
            </div>
          ) : null}
        </div>
        {actions ? <div className="row" style={{ gap: 6 }}>{actions}</div> : null}
      </div>
    </div>
  );
}

// ── Tools tab — agent-level tool catalog (auto-saves) ────────────────────
function ToolsTab({
  cfg,
  setCfg,
  savePatch,
}: {
  cfg: AgentConfig;
  setCfg: (c: AgentConfig) => void;
  savePatch: (patch: Partial<AgentConfig>, msg?: string) => Promise<boolean>;
}) {
  const t = useT(STR);
  const cat = useCatalog();
  const policy = useHitlPolicy();
  const [showApproval, setShowApproval] = useState(false);
  // Cast: the AgentConfig type doesn't model the per-tool `security.tools` map.
  const sec = (cfg.security || {}) as Record<string, unknown> & {
    tools?: Record<string, { approval?: ToolApproval }>;
  };
  // The Tools tab shows the *effective* toolset: the agent's own extra tools,
  // every auto-injected default (universal + document), and every internal tool
  // (platform-granted, GET /api/agents/{name} → internal_tools)
  // that hasn't been explicitly disabled. Unchecking a default or internal tool
  // records the removal in `disabled_tools` — a plain `tools` edit can't drop
  // one because AgentConfig/the builder re-injects it (that was the "removed
  // talk_to_agent but it kept working" bug).
  const disabledTools = cfg.disabled_tools || [];
  const defaults = cat.agentDefaults || [];
  const internalRows = cfg.internal_tools || [];
  const selectedTools = useMemo(
    () => selectedExtraTools(cfg, defaults),
    [cfg.tools, cfg.disabled_tools, defaults],
  );
  // Union by name with the internal tools currently on — several of them
  // (e.g. the universal defaults, graph/notes when memory is on) also exist in
  // the catalog, so this must dedupe, not concatenate. internalOnNames resolves
  // on-state against the live disabled_tools so an optimistic uncheck doesn't
  // flicker back while savePatch's reconcile GET is in flight.
  const internalOn = useMemo(() => internalOnNames(cfg), [cfg.internal_tools, cfg.disabled_tools]);
  const effectiveSelected = useMemo(
    () => [...new Set([...selectedTools, ...internalOn])],
    [selectedTools, internalOn],
  );
  const internalOnCount = internalOn.length;

  function onToolsChange(next: string[]) {
    const defaultsSet = new Set(defaults);
    const internalNames = new Set(internalRows.map((r) => r.name));
    // Everything auto-granted whose on/off state lives in disabled_tools:
    // the universal defaults plus every toggleable internal row. Locked rows
    // (toggleable: false — mode condition unmet or allow-list-excluded) are
    // not user-controllable, so they must never be recorded as disabled.
    const disableCandidates = [
      ...new Set([
        ...defaults,
        ...internalRows.filter((r) => r.toggleable !== false).map((r) => r.name),
      ]),
    ];
    const nextSet = new Set(next);
    // Preserve prior disables for tools still unchecked; drop any the user just
    // re-checked. Then add auto-granted tools that are now unchecked (this is
    // the only way to actually drop a default like talk_to_agent or an internal tool
    // like memory_save).
    const preserved = disabledTools.filter((d) => !nextSet.has(d));
    const unchecked = disableCandidates.filter((d) => !nextSet.has(d));
    const nextDisabled = [...new Set([...preserved, ...unchecked])].sort();
    // Checked tools that aren't auto-granted → the agent's extras. An internal tool
    // the agent ALSO listed explicitly in `tools` stays listed (dropping it
    // would silently lose the tool if the internal row's condition later turns
    // off, e.g. memory disabled).
    const explicit = new Set(cfg.tools || []);
    const nextTools = next.filter(
      (n) => !defaultsSet.has(n) && (!internalNames.has(n) || explicit.has(n)),
    );
    setCfg({ ...cfg, tools: nextTools, disabled_tools: nextDisabled });
    void savePatch({ tools: nextTools, disabled_tools: nextDisabled });
  }

  function setToolApproval(name: string, decision: ToolApproval) {
    const tools = { ...(sec.tools || {}) };
    if (decision === "default") {
      delete tools[name];
    } else {
      tools[name] = { ...(tools[name] || {}), approval: decision };
    }
    const nextSec = { ...sec, tools };
    setCfg({ ...cfg, security: nextSec });
    void savePatch({ security: nextSec });
  }

  return (
    <>
      <SectionHead title={t("ov.tools")}>
        <span className="muted" style={{ fontSize: 12 }}>
          {t("ov.toolsActive", { n: String(effectiveSelected.length) })}
          {internalRows.length > 0
            ? ` · ${t("ov.toolsInternal", { n: String(internalOnCount) })}`
            : ""}
        </span>
      </SectionHead>
      <ToolBrowser
        all={cat.tools}
        bundles={cat.bundles}
        selected={effectiveSelected}
        internal={internalRows}
        onChange={onToolsChange}
      />
      <p className="muted" style={{ fontSize: 11, margin: "12px 0 0" }}>
        {t("ov.toolsHint")}
      </p>

      {/* Per-tool approval overrides — advanced, collapsed by default. */}
      <div style={{ marginTop: 20, borderTop: "1px solid var(--border)", paddingTop: 14 }}>
        <button
          className="btn-ghost"
          onClick={() => setShowApproval((v) => !v)}
          style={{ fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 6 }}
        >
          <span style={{ color: "var(--text-3)" }}>{showApproval ? "▾" : "▸"}</span>
          {t("ov.approvalOverrides")}
        </button>
        {showApproval && (
          <div style={{ marginTop: 10 }}>
            <div className="muted" style={{ fontSize: 11, margin: "0 0 10px", lineHeight: 1.45 }}>
              {t("ov.approvalOverridesHint")}
              <OptionNotes items={noteItems(t, APPROVAL_NOTES)} />
            </div>
            {selectedTools.length === 0 ? (
              <p className="muted" style={{ fontSize: 12 }}>
                {t("ov.approvalNoTools")}
              </p>
            ) : (
              [...selectedTools].sort().map((name) => {
                const eff = effectiveApproval(policy, sec, name);
                const override = sec.tools?.[name]?.approval ?? "default";
                return (
                  <div
                    key={name}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      gap: 10,
                      padding: "6px 0",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                      <span
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontSize: 13,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                        }}
                      >
                        {name}
                      </span>
                      <span
                        className="badge"
                        style={{
                          padding: "0 6px",
                          color: eff.required ? "var(--warn)" : "var(--text-3)",
                          borderColor: eff.required ? "var(--warn)" : "var(--border)",
                        }}
                      >
                        {eff.required ? t("ov.approvalRequired") : t("ov.approvalNotRequired")}
                      </span>
                    </div>
                    <select
                      className="input"
                      value={override}
                      onChange={(e) => setToolApproval(name, e.target.value as ToolApproval)}
                      style={{ maxWidth: 170, flexShrink: 0 }}
                    >
                      <option value="default">{t("ov.approvalDefault")}</option>
                      <option value="always">{t("ov.approvalAlways")}</option>
                      <option value="never">{t("ov.approvalNever")}</option>
                    </select>
                  </div>
                );
              })
            )}
          </div>
        )}
      </div>
    </>
  );
}

// ── Subagents tab ────────────────────────────────────────────────────────
function SubagentsTab({ cfg, agentName, reload }: { cfg: AgentConfig; agentName: string; reload: () => void }) {
  const toast = useToast();
  const t = useT(STR);
  const subs = cfg.subagents_resolved || cfg.subagents || [];
  const [edit, setEdit] = useState<{ sub: SubagentEntry | null } | null>(null);
  // Catalog-sourced rows (archetype experts / builtin integrations) edit their
  // OVERRIDES (model, appended instructions, optional prompt detach) — a
  // different modal from the inline free-form editor above.
  const [overrideEdit, setOverrideEdit] = useState<SubagentEntry | null>(null);
  const [view, setView] = useState<SubagentEntry | null>(null);
  const [del, setDel] = useState<SubagentEntry | null>(null);
  const [busy, setBusy] = useState(false);

  async function doDelete(s: SubagentEntry) {
    setBusy(true);
    const ref = s.ref || s.from_archetype || s.name;
    try {
      await api.del(`/api/agents/${encodeURIComponent(agentName)}/subagents/${encodeURIComponent(ref)}`);
      toast.push(t("toast.subRemoved"));
      reload();
    } catch (e) {
      toast.push(errText(e, t("toast.subRemoveFailed")), "error");
    } finally {
      setBusy(false);
      setDel(null);
    }
  }

  return (
    <>
      <SectionHead title={t("sub.title")}>
        <button className="btn primary row" style={{ gap: 6 }} onClick={() => setEdit({ sub: null })}>
          <Plus size={15} strokeWidth={1.75} /> {t("sub.add")}
        </button>
      </SectionHead>
      {subs.length ? (
        subs.map((s) => {
          const fromCatalog = !!(s.from_builtin || s.from_archetype);
          return (
            <Row
              key={s.ref || s.name}
              glyph={<Bot size={16} strokeWidth={1.75} />}
              title={
                <>
                  <strong style={{ fontSize: 14 }}>{s.name}</strong>
                  {s.from_builtin && (
                    <span className="badge row" style={{ gap: 4 }}>
                      <Plug size={12} strokeWidth={1.75} /> {s.from_builtin}
                    </span>
                  )}
                  {s.from_archetype && (
                    <span className="badge row" style={{ gap: 4 }}>
                      <BookOpen size={12} strokeWidth={1.75} /> {s.from_archetype}
                    </span>
                  )}
                </>
              }
              sub={s.description}
              meta={
                <>
                  {(s.tools || []).slice(0, 8).map((tool) => (
                    <span key={tool} className="badge">
                      {tool}
                    </span>
                  ))}
                  {(s.tools || []).length > 8 && <span className="badge">+{(s.tools || []).length - 8}</span>}
                  {s.model && (
                    <span className="badge" style={{ fontFamily: "var(--font-mono)" }}>
                      {s.model}
                    </span>
                  )}
                </>
              }
              actions={
                <>
                  <button className="btn ghost" style={{ padding: "4px 8px" }} aria-label={t("sub.viewAria")} onClick={() => setView(s)}>
                    <Info size={14} strokeWidth={1.75} />
                  </button>
                  <button
                    className="btn ghost"
                    style={{ padding: "4px 8px" }}
                    aria-label={t("sub.editAria")}
                    onClick={() => (fromCatalog ? setOverrideEdit(s) : setEdit({ sub: s }))}
                  >
                    <Pencil size={14} strokeWidth={1.75} />
                  </button>
                  <button className="btn danger" style={{ padding: "4px 8px" }} aria-label={t("sub.removeAria")} onClick={() => setDel(s)}>
                    <Trash2 size={14} strokeWidth={1.75} />
                  </button>
                </>
              }
            />
          );
        })
      ) : (
        <Empty
          icon={<Bot size={34} strokeWidth={1.5} />}
          title={t("sub.emptyTitle")}
          sub={t("sub.emptySub")}
        />
      )}
      {view && (
        <SubagentDetailModal
          sub={view}
          parentModel={cfg.model}
          onClose={() => setView(null)}
          onEdit={() => {
            if (view.from_builtin || view.from_archetype) setOverrideEdit(view);
            else setEdit({ sub: view });
            setView(null);
          }}
        />
      )}
      {edit && (
        <SubagentModal
          agentName={agentName}
          existing={edit.sub}
          onClose={() => setEdit(null)}
          onDone={reload}
        />
      )}
      {overrideEdit && (
        <SubagentOverridesModal
          agentName={agentName}
          sub={overrideEdit}
          onClose={() => setOverrideEdit(null)}
          onDone={reload}
        />
      )}
      {del && (
        <ConfirmModal
          title={t("sub.removeTitle", { name: del.name })}
          message={t("sub.removeMsg")}
          confirmLabel={t("btn.remove")}
          danger
          busy={busy}
          onConfirm={() => void doDelete(del)}
          onClose={() => setDel(null)}
        />
      )}
    </>
  );
}

// ── Connectors tab ───────────────────────────────────────────────────────
function ConnectorsTab({ cfg, agentName, reload }: { cfg: AgentConfig; agentName: string; reload: () => void }) {
  const toast = useToast();
  const t = useT(STR);
  const channels = cfg.channels || [];
  const [health, setHealth] = useState<ConnectorHealth | null>(null);
  const [edit, setEdit] = useState<{ type: string | null; config: Record<string, unknown> } | null>(null);
  const [del, setDel] = useState<string | null>(null);
  const [diag, setDiag] = useState<ConnectorRow | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get<ConnectorHealth>(`/api/agents/${encodeURIComponent(agentName)}/connectors/health`)
      .then(setHealth)
      .catch((e) => setHealth({ connectors: [], agent_booted: false, error: errText(e, t("conn.diagUnreachable")) }));
  }, [agentName, cfg, t]);

  const byType = new Map<string, ConnectorRow>((health?.connectors || []).map((r) => [r.type, r]));

  function pill(type: string): React.ReactNode {
    const row = byType.get(type);
    if (!row || !row.runtime_attached) return <span className="badge" style={{ color: "var(--warn)" }}>{t("conn.notLoaded")}</span>;
    const failed = (row.snapshot?.checks || []).filter((c) => c.ok === false);
    if (failed.length) return <span className="badge" style={{ color: "var(--danger)" }}>{t(pluralKey(failed.length, "issue"), { n: String(failed.length) })}</span>;
    if (row.snapshot?.live) return <span className="badge" style={{ color: "var(--success)" }}>{t("conn.live")}</span>;
    return <span className="badge" style={{ color: "var(--warn)" }}>{t("conn.connecting")}</span>;
  }

  async function doDelete(type: string) {
    setBusy(true);
    try {
      await api.del(`/api/agents/${encodeURIComponent(agentName)}/channels/${encodeURIComponent(type)}`);
      toast.push(t("toast.connRemoved"));
      reload();
    } catch (e) {
      toast.push(errText(e, t("toast.connRemoveFailed")), "error");
    } finally {
      setBusy(false);
      setDel(null);
    }
  }

  return (
    <>
      <SectionHead title={t("conn.title")}>
        <button className="btn primary row" style={{ gap: 6 }} onClick={() => setEdit({ type: null, config: {} })}>
          <Plus size={15} strokeWidth={1.75} /> {t("conn.add")}
        </button>
      </SectionHead>
      {health?.error && (
        <div
          className="card"
          style={{ padding: "10px 14px", marginBottom: 12, color: "var(--danger)", fontSize: 13 }}
        >
          {t("conn.diagFailed", { error: health.error })}
        </div>
      )}
      {health && !health.agent_booted && !health.error && (
        <div
          className="card"
          style={{ padding: "10px 14px", marginBottom: 12, background: "var(--warn-soft)", fontSize: 13 }}
        >
          <strong>{t("conn.notRunningTitle")}</strong> {t("conn.notRunningMsg")}
        </div>
      )}
      {channels.length ? (
        channels.map((ch) => {
          const type = typeof ch === "string" ? ch : (ch as ChannelObject).type;
          const cfgObj: Record<string, unknown> = typeof ch === "string" ? {} : (ch as ChannelObject);
          const config = Object.fromEntries(Object.entries(cfgObj).filter(([k]) => k !== "type"));
          const snap = byType.get(type)?.snapshot;
          // A variant row (Lark on the Feishu adapter) prints its own name.
          const variantName = connectorVariantName(type, config);
          return (
            <Row
              key={type}
              glyph={channelGlyph(type)}
              title={
                <>
                  <strong style={{ fontSize: 14 }}>{variantName ? t(variantName) : channelLabel(type)}</strong>
                  {pill(type)}
                </>
              }
              sub={t(channelSummary(cfgObj as Record<string, unknown>))}
              meta={
                snap?.bot_user ? (
                  <span className="badge" style={{ fontFamily: "var(--font-mono)" }}>
                    {snap.bot_user}
                  </span>
                ) : undefined
              }
              actions={
                <>
                  <button className="btn ghost" style={{ padding: "4px 8px" }} aria-label={t("conn.editAria")} onClick={() => setEdit({ type, config })}>
                    <Pencil size={14} strokeWidth={1.75} />
                  </button>
                  <button
                    className="btn ghost"
                    style={{ padding: "4px 8px" }}
                    aria-label={t("conn.detailsAria")}
                    onClick={() => setDiag(byType.get(type) || { type, runtime_attached: false })}
                  >
                    <Info size={14} strokeWidth={1.75} />
                  </button>
                  <button className="btn danger" style={{ padding: "4px 8px" }} aria-label={t("conn.removeAria")} onClick={() => setDel(type)}>
                    <Trash2 size={14} strokeWidth={1.75} />
                  </button>
                </>
              }
            />
          );
        })
      ) : (
        <Empty
          icon={<Cable size={34} strokeWidth={1.5} />}
          title={t("conn.emptyTitle")}
          sub={t("conn.emptySub")}
        />
      )}
      {edit && (
        <ChannelModal
          agentName={agentName}
          existing={edit.type}
          initialConfig={edit.config}
          onClose={() => setEdit(null)}
          onDone={reload}
        />
      )}
      {diag && <ConnectorDiagnosticsModal row={diag} onClose={() => setDiag(null)} />}
      {del && (
        <ConfirmModal
          title={t("conn.removeTitle", { name: channelLabel(del) })}
          message={t("conn.removeMsg")}
          confirmLabel={t("btn.remove")}
          danger
          busy={busy}
          onConfirm={() => void doDelete(del)}
          onClose={() => setDel(null)}
        />
      )}
    </>
  );
}

function ConnectorDiagnosticsModal({ row, onClose }: { row: ConnectorRow; onClose: () => void }) {
  const t = useT(STR);
  const snap = row.snapshot;
  const checks = snap?.checks || [];
  return (
    <Modal title={t("conn.diagTitle", { type: channelLabel(row.type) })} subtitle={row.runtime_attached ? t("conn.loaded") : t("conn.notLoaded")} onClose={onClose} width={560}>
      <div className="connector-diag">
        <div className="connector-diag__summary">
          <span className="badge">{row.type}</span>
          {snap?.live ? <span className="badge" style={{ color: "var(--success)" }}>{t("conn.live")}</span> : null}
          {snap?.bot_user ? <span className="badge" style={{ fontFamily: "var(--font-mono)" }}>{snap.bot_user}</span> : null}
        </div>
        {checks.length ? (
          <div className="connector-diag__checks">
            {checks.map((check, idx) => (
              <div key={`${check.id || "check"}-${idx}`} className="connector-diag__check">
                <span style={{ color: check.ok === false ? "var(--danger)" : check.ok === true ? "var(--success)" : "var(--text-3)" }}>
                  {check.ok === false ? t("conn.diagIssue") : check.ok === true ? t("conn.diagOk") : t("conn.diagInfo")}
                </span>
                <div>
                  <strong>{check.id || `check ${idx + 1}`}</strong>
                  <p>{check.message || t("conn.diagNoDetail")}</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>{t("conn.noChecks")}</div>
        )}
      </div>
    </Modal>
  );
}

// ── MCP tab ──────────────────────────────────────────────────────────────

// The card's headline. A server's identity is its host ("mcp.notion.com"), not
// the full endpoint — the host fits a phone column, is what a user scans the
// list for, and the complete URL is still printed underneath it. stdio servers
// have no host, so they headline with the name the user gave them (stored in
// `url`), falling back to the command they run.
function mcpLabel(m: McpServer): string {
  if (m.transport === "stdio") return m.url || m.command || "";
  try {
    return new URL(m.url).host || m.url;
  } catch {
    // Not a parseable URL (a bare host, a template with placeholders) — show
    // it verbatim rather than inventing a label for it.
    return m.url;
  }
}

// Per-tool switches. Beyond `PREVIEW` tools the list collapses behind a
// disclosure: Notion alone ships 20, which on a phone turned a single server
// card into ~1400px of scrolling before the next server came into view.
const MCP_TOOLS_PREVIEW = 6;

// A burst of switch clicks becomes one write. Each one rewrites the whole
// agent.json5 under the interprocess deployment lock, so N clicks used to mean
// N of those, racing.
const MCP_TOOLS_WRITE_DEBOUNCE_MS = 400;

function sameNames(a: string[] | undefined, b: string[]): boolean {
  return !!a && a.length === b.length && a.every((name, i) => name === b[i]);
}

function omitKey<T>(map: Record<string, T>, key: string): Record<string, T> {
  if (!(key in map)) return map;
  const { [key]: _drop, ...rest } = map;
  return rest;
}

// One switch. Memoized because a server can expose hundreds of them (Klaviyo
// ships 263) and the list auto-expands whenever any tool is off — without this,
// flipping one switch re-rendered every row on the card.
const McpToolRow = memo(function McpToolRow({
  name,
  description,
  enabled,
  disabled,
  onToggle,
}: {
  name: string;
  description?: string;
  enabled: boolean;
  disabled: boolean;
  onToggle: (name: string, next: boolean) => void;
}) {
  const onChange = useCallback((next: boolean) => onToggle(name, next), [name, onToggle]);
  return (
    <div className="mcp-tool">
      <div className="mcp-tool-name">
        <code>{name}</code>
        {description ? <div className="mcp-tool-desc">{description}</div> : null}
      </div>
      <Toggle checked={enabled} ariaLabel={name} onChange={onChange} disabled={disabled} />
    </div>
  );
});

function McpToolList({
  tools,
  serverEnabled,
  probed,
  serverUrl,
  onToggle,
  onSetAll,
  t,
}: {
  tools: NonNullable<McpServerStatus["tools"]>;
  serverEnabled: boolean;
  // Whether `tools` is the server's *discovered* catalogue. Without a probe the
  // card only knows the names already in `disabled_tools`, so "Disable all"
  // would switch off the handful that are off and quietly miss the rest.
  probed: boolean;
  serverUrl: string;
  onToggle: (url: string, name: string, next: boolean) => void;
  onSetAll: (next: boolean) => void;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  // One stable handler per list — `McpToolRow`'s memo is only worth anything if
  // the callback identity survives a re-render.
  const handleToggle = useCallback(
    (name: string, next: boolean) => onToggle(serverUrl, name, next),
    [serverUrl, onToggle],
  );
  // Start expanded when any tool is switched off: a setting the operator has
  // moved off its default must never be the thing a disclosure hides.
  const [expanded, setExpanded] = useState(() => tools.some((tool) => tool.enabled === false));
  const collapsible = tools.length > MCP_TOOLS_PREVIEW;
  const shown = collapsible && !expanded ? tools.slice(0, MCP_TOOLS_PREVIEW) : tools;
  const active = tools.filter((tool) => tool.enabled !== false).length;
  return (
    <>
      <div className="mcp-tools-head">
        <span className="muted">{t("mcp.chooseTools")}</span>
        <div className="mcp-tools-bulk">
          <button
            className="btn"
            onClick={() => onSetAll(true)}
            disabled={!serverEnabled || active === tools.length}
          >
            {t("mcp.enableAllTools")}
          </button>
          <button
            className="btn"
            onClick={() => onSetAll(false)}
            disabled={!serverEnabled || !probed || active === 0}
            title={probed ? undefined : t("mcp.toolsNeedProbe")}
          >
            {t("mcp.disableAllTools")}
          </button>
        </div>
      </div>
      <div className="mcp-tools">
        {shown.map((tool) => (
          <McpToolRow
            key={tool.name}
            name={tool.name}
            description={tool.description}
            enabled={tool.enabled !== false}
            disabled={!serverEnabled}
            onToggle={handleToggle}
          />
        ))}
      </div>
      {collapsible && (
        <button className="btn ghost row mcp-tools-more" aria-expanded={expanded} onClick={() => setExpanded((v) => !v)}>
          {expanded ? <ChevronUp size={15} strokeWidth={1.75} /> : <ChevronDown size={15} strokeWidth={1.75} />}
          {expanded ? t("mcp.showFewerTools") : t("mcp.showAllTools", { n: String(tools.length) })}
        </button>
      )}
    </>
  );
}

function McpTab({ cfg, agentName, reload }: { cfg: AgentConfig; agentName: string; reload: () => void }) {
  const toast = useToast();
  const t = useT(STR);
  const mcps = cfg.mcp_servers || [];
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [del, setDel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [showJson, setShowJson] = useState(false);
  const [statuses, setStatuses] = useState<Record<string, McpServerStatus>>({});
  const [oauthServer, setOauthServer] = useState<McpServer | null>(null);
  const [showLogsFor, setShowLogsFor] = useState<string | null>(null);
  // Per-tool enablement the user has chosen but that the config on disk may not
  // carry yet, keyed by server URL. This is what moves the switch: rendering
  // derives `enabled` from the config plus this overlay, so a click lands
  // instantly instead of waiting out a round trip. It doubles as the
  // read-modify-write base, so a burst of clicks accumulates rather than each
  // one racing off a `cfg` that has not caught up.
  const [pendingDisabled, setPendingDisabled] = useState<Record<string, string[]>>({});
  const writeTimers = useRef<Record<string, number>>({});

  // `deep` opens a live connection to each server (slower; used by the
  // "Refresh status" button and after authorize). The default is a cheap,
  // connection-free snapshot from config + stored tokens — instant on mount,
  // so revisiting the tab no longer flickers through "not authenticated".
  const refreshStatus = useCallback(async (deep = false) => {
    // Deep refreshes also fire right after the modal saves the FIRST server,
    // when this closure still sees the pre-reload (empty) list — let them run.
    if (!mcps.length && !deep) {
      setStatuses({});
      return;
    }
    if (deep) setStatusBusy(true);
    try {
      const q = deep ? "?probe=true" : "";
      const result = await api.get<{ servers: McpServerStatus[] }>(`/api/agents/${encodeURIComponent(agentName)}/mcp/status${q}`);
      setStatuses((prev) => {
        const next: Record<string, McpServerStatus> = {};
        for (const row of result.servers || []) {
          const old = prev[row.url];
          // On a cheap refresh, keep a previously-discovered tool list when the new
          // snapshot has fewer (e.g. the agent isn't running) so the per-tool
          // toggles don't flash to empty between refreshes. A deep probe is
          // authoritative and always wins.
          if (!deep && old?.tools?.length && (row.tools?.length ?? 0) < old.tools.length) {
            next[row.url] = { ...row, tools: old.tools };
          } else {
            next[row.url] = row;
          }
        }
        return next;
      });
    } catch (e) {
      toast.push(errText(e, t("mcp.statusRefreshFailed")), "error");
    } finally {
      if (deep) setStatusBusy(false);
    }
  }, [agentName, mcps.length, toast, t]);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  // Hot-reload the running agent's MCP tools (no restart). Picks up newly
  // authorized servers / enable toggles so the agent can use them immediately.
  const reloadMcp = useCallback(async () => {
    try {
      const r = await api.post<{ ok: boolean; running: boolean; reason?: string }>(
        `/api/agents/${encodeURIComponent(agentName)}/mcp/reload`,
        {},
      );
      if (r.running === false) {
        toast.push(t("mcp.reloadNotRunning"));
      } else if (r.ok) {
        toast.push(t("mcp.reloadedOk"));
      } else {
        toast.push(r.reason || t("mcp.reloadFailed"), "error");
      }
    } catch (e) {
      toast.push(errText(e, t("mcp.reloadError")), "error");
    }
  }, [agentName, toast, t]);

  // After the MCP modal saves: re-read config, seed the just-probed status so
  // the card shows connection + tools immediately, then run a deep refresh in
  // the background — no manual "Refresh status" click needed.
  const onMcpSaved = useCallback(
    (status?: McpServerStatus) => {
      reload();
      if (status?.url) setStatuses((prev) => ({ ...prev, [status.url]: status }));
      void refreshStatus(true);
      // A saved, edited or deleted server changes the live toolset, so hot-reload
      // the running agent as the enable switch does. Without this the card read
      // Connected while the agent still lacked the tools until "Reload agent".
      void reloadMcp();
    },
    [reload, refreshStatus, reloadMcp],
  );

  async function updateServer(server: McpServer, patch: Partial<McpServer>, success?: string) {
    const body = { ...server, ...patch };
    try {
      await api.put(`/api/agents/${encodeURIComponent(agentName)}/mcp?url=${encodeURIComponent(server.url)}`, body);
      if (success) toast.push(success);
      reload();
      void refreshStatus(true);
    } catch (e) {
      toast.push(errText(e, t("mcp.updateFailed")), "error");
    }
  }

  async function testOne(server: McpServer) {
    setTesting(server.url);
    try {
      const result = await api.post<McpServerStatus>(`/api/agents/${encodeURIComponent(agentName)}/mcp/test`, server);
      setStatuses((prev) => ({ ...prev, [server.url]: { ...result, url: server.url, transport: server.transport, enabled: server.enabled ?? true } }));
      if (result.status === "auth_required") {
        // Auto-launch the authorization flow — no manual OAuth step required.
        toast.push(t("mcp.authDetected"));
        setOauthServer({ ...server, auth: "oauth" });
      } else {
        toast.push(result.message || t("mcp.connectedGeneric"));
      }
    } catch (e) {
      const message = errText(e, t("mcp.connectFailed"));
      setStatuses((prev) => ({
        ...prev,
        [server.url]: { url: server.url, transport: server.transport, enabled: server.enabled ?? true, ok: false, status: "error", message },
      }));
      toast.push(message, "error");
    } finally {
      setTesting(null);
    }
  }

  // Persist one server's deny-list, coalescing a burst of clicks into a single
  // write. Deliberately does NOT deep-refresh afterwards: `refreshStatus(true)`
  // re-connects to every configured server and re-lists every tool, which is
  // what used to make a single switch take seconds — and the only thing it
  // would tell us is the flags we just computed locally.
  const flushToolWrite = useCallback(
    async (url: string, disabled: string[]) => {
      try {
        await api.patch(`/api/agents/${encodeURIComponent(agentName)}/mcp/tools?url=${encodeURIComponent(url)}`, {
          disabled_tools: disabled,
        });
        // The overlay is retired by the reconcile effect below once `cfg`
        // actually carries the write — not here. Dropping it on the response
        // would render the pre-write config for the frames until `load()`
        // lands, flashing every switch back.
        reload();
      } catch (e) {
        // Put the switches back to whatever the config still says and say so —
        // one that silently reverts reads as a UI glitch. Only if the overlay
        // still holds what we sent: a newer click may already be queued behind
        // this write, and its value wins.
        setPendingDisabled((prev) => (sameNames(prev[url], disabled) ? omitKey(prev, url) : prev));
        toast.push(errText(e, t("mcp.toolWriteFailed")), "error");
      }
    },
    [agentName, reload, toast, t],
  );

  const queueToolWrite = useCallback(
    (url: string, disabled: string[]) => {
      setPendingDisabled((prev) => ({ ...prev, [url]: disabled }));
      window.clearTimeout(writeTimers.current[url]);
      writeTimers.current[url] = window.setTimeout(() => {
        delete writeTimers.current[url];
        void flushToolWrite(url, disabled);
      }, MCP_TOOLS_WRITE_DEBOUNCE_MS);
    },
    [flushToolWrite],
  );

  // Leaving the tab (or switching agent) must not drop an edit that is still
  // sitting in the debounce window — fire the queued writes rather than
  // clearing them.
  const queuedRef = useRef<Record<string, string[]>>({});
  queuedRef.current = pendingDisabled;
  const flushRef = useRef(flushToolWrite);
  flushRef.current = flushToolWrite;
  useEffect(() => {
    const timers = writeTimers.current;
    return () => {
      for (const [url, id] of Object.entries(timers)) {
        window.clearTimeout(id);
        const queued = queuedRef.current[url];
        if (queued) void flushRef.current(url, queued);
      }
      writeTimers.current = {};
    };
  }, [agentName]);

  // Retire an overlay entry once the reloaded config says the same thing, so
  // rendering hands back to `cfg` only when the two already agree.
  useEffect(() => {
    setPendingDisabled((prev) => {
      let next = prev;
      for (const server of mcps) {
        const queued = next[server.url];
        // A queued write still pending is not settled, whatever `cfg` says.
        if (!queued || writeTimers.current[server.url]) continue;
        if (sameNames(queued, server.disabled_tools ?? [])) next = omitKey(next, server.url);
      }
      return next;
    });
  }, [mcps]);

  // Flip one tool. The deny-list it builds on is the queued value when there is
  // one, else what the config currently holds — read through refs so this stays
  // one identity for the life of the tab and the rows stay memoized.
  const mcpsRef = useRef(mcps);
  mcpsRef.current = mcps;
  const stableToggleTool = useCallback(
    (url: string, toolName: string, enabled: boolean) => {
      const server = mcpsRef.current.find((s) => s.url === url);
      const disabled = new Set(queuedRef.current[url] ?? server?.disabled_tools ?? []);
      if (enabled) disabled.delete(toolName);
      else disabled.add(toolName);
      queueToolWrite(url, [...disabled].sort());
    },
    [queueToolWrite],
  );

  function setAllTools(server: McpServer, tools: NonNullable<McpServerStatus["tools"]>, next: boolean) {
    queueToolWrite(server.url, next ? [] : tools.map((tool) => tool.name).sort());
    // Single switches confirm themselves by moving; a bulk action changes rows
    // that may be scrolled out of view, so it says what it did.
    toast.push(
      next
        ? t("mcp.allToolsEnabled", { n: String(tools.length) })
        : t("mcp.allToolsDisabled", { n: String(tools.length) }),
    );
  }

  async function doDelete(url: string) {
    setBusy(true);
    try {
      await api.del(`/api/agents/${encodeURIComponent(agentName)}/mcp?url=${encodeURIComponent(url)}`);
      toast.push(t("toast.mcpRemoved"));
      reload();
      // Removing a server changes the live toolset (and its re-authorization
      // alert) just as the enable switch does → hot-reload the agent too.
      void reloadMcp();
    } catch (e) {
      toast.push(errText(e, t("toast.mcpRemoveFailed")), "error");
    } finally {
      setBusy(false);
      setDel(null);
    }
  }

  async function clearTokens(server: McpServer) {
    try {
      await api.del(`/api/agents/${encodeURIComponent(agentName)}/mcp/oauth?url=${encodeURIComponent(server.url)}`);
      toast.push(t("mcp.tokensCleared"));
      void refreshStatus();
    } catch (e) {
      toast.push(errText(e, t("mcp.tokensClearFailed")), "error");
    }
  }

  return (
    <>
      {/* Buttons are direct children of SectionHead — an intermediate wrapper
          would be the single grid item on phones and re-create the ragged
          column this layout exists to fix. */}
      <SectionHead title={t("mcp.title")}>
        <button className="btn row" style={{ gap: 6 }} onClick={() => setShowJson((v) => !v)} disabled={!mcps.length}>
          <Code2 size={15} strokeWidth={1.75} /> {showJson ? t("mcp.hideJson") : t("mcp.viewJson")}
        </button>
        <button className="btn row" style={{ gap: 6 }} onClick={() => void refreshStatus(true)} disabled={statusBusy || !mcps.length}>
          {statusBusy ? <span className="spinner" /> : <Activity size={15} strokeWidth={1.75} />} {t("mcp.refreshStatus")}
        </button>
        <button
          className="btn row"
          style={{ gap: 6 }}
          title={t("mcp.reloadAgentTitle")}
          onClick={() => void reloadMcp()}
          disabled={!mcps.length}
        >
          <RotateCw size={15} strokeWidth={1.75} /> {t("mcp.reloadAgent")}
        </button>
        <button className="btn primary row" style={{ gap: 6 }} onClick={() => setAdding(true)}>
          <Plus size={15} strokeWidth={1.75} /> {t("mcp.add")}
        </button>
      </SectionHead>
      {showJson && (
        <div className="card" style={{ padding: 14, marginBottom: 14, borderColor: "rgba(255,255,255,.12)" }}>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
            <strong style={{ fontSize: 13 }}>{t("mcp.aggregatedConfig")}</strong>
            <button
              className="btn"
              style={{ padding: "4px 8px", fontSize: 12 }}
              onClick={() => void navigator.clipboard?.writeText(JSON.stringify(mcps, null, 2))}
            >
              <Copy size={13} strokeWidth={1.75} /> {t("mcp.copy")}
            </button>
          </div>
          <pre
            style={{
              margin: 0,
              maxHeight: 260,
              overflow: "auto",
              fontSize: 12,
              lineHeight: 1.55,
              color: "var(--text)",
              background: "rgba(0,0,0,.18)",
              border: "1px solid var(--border)",
              borderRadius: "var(--r-md)",
              padding: 12,
            }}
          >
            {JSON.stringify(mcps, null, 2)}
          </pre>
        </div>
      )}
      {mcps.length ? (
        <div style={{ display: "grid", gap: 12 }}>
          {mcps.map((m) => {
            const status = statuses[m.url];
            const enabled = m.enabled ?? true;
            const ok = status?.ok;
            // The status row supplies the tool *inventory* (names + descriptions);
            // enablement is derived from the config plus any queued edit. Reading
            // `enabled` off the status row instead is what made a switch sit
            // still until a deep probe returned — and `refreshStatus`'s
            // keep-the-longer-list guard means a cheap refresh on a stopped agent
            // hands back stale flags, which would snap the switch back.
            const probed = status?.ok === true || status?.status === "connected";
            const disabledSet = new Set(pendingDisabled[m.url] ?? m.disabled_tools ?? []);
            const inventory: NonNullable<McpServerStatus["tools"]> =
              status?.tools || (m.disabled_tools || []).map((name) => ({ name, description: "" }));
            const tools = inventory.map((tool) => ({ ...tool, enabled: !disabledSet.has(tool.name) }));
            const activeTools = tools.filter((tool) => tool.enabled !== false).length;
            const isOAuth = m.auth === "oauth" || !!status?.needs_oauth;
            const rawStatus = status?.status;
            const tokenExpired = status?.token_state === "expired";
            const tokenExpiresAt = status?.token_expires_at;
            // Expiry-aware OAuth pill: an expired access token with no refresh
            // token is dead, not "Authorized" — it must read as such.
            const oauthPill: { label: string; bg: string; icon: ReactElement; hint?: string } = tokenExpired
              ? {
                  label: t("mcp.oauthExpired"),
                  bg: "rgba(244, 99, 99, .16)",
                  icon: <AlertTriangle size={13} />,
                  hint: t("mcp.oauthExpiredHint"),
                }
              : status?.authorized
                ? {
                    label: t("mcp.oauthAuthorized"),
                    bg: "rgba(72, 211, 137, .16)",
                    icon: <ShieldCheck size={13} />,
                    hint:
                      status?.token_state === "refreshable"
                        ? t("mcp.oauthAutoRefresh")
                        : tokenExpiresAt
                          ? `${t("mcp.oauthValidUntil")} ${new Date(tokenExpiresAt * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
                          : undefined,
                  }
                : { label: t("mcp.oauthNeeded"), bg: "rgba(245, 191, 79, .16)", icon: <KeyRound size={13} /> };
            // The connection pill reports *connection* health only — never the
            // authorization state (the OAuth pill owns that), so "authorized"
            // never appears twice and an unverified-but-authorized server reads as
            // a neutral "not checked" rather than an alarming yellow warning.
            const conn: { label: string; bg: string; icon: ReactElement; hint?: string } = !enabled
              ? { label: t("mcp.connDisabled"), bg: "rgba(150,150,150,.16)", icon: <Power size={14} /> }
              : ok === true || rawStatus === "connected"
                ? { label: t("mcp.connConnected"), bg: "rgba(72, 211, 137, .16)", icon: <CheckCircle2 size={14} /> }
                : rawStatus === "timeout"
                  ? { label: t("mcp.connTimeout"), bg: "rgba(244, 99, 99, .16)", icon: <XCircle size={14} /> }
                  : rawStatus === "error" || (!isOAuth && ok === false)
                    ? { label: t("mcp.connError"), bg: "rgba(244, 99, 99, .16)", icon: <XCircle size={14} /> }
                    : rawStatus === "misconfigured"
                      ? { label: t("mcp.connMisconfigured"), bg: "rgba(245, 191, 79, .16)", icon: <AlertTriangle size={14} /> }
                      : {
                          label: t("mcp.connNotChecked"),
                          bg: "rgba(255,255,255,.06)",
                          icon: <Circle size={12} strokeWidth={2} />,
                          hint: t("mcp.connNotCheckedHint"),
                        };
            // Surface and border come from the theme tokens (.mcp-card), not
            // from inline white overlays: those were invisible on the light
            // theme, and the `borderColor` beside them never rendered at all —
            // `.card` sets no border-width for a colour to apply to.
            return (
              <div key={m.url} className="card mcp-card" data-enabled={enabled ? "true" : "false"}>
                <div className="mcp-card-head">
                  <div className="mcp-card-icon" style={{ opacity: enabled ? 1 : 0.45 }}>
                    <Cable size={19} strokeWidth={1.75} />
                  </div>
                  <div className="mcp-card-body">
                    {/* Host first, full URL underneath: the host is what a user
                        scans for ("is Notion connected?"), and it survives a
                        narrow column where a 40-char URL cannot. */}
                    <div className="mcp-card-title">{mcpLabel(m)}</div>
                    <div className="mcp-card-url">{m.url}</div>
                    <div className="mcp-card-pills">
                      <span className="pill" style={{ background: conn.bg }} title={conn.hint}>
                        {conn.icon}
                        {conn.label}
                      </span>
                      {isOAuth && (
                        <span className="pill" style={{ background: oauthPill.bg }} title={oauthPill.hint}>
                          {oauthPill.icon}
                          {oauthPill.label}
                        </span>
                      )}
                    </div>
                    <div className="mcp-card-meta">
                      <span>
                        {m.transport || "sse"}
                        {m.command ? ` · ${m.command}` : ""}
                      </span>
                      <span>{tools.length ? t("mcp.toolsEnabledCount", { active: String(activeTools), total: String(tools.length) }) : t("mcp.noToolsYet")}</span>
                      {!!m.args?.length && <span>args: {m.args.join(" ")}</span>}
                      {!!m.env && Object.keys(m.env).length > 0 && <span>env: {Object.keys(m.env).join(", ")}</span>}
                      {status?.message ? <span>{status.message}</span> : null}
                    </div>
                  </div>
                  {/* The on/off switch is the card's state, not one of its
                      actions — it stays with the identity, top-right. */}
                  <Toggle
                    checked={enabled}
                    ariaLabel={enabled ? t("mcp.disableAria") : t("mcp.enableAria")}
                    title={enabled ? t("mcp.disableAria") : t("mcp.enableAria")}
                    onChange={async (next) => {
                      await updateServer(m, { enabled: next }, next ? t("mcp.enabledToast") : t("mcp.disabledToast"));
                      // Enable/disable changes the live toolset → hot-reload the agent.
                      void reloadMcp();
                    }}
                  />
                </div>
                {/* Actions are a footer row, not a third column. As a column
                    their min-content width collapsed the identity beside them
                    to a few pixels, which is what wrapped the URL one character
                    per line on a phone. */}
                <div className="mcp-card-actions">
                  <button className="btn" disabled={testing === m.url} onClick={() => void testOne(m)}>
                    {testing === m.url ? <span className="spinner" /> : t("mcp.test")}
                  </button>
                  {(m.auth === "oauth" || status?.needs_oauth) && m.transport !== "stdio" && (
                    <button className={`btn row ${status?.authorized ? "" : "primary"}`} style={{ gap: 5 }} onClick={() => setOauthServer(m)}>
                      <KeyRound size={14} strokeWidth={1.75} />
                      {status?.authorized || tokenExpired ? t("mcp.reauthorize") : t("mcp.authorize")}
                    </button>
                  )}
                  <div className="mcp-card-actions-icons">
                    {(status?.authorized || tokenExpired) && (
                      <button
                        className="btn icon"
                        title={t("mcp.clearTokens")}
                        aria-label={t("mcp.clearTokens")}
                        onClick={() => void clearTokens(m)}
                      >
                        <ShieldCheck size={16} strokeWidth={1.75} />
                      </button>
                    )}
                    {!!status?.logs?.length && (
                      <button
                        className="btn icon"
                        title={t("mcp.logs")}
                        aria-label={t("mcp.logs")}
                        aria-expanded={showLogsFor === m.url}
                        onClick={() => setShowLogsFor((v) => (v === m.url ? null : m.url))}
                      >
                        <FileText size={16} strokeWidth={1.75} />
                      </button>
                    )}
                    <button className="btn icon" title={t("mcp.editAria")} aria-label={t("mcp.editAria")} onClick={() => setEditing(m)}>
                      <Pencil size={16} strokeWidth={1.75} />
                    </button>
                    <button
                      className="btn danger icon"
                      title={t("mcp.removeAria")}
                      aria-label={t("mcp.removeAria")}
                      onClick={() => setDel(m.url)}
                    >
                      <Trash2 size={16} strokeWidth={1.75} />
                    </button>
                  </div>
                </div>
                {tools.length > 0 && (
                  <McpToolList
                    tools={tools}
                    serverEnabled={enabled}
                    probed={probed}
                    serverUrl={m.url}
                    onToggle={stableToggleTool}
                    onSetAll={(next) => setAllTools(m, tools, next)}
                    t={t}
                  />
                )}
                {showLogsFor === m.url && !!status?.logs?.length && (
                  <pre
                    style={{
                      margin: "12px 0 0",
                      padding: 10,
                      borderRadius: "var(--r-sm)",
                      background: "rgba(0,0,0,.18)",
                      border: "1px solid var(--border)",
                      fontFamily: "var(--font-mono)",
                      fontSize: 11,
                      color: "var(--text-3)",
                      maxHeight: 200,
                      overflow: "auto",
                      whiteSpace: "pre-wrap",
                    }}
                  >
                    {(status.logs || [])
                      .map((l) => `${(l.level || "info").toUpperCase().padEnd(5)} ${l.message}`)
                      .join("\n")}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <Empty
          icon={<Settings size={34} strokeWidth={1.5} />}
          title={t("mcp.emptyTitle")}
          sub={t("mcp.emptySub")}
        />
      )}
      {adding && <McpModal agentName={agentName} onClose={() => setAdding(false)} onDone={onMcpSaved} />}
      {editing && <McpModal agentName={agentName} existing={editing} onClose={() => setEditing(null)} onDone={onMcpSaved} />}
      {oauthServer && (
        <McpOAuthDialog
          agentName={agentName}
          server={oauthServer}
          onClose={() => setOauthServer(null)}
          onAuthorized={() => {
            // Newly-authorized → the backend has persisted `auth: oauth` on the
            // server entry, so re-fetch the config, confirm live status, AND
            // hot-reload the running agent so the just-unlocked MCP tools
            // appear without a restart.
            reload();
            void refreshStatus(true);
            void reloadMcp();
          }}
        />
      )}
      {del && (
        <ConfirmModal
          title={t("mcp.removeTitle")}
          message={del}
          confirmLabel={t("btn.remove")}
          danger
          busy={busy}
          onConfirm={() => void doDelete(del)}
          onClose={() => setDel(null)}
        />
      )}
    </>
  );
}

// A burst of switch clicks becomes one write. Same reason as the MCP tool
// switches: each write rewrites the whole agent.json5 under the interprocess
// deployment lock, so N clicks would mean N of those, racing.
const SKILLS_WRITE_DEBOUNCE_MS = 400;

// Above this many rows the list gets a search box. The common pack alone ships
// ~20, and a name you can see is the difference between a switch you can find
// and one you scroll past.
const SKILLS_SEARCH_THRESHOLD = 8;

// Compared as a normalized SET, not a sequence. The overlay retires only when
// this returns true, so an order or casing difference that the backend treats
// as the same list would otherwise strand the overlay forever — and a stranded
// overlay silently stops the tab reflecting anything changed elsewhere.
// Order genuinely does differ: `admin.set_skill_enabled` (the agent's own
// tool) appends to its own list, and matching is case-insensitive throughout.
function sameSkillNames(a: string[] | undefined, b: string[]): boolean {
  if (!a || a.length !== b.length) return false;
  const norm = (list: string[]) => list.map((n) => n.trim().toLowerCase()).sort();
  const left = norm(a);
  const right = norm(b);
  return left.every((name, i) => name === right[i]);
}

// One skill: an open-the-viewer button plus its on/off switch.
//
// The switch cannot live inside the row's own button — nested <button> is
// invalid HTML and swallows the inner click — so the card is laid out here as a
// flex row with two independent controls instead of reusing the shared `Row`.
//
// Memoized, like `McpToolRow` and for the same reason: a box can carry dozens
// of skills, and without this every flipped switch re-rendered all of them.
const SkillRow = memo(function SkillRow({
  skill,
  enabled,
  onOpen,
  onToggle,
}: {
  skill: SkillEntry;
  enabled: boolean;
  onOpen: (skill: SkillEntry) => void;
  onToggle: (skill: SkillEntry, next: boolean) => void;
}) {
  const t = useT(STR);
  const name = skill.name || t("skills.unnamed");
  const handleOpen = useCallback(() => onOpen(skill), [onOpen, skill]);
  const handleToggle = useCallback((next: boolean) => onToggle(skill, next), [onToggle, skill]);
  // A gated skill (`requires_tools` this agent lacks) has no switch: the
  // denylist is not what keeps it out, so a toggle here would lie in both
  // directions. The badge names the missing tools instead.
  const gated = typeof skill.gated === "string" && skill.gated.length > 0;
  return (
    <div className={`card skill-card${enabled && !gated ? "" : " skill-card-off"}`}>
      <button
        type="button"
        className="skill-row"
        onClick={handleOpen}
        aria-label={t("skills.openAria", { name })}
      >
        <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
          <div className="avatar-sm" style={{ background: "var(--surface-3)", color: "var(--text-2)" }}>
            <Zap size={16} strokeWidth={1.75} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <strong style={{ fontSize: 14 }}>{name}</strong>
              {/* The gated badge is drawn where the switch would be (below),
                  so it is dropped from the inline badges here — the viewer
                  header, which has no switch slot, keeps it. */}
              <SkillBadges skill={{ ...skill, enabled, gated: null }} />
            </div>
            {/* Clamped: a skill's `description` is written for the model, and
                several run to a full paragraph — unclamped, one row filled a
                phone screen and the list stopped being scannable. The viewer
                a click away shows all of it. */}
            <div className="muted skill-desc">{skill.description || skill.location}</div>
          </div>
        </div>
      </button>
      {gated ? (
        <span
          className="badge"
          title={t("skills.gatedHint", { tools: skill.gated ?? "" })}
          style={{ gap: 4, color: "var(--text-3)", whiteSpace: "nowrap" }}
        >
          <Lock size={11} strokeWidth={2} />
          {t("skills.gated")}
        </span>
      ) : (
        <Toggle
          checked={enabled}
          onChange={handleToggle}
          ariaLabel={t(enabled ? "skills.turnOffAria" : "skills.turnOnAria", { name })}
          title={t(enabled ? "skills.turnOffAria" : "skills.turnOnAria", { name })}
        />
      )}
    </div>
  );
});

// ── Skills tab ───────────────────────────────────────────────────────────
// Skills are listed per-agent (`?agent=`), not from the shared catalog: the
// module-level catalog fetches the *unscoped* listing — managed pack plus the
// default workspace — which never holds an agent's own workspace or learned
// skills, and carries no scope to badge them by. Every row opens the viewer and
// carries a switch.
//
// The switch writes `disabled_skills` (a DENYlist of skill names) through the
// ordinary config save, so turning one off works the same for a common skill
// from the shared pack as for one of this agent's own — the skill is never
// touched on disk, and no other agent is affected.
//
// The write path mirrors the MCP per-tool switches, which solve the identical
// problem (a list of switches persisting one denylist field), and it is worth
// saying why each half is there:
//
//   * **Rendered from `cfg` + a pending overlay, never from a private copy.**
//     The rows come from the catalog fetch (they are files on disk), but which
//     of them are ON is read from `cfg.disabled_skills`. So a change made
//     anywhere else — another tab, the raw Config editor, the agent itself via
//     `self_disable_skill` — lands here on the next `agents_update` frame
//     instead of being overwritten by a stale local copy.
//   * **Debounced and coalesced**, so flipping six switches is one write.
//   * **The next denylist is built from the queued value, else `cfg`** — read
//     through refs, never from a render closure. Building it from state was a
//     real race: two quick clicks both read the pre-click list and the second
//     silently dropped the first.
//   * **The overlay is retired by the reconcile effect**, once `cfg` actually
//     carries the write — not on the response, which would render the
//     pre-write config for a few frames and flash every switch back.
function SkillsTab({
  cfg,
  savePatch,
}: {
  cfg: AgentConfig;
  savePatch: (patch: Partial<AgentConfig>, successMsg?: string) => Promise<boolean>;
}) {
  const t = useT(STR);
  const toast = useToast();
  const configured = cfg.skills || [];
  const [available, setAvailable] = useState<SkillEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<SkillEntry | null>(null);
  const [query, setQuery] = useState("");
  // The write in flight or waiting out the debounce. `undefined` = none, so the
  // config is what renders.
  const [pendingDenied, setPendingDenied] = useState<string[] | undefined>(undefined);
  const agent = cfg.name || "";

  useEffect(() => {
    let alive = true;
    setAvailable(null);
    setError(null);
    setQuery("");
    api
      .get<SkillsResponse>(`/api/catalog/skills?agent=${encodeURIComponent(agent)}`)
      .then((d) => alive && setAvailable(d.skills || []))
      .catch((e) => alive && setError(errText(e, t("skills.loadFailed"))));
    return () => {
      alive = false;
    };
  }, [agent, t]);

  // The denylist that governs what renders: the queued write while one is
  // outstanding, otherwise whatever the config says.
  const configDenied = useMemo(() => cfg.disabled_skills || [], [cfg.disabled_skills]);
  const denied = pendingDenied ?? configDenied;
  const deniedSet = useMemo(
    () => new Set(denied.map((n) => n.trim().toLowerCase())),
    [denied],
  );
  // Matching is case-insensitive on the backend, so it must be here too — or a
  // row whose skill really is off would render as on.
  // A gated skill counts as not-on whatever the denylist says: the agent never
  // loads it, and the count above the list must not claim otherwise.
  const isOn = useCallback(
    (skill: SkillEntry) =>
      !skill.gated && !deniedSet.has((skill.name || "").trim().toLowerCase()),
    [deniedSet],
  );

  const writeTimer = useRef<number | undefined>(undefined);
  const queuedRef = useRef<string[] | undefined>(undefined);
  queuedRef.current = pendingDenied;
  const configDeniedRef = useRef(configDenied);
  configDeniedRef.current = configDenied;

  const flushWrite = useCallback(
    async (next: string[]) => {
      const ok = await savePatch({ disabled_skills: next });
      if (!ok) {
        // Put the switches back to whatever the config still says and say so —
        // one that silently reverts reads as a UI glitch. Only if the overlay
        // still holds what we sent: a newer click may already be queued behind
        // this write, and its value wins.
        setPendingDenied((prev) => (sameSkillNames(prev, next) ? undefined : prev));
        toast.push(t("skills.saveFailed"), "error");
      }
      // On success the overlay is retired by the reconcile effect below, once
      // `cfg` carries the write — not here.
    },
    [savePatch, toast, t],
  );

  const flushRef = useRef(flushWrite);
  flushRef.current = flushWrite;

  const queueWrite = useCallback((next: string[]) => {
    setPendingDenied(next);
    window.clearTimeout(writeTimer.current);
    writeTimer.current = window.setTimeout(() => {
      writeTimer.current = undefined;
      void flushRef.current(next);
    }, SKILLS_WRITE_DEBOUNCE_MS);
  }, []);

  // Leaving the tab (or switching agent) must not drop an edit still sitting in
  // the debounce window — fire it rather than clearing it.
  useEffect(() => {
    return () => {
      if (writeTimer.current === undefined) return;
      window.clearTimeout(writeTimer.current);
      writeTimer.current = undefined;
      const queued = queuedRef.current;
      if (queued) void flushRef.current(queued);
    };
  }, [agent]);

  // Retire the overlay once the reloaded config says the same thing, so
  // rendering hands back to `cfg` only when the two already agree.
  useEffect(() => {
    setPendingDenied((prev) => {
      if (!prev || writeTimer.current !== undefined) return prev;
      return sameSkillNames(prev, configDenied) ? undefined : prev;
    });
  }, [configDenied]);

  // Flip one skill. The denylist it builds on is the queued value when there is
  // one, else what the config currently holds — read through refs so a burst of
  // clicks cannot each build on the same pre-burst list, and so this stays one
  // identity for the life of the tab and the rows stay memoized.
  const toggleSkill = useCallback(
    (skill: SkillEntry, next: boolean) => {
      const name = (skill.name || "").trim();
      if (!name) return;
      const base = queuedRef.current ?? configDeniedRef.current;
      const without = base.filter((n) => n.trim().toLowerCase() !== name.toLowerCase());
      // Re-added with the skill's own spelling, and `without` first, so a list
      // that already named it cannot end up holding it twice.
      queueWrite(next ? without : [...without, name]);
    },
    [queueWrite],
  );

  const openSkill = useCallback((skill: SkillEntry) => setOpen(skill), []);

  const rows = available ?? [];
  const onCount = rows.filter(isOn).length;
  const showSearch = rows.length > SKILLS_SEARCH_THRESHOLD;
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (s) =>
        (s.name || "").toLowerCase().includes(q) ||
        (s.description || "").toLowerCase().includes(q) ||
        (s.category || "").toLowerCase().includes(q),
    );
  }, [rows, query]);
  const visibleOn = visible.some(isOn);
  const visibleOff = visible.some((s) => !isOn(s));

  // Bulk actions act on what is LISTED, not on every skill the agent has —
  // with a search active, a button that silently reached rows the filter is
  // hiding would be the worst kind of surprise. With no query the two sets are
  // the same, which is the ordinary case. (`ToolBrowser` resolves this the same
  // way, for the same reason.)
  function setAll(next: boolean) {
    // Gated rows have no switch, so a bulk action leaves them alone too —
    // "turn all on" must not write a denylist edit that changes nothing.
    const named = visible
      .filter((s) => !s.gated)
      .map((s) => (s.name || "").trim())
      .filter(Boolean);
    if (!named.length) return;
    const lower = new Set(named.map((n) => n.toLowerCase()));
    const base = queuedRef.current ?? configDeniedRef.current;
    // Entries naming a skill that is not listed are preserved untouched: the
    // list is the agent's, not this view's, and it may name a skill that is
    // filtered out right now or not installed yet.
    const untouched = base.filter((n) => !lower.has(n.trim().toLowerCase()));
    queueWrite(next ? untouched : [...untouched, ...named]);
    // A single switch confirms itself by moving; a bulk action changes rows
    // that may be scrolled out of view, so it says what it did — and the count
    // is of what it actually touched, so a filtered run cannot overstate it.
    toast.push(
      next
        ? t("skills.allOn", { n: String(named.length) })
        : t("skills.allOff", { n: String(named.length) }),
    );
  }

  return (
    <>
      <SectionHead title={t("skills.configured")} />
      {configured.length ? (
        <div className="card" style={{ padding: 14, marginBottom: 18 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {configured.map((s) => (
              <code key={s} style={{ fontSize: 13 }}>
                {s}
              </code>
            ))}
          </div>
        </div>
      ) : (
        <p className="muted" style={{ fontSize: 13, marginBottom: 18 }}>
          {t("skills.autoIncluded")}
        </p>
      )}

      <SectionHead title={t("skills.available")}>
        <span className="muted" style={{ fontSize: 12 }}>
          {available === null
            ? ""
            : t("skills.countWithOff", { on: String(onCount), total: String(rows.length) })}
        </span>
      </SectionHead>
      <p className="muted" style={{ fontSize: 12, marginTop: -4, marginBottom: 10, lineHeight: 1.5 }}>
        {t("skills.openHint")}
      </p>
      {/* Folded, not trimmed: the switches genuinely need this explanation, but
          nine lines of it above the list put the first skill below the fold on
          a phone, on every visit, whether or not you came to change anything.
          Same trade the Cortex settings card makes. */}
      <details className="skills-help">
        <summary>
          <ChevronRight size={13} strokeWidth={2.25} aria-hidden />
          {t("skills.toggleHintSummary")}
        </summary>
        <p>{t("skills.toggleHint")}</p>
      </details>
      {rows.length ? (
        <div className="skills-toolbar">
          {showSearch ? (
            <div className="skills-search">
              <Search size={14} strokeWidth={1.75} aria-hidden />
              <input
                className="input"
                type="search"
                value={query}
                placeholder={t("skills.searchPlaceholder")}
                aria-label={t("skills.searchPlaceholder")}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
          ) : (
            <span />
          )}
          {/* Enablement is judged over the LISTED rows, matching what the
              buttons act on — otherwise a filtered view could show "Turn all
              off" greyed out while every row on screen is on. */}
          <div className="skills-bulk">
            <button
              className="btn"
              onClick={() => setAll(true)}
              disabled={!visibleOff}
              title={t("skills.enableAllHint")}
            >
              {t("skills.enableAll")}
            </button>
            <button
              className="btn"
              onClick={() => setAll(false)}
              disabled={!visibleOn}
              title={t("skills.disableAllHint")}
            >
              {t("skills.disableAll")}
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <div className="muted" style={{ fontSize: 13 }}>
          {error}
        </div>
      ) : available === null ? (
        <div className="spinner" style={{ margin: 24 }} />
      ) : visible.length ? (
        visible.map((s, i) => (
          <SkillRow
            key={s.name || i}
            skill={s}
            enabled={isOn(s)}
            onOpen={openSkill}
            onToggle={toggleSkill}
          />
        ))
      ) : rows.length ? (
        // Filtered to nothing — distinct from having no skills at all, which is
        // a different problem with a different fix.
        <p className="muted" style={{ fontSize: 13, padding: "8px 2px" }}>
          {t("skills.noMatches", { query: query.trim() })}
        </p>
      ) : (
        <Empty
          icon={<Zap size={34} strokeWidth={1.5} />}
          title={t("skills.emptyTitle")}
          sub={t("skills.emptySub")}
        />
      )}
      {open ? (
        <SkillDetailModal
          skill={{ ...open, enabled: isOn(open) }}
          agent={agent}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </>
  );
}

// ── Config tab — editable view of the literal agent.json5 file ────────────
// Loads and saves the verbatim file text via GET/PUT /api/agents/{name}/config/raw,
// NOT the enriched GET /api/agents/{name} response. So what's shown and saved is
// exactly the file on disk — the injected/computed views (`tools_resolved`,
// `subagents_resolved`, `_running`) only ever live on the read-side endpoint and
// never reach this tab, so there is nothing to strip and edits hit the real file.
function RawTab({ name }: { name: string }) {
  const toast = useToast();
  const t = useT(STR);
  const [content, setContent] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    setContent(null);
    setError(null);
    api
      .get<{ content: string }>(`/api/agents/${name}/config/raw`)
      .then((r) => {
        if (!alive) return;
        setContent(r.content);
        setDraft(r.content);
      })
      .catch((e) => alive && setError(e instanceof ApiError ? e.detail : String(e)));
    return () => {
      alive = false;
    };
  }, [name]);

  const dirty = content !== null && draft !== content;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.put(`/api/agents/${name}/config/raw`, { content: draft });
      setContent(draft);
      toast.push(t("raw.saved"));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card" style={{ padding: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
        <strong style={{ fontSize: 13 }}>{t("raw.title")}</strong>
        <div className="row" style={{ gap: 8 }}>
          <button
            className="btn ghost row"
            style={{ padding: "3px 8px", fontSize: 12, gap: 5 }}
            disabled={content === null}
            onClick={() => {
              void navigator.clipboard.writeText(draft);
              toast.push(t("toast.copied"));
            }}
          >
            <Copy size={13} strokeWidth={1.75} /> {t("raw.copy")}
          </button>
          <button
            className="btn row"
            style={{ padding: "3px 10px", fontSize: 12, gap: 5 }}
            disabled={!dirty || saving}
            onClick={() => void save()}
          >
            {saving ? t("raw.saving") : t("raw.save")}
          </button>
        </div>
      </div>
      {error && (
        <div
          style={{
            padding: "8px 14px",
            color: "var(--danger, #f1605d)",
            fontSize: 12,
            fontFamily: "var(--font-mono)",
            whiteSpace: "pre-wrap",
            borderBottom: "1px solid var(--border)",
          }}
        >
          {error}
        </div>
      )}
      {content === null && !error ? (
        <p className="muted" style={{ padding: "12px 16px", fontSize: 12 }}>
          {t("raw.loading")}
        </p>
      ) : (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          style={{
            display: "block",
            width: "100%",
            boxSizing: "border-box",
            minHeight: 480,
            margin: 0,
            padding: "12px 16px",
            border: "none",
            outline: "none",
            resize: "vertical",
            background: "transparent",
            color: "var(--text-2)",
            fontFamily: "var(--font-mono)",
            fontSize: 12.5,
            lineHeight: 1.55,
          }}
        />
      )}
    </div>
  );
}
