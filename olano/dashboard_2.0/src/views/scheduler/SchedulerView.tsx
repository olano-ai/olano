// Scheduler view — cron and one-time tasks dispatched to agents.
//
// Full parity port of the original dashboard's loadScheduler() and its modal
// chain (openTaskCreate / openTaskDetail / showTaskHistory / toggleTask /
// deleteTask). Endpoints (olano/runtime/api.py):
//   GET    /api/scheduler/tasks                list (auth-filtered server-side)
//   POST   /api/scheduler/tasks                create  → {result, id}
//   PUT    /api/scheduler/tasks/{id}           update / toggle enabled
//   DELETE /api/scheduler/tasks/{id}           delete
//   GET    /api/scheduler/history/{id}?limit=  run history
// Live refresh comes from the global /api/stream "scheduler_update" frame.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Clock, Eye, Globe, History, Lock, Pause, Pencil, Play, Plus, RotateCw, Trash2, Zap } from "lucide-react";
import { usePathAgent } from "../../core/usePathAgent";
import { useUrlParam, type UrlHistory } from "../../core/useUrlParam";
import { subscribeStream } from "../../core/agentStream";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useCatalog, modelOptions, modelReasoningMeta, modelValueLabel } from "../agents/catalog";
import { ModelOptionGroups } from "../agents/ModelOptions";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import { ErrorState } from "../../ui/ErrorState";
import { Markdown } from "../../ui/Markdown";
import type { Agent } from "../../core/types";
import { Modal, Field } from "./Modal";
import { ScheduleBuilder } from "./ScheduleBuilder";
import { guessTimeZone } from "./recurrence";
import { fmtRel, fmtDateTime, fmtStamp, isCronTask, taskStatus, nextUpcoming } from "./helpers";
import { STR } from "./strings";
import type { CreateForm, CreateResponse, EditForm, HistoryEntry, SchedulerEvent, Task, Visibility } from "./types";

type AgentColor = (name: string | null) => string | null;

