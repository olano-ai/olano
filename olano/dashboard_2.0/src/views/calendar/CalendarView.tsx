// Calendar view — day / week / month views of scheduled task occurrences.
//
// It reads the SAME data and endpoints as the Scheduler view
// (GET /api/scheduler/tasks, already auth-scoped server-side to the agents the
// caller may access — see auth.filter_tasks_for_current_user), so in multi-user
// mode a member only ever sees occurrences for their own / granted agents. On
// top of that list it offers:
//   • a Day / Week / Month view switcher (segmented control)
//   • hide recurring / hide one-time toggles
//   • a scalable agent filter — a searchable popover multi-select (mirrors the
//     chat ModelPicker) that stays usable with dozens of agents
//   • click a day (or its "+") to schedule a new task pre-filled to that date
//   • click an occurrence to open the task detail / edit / history / delete
// Recurring (cron) tasks are expanded onto every day they fire within the
// visible range via the small parser in ./cron; one-time tasks land on run_at.
// Live refresh comes from the global /api/stream "scheduler_update" frame.
//
// Layout note: the month + week grids use `repeat(7, minmax(0, 1fr))` (NOT
// `1fr`, whose `auto` minimum is max-content) and give every cell `min-width:0`
// so a long task name/excerpt truncates inside its column instead of blowing the
// track out past the viewport. See `.cal-grid` / `.cal-cell` in globals.css.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { CalendarPlus, ChevronLeft, ChevronRight, Plus, RotateCw } from "lucide-react";
import { usePersistentState } from "../../core/usePersistentState";
import { parseRoute, viewPath } from "../../core/routeUrls";
import { type UrlHistory } from "../../core/useUrlParam";
import { subscribeStream } from "../../core/agentStream";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { AgentFilter } from "../../ui/AgentFilter";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import { ErrorState } from "../../ui/ErrorState";
import type { Agent } from "../../core/types";
import type { CreateForm, HistoryEntry, SchedulerEvent, Task } from "../scheduler/types";
import { isCronTask } from "../scheduler/helpers";
import { CreateModal, DetailModal, HistoryModal } from "../scheduler/SchedulerView";
import { cronEarliestMinutes, cronOccursOn, cronSingleTime, fmtMinutes, parseCron } from "./cron";
import { STR } from "./strings";

type AgentColor = (name: string | null) => string | null;
type Translate = (key: string, vars?: Record<string, string>) => string;
type ViewMode = "day" | "week" | "month";

interface CalEvent {
  task: Task;
  minutes: number; // minutes-since-midnight, for ordering + the time chip
  recurring: boolean;
  single: boolean; // a single concrete fire time → show "HH:MM" (else "≈")
}

// Two chips/cell keeps the two-line (name + excerpt) chip legible at 110px tall.
const MAX_PER_CELL = 2;

// A short, single-line excerpt of what the task does, for at-a-glance ID.
function excerpt(task: Task): string {
  return (task.prompt || "").replace(/\s+/g, " ").trim();
}

// Stable local-date key (avoids UTC drift from toISOString()).
function dateKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

const VIEW_MODES: readonly ViewMode[] = ["month", "week", "day"];

// `?date=` is a zero-padded local date, 2026-10-10: readable, sortable and
// free of the UTC drift toISOString() would add.
function dateParam(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function parseDateParam(raw: string | null): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw ?? "");
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d.getTime();
}
/** The calendar's address. Month and today are the bare defaults; every other
 *  query key already in the URL (`?focus=` …) rides along. */
function calendarHref(view: ViewMode, anchor: number, task: string | null, search: string): string {
  const params = new URLSearchParams(search);
  const today = new Date();
  const todayMs = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (anchor !== todayMs) params.set("date", dateParam(anchor));
  else params.delete("date");
  if (task) params.set("task", task);
  else params.delete("task");
  const q = params.toString();
  return `${viewPath("calendar", { section: view === "month" ? null : view })}${q ? `?${q}` : ""}`;
}
function sameDay(a: Date, b: Date): boolean {
  return dateKey(a) === dateKey(b);
}
// Date arithmetic via the local constructor (DST-safe — no 86_400_000 math).
function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}
function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}
function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}
function addMonths(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}
// Sunday-first week start (matches Date.getDay()).
function startOfWeek(d: Date): Date {
  const s = startOfDay(d);
  return addDays(s, -s.getDay());
}

// The 6×7 grid of days covering the visible month, padded to full weeks.
function buildMonthGrid(month: Date): Date[] {
  const start = addDays(startOfMonth(month), -startOfMonth(month).getDay());
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}
// The 7 days of the week containing `d` (Sunday-first).
function buildWeek(d: Date): Date[] {
  const start = startOfWeek(d);
  return Array.from({ length: 7 }, (_, i) => addDays(start, i));
}

