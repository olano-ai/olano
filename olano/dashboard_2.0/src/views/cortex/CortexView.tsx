import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Brain, ListChecks } from "lucide-react";
import { usePersistentState } from "../../core/usePersistentState";
import { usePathSection } from "../../core/usePathSection";
import { viewPath } from "../../core/routeUrls";
import { useCurrentAgent } from "../../core/currentAgent";
import { api } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { AgentFilter } from "../../ui/AgentFilter";
import { AgentPicker } from "../nav/AgentPicker";
import { BotMark } from "../agents/BotMark";
import { agentLabel } from "../agents/helpers";
import { STR } from "./strings";
import { type AutonomyHandle } from "./AutonomyPanel";
import { CortexSettingsPanel } from "./CortexSettingsPanel";
import { PulseTab } from "./PulseTab";
import { CortexItemsTab } from "./CortexItemsTab";
import { ThinkDrawer } from "./ThinkDrawer";
import { useThinkRun, thinkStateFromRunning, countFinished } from "./useThinkRun";
import { OptimizeDrawer } from "./OptimizeDrawer";
import { OPTIMIZE_JOB_KIND, useOptimizeRun, type OptimizeKind } from "./useOptimizeRun";
import { ActivityDrawer } from "./ActivityDrawer";
import { CortexJobsProvider, useCortexJobs, type OpenJob } from "./cortexJobs";
import { JobDrawer } from "./JobDrawer";
import { RunsDrawer } from "./RunsDrawer";
import { useToast } from "../../ui/toast";
import { ConfirmModal } from "./ui";
import { subscribeStream } from "../../core/agentStream";
import type { CognitionStatusResponse, CortexKindCounts, TabId } from "./types";

// ── tab model ─────────────────────────────────────────────────────────────────
// labelKey/hintKey are translation keys (resolved at render time via useT).

interface TabDef {
  id: TabId;
  labelKey: string;
  hintKey: string;
  mode: "pulse" | "cortex";
  lane?: string;
  kind?: "idea" | "brainstorm" | "prototype" | "project";
}

const TABS: TabDef[] = [
  { id: "pulse", labelKey: "tab.pulse", mode: "pulse", lane: "pulse", hintKey: "hint.pulse" },
  { id: "brainstorm", labelKey: "tab.brainstorm", mode: "cortex", kind: "brainstorm", hintKey: "hint.brainstorm" },
  { id: "ideas", labelKey: "tab.ideas", mode: "cortex", kind: "idea", hintKey: "hint.ideas" },
  { id: "prototypes", labelKey: "tab.prototypes", mode: "cortex", kind: "prototype", hintKey: "hint.prototypes" },
  { id: "projects", labelKey: "tab.projects", mode: "cortex", kind: "project", hintKey: "hint.projects" },
  { id: "skills", labelKey: "tab.skills", mode: "pulse", lane: "approval", hintKey: "hint.skills" },
  { id: "improve", labelKey: "tab.improve", mode: "pulse", lane: "approval", hintKey: "hint.improve" },
  { id: "checks", labelKey: "tab.checks", mode: "pulse", lane: "ops", hintKey: "hint.checks" },
];

const TAB_IDS: readonly TabId[] = TABS.map((d) => d.id);

type Counts = Partial<Record<TabId, number>>;