// ── main view ──────────────────────────────────────────────────────────────
export function SchedulerView() {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Mirror so load() can tell "first load failed" from "refresh failed"
  // without putting `tasks` in its deps (that would re-run the mount effect).
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;
  const [agents, setAgents] = useState<Agent[]>([]);
  // The agent filter is the path (/scheduler/<agent>; none = all) and the open
  // task is `?task=<id>`, so a reload or a copied link lands on both. Opening
  // a task is a Back step; narrowing the list is not.
  const [agentFilter, setAgentFilter] = usePathAgent("scheduler", "scheduler.agentFilter", "replace"); // "" = all
  const [creating, setCreating] = useState(false);
  const [taskParam, setTaskParam] = useUrlParam("task", { history: "push" });
  const detailId = taskParam;
  const setDetailId = useCallback(
    (id: string | null, history: UrlHistory = "push") => setTaskParam(id, { history }),
    [setTaskParam],
  );
  const [history, setHistory] = useState<{ name: string; rows: HistoryEntry[] } | null>(null);

  const agentColor = useCallback<AgentColor>(
    (name) => (name ? agents.find((a) => a.name === name)?.color || null : null),
    [agents],
  );

  const load = useCallback(() => {
    // Keep the current rows on screen while refetching — blanking to a spinner
    // on every refresh (post-mutation, SSE update) made the list flash. The
    // initial state is already null, so first load still shows the spinner.
    api
      .get<Task[]>("/api/scheduler/tasks")
      .then((rows) => {
        setTasks(rows);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        // First load failed → inline error card with retry; a failed refresh
        // keeps the stale rows and toasts instead of faking an empty list.
        setLoadError(e instanceof Error ? e.message : t("toast.loadFailed"));
        if (tasksRef.current !== null) toast.push(t("toast.loadFailed"), "error");
      });
  }, [toast, t]);

  // Agents power the create/edit agent selector, the per-agent filter, and the
  // coloured "created by / assigned to" labels.
  useEffect(() => {
    api
      .cachedGet<Agent[]>("/api/agents")
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  useEffect(load, [load]);

  // Live refresh: the backend pushes scheduler_update on create/delete. We also
  // refresh on our own PUT (which the server does not broadcast) via load().
  useEffect(() => {
    return subscribeStream((rawFrame) => {
      const frame = rawFrame as SchedulerEvent;
      if (frame.type === "scheduler_update") load();
    });
  }, [load]);

  // contextAgent parity: filter to tasks assigned to OR created by the chosen
  // agent. "" shows everything the server already scoped to the caller.
  const filtered = useMemo(() => {
    const list = tasks ?? [];
    if (!agentFilter) return list;
    return list.filter((t) => (t.agent_name || "") === agentFilter || (t.created_by || "") === agentFilter);
  }, [tasks, agentFilter]);

  const stats = useMemo(() => {
    const enabled = filtered.filter((t) => t.enabled).length;
    const totalRuns = filtered.reduce((sum, t) => sum + (t.run_count || 0), 0);
    return {
      total: filtered.length,
      enabled,
      paused: filtered.length - enabled,
      totalRuns,
      next: nextUpcoming(filtered),
    };
  }, [filtered]);

  // Group by source (not status) so the two schedule stores are visible as
  // distinct sections: saved DB tasks vs agent.json5 config schedules. Within
  // each, active jobs sort ahead of paused/completed ones.
  const byStatus = (a: Task, b: Task) => Number(b.enabled) - Number(a.enabled);
  const dbTasks = useMemo(
    () => filtered.filter((t) => t.source !== "config").sort(byStatus),
    [filtered],
  );
  const configTasks = useMemo(
    () => filtered.filter((t) => t.source === "config").sort(byStatus),
    [filtered],
  );

  const detailTask = useMemo(
    () => (detailId ? filtered.find((t) => t.id === detailId) ?? null : null),
    [detailId, filtered],
  );

  // ── mutations ──────────────────────────────────────────────────────────
  // One mutation per TASK at a time: rapid clicks on the same card's
  // Run/Pause/Delete were firing duplicate requests with no feedback. The
  // guard is per task id (matching the per-card disabled state) — mutations
  // on different tasks stay concurrent, so no click is silently swallowed.
  // The pause/resume flip is optimistic (reverted on error) so the toggle
  // answers instantly instead of after PUT + reload.
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  // Live mirror for the async handlers: after an `await` (confirm dialog,
  // request) the closure's `busyTaskId` is stale — guards read the ref.
  const busyTaskIdRef = useRef(busyTaskId);
  busyTaskIdRef.current = busyTaskId;

  async function toggleTask(task: Task) {
    if (busyTaskIdRef.current === task.id) return;
    const enable = !task.enabled;
    setBusyTaskId(task.id);
    setTasks((prev) => prev?.map((x) => (x.id === task.id ? { ...x, enabled: enable } : x)) ?? prev);
    try {
      await api.put(`/api/scheduler/tasks/${encodeURIComponent(task.id)}`, { enabled: enable });
      toast.push(enable ? t("toast.resumed") : t("toast.paused"));
      load();
    } catch {
      setTasks((prev) => prev?.map((x) => (x.id === task.id ? { ...x, enabled: task.enabled } : x)) ?? prev);
      toast.push(t("toast.updateFailed"), "error");
    } finally {
      setBusyTaskId(null);
    }
  }

  async function runNow(task: Task) {
    if (busyTaskIdRef.current === task.id) return;
    setBusyTaskId(task.id);
    try {
      await api.post(`/api/scheduler/tasks/${encodeURIComponent(task.id)}/run`, {});
      toast.push(t("toast.ranNow", { name: task.name }));
      // Give the run a beat to record history, then refresh counts/last-run.
      setTimeout(load, 1200);
    } catch (e) {
      // ApiError.message carries the server's {detail} (e.g. "Agent … not active").
      toast.push(e instanceof Error ? e.message : t("toast.runNowFailed"), "error");
    } finally {
      setBusyTaskId(null);
    }
  }

  async function deleteTask(task: Task) {
    if (busyTaskIdRef.current === task.id) return;
    const ok = await confirm({
      title: t("confirm.delete.title", { name: task.name }),
      message: t("confirm.delete.msg"),
      confirmLabel: t("confirm.delete.ok"),
      danger: true,
    });
    // Re-check after the confirm: this task may have gone busy (e.g. a Run
    // Now clicked before the dialog opened) while the dialog was up.
    if (!ok || busyTaskIdRef.current === task.id) return;
    setBusyTaskId(task.id);
    try {
      await api.del(`/api/scheduler/tasks/${encodeURIComponent(task.id)}`);
      toast.push(t("toast.deleted"));
      if (detailId === task.id) setDetailId(null, "replace");
      load();
    } catch {
      toast.push(t("toast.deleteFailed"), "error");
    } finally {
      setBusyTaskId(null);
    }
  }

  const openHistory = useCallback(
    async (taskId: string, name: string) => {
      try {
        const rows = await api.get<HistoryEntry[]>(
          `/api/scheduler/history/${encodeURIComponent(taskId)}?limit=20`,
        );
        setHistory({ name, rows });
      } catch {
        toast.push(t("toast.historyFailed"), "error");
      }
    },
    [toast, t],
  );

  // ── render ─────────────────────────────────────────────────────────────
  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          <button className="btn" onClick={load}>
            <RotateCw size={15} strokeWidth={1.75} /> {t("btn.refresh")}
          </button>
          <button className="btn primary" onClick={() => setCreating(true)}>
            <Plus size={15} strokeWidth={1.75} /> {t("btn.newTask")}
          </button>
        </div>
      </div>

      <div className="page">
        <h1>{t("title")}</h1>
        <p className="muted">{t("subtitle")}</p>

        {/* agent filter (contextAgent parity) */}
        {agents.length > 0 && (
          <div className="row" style={{ marginTop: 14, gap: 8 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {t("filter.agent")}
            </span>
            <select
              className="input"
              style={{ width: "auto", minWidth: 180 }}
              value={agentFilter}
              onChange={(e) => setAgentFilter(e.target.value)}
            >
              <option value="">{t("filter.allAgents")}</option>
              {agents.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
            {agentFilter && (
              <button className="btn" style={{ padding: "4px 10px" }} onClick={() => setAgentFilter("")}>
                {t("filter.clear")}
              </button>
            )}
          </div>
        )}

        {/* stats */}
        <div
          style={{
            display: "grid",
            gap: 12,
            marginTop: 16,
            gridTemplateColumns: "repeat(auto-fit,minmax(200px,1fr))",
          }}
        >
          <StatCard
            label={t("stat.tasks")}
            value={String(stats.total)}
            sub={t("stat.tasksSub", { enabled: String(stats.enabled), paused: String(stats.paused) })}
          />
          <StatCard label={t("stat.runs")} value={String(stats.totalRuns)} sub={t("stat.runsSub")} />
          <StatCard
            label={t("stat.nextRun")}
            value={stats.next ? fmtRel(stats.next.next_run, t) : "—"}
            sub={stats.next ? stats.next.name : t("stat.noUpcoming")}
          />
        </div>

        {/* list */}
        {tasks === null && loadError ? (
          <ErrorState message={t("toast.loadFailed")} detail={loadError} onRetry={load} />
        ) : tasks === null ? (
          <div className="spinner" style={{ marginTop: 24 }} />
        ) : filtered.length === 0 ? (
          <div className="card" style={{ marginTop: 20, padding: 40, textAlign: "center" }}>
            <div style={{ display: "flex", justifyContent: "center", color: "var(--text-4)" }}>
              <Clock size={32} strokeWidth={1.5} />
            </div>
            <div style={{ marginTop: 8, fontWeight: 600 }}>
              {agentFilter ? t("empty.titleAgent") : t("empty.title")}
            </div>
            <div className="muted" style={{ marginTop: 4, fontSize: 13 }}>
              {t("empty.sub")}
            </div>
            <button className="btn primary" style={{ marginTop: 14 }} onClick={() => setCreating(true)}>
              <Plus size={15} strokeWidth={1.75} /> {t("empty.create")}
            </button>
          </div>
        ) : (
          <>
            <TaskSection
              heading={t("section.db")}
              subheading={t("section.dbSub")}
              emptyHint={t("section.dbEmpty")}
              tasks={dbTasks}
              agentColor={agentColor}
              busyId={busyTaskId}
              onOpen={(id) => setDetailId(id)}
              onToggle={toggleTask}
              onRunNow={runNow}
              onDelete={deleteTask}
              onHistory={openHistory}
            />
            <TaskSection
              heading={t("section.config")}
              subheading={t("section.configSub")}
              emptyHint={t("section.configEmpty")}
              tasks={configTasks}
              agentColor={agentColor}
              busyId={busyTaskId}
              onOpen={(id) => setDetailId(id)}
              onToggle={toggleTask}
              onRunNow={runNow}
              onDelete={deleteTask}
              onHistory={openHistory}
            />
          </>
        )}
      </div>

      {creating && (
        <CreateModal
          agents={agents}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            load();
          }}
        />
      )}

      {detailTask && (
        <DetailModal
          task={detailTask}
          agentColor={agentColor}
          agents={agents}
          onClose={() => setDetailId(null)}
          onSaved={() => {
            setDetailId(null);
            load();
          }}
          onHistory={openHistory}
          onRunNow={runNow}
          onDelete={deleteTask}
        />
      )}

      {history && <HistoryModal name={history.name} rows={history.rows} onClose={() => setHistory(null)} />}
    </>
  );
}

// ── stat card ──────────────────────────────────────────────────────────────
function StatCard({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 0.4 }}>
        {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 700, marginTop: 4 }}>{value}</div>
      <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
        {sub}
      </div>
    </div>
  );
}