export function CalendarView() {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;
  const [agents, setAgents] = useState<Agent[]>([]);
  // View mode + the focused date (the "cursor"). Prev/Next shifts the anchor by
  // one day / week / month depending on the active view; the month/week grids
  // and the day agenda all derive from it.
  //
  // Both are the address: /calendar/<mode>?date=YYYY-MM-DD (month and today
  // are the bare defaults), plus `?task=<id>` for an open task, so a reload or
  // a copied link lands on the same view, and Back/Forward step through the
  // views, dates and tasks visited. The last view and date are remembered for
  // the session too, so the sidebar's bare /calendar returns to them.
  const location = useLocation();
  const navigate = useNavigate();
  const [rememberedView, setRememberedView] = usePersistentState<ViewMode>("calendar.view", "month");
  const [rememberedAnchor, setRememberedAnchor] = usePersistentState<number>("calendar.anchor", () =>
    startOfDay(new Date()).getTime(),
  );
  const [arrived, setArrived] = useState(false);
  const todayMs = startOfDay(new Date()).getTime();
  const urlSection = parseRoute(location.pathname).section;
  const urlParams = new URLSearchParams(location.search);
  const urlView = VIEW_MODES.find((m) => m === urlSection) ?? null;
  const urlAnchor = parseDateParam(urlParams.get("date"));
  const view: ViewMode = urlView ?? (arrived ? "month" : rememberedView);
  const anchor: number = urlAnchor ?? (arrived ? todayMs : rememberedAnchor);
  const detailId = urlParams.get("task");

  // Every change lands as ONE navigation even when a handler makes several
  // (opening a day sets the date and the view): changes made in the same tick
  // are merged, then written against the latest state. Any "push" in the
  // batch makes it a Back step.
  const latestRef = useRef({ view, anchor, task: detailId as string | null });
  latestRef.current = { view, anchor, task: detailId };
  const pendingRef = useRef<{ patch: Partial<{ view: ViewMode; anchor: number; task: string | null }>; push: boolean } | null>(null);
  const writeUrl = useCallback(
    (patch: Partial<{ view: ViewMode; anchor: number; task: string | null }>, history: UrlHistory) => {
      const first = !pendingRef.current;
      const pending = pendingRef.current ?? { patch: {}, push: false };
      pending.patch = { ...pending.patch, ...patch };
      pending.push = pending.push || history === "push";
      pendingRef.current = pending;
      if (!first) return;
      queueMicrotask(() => {
        const batch = pendingRef.current;
        pendingRef.current = null;
        if (!batch) return;
        const next = { ...latestRef.current, ...batch.patch };
        const to = calendarHref(next.view, next.anchor, next.task, window.location.search);
        if (to === window.location.pathname + window.location.search) return;
        navigate(to, { replace: !batch.push });
      });
    },
    [navigate],
  );
  const setView = useCallback((v: ViewMode) => writeUrl({ view: v }, "push"), [writeUrl]);
  const setAnchor = useCallback(
    (next: number | ((cur: number) => number)) => {
      const base = pendingRef.current?.patch.anchor ?? latestRef.current.anchor;
      writeUrl({ anchor: typeof next === "function" ? next(base) : next }, "push");
    },
    [writeUrl],
  );
  const setDetailId = useCallback(
    (id: string | null, history: UrlHistory = "push") => writeUrl({ task: id }, history),
    [writeUrl],
  );
  // Arrival: a bare /calendar shows the remembered view and date, and the
  // address is filled in to say so (replace: same place, not a step).
  useEffect(() => {
    if (arrived) return;
    setArrived(true);
    if (!urlView && !urlAnchor && (rememberedView !== "month" || rememberedAnchor !== todayMs)) {
      writeUrl({ view: rememberedView, anchor: rememberedAnchor }, "replace");
    }
  }, [arrived, urlView, urlAnchor, rememberedView, rememberedAnchor, todayMs, writeUrl]);
  useEffect(() => {
    if (!arrived) return;
    if (view !== rememberedView) setRememberedView(view);
    if (anchor !== rememberedAnchor) setRememberedAnchor(anchor);
  }, [arrived, view, anchor, rememberedView, rememberedAnchor, setRememberedView, setRememberedAnchor]);
  const [hideRecurring, setHideRecurring] = usePersistentState("calendar.hideRecurring", false);
  const [hideOneTime, setHideOneTime] = usePersistentState("calendar.hideOneTime", false);
  // Persist the agents the user has HIDDEN (empty = all shown), so newly created
  // agents are visible by default rather than silently filtered out.
  const [hiddenAgents, setHiddenAgents] = usePersistentState<string[]>("calendar.hiddenAgents", []);
  // The day whose agenda shows beneath the month grid. (Week shows every day's
  // events inline; Day's agenda is the focused anchor day itself.)
  const [selectedKey, setSelectedKey] = usePersistentState<string>("calendar.selectedDay", () => dateKey(new Date()));
  const [createInitial, setCreateInitial] = useState<Partial<CreateForm> | null>(null);
  const [history, setHistory] = useState<{ name: string; rows: HistoryEntry[] } | null>(null);

  const anchorDate = useMemo(() => new Date(anchor), [anchor]);
  const anchorDay = useMemo(() => startOfDay(anchorDate), [anchorDate]);
  const today = useMemo(() => new Date(), []);

  const agentColor = useCallback<AgentColor>(
    (name) => (name ? agents.find((a) => a.name === name)?.color || null : null),
    [agents],
  );

  const load = useCallback(() => {
    // Keep current events on screen while refetching (no blank-to-spinner
    // flash); a failed first load renders an inline error card with retry,
    // a failed refresh keeps the stale grid and toasts.
    api
      .get<Task[]>("/api/scheduler/tasks")
      .then((rows) => {
        setTasks(rows);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        setLoadError(e instanceof Error ? e.message : t("toast.loadFailed"));
        if (tasksRef.current !== null) toast.push(t("toast.loadFailed"), "error");
      });
  }, [toast, t]);

  useEffect(() => {
    api
      .cachedGet<Agent[]>("/api/agents")
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    return subscribeStream((rawFrame) => {
      const frame = rawFrame as SchedulerEvent;
      if (frame.type === "scheduler_update") load();
    });
  }, [load]);

  // Agent filter: a key is the task's agent_name, or "" for the default agent.
  // "" sorts first; the rest follow the /api/agents order, plus any agent that
  // only appears on a task (e.g. a since-deleted agent still owning history).
  const hidden = useMemo(() => new Set(hiddenAgents), [hiddenAgents]);
  const agentKeys = useMemo(() => {
    const keys = new Set<string>([""]);
    for (const a of agents) keys.add(a.name);
    for (const task of tasks ?? []) keys.add(task.agent_name || "");
    return Array.from(keys);
  }, [agents, tasks]);

  const toggleAgent = useCallback(
    (key: string) =>
      setHiddenAgents((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key])),
    [setHiddenAgents],
  );

  // Tasks passing the type + agent filters. The server already scoped the list
  // to this user, so this is purely view-level filtering.
  const visibleTasks = useMemo(() => {
    return (tasks ?? []).filter((task) => {
      const cron = isCronTask(task);
      if (cron && hideRecurring) return false;
      if (!cron && hideOneTime) return false;
      if (hidden.has(task.agent_name || "")) return false;
      return true;
    });
  }, [tasks, hideRecurring, hideOneTime, hidden]);

  // The days the active view paints onto: 42 (month grid), 7 (week), or 1 (day).
  const days = useMemo(() => {
    if (view === "month") return buildMonthGrid(anchorDate);
    if (view === "week") return buildWeek(anchorDate);
    return [anchorDay];
  }, [view, anchorDate, anchorDay]);

  // Expand every visible task onto the days it occurs within the current range.
  const eventsByDay = useMemo(() => {
    const map = new Map<string, CalEvent[]>();
    for (const d of days) map.set(dateKey(d), []);
    for (const task of visibleTasks) {
      if (isCronTask(task)) {
        const f = parseCron(task.schedule);
        if (!f) continue;
        const minutes = cronEarliestMinutes(f);
        const single = cronSingleTime(f);
        for (const d of days) {
          if (cronOccursOn(f, d)) map.get(dateKey(d))!.push({ task, minutes, recurring: true, single });
        }
      } else if (task.run_at) {
        const dt = new Date(task.run_at);
        if (Number.isNaN(dt.getTime())) continue;
        const bucket = map.get(dateKey(dt));
        if (bucket) bucket.push({ task, minutes: dt.getHours() * 60 + dt.getMinutes(), recurring: false, single: true });
      }
    }
    for (const list of map.values()) list.sort((a, b) => a.minutes - b.minutes);
    return map;
  }, [visibleTasks, days]);

  const periodEventCount = useMemo(() => {
    let n = 0;
    for (const list of eventsByDay.values()) n += list.length;
    return n;
  }, [eventsByDay]);

  // Month-view selected-day panel + day-view agenda.
  const selectedDate = useMemo(() => days.find((d) => dateKey(d) === selectedKey) ?? null, [days, selectedKey]);
  const dayEvents = eventsByDay.get(dateKey(anchorDay)) ?? [];

  const detailTask = useMemo(
    () => (detailId ? visibleTasks.find((task) => task.id === detailId) ?? null : null),
    [detailId, visibleTasks],
  );

  // ── mutations (mirror the Scheduler view) ───────────────────────────────────
  async function deleteTask(task: Task) {
    const ok = await confirm({
      title: t("confirm.delete.title", { name: task.name }),
      message: t("confirm.delete.msg"),
      confirmLabel: t("confirm.delete.ok"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/scheduler/tasks/${task.id}`);
      if (detailId === task.id) setDetailId(null, "replace");
      load();
    } catch {
      toast.push(t("toast.loadFailed"), "error");
    }
  }

  async function runNow(task: Task) {
    try {
      await api.post(`/api/scheduler/tasks/${encodeURIComponent(task.id)}/run`, {});
      toast.push(t("toast.ranNow", { name: task.name }));
      setTimeout(load, 1200);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("toast.runNowFailed"), "error");
    }
  }

  const openHistory = useCallback(
    async (taskId: string, name: string) => {
      try {
        const rows = await api.get<HistoryEntry[]>(`/api/scheduler/history/${taskId}?limit=20`);
        setHistory({ name, rows });
      } catch {
        toast.push(t("toast.loadFailed"), "error");
      }
    },
    [toast, t],
  );

  // ── navigation ──────────────────────────────────────────────────────────────
  // Prev/Next step by the active view's unit.
  const step = useCallback(
    (dir: -1 | 1) => {
      setAnchor((cur) => {
        const d = new Date(cur);
        if (view === "month") return addMonths(d, dir).getTime();
        if (view === "week") return addDays(d, dir * 7).getTime();
        return addDays(d, dir).getTime();
      });
    },
    [view, setAnchor],
  );

  const goToday = useCallback(() => {
    setAnchor(startOfDay(new Date()).getTime());
    setSelectedKey(dateKey(new Date()));
  }, [setAnchor, setSelectedKey]);

  // Select a day in the month grid (and follow into its month if it's an
  // overflow cell, so the header stays truthful). Pure selection — no modal.
  const selectDay = useCallback(
    (day: Date) => {
      setSelectedKey(dateKey(day));
      if (day.getMonth() !== anchorDate.getMonth() || day.getFullYear() !== anchorDate.getFullYear()) {
        setAnchor(startOfDay(day).getTime());
      }
    },
    [anchorDate, setSelectedKey, setAnchor],
  );

  // Drill into a single day (from a week-column header) — switch to Day view
  // anchored there.
  const openDay = useCallback(
    (day: Date) => {
      setAnchor(startOfDay(day).getTime());
      setSelectedKey(dateKey(day));
      setView("day");
    },
    [setAnchor, setSelectedKey, setView],
  );

  // Open the create modal pre-filled to a one-time run at 09:00 on `day`.
  // Zero-pad the date — datetime.fromisoformat (and <input type="date">) reject
  // the non-padded `dateKey` form like "2026-6-9".
  const scheduleForDay = useCallback(
    (day: Date) => {
      setSelectedKey(dateKey(day));
      const p = (n: number) => String(n).padStart(2, "0");
      const iso = `${day.getFullYear()}-${p(day.getMonth() + 1)}-${p(day.getDate())}T09:00:00`;
      setCreateInitial({ run_at: iso });
    },
    [setSelectedKey],
  );

  function resetFilters() {
    setHiddenAgents([]);
    setHideRecurring(false);
    setHideOneTime(false);
  }

  const weekdays = [
    t("weekday.sun"),
    t("weekday.mon"),
    t("weekday.tue"),
    t("weekday.wed"),
    t("weekday.thu"),
    t("weekday.fri"),
    t("weekday.sat"),
  ];

  // Header label + min-width per view: "June 2026" / "Jun 21 – 27, 2026" /
  // "Sunday, June 21, 2026". The week range is assembled from valid Intl combos
  // only — a partial { day, year } (no month) makes ICU emit garbage like
  // "2026 (day: 27)", so the end day is appended as a plain numeral + year.
  const weekStart = startOfWeek(anchorDate);
  const weekEnd = addDays(weekStart, 6);
  const md = (d: Date) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const weekLabel =
    weekStart.getMonth() === weekEnd.getMonth() && weekStart.getFullYear() === weekEnd.getFullYear()
      ? `${md(weekStart)} – ${weekEnd.getDate()}, ${weekEnd.getFullYear()}`
      : weekStart.getFullYear() === weekEnd.getFullYear()
        ? `${md(weekStart)} – ${md(weekEnd)}, ${weekEnd.getFullYear()}`
        : `${md(weekStart)}, ${weekStart.getFullYear()} – ${md(weekEnd)}, ${weekEnd.getFullYear()}`;
  const navLabel =
    view === "month"
      ? anchorDate.toLocaleDateString(undefined, { month: "long", year: "numeric" })
      : view === "week"
        ? weekLabel
        : anchorDay.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  const navMin = view === "day" ? 230 : view === "week" ? 188 : 150;

  // Distinguish "nothing scheduled here" from "filters hid everything".
  const filteredAllOut = periodEventCount === 0 && (tasks?.length ?? 0) > 0 && visibleTasks.length === 0;
  const periodLabel = view === "week" ? t("period.week") : t("period.month");

  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          <button className="btn" onClick={load}>
            <RotateCw size={15} strokeWidth={1.75} /> {t("btn.refresh")}
          </button>
          <button className="btn primary" onClick={() => setCreateInitial({})}>
            <Plus size={15} strokeWidth={1.75} /> {t("btn.newTask")}
          </button>
        </div>
      </div>

      <div className="page">
        <h1>{t("title")}</h1>
        <p className="muted">{t("subtitle")}</p>

        {/* navigation + view switcher */}
        <div className="row" style={{ justifyContent: "space-between", marginTop: 16, gap: 10, flexWrap: "wrap" }}>
          <div className="row" style={{ gap: 6 }}>
            <button
              className="btn"
              style={{ padding: "5px 9px" }}
              title={t("btn.prev")}
              aria-label={t("btn.prev")}
              onClick={() => step(-1)}
            >
              <ChevronLeft size={16} strokeWidth={1.75} />
            </button>
            <strong style={{ fontSize: 16, minWidth: navMin, textAlign: "center" }}>{navLabel}</strong>
            <button
              className="btn"
              style={{ padding: "5px 9px" }}
              title={t("btn.next")}
              aria-label={t("btn.next")}
              onClick={() => step(1)}
            >
              <ChevronRight size={16} strokeWidth={1.75} />
            </button>
            <button className="btn" style={{ padding: "5px 12px" }} onClick={goToday}>
              {t("btn.today")}
            </button>
          </div>
          <div className="row" style={{ gap: 14, flexWrap: "wrap" }}>
            <ViewSwitcher view={view} onChange={setView} t={t} />
            <Legend t={t} />
          </div>
        </div>

        {/* filters */}
        <div
          className="card"
          style={{ marginTop: 14, padding: 12, display: "flex", gap: 16, flexWrap: "wrap", alignItems: "center" }}
        >
          <div className="row" style={{ gap: 16, alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 12, fontWeight: 600 }}>
              {t("filter.show")}
            </span>
            <Check label={t("filter.recurring")} checked={!hideRecurring} onChange={() => setHideRecurring((v) => !v)} />
            <Check label={t("filter.oneTime")} checked={!hideOneTime} onChange={() => setHideOneTime((v) => !v)} />
          </div>
          {agentKeys.length > 1 && (
            <AgentFilter
              items={agentKeys.map((k) => ({ key: k, label: k || t("filter.default"), color: agentColor(k || null) }))}
              hidden={hidden}
              onToggle={toggleAgent}
              onAll={() => setHiddenAgents([])}
              onNone={() => setHiddenAgents(agentKeys)}
              labels={{
                title: t("filter.agents"),
                allShown: t("filter.allAgentsShown"),
                noneShown: t("filter.noneShown"),
                summary: (n, m) => t("filter.agentsSummary", { n: String(n), m: String(m) }),
                search: t("filter.searchAgents"),
                selectAll: t("filter.selectAll"),
                clear: t("filter.clear"),
                noMatch: t("filter.noMatch"),
              }}
            />
          )}
        </div>

        {/* the active view */}
        {tasks === null && loadError ? (
          <ErrorState message={t("toast.loadFailed")} detail={loadError} onRetry={load} />
        ) : tasks === null ? (
          <div className="spinner" style={{ marginTop: 24 }} />
        ) : view === "month" ? (
          <MonthGrid
            grid={days}
            anchorDate={anchorDate}
            today={today}
            selectedKey={selectedKey}
            weekdays={weekdays}
            eventsByDay={eventsByDay}
            agentColor={agentColor}
            onSelectDay={selectDay}
            onSchedule={scheduleForDay}
            onOpenEvent={(id) => setDetailId(id)}
            t={t}
          />
        ) : view === "week" ? (
          <WeekGrid
            week={days}
            today={today}
            eventsByDay={eventsByDay}
            agentColor={agentColor}
            onOpenDay={openDay}
            onSchedule={scheduleForDay}
            onOpenEvent={(id) => setDetailId(id)}
            t={t}
          />
        ) : (
          <DayAgenda
            date={anchorDay}
            events={dayEvents}
            agentColor={agentColor}
            onSchedule={scheduleForDay}
            onOpenEvent={(id) => setDetailId(id)}
            t={t}
            title={null}
          />
        )}

        {/* empty state for the month/week grids (day handles its own) */}
        {tasks !== null && view !== "day" && periodEventCount === 0 && (
          <div className="card" style={{ marginTop: 14, padding: 32, textAlign: "center" }}>
            <div style={{ fontWeight: 600 }}>
              {filteredAllOut ? t("empty.filteredTitle") : t("empty.title", { period: periodLabel })}
            </div>
            <div className="muted" style={{ marginTop: 4, fontSize: 13 }}>
              {filteredAllOut ? t("empty.filteredSub") : t("empty.sub")}
            </div>
            <button
              className="btn primary"
              style={{ marginTop: 14 }}
              onClick={() => (filteredAllOut ? resetFilters() : setCreateInitial({}))}
            >
              {filteredAllOut ? t("empty.reset") : t("btn.newTask")}
            </button>
          </div>
        )}

        {/* month-view selected-day panel */}
        {view === "month" && selectedDate && (
          <DayAgenda
            date={selectedDate}
            events={eventsByDay.get(dateKey(selectedDate)) ?? []}
            agentColor={agentColor}
            onSchedule={scheduleForDay}
            onOpenEvent={(id) => setDetailId(id)}
            t={t}
            title={selectedDate.toLocaleDateString(undefined, {
              weekday: "long",
              month: "long",
              day: "numeric",
              year: "numeric",
            })}
          />
        )}
      </div>

      {createInitial && (
        <CreateModal
          agents={agents}
          initial={createInitial}
          onClose={() => setCreateInitial(null)}
          onCreated={() => {
            setCreateInitial(null);
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

// ── view switcher (segmented control) ────────────────────────────────────────
function ViewSwitcher({ view, onChange, t }: { view: ViewMode; onChange: (v: ViewMode) => void; t: Translate }) {
  const opts: { id: ViewMode; label: string }[] = [
    { id: "day", label: t("view.day") },
    { id: "week", label: t("view.week") },
    { id: "month", label: t("view.month") },
  ];
  return (
    <div className="seg" role="tablist" aria-label={t("view.label")}>
      {opts.map((o) => (
        <button
          key={o.id}
          type="button"
          role="tab"
          aria-selected={view === o.id}
          className={`seg-btn${view === o.id ? " active" : ""}`}
          style={{ padding: "5px 13px" }}
          onClick={() => onChange(o.id)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Legend({ t }: { t: Translate }) {
  const dot = (color: string) => (
    <span style={{ width: 9, height: 9, borderRadius: 3, background: color, display: "inline-block" }} />
  );
  return (
    <div className="row muted" style={{ gap: 12, fontSize: 12 }}>
      <span className="row" style={{ gap: 5 }}>
        <RotateCw size={11} strokeWidth={2} style={{ color: "var(--info)" }} /> {t("legend.recurring")}
      </span>
      <span className="row" style={{ gap: 5 }}>
        {dot("var(--text-3)")} {t("legend.oneTime")}
      </span>
    </div>
  );
}

function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: () => void }) {
  return (
    <label className="row" style={{ gap: 6, fontSize: 13, cursor: "pointer", userSelect: "none" }}>
      <input type="checkbox" checked={checked} onChange={onChange} />
      {label}
    </label>
  );
}

// ── month grid ───────────────────────────────────────────────────────────────
// A small, accent-filled disc for "today"; a quiet number otherwise. Shared by
// the month cell and the week-column header so "today" reads identically.
function DayNumber({ day, today, size = 22 }: { day: Date; today: Date; size?: number }) {
  const isToday = sameDay(day, today);
  return (
    <span
      style={{
        fontSize: 12,
        fontWeight: isToday ? 700 : 500,
        minWidth: size,
        height: size,
        padding: "0 6px",
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        borderRadius: size,
        background: isToday ? "var(--accent, var(--info))" : "transparent",
        color: isToday ? "var(--accent-fg, #fff)" : undefined,
        flexShrink: 0,
      }}
    >
      {day.getDate()}
    </span>
  );
}

function MonthGrid({
  grid,
  anchorDate,
  today,
  selectedKey,
  weekdays,
  eventsByDay,
  agentColor,
  onSelectDay,
  onSchedule,
  onOpenEvent,
  t,
}: {
  grid: Date[];
  anchorDate: Date;
  today: Date;
  selectedKey: string;
  weekdays: string[];
  eventsByDay: Map<string, CalEvent[]>;
  agentColor: AgentColor;
  onSelectDay: (d: Date) => void;
  onSchedule: (d: Date) => void;
  onOpenEvent: (id: string) => void;
  t: Translate;
}) {
  return (
    <div className="card" style={{ marginTop: 14, padding: 0, overflow: "hidden" }}>
      <div className="cal-grid">
        {weekdays.map((w, i) => (
          <div
            key={i}
            className="muted"
            style={{
              padding: "8px 10px",
              fontSize: 11,
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: 0.4,
              borderBottom: "1px solid var(--border)",
              textAlign: "center",
            }}
          >
            {w}
          </div>
        ))}
        {grid.map((day) => {
          const inMonth = day.getMonth() === anchorDate.getMonth();
          const isSelected = dateKey(day) === selectedKey;
          const events = eventsByDay.get(dateKey(day)) ?? [];
          const a11y = t("a11y.day", {
            date: day.toLocaleDateString(undefined, { month: "long", day: "numeric" }),
            n: String(events.length),
          });
          return (
            <div
              key={dateKey(day)}
              className="cal-cell"
              role="button"
              tabIndex={0}
              aria-label={a11y}
              aria-pressed={isSelected}
              onClick={() => onSelectDay(day)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelectDay(day);
                }
              }}
              style={{
                minHeight: 110,
                minWidth: 0,
                padding: 6,
                borderRight: "1px solid var(--border)",
                borderBottom: "1px solid var(--border)",
                background: inMonth ? "transparent" : "var(--surface-2)",
                // A clear selection ring that reads independently of "today".
                boxShadow: isSelected ? "inset 0 0 0 2px var(--accent, var(--info))" : "none",
                opacity: inMonth ? 1 : 0.5,
                cursor: "pointer",
                position: "relative",
                display: "flex",
                flexDirection: "column",
                gap: 3,
                outline: "none",
              }}
            >
              <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                <DayNumber day={day} today={today} />
                <button
                  className="btn ghost cal-add"
                  style={{ padding: "1px 4px" }}
                  title={t("day.add")}
                  aria-label={t("day.add")}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSchedule(day);
                  }}
                >
                  <Plus size={12} strokeWidth={2} />
                </button>
              </div>
              {events.slice(0, MAX_PER_CELL).map((ev, i) => (
                <EventChip
                  key={`${ev.task.id}-${i}`}
                  ev={ev}
                  color={agentColor(ev.task.agent_name)}
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpenEvent(ev.task.id);
                  }}
                />
              ))}
              {events.length > MAX_PER_CELL && (
                <button
                  className="btn ghost"
                  style={{ padding: "1px 4px", fontSize: 10, justifyContent: "flex-start", color: "var(--text-3)" }}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectDay(day);
                  }}
                >
                  {t("grid.more", { n: String(events.length - MAX_PER_CELL) })}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── week grid ────────────────────────────────────────────────────────────────
// Seven day-columns, each listing ALL of that day's occurrences (no +N collapse
// — there's vertical room). The header (weekday + date) drills into Day view.
function WeekGrid({
  week,
  today,
  eventsByDay,
  agentColor,
  onOpenDay,
  onSchedule,
  onOpenEvent,
  t,
}: {
  week: Date[];
  today: Date;
  eventsByDay: Map<string, CalEvent[]>;
  agentColor: AgentColor;
  onOpenDay: (d: Date) => void;
  onSchedule: (d: Date) => void;
  onOpenEvent: (id: string) => void;
  t: Translate;
}) {
  const weekdayShort = (d: Date) => d.toLocaleDateString(undefined, { weekday: "short" });
  return (
    <div className="card" style={{ marginTop: 14, padding: 0, overflow: "hidden" }}>
      <div className="cal-grid">
        {week.map((day) => {
          const isToday = sameDay(day, today);
          const events = eventsByDay.get(dateKey(day)) ?? [];
          return (
            <div
              key={dateKey(day)}
              className="cal-cell"
              style={{
                minWidth: 0,
                minHeight: 340,
                borderRight: "1px solid var(--border)",
                background: isToday ? "var(--accent-soft)" : "transparent",
                display: "flex",
                flexDirection: "column",
                position: "relative",
              }}
            >
              {/* header — drills into Day view */}
              <div
                role="button"
                tabIndex={0}
                aria-label={t("a11y.day", {
                  date: day.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }),
                  n: String(events.length),
                })}
                onClick={() => onOpenDay(day)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpenDay(day);
                  }
                }}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 2,
                  padding: "8px 6px 6px",
                  borderBottom: "1px solid var(--border)",
                  cursor: "pointer",
                  outline: "none",
                }}
              >
                <span
                  className="muted"
                  style={{ fontSize: 10.5, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.4 }}
                >
                  {weekdayShort(day)}
                </span>
                <DayNumber day={day} today={today} size={24} />
              </div>
              <button
                className="btn ghost cal-add"
                style={{ position: "absolute", top: 6, right: 6, padding: "1px 4px" }}
                title={t("day.add")}
                aria-label={t("day.add")}
                onClick={(e) => {
                  e.stopPropagation();
                  onSchedule(day);
                }}
              >
                <Plus size={12} strokeWidth={2} />
              </button>
              {/* body — every occurrence for the day */}
              <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "6px 6px 8px", minWidth: 0 }}>
                {events.length === 0 ? (
                  <span className="muted" style={{ fontSize: 11, textAlign: "center", padding: "8px 0", opacity: 0.6 }}>
                    —
                  </span>
                ) : (
                  events.map((ev, i) => (
                    <EventChip
                      key={`${ev.task.id}-${i}`}
                      ev={ev}
                      color={agentColor(ev.task.agent_name)}
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenEvent(ev.task.id);
                      }}
                    />
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EventChip({
  ev,
  color,
  onClick,
}: {
  ev: CalEvent;
  color: string | null;
  onClick: (e: React.MouseEvent) => void;
}) {
  // Agent colour stays the left-border cue even for recurring tasks; a small
  // repeat glyph (not a colour override) carries the "recurring" signal so both
  // signals survive when filtering by agent.
  const accent = color || "var(--text-3)";
  const ex = excerpt(ev.task);
  const timeLabel = ev.single ? fmtMinutes(ev.minutes) : `≈${fmtMinutes(ev.minutes)}`;
  return (
    <button
      onClick={onClick}
      title={`${ev.task.name}${ev.recurring && ev.task.schedule ? ` · ${ev.task.schedule}` : ""}${ex ? `\n${ex}` : ""}`}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 1,
        width: "100%",
        minWidth: 0,
        textAlign: "left",
        border: "none",
        background: "var(--surface-3)",
        borderLeft: `3px solid ${accent}`,
        borderRadius: 4,
        padding: "2px 5px",
        cursor: "pointer",
        overflow: "hidden",
        color: "var(--text-1)",
      }}
    >
      <span className="row" style={{ gap: 3, fontSize: 11, minWidth: 0 }}>
        {ev.recurring && <RotateCw size={9} strokeWidth={2} style={{ color: "var(--info)", flexShrink: 0 }} />}
        <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-3)", flexShrink: 0 }}>{timeLabel}</span>
        <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {ev.task.name}
        </span>
      </span>
      {ex && (
        <span
          style={{
            fontSize: 10,
            color: "var(--text-3)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {ex}
        </span>
      )}
    </button>
  );
}

// ── day agenda ───────────────────────────────────────────────────────────────
// The focused-day event list. Used both as the Day view's main content
// (title=null — the toolbar already shows the date) and as the month grid's
// selected-day panel (title=the long date).
function DayAgenda({
  date,
  events,
  agentColor,
  onSchedule,
  onOpenEvent,
  t,
  title,
}: {
  date: Date;
  events: CalEvent[];
  agentColor: AgentColor;
  onSchedule: (d: Date) => void;
  onOpenEvent: (id: string) => void;
  t: Translate;
  title: string | null;
}) {
  const countLabel = events.length > 0 ? t("day.count", { n: String(events.length) }) : t("day.none");
  return (
    <div className="card" style={{ marginTop: 14, padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <div className="row" style={{ gap: 8, minWidth: 0 }}>
          <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {title ?? countLabel}
          </strong>
          {title && events.length > 0 && <span className="badge">{events.length}</span>}
        </div>
        <button
          className="btn"
          style={{ padding: "5px 11px", flexShrink: 0 }}
          onClick={() => onSchedule(date)}
        >
          <CalendarPlus size={14} strokeWidth={1.75} /> {t("day.add")}
        </button>
      </div>
      {events.length === 0 ? (
        <div className="muted" style={{ marginTop: 12, fontSize: 13 }}>
          {t("day.empty")}
        </div>
      ) : (
        <div style={{ display: "grid", gap: 8, marginTop: 12 }}>
          {events.map((ev, i) => (
            <DayCard
              key={`${ev.task.id}-${i}`}
              ev={ev}
              color={agentColor(ev.task.agent_name)}
              onOpen={() => onOpenEvent(ev.task.id)}
              t={t}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function DayCard({ ev, color, onOpen, t }: { ev: CalEvent; color: string | null; onOpen: () => void; t: Translate }) {
  const ex = excerpt(ev.task);
  return (
    <div
      className="card"
      style={{ padding: "10px 12px", cursor: "pointer", borderLeft: `3px solid ${color || "var(--border-strong)"}` }}
      onClick={onOpen}
    >
      <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
        <div className="row" style={{ gap: 8, minWidth: 0 }}>
          <span className="badge" style={{ fontFamily: "var(--font-mono)", background: "var(--surface-3)" }}>
            {ev.single ? fmtMinutes(ev.minutes) : `≈ ${fmtMinutes(ev.minutes)}`}
          </span>
          <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ev.task.name}</strong>
        </div>
        <span
          className="badge"
          style={ev.recurring ? { background: "var(--info-soft)", color: "var(--info)", gap: 4 } : { fontFamily: "var(--font-mono)" }}
        >
          {ev.recurring && <RotateCw size={11} strokeWidth={2} />}
          {ev.recurring ? t("legend.recurring") : t("legend.oneTime")}
        </span>
      </div>
      {ex && (
        <div
          style={{
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
            fontSize: 12,
            color: "var(--text-2)",
            marginTop: 6,
          }}
        >
          {ex}
        </div>
      )}
      <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
        {ev.task.agent_name || t("filter.default")}
        {ev.recurring ? ` · ${ev.task.schedule}` : ""}
      </div>
    </div>
  );
}