// Two scopes on one page, deliberately.
//
// The OPERATOR controls — Think now, the optimizers, Activity, the settings
// card — act on ONE agent: the app-wide selection, changed with the picker in
// the top bar the way Notes and Memory change theirs.
//
// The TABS list every agent's items at once, because reviewing Cortex used to
// mean switching the picker eight times to see eight agents' ideas, and the
// question a reviewer has is "what has the fleet come up with", not "what has
// this one". The agents filter beside the tabs narrows them (a HIDDEN set, so
// an agent created later is shown by default), and every card names its owner
// — which is also the agent its buttons act on.
export function CortexView() {
  // Which run's log is open, and whether the Runs list is. Held above the jobs
  // provider so anything on the page (a card, a phase row, the header) can
  // open them through the provider.
  const [openJob, setOpenJob] = useState<OpenJob | null>(null);
  const [runsOpen, setRunsOpen] = useState(false);
  return (
    <CortexJobsProvider onOpenJob={setOpenJob} onOpenRuns={() => setRunsOpen(true)}>
      <CortexViewBody
        openJob={openJob}
        onCloseJob={() => setOpenJob(null)}
        runsOpen={runsOpen}
        onCloseRuns={() => setRunsOpen(false)}
      />
    </CortexJobsProvider>
  );
}