// ── task section + card ──────────────────────────────────────────────────────
function TaskSection({
  heading,
  subheading,
  emptyHint,
  tasks,
  agentColor,
  busyId,
  onOpen,
  onToggle,
  onRunNow,
  onDelete,
  onHistory,
}: {
  heading: string;
  // Optional one-line description of what the section holds (source of the jobs).
  subheading?: string;
  // When set, an empty section renders its header + this hint instead of hiding,
  // so both schedule stores stay visible even when one is empty.
  emptyHint?: string;
  tasks: Task[];
  agentColor: AgentColor;
  // Task id whose mutation is in flight — that card's actions are disabled.
  busyId: string | null;
  onOpen: (id: string) => void;
  onToggle: (t: Task) => void;
  onRunNow: (t: Task) => void;
  onDelete: (t: Task) => void;
  onHistory: (taskId: string, name: string) => void;
}) {
  if (tasks.length === 0 && !emptyHint) return null;
  return (
    <div style={{ marginTop: 22 }}>
      <div
        className="row"
        style={{ gap: 8, fontSize: 12, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.4 }}
      >
        <span className="muted">{heading}</span>
        <span className="badge">{tasks.length}</span>
      </div>
      {subheading ? (
        <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
          {subheading}
        </div>
      ) : null}
      {tasks.length === 0 ? (
        <div className="muted" style={{ fontSize: 13, marginTop: 10, fontStyle: "italic" }}>
          {emptyHint}
        </div>
      ) : null}
      <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
        {tasks.map((t) => (
          <TaskCard
            key={t.id}
            task={t}
            agentColor={agentColor}
            busy={busyId === t.id}
            onOpen={() => onOpen(t.id)}
            onToggle={() => onToggle(t)}
            onRunNow={() => onRunNow(t)}
            onDelete={() => onDelete(t)}
            onHistory={() => onHistory(t.id, t.name)}
          />
        ))}
      </div>
    </div>
  );
}

// Soft, token-driven status pills mirroring the old badge--green / badge--orange
// look. `dot` adds a leading status dot for live states (active/paused); the
// terminal "completed" state has no dot.
const STATUS_META: Record<
  ReturnType<typeof taskStatus>,
  { key: string; color: string; bg: string; dot: boolean }
> = {
  active: { key: "status.active", color: "var(--success)", bg: "var(--success-soft)", dot: true },
  completed: { key: "status.completed", color: "var(--text-3)", bg: "var(--surface-3)", dot: false },
  paused: { key: "status.paused", color: "var(--warn)", bg: "var(--warn-soft)", dot: true },
};

// Small status dot with a soft halo ring (parity with the old `.dot` indicator).
function StatusDot({ color }: { color: string }) {
  return (
    <span
      aria-hidden
      style={{
        width: 6,
        height: 6,
        borderRadius: "50%",
        background: color,
        boxShadow: `0 0 0 3px color-mix(in srgb, ${color} 22%, transparent)`,
        flexShrink: 0,
      }}
    />
  );
}

function TaskCard({
  task,
  agentColor,
  busy,
  onOpen,
  onToggle,
  onRunNow,
  onDelete,
  onHistory,
}: {
  task: Task;
  agentColor: AgentColor;
  busy: boolean;
  onOpen: () => void;
  onToggle: () => void;
  onRunNow: () => void;
  onDelete: () => void;
  onHistory: () => void;
}) {
  const t = useT(STR);
  const cron = isCronTask(task);
  const isConfig = task.source === "config";
  const status = STATUS_META[taskStatus(task)];
  const assignedColor = agentColor(task.agent_name);
  const accent = task.enabled ? "var(--success)" : assignedColor || "var(--border-strong)";
  const createdColor = agentColor(task.created_by);
  const shared = task.visibility === "shared";

  return (
    <div className="card" style={{ padding: 14, borderLeft: `3px solid ${accent}`, cursor: "pointer" }} onClick={onOpen}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <strong>{task.name}</strong>
            <span
              className="badge"
              style={cron ? { background: "var(--info-soft)", color: "var(--info)" } : { fontFamily: "var(--font-mono)" }}
            >
              {cron ? t("type.recurring") : t("type.oneTime")}
            </span>
            <span
              className="badge"
              title={
                isConfig ? t("src.configTitle", { agent: task.agent_name || "—" }) : t("src.savedTitle")
              }
              style={
                isConfig
                  ? { background: "var(--warning-soft)", color: "var(--warning)" }
                  : { background: "var(--surface-2)", color: "var(--text-3)" }
              }
            >
              {isConfig ? t("src.config") : t("src.saved")}
            </span>
            <span
              title={shared ? t("vis.sharedTitle") : t("vis.privateTitle")}
              style={{ display: "inline-flex", alignItems: "center", color: "var(--text-3)" }}
            >
              {shared ? <Globe size={14} strokeWidth={1.75} /> : <Lock size={14} strokeWidth={1.75} />}
            </span>
          </div>
          <div className="muted" style={{ marginTop: 4, fontFamily: "var(--font-mono)", fontSize: 12 }}>
            {cron ? task.schedule : fmtDateTime(task.run_at)}
            {!cron && task.timezone ? ` · ${task.timezone}` : ""}
          </div>
        </div>
        <div className="row" style={{ gap: 6 }} onClick={(e) => e.stopPropagation()}>
          <span
            className="badge"
            style={{ background: status.bg, color: status.color, border: "1px solid transparent", gap: 5 }}
          >
            {status.dot && <StatusDot color={status.color} />}
            {t(status.key)}
          </span>
          <button
            className="btn"
            style={{ padding: "4px 8px" }}
            title={t("act.runNow")}
            aria-label={t("act.runNow")}
            disabled={busy}
            onClick={onRunNow}
          >
            <Zap size={14} strokeWidth={1.75} />
          </button>
          <button
            className="btn"
            style={{ padding: "4px 8px" }}
            title={task.enabled ? t("act.pause") : t("act.resume")}
            aria-label={task.enabled ? t("act.pause") : t("act.resume")}
            disabled={busy}
            onClick={onToggle}
          >
            {task.enabled ? <Pause size={14} strokeWidth={1.75} /> : <Play size={14} strokeWidth={1.75} />}
          </button>
          <button
            className="btn"
            style={{ padding: "4px 8px" }}
            title={t("act.history")}
            aria-label={t("act.history")}
            onClick={onHistory}
          >
            <History size={14} strokeWidth={1.75} />
          </button>
          <button
            className="btn danger"
            style={{ padding: "4px 8px" }}
            title={t("act.delete")}
            aria-label={t("act.delete")}
            disabled={busy}
            onClick={onDelete}
          >
            <Trash2 size={14} strokeWidth={1.75} />
          </button>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gap: 8,
          marginTop: 12,
          gridTemplateColumns: "repeat(auto-fit,minmax(110px,1fr))",
          fontSize: 12,
        }}
      >
        <Attr label={t("attr.createdBy")} value={task.created_by || "—"} color={createdColor} />
        <Attr label={t("attr.assignedTo")} value={task.agent_name || t("val.default")} color={assignedColor} />
        <Attr label={t("attr.model")} value={task.model ? modelValueLabel(task.model) : t("val.default")} />
        {task.reasoning_effort ? <Attr label={t("attr.effort")} value={task.reasoning_effort} /> : null}
        <Attr label={t("attr.runs")} value={String(task.run_count || 0)} />
        <Attr label={t("attr.last")} value={task.last_run ? fmtRel(task.last_run, t) : t("val.never")} />
        <Attr label={t("attr.next")} value={fmtRel(task.next_run, t)} />
      </div>
    </div>
  );
}