function CortexViewBody({
  openJob,
  onCloseJob,
  runsOpen,
  onCloseRuns,
}: {
  openJob: OpenJob | null;
  onCloseJob: () => void;
  runsOpen: boolean;
  onCloseRuns: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const jobs = useCortexJobs();
  // Think / Optimize / Activity and the autonomy panel drive /api/cognition/*,
  // which is admin-only server-side — members get the (grant-scoped) pulse and
  // cortex-item tabs without the operator controls.
  const isAdmin = !!useAuth().user?.is_admin;
  const { agent, agents, current } = useCurrentAgent();
  // /cortex/<agent>/<tab>; the pulse tab is the bare page.
  const [tab, setTab] = usePathSection<TabId>({
    allowed: TAB_IDS,
    fallback: "pulse",
    rememberKey: "cortex.tab",
    pathFor: (section) => (agent ? viewPath("cortex", { agent, section }) : null),
  });
  const [counts, setCounts] = useState<Counts>({});
  const [reloadKey, setReloadKey] = useState(0);
  // Deep-run budget snapshot (from the Autonomy panel) — drives the over-budget
  // confirmation on Think now / Cortex actions.
  const [cogStatus, setCogStatus] = useState<CognitionStatusResponse | null>(null);
  const overCap = !!cogStatus && cogStatus.deep_run_cap > 0 && cogStatus.deep_runs_remaining <= 0;

  // The agents whose items the tabs show. Persisted as the HIDDEN names so a
  // new agent is visible by default; pruned to the roster on read so a deleted
  // agent cannot keep "1 of 8" showing forever.
  const [hiddenAgents, setHiddenAgents] = usePersistentState<string[]>("cortex.hiddenAgents", []);
  const roster = useMemo(() => agents.map((a) => a.name), [agents]);
  const hidden = useMemo(
    () => new Set(hiddenAgents.filter((n) => roster.length === 0 || roster.includes(n))),
    [hiddenAgents, roster],
  );
  const shownCount = roster.filter((n) => !hidden.has(n)).length;
  const toggleAgent = useCallback(
    (name: string) =>
      setHiddenAgents((cur) => (cur.includes(name) ? cur.filter((n) => n !== name) : [...cur, name])),
    [setHiddenAgents],
  );
  // Modal drawers
  const [thinkOpen, setThinkOpen] = useState(false);
  const [confirmThink, setConfirmThink] = useState(false);
  const [optimize, setOptimize] = useState<OptimizeKind | null>(null);
  // A pending optimize awaiting its confirmation (mirrors the original's
  // themedConfirm before the deep, file-rewriting pass kicks off).
  const [confirmOptimize, setConfirmOptimize] = useState<OptimizeKind | null>(null);
  const [activity, setActivity] = useState(false);

  const autonomyRef = useRef<AutonomyHandle | null>(null);

  const setCount = useCallback((t: TabId, n: number) => {
    setCounts((c) => (c[t] === n ? c : { ...c, [t]: n }));
  }, []);

  // Stable: passed to AutonomyPanel. An inline arrow here would change identity
  // every render and (via AutonomyPanel's load deps) drive an endless
  // refetch/spinner-flicker loop. setCogStatus is already stable.
  const handleStatusError = useCallback(
    () => setCogStatus((s) => (s ? { ...s, running: [] } : s)),
    [],
  );

  // Best-effort: populate ALL tab counters (not just the active one), fleet-wide
  // — the pulse and self-learning endpoints aggregate every visible agent when
  // no `agent` is given and stamp each row with its owner; the Cortex items
  // endpoint does the same on `all_agents=true` and splits its counts per
  // agent. The raw answers are kept so the agents filter can re-narrow the
  // badges locally: toggling an agent never costs a round-trip. Mirrors
  // refreshAllPulseCounts so badges update the moment phases produce items.
  type Owned = { agent?: string };
  type ListResp = { items?: (Owned & { metadata?: { kind?: string }; title?: string })[] };
  type SlResp = { services?: (Owned & { failures?: Record<string, number> })[] };
  type CxResp = { counts?: CortexKindCounts; counts_by_agent?: Record<string, CortexKindCounts> };
  type RawCounts = { pulse: ListResp; approval: ListResp; ops: ListResp; sl: SlResp; cortex: CxResp };
  const rawCountsRef = useRef<RawCounts | null>(null);
  const hiddenRef = useRef(hidden);
  hiddenRef.current = hidden;

  const narrowCounts = useCallback((raw: RawCounts, hiddenNow: Set<string>): Counts => {
    const visible = (row: Owned) => !row.agent || !hiddenNow.has(row.agent);
    const appr = (raw.approval.items || []).filter(visible);
    const isSkill = (it: { metadata?: { kind?: string }; title?: string }) =>
      it.metadata?.kind === "learned_skill" || (it.title || "").toLowerCase().includes("skill");
    const skills = appr.filter(isSkill);
    const slFails = (raw.sl.services || [])
      .filter(visible)
      .reduce((n, s) => n + Object.keys(s.failures || {}).length, 0);
    // Per-agent counts when the fleet form answered; the summed counts (an
    // older backend) otherwise, which the filter then cannot narrow.
    const perAgent = raw.cortex.counts_by_agent;
    const sum = (kind: keyof CortexKindCounts): number =>
      perAgent
        ? Object.entries(perAgent)
            .filter(([name]) => !hiddenNow.has(name))
            .reduce((n, [, c]) => n + (c[kind] || 0), 0)
        : raw.cortex.counts?.[kind] || 0;
    return {
      pulse: (raw.pulse.items || []).filter(visible).length,
      skills: skills.length,
      improve: appr.length - skills.length,
      checks: (raw.ops.items || []).filter(visible).length + slFails,
      ideas: sum("idea"),
      brainstorm: sum("brainstorm"),
      prototypes: sum("prototype"),
      projects: sum("project"),
    };
  }, []);

  const refreshAllCounts = useCallback(() => {
    Promise.all([
      api.get<ListResp>("/api/pulse?lane=pulse&status=pending").catch(() => ({}) as ListResp),
      api.get<ListResp>("/api/pulse?lane=approval&status=pending").catch(() => ({}) as ListResp),
      api.get<ListResp>("/api/pulse?lane=ops").catch(() => ({}) as ListResp),
      api.get<SlResp>("/api/self-learning/status").catch(() => ({}) as SlResp),
      api.get<CxResp>("/api/cortex/items?all_agents=true").catch(() => ({}) as CxResp),
    ])
      .then(([pulse, approval, ops, sl, cortex]) => {
        const raw: RawCounts = { pulse, approval, ops, sl, cortex };
        rawCountsRef.current = raw;
        setCounts(narrowCounts(raw, hiddenRef.current));
      })
      .catch(() => {
        /* counts are best-effort */
      });
  }, [narrowCounts]);

  useEffect(() => {
    refreshAllCounts();
  }, [refreshAllCounts]);

  // The filter changed: re-narrow the badges from the last answers.
  useEffect(() => {
    if (rawCountsRef.current) setCounts(narrowCounts(rawCountsRef.current, hidden));
  }, [hidden, narrowCounts]);

  // Drop the budget snapshot + any in-flight optimize view when the agent changes
  // (they belong to the previous agent) so the newly selected agent doesn't
  // inherit a stale budget or an "Optimizing…" drawer.
  useEffect(() => {
    setCogStatus(null);
    setOptimize(null);
    setConfirmOptimize(null);
    opt.clear("workspace");
    opt.clear("skills");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent]);

  // Live updates: refresh on pulse resolution / file changes from any tab.
  useEffect(() => {
    return subscribeStream((frame) => {
      const ev = frame as { type?: string };
      if (ev.type === "pulse_resolved" || ev.type === "file_updated") {
        refreshAllCounts();
        setReloadKey((k) => k + 1);
      }
    });
  }, [refreshAllCounts]);

  const reloadEverything = useCallback(() => {
    setReloadKey((k) => k + 1);
    refreshAllCounts();
    autonomyRef.current?.reload();
  }, [refreshAllCounts]);

  // "Think now" run — owned here so it survives closing the drawer.
  const think = useThinkRun({ onPhaseDone: refreshAllCounts, onFinished: reloadEverything, t });

  // Optimize runs — likewise owned here so they survive closing the drawer (the
  // "Optimizing…" button re-opens the live status, never restarts).
  const opt = useOptimizeRun({ onDone: reloadEverything, t });

  // An optimize is "in flight" when this page started it OR a scheduled/background
  // run of its phase is live (from the polled cognition status).
  const optimizing = (k: OptimizeKind) =>
    (opt.runs[k]?.running && opt.runs[k]?.agent === agent) ||
    !!(agent && jobs.liveFor(agent, OPTIMIZE_JOB_KIND[k])) ||
    (cogStatus?.running || []).includes(k === "workspace" ? "workspace_optimize" : "skill_librarian");

  // An optimize job running on the server that this page is not following (it
  // was started in another tab, or before a reload): follow it, so its drawer
  // shows the live state and then the result instead of a bare spinner.
  const liveWorkspaceJob = agent ? jobs.liveFor(agent, OPTIMIZE_JOB_KIND.workspace)?.id : undefined;
  const liveSkillsJob = agent ? jobs.liveFor(agent, OPTIMIZE_JOB_KIND.skills)?.id : undefined;
  useEffect(() => {
    if (!agent) return;
    for (const [k, id] of [
      ["workspace", liveWorkspaceJob],
      ["skills", liveSkillsJob],
    ] as const) {
      const local = opt.runs[k];
      if (id && local?.jobId !== id && !(local?.running && !local.jobId)) opt.adopt(k, agent, id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- opt.adopt is stable
  }, [agent, liveWorkspaceJob, liveSkillsJob]);

  // The Think-now job behind the Think drawer, remembered so its step log can
  // be opened after it ends too.
  const liveThinkJob = agent ? jobs.liveFor(agent, "generate")?.id : undefined;
  const [thinkJobId, setThinkJobId] = useState("");
  // Reset on an agent switch first, then pick up that agent's live run.
  useEffect(() => setThinkJobId(""), [agent]);
  useEffect(() => {
    if (liveThinkJob) setThinkJobId(liveThinkJob);
  }, [liveThinkJob]);

  // A run ended: refresh what it changed, and say so when a person started it
  // (a scheduled phase ending is not news).
  useEffect(
    () =>
      jobs.onFinished((job) => {
        reloadEverything();
        if (job.source === "autonomous") return;
        if (job.status === "done")
          toast.push(
            job.summary
              ? t("auto.toast.phaseResult", { phase: job.title, summary: job.summary.replace(/\s+/g, " ").slice(0, 140) })
              : t("jobs.toast.done", { title: job.title }),
            "success",
          );
        else
          toast.push(
            t("jobs.toast.failed", {
              title: job.title,
              status: t(`jobs.status.${job.status}`),
              error: job.error || "",
            }),
            job.status === "error" ? "error" : "info",
          );
      }),
    [jobs, reloadEverything, t, toast],
  );
  const runningCount = jobs.running.filter((j) => !hidden.has(j.agent)).length;

  // "Think now" is in flight when this page started it (local hook state) OR a
  // think-cycle phase is live in the backend's polled status. The second source is
  // what survives a tab switch: when CortexView remounts the hook state is gone,
  // but the backend still reports the running phases — so the button stays
  // "Thinking…" and the drawer can re-open with progress, mirroring Optimize.
  const liveThink = think.state ?? thinkStateFromRunning(cogStatus?.running, agent || "", t);
  const thinking = !!liveThink?.running || !!liveThinkJob;
  const thinkDone = liveThink ? countFinished(liveThink) : 0;

  function onThinkClick() {
    if (thinking) {
      // Already in flight (here or in the background): re-open the live
      // status. Between two phases the polled status lists no running phase,
      // so there may be no per-phase view to show; the job's own log is
      // always there.
      if (liveThink) setThinkOpen(true);
      else if (thinkJobId && agent) jobs.openJob({ id: thinkJobId, agent });
      return;
    }
    setConfirmThink(true);
  }

  // Clicking an optimize button: re-open the live status while running, else
  // confirm a fresh run.
  function onOptimizeClick(kind: OptimizeKind) {
    if (optimizing(kind)) setOptimize(kind);
    else setConfirmOptimize(kind);
  }

  const tabDef = TABS.find((d) => d.id === tab) ?? TABS[0];
  const scopeLine =
    roster.length === 0 || shownCount === roster.length
      ? t("scope.items.all")
      : shownCount === 0
        ? t("scope.items.none")
        : t("scope.items.some", { n: String(shownCount), m: String(roster.length) });

  return (
    <div className="cortex-scope">
      <div className="topbar">
        <BrandWordmark />
        {/* The app-wide agent picker, here for the same reason as on Notes and
            Memory: the sidebar copy is a closed drawer on a phone, and the
            operator controls on this page act on the picked agent. */}
        <AgentPicker variant="inline" />
      </div>

      <div className="page" style={{ maxWidth: 1040 }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
          <div>
            <h1>{t("title")}</h1>
            <p className="muted cortex-scope-line" style={{ marginTop: 0 }} title={t("scope.actions.help")}>
              {t("scope.actions")}{" "}
              {current ? (
                <strong className="cortex-scope-agent">
                  <BotMark name={current.name} avatarUrl={current.avatar_url} emoji={current.emoji} size={16} />
                  {agentLabel(current)}
                </strong>
              ) : (
                <strong>{agent || "—"}</strong>
              )}
            </p>
          </div>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <button className="btn" onClick={reloadEverything} title={t("btn.refresh.title")}>
              {t("btn.refresh")}
            </button>
            <button
              className="btn"
              data-testid="cortex-runs-button"
              onClick={jobs.openRuns}
              title={t("btn.runs.title")}
            >
              {runningCount ? (
                <span className="spinner" style={{ width: 12, height: 12, marginRight: 6, display: "inline-block", verticalAlign: "-2px" }} />
              ) : (
                <ListChecks size={15} strokeWidth={1.75} style={{ marginRight: 6, verticalAlign: "-2px" }} />
              )}
              {t("btn.runs")}
              {runningCount ? (
                <span className="badge" style={{ marginLeft: 6 }}>
                  {runningCount}
                </span>
              ) : null}
            </button>
            {isAdmin && (
            <>
            <button
              className="btn primary"
              disabled={!agent}
              onClick={onThinkClick}
              title={t("btn.think.title")}
            >
              <Brain size={15} strokeWidth={1.75} style={{ marginRight: 6, verticalAlign: "-2px" }} />
              {thinking
                ? t("btn.thinking", { done: String(thinkDone) })
                : t("btn.think")}
            </button>
            <button
              className="btn"
              disabled={!agent}
              onClick={() => onOptimizeClick("workspace")}
              title={optimizing("workspace") ? t("btn.optimizing.title") : t("btn.optimizeWorkspace.title")}
            >
              {optimizing("workspace") ? t("btn.optimizingWorkspace") : t("btn.optimizeWorkspace")}
            </button>
            <button
              className="btn"
              disabled={!agent}
              onClick={() => onOptimizeClick("skills")}
              title={optimizing("skills") ? t("btn.optimizing.title") : t("btn.optimizeSkills.title")}
            >
              {optimizing("skills") ? t("btn.optimizingSkills") : t("btn.optimizeSkills")}
            </button>
            <button className="btn" disabled={!agent} onClick={() => setActivity(true)} title={t("btn.activity.title")}>
              {t("btn.activity")}
            </button>
            </>
            )}
          </div>
        </div>

        {/* One card for everything configurable — what the engine may do, and
            what it should aim at. Two peer cards used to say roughly that in
            two headings, both always expanded, between you and the tabs below. */}
        {agent ? (
          <CortexSettingsPanel
            agent={agent}
            isAdmin={isAdmin}
            status={cogStatus}
            autonomyRef={autonomyRef}
            onPhaseRan={reloadEverything}
            onStatus={setCogStatus}
            onStatusError={handleStatusError}
          />
        ) : null}

        {/* tabs with live counters — a single, non-wrapping strip that scrolls
            horizontally on narrow viewports instead of spilling onto a 2nd row */}
        <div
          className="cortex-tabstrip"
          style={{
            marginTop: 22,
            display: "flex",
            flexWrap: "nowrap",
            gap: 6,
            overflowX: "auto",
            paddingBottom: 4,
            scrollbarWidth: "thin",
          }}
        >
          {TABS.map((d) => {
            const n = counts[d.id];
            const active = d.id === tab;
            return (
              <button
                key={d.id}
                className={active ? "btn primary" : "btn"}
                style={{
                  fontSize: 12.5,
                  padding: "6px 10px",
                  flexShrink: 0,
                  whiteSpace: "nowrap",
                }}
                onClick={() => setTab(d.id)}
              >
                {t(d.labelKey)}
                {n != null ? (
                  <span
                    className="badge"
                    style={{ marginLeft: 4, ...(active ? { background: "color-mix(in srgb, var(--accent-fg) 25%, transparent)", color: "var(--accent-fg)" } : {}) }}
                  >
                    {n}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
        {/* Whose items the tabs show, and the control that changes it. */}
        <div className="row cortex-scope-row" style={{ gap: 10, margin: "10px 0 0", flexWrap: "wrap", alignItems: "center" }}>
          <AgentFilter
            items={agents.map((a) => ({
              key: a.name,
              label: agentLabel(a),
              mark: <BotMark name={a.name} avatarUrl={a.avatar_url} emoji={a.emoji} size={16} />,
            }))}
            hidden={hidden}
            onToggle={toggleAgent}
            onAll={() => setHiddenAgents([])}
            onNone={() => setHiddenAgents(roster)}
            labels={{
              title: t("agentFilter.title"),
              allShown: t("agentFilter.all"),
              noneShown: t("agentFilter.none"),
              summary: (n, m) => t("agentFilter.summary", { n: String(n), m: String(m) }),
              search: t("agentFilter.search"),
              selectAll: t("agentFilter.selectAll"),
              clear: t("agentFilter.clear"),
              noMatch: t("agentFilter.noMatch"),
            }}
          />
          <span className="muted" style={{ fontSize: 12.5 }} data-testid="cortex-scope-line">
            {scopeLine}
          </span>
        </div>
        <div className="muted" style={{ fontSize: 13, margin: "8px 0 14px" }}>
          {t(tabDef.hintKey)}
        </div>

        <div>
          {!agent ? (
            <div className="spinner" style={{ marginTop: 16 }} />
          ) : tabDef.mode === "pulse" ? (
            <PulseTab
              key={tab}
              tab={tab}
              lane={tabDef.lane ?? "pulse"}
              agent={agent}
              hidden={hidden}
              reloadKey={reloadKey}
              onCount={setCount}
            />
          ) : (
            <CortexItemsTab
              key={tab}
              tab={tab}
              kind={tabDef.kind ?? "idea"}
              agent={agent}
              hidden={hidden}
              reloadKey={reloadKey}
              overCap={overCap}
              onCount={setCount}
              onCountsChanged={refreshAllCounts}
            />
          )}
        </div>
      </div>

      {confirmThink ? (
        <ConfirmModal
          title={t("btn.think")}
          confirmLabel={t("btn.think")}
          message={
            (overCap && cogStatus
              ? t("confirm.budget.prefix", {
                  used: String(cogStatus.deep_runs_today),
                  cap: String(cogStatus.deep_run_cap),
                }) + "\n\n"
              : "") + t("confirm.think.message")
          }
          onCancel={() => setConfirmThink(false)}
          onConfirm={() => {
            setConfirmThink(false);
            think.start(agent);
            setThinkOpen(true);
          }}
        />
      ) : null}

      {confirmOptimize ? (
        <ConfirmModal
          title={confirmOptimize === "workspace" ? t("btn.optimizeWorkspace") : t("btn.optimizeSkills")}
          confirmLabel={confirmOptimize === "workspace" ? t("btn.optimizeWorkspace") : t("btn.optimizeSkills")}
          message={
            (overCap && cogStatus
              ? t("confirm.budget.prefix", {
                  used: String(cogStatus.deep_runs_today),
                  cap: String(cogStatus.deep_run_cap),
                }) + "\n\n"
              : "") +
            t("confirm.optimize.message", {
              action: confirmOptimize === "workspace" ? t("btn.optimizeWorkspace") : t("btn.optimizeSkills"),
            })
          }
          onCancel={() => setConfirmOptimize(null)}
          onConfirm={() => {
            const kind = confirmOptimize;
            setConfirmOptimize(null);
            opt.start(kind, agent);
            setOptimize(kind);
          }}
        />
      ) : null}

      {thinkOpen && liveThink ? (
        <ThinkDrawer
          state={liveThink}
          onViewLog={thinkJobId && agent ? () => jobs.openJob({ id: thinkJobId, agent }) : undefined}
          onClose={() => {
            setThinkOpen(false);
            // Once a finished run's panel is dismissed, drop its (local) state. A
            // backend-reconstructed state has no local copy to clear (no-op).
            if (think.state && !think.state.running) think.clear();
          }}
        />
      ) : null}

      {optimize && agent ? (
        <OptimizeDrawer
          kind={optimize}
          agent={agent}
          run={opt.runs[optimize]}
          onViewLog={
            opt.runs[optimize]?.jobId
              ? () => jobs.openJob({ id: opt.runs[optimize]!.jobId, agent: opt.runs[optimize]!.agent })
              : undefined
          }
          onClose={() => {
            // Closing only hides the drawer — the run keeps going and the button
            // stays "Optimizing…". Once it's finished, dismissing it clears the run.
            if (!opt.runs[optimize]?.running) opt.clear(optimize);
            setOptimize(null);
          }}
          onUndone={reloadEverything}
        />
      ) : null}

      {activity && agent ? <ActivityDrawer agent={agent} onClose={() => setActivity(false)} /> : null}

      {runsOpen ? <RunsDrawer hidden={hidden} onClose={onCloseRuns} /> : null}

      {/* After the Runs list, so a run opened from it stacks on top. */}
      {openJob ? (
        <JobDrawer
          key={openJob.id}
          agent={openJob.agent}
          jobId={openJob.id}
          onClose={onCloseJob}
          onViewProjects={() => {
            onCloseRuns();
            setTab("projects");
          }}
        />
      ) : null}
    </div>
  );
}