// The server's own reason for refusing a save (a model this box cannot run
// comes back as a 422 naming why), else the generic toast.
function saveFailureText(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.status === 422 && e.detail ? e.detail : fallback;
}

// The task's model: a native <select> over the same catalog the agent form
// uses, "Agent default" first. The default names the chosen agent's configured
// model so the choice reads as "keep X" rather than a blank. A saved value the
// catalog no longer offers is kept as its own option so an edit never silently
// swaps a job's model.
export function TaskModelField({
  value,
  onChange,
  agentModel,
}: {
  value: string;
  onChange: (model: string) => void;
  /** The chosen agent's configured model, when known. */
  agentModel: string | undefined;
}) {
  const t = useT(STR);
  const cat = useCatalog();
  const models = useMemo(() => modelOptions(cat.providers), [cat.providers]);
  const defaultLabel = agentModel
    ? t("field.modelDefaultCurrent", { model: modelValueLabel(agentModel) })
    : t("field.modelDefault");
  return (
    <Field label={t("field.model")} hint={t("field.modelHint")}>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)} data-testid="task-model">
        <option value="">{defaultLabel}</option>
        {value && !models.some((m) => m.value === value) && <option value={value}>{modelValueLabel(value)}</option>}
        <ModelOptionGroups options={models} />
      </select>
    </Field>
  );
}

// The task's reasoning effort: rendered only when the model the task runs on
// (its own, else the agent's) advertises effort levels, and listing exactly
// that ladder (`modelReasoningMeta`, the chat effort chip's source), so a task
// can never be saved with an effort its model rejects. "Agent default" first.
export function TaskEffortField({
  value,
  onChange,
  runsOn,
}: {
  value: string;
  onChange: (effort: string) => void;
  /** The model the task will run on: its own when set, else the agent's. */
  runsOn: string;
}) {
  const t = useT(STR);
  const cat = useCatalog();
  const meta = useMemo(() => modelReasoningMeta(cat.providers, runsOn), [cat.providers, runsOn]);
  if (!meta) return null;
  return (
    <Field label={t("field.effort")} hint={t("field.effortHint")}>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)} data-testid="task-effort">
        <option value="">{t("field.effortAgentDefault")}</option>
        {value && !meta.efforts.includes(value) && <option value={value}>{value}</option>}
        {meta.efforts.map((lvl) => (
          <option key={lvl} value={lvl}>
            {lvl}
          </option>
        ))}
      </select>
    </Field>
  );
}

// Both overrides together, for the two modals here and the agent page's
// schedule editor: picking a model whose ladder lacks the chosen effort resets
// the effort to the agent default rather than carrying a value the server
// would refuse (it validates the pair as it will be after the save).
export function TaskOverrideFields({
  model,
  effort,
  agentModel,
  onChange,
}: {
  model: string;
  effort: string;
  /** The owning agent's configured model, when known. */
  agentModel: string | undefined;
  onChange: (next: { model: string; reasoning_effort: string }) => void;
}) {
  const cat = useCatalog();
  const runsOn = model || agentModel || "";
  return (
    <>
      <TaskModelField
        value={model}
        agentModel={agentModel}
        onChange={(nextModel) => {
          const nextMeta = modelReasoningMeta(cat.providers, nextModel || agentModel || "");
          const keep = !!effort && !!nextMeta && nextMeta.efforts.includes(effort);
          onChange({ model: nextModel, reasoning_effort: keep ? effort : "" });
        }}
      />
      <TaskEffortField value={effort} runsOn={runsOn} onChange={(e) => onChange({ model, reasoning_effort: e })} />
    </>
  );
}

function Attr({ label, value, color }: { label: string; value: string; color?: string | null }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        {label}
      </div>
      <div
        style={{
          marginTop: 2,
          color: color || undefined,
          fontWeight: color ? 600 : 400,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {value}
      </div>
    </div>
  );
}

// ── create modal ─────────────────────────────────────────────────────────────
// `initial` lets other views (e.g. the Calendar) pre-fill the form — most often
// a one-time run_at for a clicked day — while keeping every default in one place.
export function CreateModal({
  agents,
  onClose,
  onCreated,
  initial,
}: {
  agents: Agent[];
  onClose: () => void;
  onCreated: () => void;
  initial?: Partial<CreateForm>;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [form, setForm] = useState<CreateForm>({
    name: "",
    agent_name: "",
    prompt: "",
    schedule: "",
    run_at: "",
    // Default to the viewer's own timezone instead of UTC — the builder lets
    // them change it. `initial` (e.g. a Calendar day click) still wins.
    timezone: guessTimeZone(),
    visibility: "private",
    model: "",
    reasoning_effort: "",
    ...initial,
  });
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof CreateForm>(k: K, v: CreateForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const agentModel = agents.find((a) => a.name === form.agent_name)?.model;

  async function submit() {
    if (!form.name.trim() || !form.prompt.trim()) {
      toast.push(t("toast.namePromptRequired"), "error");
      return;
    }
    if (!form.schedule.trim() && !form.run_at.trim()) {
      toast.push(t("toast.scheduleRequired"), "error");
      return;
    }
    setSaving(true);
    try {
      await api.post<CreateResponse>("/api/scheduler/tasks", {
        name: form.name.trim(),
        prompt: form.prompt.trim(),
        schedule: form.schedule.trim(),
        run_at: form.run_at.trim(),
        timezone: form.timezone.trim() || "UTC",
        agent_name: form.agent_name.trim(),
        visibility: form.visibility,
        model: form.model.trim(),
        reasoning_effort: form.reasoning_effort.trim(),
      });
      toast.push(t("toast.created"));
      onCreated();
    } catch (e) {
      toast.push(saveFailureText(e, t("toast.createFailed")), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={t("create.title")} subtitle={t("create.subtitle")} onClose={onClose} width={600}>
      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr" }}>
        <Field label={t("field.name")}>
          <input
            className="input"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder={t("field.namePlaceholder")}
          />
        </Field>
        <Field label={t("field.agent")}>
          <select className="input" value={form.agent_name} onChange={(e) => set("agent_name", e.target.value)}>
            <option value="">{t("field.agentDefault")}</option>
            {agents.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Field label={t("field.prompt")}>
        <textarea
          className="input"
          style={{ minHeight: 90, resize: "vertical" }}
          value={form.prompt}
          onChange={(e) => set("prompt", e.target.value)}
          placeholder={t("field.promptPlaceholder")}
        />
      </Field>

      <ScheduleBuilder
        schedule={form.schedule}
        runAt={form.run_at}
        timezone={form.timezone}
        onChange={(next) =>
          setForm((f) => ({ ...f, schedule: next.schedule, run_at: next.run_at, timezone: next.timezone }))
        }
      />

      <TaskOverrideFields
        model={form.model}
        effort={form.reasoning_effort}
        agentModel={agentModel}
        onChange={(next) => setForm((f) => ({ ...f, model: next.model, reasoning_effort: next.reasoning_effort }))}
      />

      <Field label={t("field.visibility")}>
        <select className="input" value={form.visibility} onChange={(e) => set("visibility", e.target.value as Visibility)}>
          <option value="private">{t("vis.private")}</option>
          <option value="shared">{t("vis.shared")}</option>
        </select>
      </Field>

      <div className="row" style={{ justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn" onClick={onClose}>
          {t("btn.cancel")}
        </button>
        <button className="btn primary" disabled={saving} onClick={() => void submit()}>
          {saving ? "…" : t("btn.createTask")}
        </button>
      </div>
    </Modal>
  );
}

// ── detail / edit modal ──────────────────────────────────────────────────────
export function DetailModal({
  task,
  agentColor,
  agents = [],
  onClose,
  onSaved,
  onHistory,
  onRunNow,
  onDelete,
}: {
  task: Task;
  agentColor: AgentColor;
  /** The roster, so the model field can name the agent's configured model. */
  agents?: Agent[];
  onClose: () => void;
  onSaved: () => void;
  onHistory: (taskId: string, name: string) => void;
  onRunNow: (t: Task) => void;
  onDelete: (t: Task) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const cron = isCronTask(task);
  const isConfig = task.source === "config";
  const [form, setForm] = useState<EditForm>({
    name: task.name,
    prompt: task.prompt,
    schedule: task.schedule ?? "",
    run_at: task.run_at ?? "",
    timezone: task.timezone,
    visibility: task.visibility,
    model: task.model ?? "",
    reasoning_effort: task.reasoning_effort ?? "",
  });
  const [saving, setSaving] = useState(false);
  const [previewPrompt, setPreviewPrompt] = useState(false);
  const set = <K extends keyof EditForm>(k: K, v: EditForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const agentModel = agents.find((a) => a.name === task.agent_name)?.model;

  async function save() {
    if (!form.name.trim() || !form.prompt.trim()) {
      toast.push(t("toast.namePromptRequired"), "error");
      return;
    }
    // Config-schedule jobs are cron-only (an agent.json5 entry has no one-time
    // form), so require a recurring expression rather than letting an empty
    // schedule fall back to "every minute" server-side.
    if (isConfig && !form.schedule.trim()) {
      toast.push(t("toast.scheduleRequired"), "error");
      return;
    }
    if (!isConfig && !form.schedule.trim() && !form.run_at.trim()) {
      toast.push(t("toast.scheduleRequired"), "error");
      return;
    }
    setSaving(true);
    try {
      const body: Record<string, unknown> = isConfig
        ? {
            name: form.name.trim(),
            prompt: form.prompt.trim(),
            timezone: form.timezone.trim() || "UTC",
            schedule: form.schedule.trim(),
            // "" = back to the agent's own model / effort (both stores).
            model: form.model.trim(),
            reasoning_effort: form.reasoning_effort.trim(),
            // no run_at / visibility: config jobs are recurring and file-scoped
          }
        : {
            name: form.name.trim(),
            prompt: form.prompt.trim(),
            timezone: form.timezone.trim() || "UTC",
            visibility: form.visibility,
            model: form.model.trim(),
            reasoning_effort: form.reasoning_effort.trim(),
            // The builder keeps exactly one of these populated; send both so a
            // recurring ⇄ one-time switch clears the other column server-side.
            schedule: form.schedule.trim(),
            run_at: form.run_at.trim(),
          };
      await api.put(`/api/scheduler/tasks/${encodeURIComponent(task.id)}`, body);
      toast.push(t("toast.updated"));
      onSaved();
    } catch (e) {
      toast.push(saveFailureText(e, t("toast.updateFailed")), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={task.name}
      subtitle={t(cron ? "detail.recurringWith" : "detail.oneTimeWith", {
        agent: task.agent_name || t("val.default"),
      })}
      onClose={onClose}
      width={600}
    >
      <div
        style={{
          display: "grid",
          gap: 8,
          gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))",
          marginBottom: 16,
          fontSize: 12,
        }}
      >
        <Attr label={t("attr.type")} value={cron ? t("attr.typeRecurring") : t("attr.typeOneTime")} />
        <Attr label={t("attr.createdBy")} value={task.created_by || "—"} color={agentColor(task.created_by)} />
        <Attr label={t("attr.assignedTo")} value={task.agent_name || t("val.default")} color={agentColor(task.agent_name)} />
        <Attr label={t("attr.model")} value={task.model ? modelValueLabel(task.model) : t("val.default")} />
        <Attr label={t("attr.effort")} value={task.reasoning_effort || t("val.default")} />
        <Attr label={t("attr.visibility")} value={task.visibility === "shared" ? t("vis.shortShared") : t("vis.shortPrivate")} />
        <Attr label={t("attr.runCount")} value={String(task.run_count || 0)} />
        <Attr label={t("attr.lastRun")} value={task.last_run ? fmtRel(task.last_run, t) : t("val.never")} />
      </div>

      {isConfig ? (
        <div
          className="card"
          style={{
            padding: "9px 12px",
            marginBottom: 12,
            background: "var(--warning-soft)",
            color: "var(--warning)",
            fontSize: 12,
          }}
        >
          {t("edit.configNote", { agent: task.agent_name || "—" })}
        </div>
      ) : null}

      <Field label={t("field.name")}>
        <input className="input" value={form.name} onChange={(e) => set("name", e.target.value)} />
      </Field>
      <div className="field" style={{ marginBottom: 12 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <label>{t("field.prompt")}</label>
          <button
            type="button"
            className="btn ghost"
            style={{ padding: "2px 9px", fontSize: 12 }}
            onClick={() => setPreviewPrompt((p) => !p)}
          >
            {previewPrompt ? (
              <>
                <Pencil size={13} strokeWidth={1.75} /> {t("btn.edit")}
              </>
            ) : (
              <>
                <Eye size={13} strokeWidth={1.75} /> {t("btn.preview")}
              </>
            )}
          </button>
        </div>
        {previewPrompt ? (
          <div
            className="card"
            style={{
              padding: "10px 13px",
              minHeight: 120,
              maxHeight: 260,
              overflow: "auto",
              background: "var(--surface-2)",
              fontSize: 13,
            }}
          >
            {form.prompt.trim() ? (
              <Markdown text={form.prompt} />
            ) : (
              <span className="muted">{t("preview.empty")}</span>
            )}
          </div>
        ) : (
          <textarea
            className="input"
            rows={6}
            style={{ resize: "vertical" }}
            value={form.prompt}
            onChange={(e) => set("prompt", e.target.value)}
          />
        )}
      </div>
      <ScheduleBuilder
        schedule={form.schedule}
        runAt={form.run_at}
        timezone={form.timezone}
        onChange={(next) =>
          setForm((f) => ({ ...f, schedule: next.schedule, run_at: next.run_at, timezone: next.timezone }))
        }
      />
      <TaskOverrideFields
        model={form.model}
        effort={form.reasoning_effort}
        agentModel={agentModel}
        onChange={(next) => setForm((f) => ({ ...f, model: next.model, reasoning_effort: next.reasoning_effort }))}
      />
      {isConfig ? null : (
        <Field label={t("field.visibility")}>
          <select className="input" value={form.visibility} onChange={(e) => set("visibility", e.target.value as Visibility)}>
            <option value="private">{t("vis.private")}</option>
            <option value="shared">{t("vis.shared")}</option>
          </select>
        </Field>
      )}

      <div className="row" style={{ justifyContent: "space-between", marginTop: 16 }}>
        <button className="btn danger" onClick={() => onDelete(task)}>
          <Trash2 size={14} strokeWidth={1.75} /> {t("act.delete")}
        </button>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn" onClick={onClose}>
            {t("btn.cancel")}
          </button>
          <button className="btn" onClick={() => onRunNow(task)}>
            <Zap size={14} strokeWidth={1.75} /> {t("act.runNow")}
          </button>
          <button className="btn" onClick={() => onHistory(task.id, task.name)}>
            <History size={14} strokeWidth={1.75} /> {t("act.history")}
          </button>
          <button className="btn primary" disabled={saving} onClick={() => void save()}>
            {saving ? "…" : t("btn.saveChanges")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── history modal ────────────────────────────────────────────────────────────
export function HistoryModal({ name, rows, onClose }: { name: string; rows: HistoryEntry[]; onClose: () => void }) {
  const t = useT(STR);
  const toast = useToast();
  const copy = useCallback(
    (text: string) => {
      navigator.clipboard?.writeText(text).then(
        () => toast.push(t("toast.copied")),
        () => toast.push(t("toast.copyFailed"), "error"),
      );
    },
    [toast, t],
  );

  return (
    <Modal
      title={t("history.title", { name })}
      subtitle={t(rows.length === 1 ? "history.subtitleOne" : "history.subtitleMany", { count: String(rows.length) })}
      onClose={onClose}
      width={640}
    >
      {rows.length === 0 ? (
        <div className="card" style={{ padding: 28, textAlign: "center", background: "var(--surface-2)" }}>
          <div style={{ fontWeight: 600 }}>{t("history.empty")}</div>
          <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
            {t("history.emptySub")}
          </div>
        </div>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {rows.map((h, i) => {
            const ok = !h.error;
            const stamp = h.ran_at || h.timestamp || null;
            const body = ok ? (h.result ?? "") : (h.error ?? "");
            return (
              <div
                key={h.id ?? i}
                className="card"
                style={{ padding: 12, borderLeft: `3px solid ${ok ? "var(--success)" : "var(--danger)"}` }}
              >
                <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
                  <span
                    className="badge"
                    style={
                      ok
                        ? { background: "var(--success-soft)", color: "var(--success)", border: "1px solid transparent", gap: 5 }
                        : { background: "var(--danger-soft)", color: "var(--danger)", border: "1px solid transparent", gap: 5 }
                    }
                  >
                    <StatusDot color={ok ? "var(--success)" : "var(--danger)"} />
                    {ok ? t("history.success") : t("history.failed")}
                  </span>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="muted" style={{ fontSize: 12, fontFamily: "var(--font-mono)" }}>
                      {fmtStamp(stamp)} · {fmtRel(stamp, t)}
                    </span>
                    {body ? (
                      <button
                        className="btn"
                        style={{ padding: "2px 8px", fontSize: 11 }}
                        title={t("history.copyOutput")}
                        onClick={() => copy(body)}
                      >
                        {t("history.copy")}
                      </button>
                    ) : null}
                  </div>
                </div>
                {body ? (
                  <pre
                    style={{
                      marginTop: 10,
                      marginBottom: 0,
                      maxHeight: 220,
                      overflow: "auto",
                      background: ok ? "var(--code-bg)" : "var(--danger-soft)",
                      border: `1px solid ${ok ? "var(--code-border)" : "var(--danger)"}`,
                      borderRadius: "var(--r-sm)",
                      padding: 10,
                      fontSize: 12,
                      color: ok ? undefined : "var(--danger)",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}
                  >
                    {body.slice(0, 2000)}
                  </pre>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn" onClick={onClose}>
          {t("btn.close")}
        </button>
      </div>
    </Modal>
  );
}
