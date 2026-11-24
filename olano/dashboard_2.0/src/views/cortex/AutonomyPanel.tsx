import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, forwardRef } from "react";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import { useToast } from "../../ui/toast";
import { subscribeStream } from "../../core/agentStream";
import {
  countOverrides,
  cronHuman,
  errMessage,
  OVERRIDE_FIELDS,
  relFuture,
  relPast,
  statusColor,
} from "./helpers";
import { ConfirmModal } from "./ui";
import { ActivityDrawer } from "./ActivityDrawer";
import { ErrorRemediation } from "../chat/ErrorRemediation";
import { STR } from "./strings";
import { startCortexJob, useCortexJobsOptional } from "./cortexJobs";
import type { CognitionPhase, CognitionStatusResponse } from "./types";

export interface AutonomyHandle {
  reload: () => void;
}

// Three-state override value: null ⇒ inherit the All-agents default.
type Tri = boolean | null;

// What a per-agent override can hold: a tri-state switch, a preset name
// (intensity), or a number (the background-run budget). `null` always means
// inherit, whatever the field's type.
type OverrideValue = Tri | string | number;

// What the All-agents column can hold. No null — the global tier is the floor
// the agent column inherits from, so it always carries a value.
type GlobalValue = boolean | string | number;

// Mirrors system_settings._resolve. Used ONLY to preview a click before its
// response lands — every settled value on screen comes from the server's
// `resolved`, so the two tiers can never be resolved differently here than they
// are in the engine.
function previewResolve(override: Tri | undefined, fallback: boolean): boolean {
  return typeof override === "boolean" ? override : fallback;
}

// Writes are optimistic and coalesced per key: the knob moves on click, the
// request goes out after a short quiet period, and only a failure moves it back.
// Clicking a knob used to cost three sequential round trips (read the agent's
// whole config, PUT the whole block back, refetch the status) with every control
// disabled throughout — about half a second per click against a remote box, and
// two quick clicks lost one of the two.
const WRITE_DEBOUNCE_MS = 300;

// A number knob for one half of the background-run budget.
//
// Deliberately declared at MODULE scope, unlike the Row/TriControl helpers
// below. Those are re-created on every render of AutonomyPanel, which gives
// React a new component type each time and remounts the subtree — invisible for
// a button, but it would wipe a half-typed number here every time the panel's
// background poll landed. A stable identity keeps the draft alive while you
// type.
//
// The draft commits on blur or Enter, never per keystroke: a debounced write
// per digit would send "1", "10", "100" as three separate budgets.
function BudgetField({
  value,
  effective,
  min,
  max,
  label,
  inheritable,
  disabled,
  onCommit,
}: {
  /** The tier's own value. `null` in the agent column means "inheriting". */
  value: number | null;
  /** What the agent actually runs with — shown as the placeholder while inheriting. */
  effective: number;
  min: number;
  max: number;
  label: string;
  /** Agent column: offers Inherit. Global column: always holds a value. */
  inheritable: boolean;
  disabled?: boolean;
  onCommit: (v: number | null) => void;
}) {
  const t = useT(STR);
  const [draft, setDraft] = useState<string>(value === null ? "" : String(value));

  // Re-sync when the server (or an optimistic overlay) moves the value under
  // us — but only when it actually differs, so this never fights the user's
  // in-flight typing.
  const settled = value === null ? "" : String(value);
  const lastSettled = useRef(settled);
  useEffect(() => {
    if (lastSettled.current !== settled) {
      lastSettled.current = settled;
      setDraft(settled);
    }
  }, [settled]);

  const commit = () => {
    const raw = draft.trim();
    if (!raw) {
      // Empty means "inherit" where that is expressible; in the global column
      // there is nothing to fall back to, so restore what was there.
      if (inheritable) onCommit(null);
      else setDraft(settled);
      return;
    }
    const parsed = Number.parseInt(raw, 10);
    if (Number.isNaN(parsed)) {
      setDraft(settled);
      return;
    }
    const clamped = Math.max(min, Math.min(max, parsed));
    setDraft(String(clamped));
    if (clamped !== value) onCommit(clamped);
  };

  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <input
        className="input"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        step={1}
        disabled={disabled}
        aria-label={label}
        title={t("auto.budgetField.title")}
        placeholder={String(effective)}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") setDraft(settled);
        }}
        style={{ width: 84, padding: "4px 8px", fontSize: 13 }}
      />
      {inheritable && (
        <button
          className={`seg-btn${value === null ? " active" : ""}`}
          aria-pressed={value === null}
          disabled={disabled}
          title={t("auto.budgetField.inheritTitle")}
          onClick={() => {
            setDraft("");
            if (value !== null) onCommit(null);
          }}
        >
          {t("auto.tri.inherit")}
        </button>
      )}
    </div>
  );
}

export const AutonomyPanel = forwardRef<
  AutonomyHandle,
  {
    agent: string;
    onPhaseRan: () => void;
    onStatus?: (s: CognitionStatusResponse) => void;
    onStatusError?: () => void;
  }
>(function AutonomyPanel({ agent, onPhaseRan, onStatus, onStatusError }, ref) {
  const toast = useToast();
  const t = useT(STR);
  // A manual phase run is a Cortex job: it keeps going when the page is left,
  // and its row follows it (View) instead of starting it again.
  const jobs = useCortexJobsOptional();
  const [status, setStatus] = useState<CognitionStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runningPhase, setRunningPhase] = useState<string | null>(null);
  // The three parts of the settings card are uniform disclosures, so any of
  // them can be folded away — but "How it runs" is what you opened the card
  // for, so it starts expanded and the eleven-row phase grid does not. Both
  // persist, so a manual expand/collapse survives left-nav tab switches.
  const [howOpen, setHowOpen] = usePersistentState("cortex.howItRuns.open", true);
  const [open, setOpen] = usePersistentState("cortex.autonomy.open", false);
  const [logPhase, setLogPhase] = useState<CognitionPhase | null>(null);
  const [confirmRun, setConfirmRun] = useState<CognitionPhase | null>(null);

  // ── Optimistic overlays ───────────────────────────────────────────────────
  // Keys the user has moved but the server hasn't confirmed. Rendered over the
  // server snapshot, dropped on success (the response carries fresh values) and
  // on failure (falling back to what the server still says).
  const [pendingAgent, setPendingAgent] = useState<Record<string, OverrideValue>>({});
  const [pendingGlobal, setPendingGlobal] = useState<Record<string, GlobalValue>>({});
  const timers = useRef<Record<string, number>>({});

  // Hold the parent callbacks in refs so `load`'s identity depends only on
  // [agent, t]. Parents pass inline callbacks; if those were in load's deps,
  // every parent re-render would mint a new `load`, which the reset effect keys
  // on — nulling status (→ spinner) and refetching on every render. That
  // refetch calls onStatus → re-renders the parent → new callbacks → new load …
  // an endless flicker loop. Refs cut that link.
  const onStatusRef = useRef(onStatus);
  const onStatusErrorRef = useRef(onStatusError);
  onStatusRef.current = onStatus;
  onStatusErrorRef.current = onStatusError;

  const load = useCallback(() => {
    setError(null);
    api
      .get<CognitionStatusResponse>(`/api/cognition/status?agent=${encodeURIComponent(agent)}`)
      .then((s) => {
        setStatus(s);
        onStatusRef.current?.(s);
      })
      .catch((e) => {
        setError(errMessage(e, t("auto.toast.statusUnavailable")));
        // Let the parent drop any stale "running" snapshot so it can't freeze
        // the Optimize buttons while the status is unreachable.
        onStatusErrorRef.current?.();
      });
  }, [agent, t]);

  useImperativeHandle(ref, () => ({ reload: load }), [load]);

  useEffect(() => {
    setStatus(null);
    setPendingAgent({});
    setPendingGlobal({});
    load();
  }, [load]);

  // Clear any queued write when the agent changes — a debounced patch must never
  // land on the agent the user switched TO.
  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const id of Object.values(pending)) window.clearTimeout(id);
      timers.current = {};
    };
  }, [agent]);

  const phaseRunning = (status?.running || []).length > 0;

  // Refresh only when something can actually have changed. The panel used to
  // poll every 5s unconditionally — a request that costs a config read, an
  // audit-log scan and the budget files, forever, per open tab. A running phase
  // is the one thing that progresses without us, so poll fast only then; poll
  // slowly otherwise, and always catch up when the tab regains focus.
  useEffect(() => {
    const period = phaseRunning ? 5000 : 30000;
    const id = window.setInterval(() => {
      if (!document.hidden) load();
    }, period);
    const onVisible = () => {
      if (!document.hidden) load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load, phaseRunning]);

  // Another tab (or agentfather) changed this agent's cognition — take the news
  // rather than waiting out the poll interval.
  useEffect(() => {
    return subscribeStream((frame) => {
      const ev = frame as { type?: string; data?: { agent?: string } };
      if (ev.type === "cognition_updated" && ev.data?.agent === agent) load();
    });
  }, [agent, load]);

  // ── Write paths ───────────────────────────────────────────────────────────
  // Both are optimistic + debounced per key. `setting` is the human label used
  // in the revert toast; `key` identifies the knob for coalescing.

  const queue = useCallback(
    (key: string, run: () => Promise<void>) => {
      window.clearTimeout(timers.current[key]);
      timers.current[key] = window.setTimeout(() => {
        delete timers.current[key];
        void run();
      }, WRITE_DEBOUNCE_MS);
    },
    [],
  );

  // The value the server last confirmed for a field — what an optimistic knob
  // falls back to when its write fails. Read through a ref so it reflects the
  // snapshot at failure time, not at click time.
  const statusRef = useRef<CognitionStatusResponse | null>(null);
  statusRef.current = status;
  const serverOverride = useCallback((field: string): OverrideValue => {
    const o = statusRef.current?.overrides;
    if (!o) return null;
    if (field in o) return (o as unknown as Record<string, OverrideValue>)[field] ?? null;
    return o.features?.[field] ?? null;
  }, []);

  // Per-agent override. `field` is a CognitionConfig key; null clears it.
  const setAgentOverride = useCallback(
    (field: string, value: OverrideValue, setting: string) => {
      setPendingAgent((p) => ({ ...p, [field]: value }));
      queue(`agent:${field}`, async () => {
        try {
          const res = await api.patch<{
            overrides?: CognitionStatusResponse["overrides"];
            resolved?: CognitionStatusResponse["resolved"];
          }>(`/api/agents/${encodeURIComponent(agent)}/cognition`, { [field]: value });
          // The response carries the agent's fresh overrides + resolved values,
          // so a settled knob needs no follow-up read.
          setStatus((s) =>
            s && res.overrides && res.resolved
              ? { ...s, overrides: res.overrides, resolved: res.resolved }
              : s,
          );
          setPendingAgent((p) => {
            const { [field]: _drop, ...rest } = p;
            return rest;
          });
          if (field === "auto_approve" && value === true) {
            // The global toggle sweeps the queue inline; a per-agent force-on
            // should clear it too, or "fully autonomous" leaves items waiting.
            try {
              const swept = await api.post<{ swept?: number }>(
                "/api/cognition/auto-approve/sweep",
                { agent },
              );
              if (swept?.swept) {
                toast.push(t("auto.fa.toast.onSwept", { count: String(swept.swept) }));
                onPhaseRan();
              }
            } catch {
              /* best-effort; the override itself saved */
            }
          }
        } catch (e) {
          // Put the knob back to whatever the server still believes, and say so
          // — an optimistic control that silently reverts reads as a UI glitch.
          setPendingAgent((p) => {
            const { [field]: _drop, ...rest } = p;
            return rest;
          });
          const server = serverOverride(field);
          toast.push(
            `${t("auto.toast.reverted", {
              setting,
              // Numbers and preset names report themselves; only a tri-state
              // switch reads as On/Off. Without this a reverted step budget
              // announced itself as "restored to On".
              state:
                server === null || server === undefined
                  ? t("auto.tri.inherit")
                  : typeof server === "boolean"
                    ? server
                      ? t("auto.tri.on")
                      : t("auto.tri.off")
                    : String(server),
            })} ${errMessage(e, t("auto.toast.overrideFailed"))}`,
            "error",
          );
        }
      });
    },
    [agent, onPhaseRan, queue, serverOverride, t, toast],
  );

  // Home-wide default.
  const setGlobal = useCallback(
    (key: string, value: GlobalValue, request: () => Promise<unknown>) => {
      setPendingGlobal((p) => ({ ...p, [key]: value }));
      queue(`global:${key}`, async () => {
        try {
          await request();
          load();
        } catch (e) {
          toast.push(errMessage(e, t("auto.toast.updateFailed")), "error");
        } finally {
          setPendingGlobal((p) => {
            const { [key]: _drop, ...rest } = p;
            return rest;
          });
        }
      });
    },
    [load, queue, t, toast],
  );

  // Starts the phase as a background job and returns at once; the page's
  // jobs store reports how it ended (a toast, and a refresh of this panel).
  async function runPhase(p: CognitionPhase) {
    setRunningPhase(p.id);
    try {
      const { job } = await startCortexJob("/api/cognition/run-phase", { agent, phase: p.id });
      jobs?.track(job);
      toast.push(t("auto.toast.running", { phase: p.label }));
      load();
      onPhaseRan();
    } catch (e) {
      toast.push(t("auto.toast.phaseFailed", { phase: p.label, error: errMessage(e, t("auto.runError")) }), "error");
    } finally {
      setRunningPhase(null);
    }
  }

  // ── Derived view state (must be before any early return) ──────────────────
  const ov = status?.overrides;
  const def = status?.defaults;
  const res = status?.resolved;

  // Server values with the optimistic overlay applied.
  const agentVal = (field: string, server: OverrideValue | undefined): OverrideValue =>
    field in pendingAgent ? pendingAgent[field] : (server ?? null);
  const globalVal = (key: string, server: GlobalValue | undefined): GlobalValue =>
    key in pendingGlobal ? pendingGlobal[key] : (server ?? false);

  // OVERRIDE_FIELDS / countOverrides live in helpers.ts: the settings header
  // renders the same count as a badge, and a second copy of the field list is
  // how a knob ends up counted in one place and unresettable in the other.
  const overrideCount = useMemo(() => countOverrides(ov), [ov]);

  async function resetOverrides() {
    if (!ov) return;
    const cleared: Record<string, null> = {};
    for (const k of OVERRIDE_FIELDS) {
      if (ov[k] !== null && ov[k] !== undefined) cleared[k] = null;
    }
    for (const [f, v] of Object.entries(ov.features || {})) {
      if (v !== null && v !== undefined) cleared[f] = null;
    }
    const count = Object.keys(cleared).length;
    if (!count) return;
    try {
      await api.patch(`/api/agents/${encodeURIComponent(agent)}/cognition`, cleared);
      toast.push(t("auto.overrides.resetDone", { count: String(count), agent }));
      load();
    } catch (e) {
      toast.push(errMessage(e, t("auto.toast.resetFailed", { agent })), "error");
    }
  }

  if (error) {
    return (
      <div className="cortex-subsection muted" style={{ fontSize: 13 }}>
        {t("auto.unavailable", { error })}
        <button className="btn" style={{ marginLeft: 8 }} onClick={load}>
          {t("auto.retry")}
        </button>
      </div>
    );
  }
  if (!status || !ov || !def || !res)
    return <div className="spinner" style={{ margin: "16px auto" }} />;

  // The deep-run meter itself lives in the settings card's header now; these
  // two are still needed for the over-budget confirmation on "Run now".
  const cap = status.deep_run_cap || 0;
  const used = status.deep_runs_today || 0;
  const over = cap > 0 && used >= cap;
  const runningSet = new Set(status.running || []);

  // The resolved engine state for this agent, previewed while a click is in
  // flight. Everything else in the panel dims when the engine is off — but the
  // engine row itself never does, or you could not turn it back on.
  const engineOn = previewResolve(
    agentVal("enabled", ov.enabled) as Tri,
    globalVal("enabled", def.enabled) as boolean,
  );

  const onOff = (v: boolean) => (v ? t("auto.tri.on") : t("auto.tri.off"));

  // ── Controls ──────────────────────────────────────────────────────────────

  // Binary On/Off for the All-agents column.
  const Binary = ({
    value,
    onChange,
    title,
  }: {
    value: boolean;
    onChange: (v: boolean) => void;
    title?: string;
  }) => (
    <div className="seg-group" title={title}>
      {[true, false].map((v) => (
        <button
          key={String(v)}
          className={`seg-btn${value === v ? " active" : ""}`}
          aria-pressed={value === v}
          onClick={() => onChange(v)}
        >
          {onOff(v)}
        </button>
      ))}
    </div>
  );

  // Inherit / On / Off for the agent column. Never disabled by the global
  // column's state — escaping a fleet-wide off is exactly what it is for.
  const TriControl = ({
    value,
    onChange,
    setting,
  }: {
    value: Tri;
    onChange: (v: Tri) => void;
    setting: string;
  }) => {
    const opts: { key: string; label: string; val: Tri; title: string }[] = [
      { key: "inherit", label: t("auto.tri.inherit"), val: null, title: t("auto.tri.title.inherit") },
      { key: "on", label: t("auto.tri.on"), val: true, title: t("auto.tri.title.on", { agent }) },
      { key: "off", label: t("auto.tri.off"), val: false, title: t("auto.tri.title.off", { agent }) },
    ];
    const cur = value === null || value === undefined ? "inherit" : value ? "on" : "off";
    return (
      <div className="seg-group" aria-label={setting}>
        {opts.map((o) => (
          <button
            key={o.key}
            className={`seg-btn${cur === o.key ? " active" : ""}`}
            aria-pressed={cur === o.key}
            title={o.title}
            onClick={() => onChange(o.val)}
          >
            {o.label}
          </button>
        ))}
      </div>
    );
  };

  // One row of the two-tier table.
  const Row = ({
    label,
    help,
    global: globalControl,
    agent: agentControl,
    effective,
    overridden,
    dim,
  }: {
    label: string;
    help: string;
    global: React.ReactNode;
    agent: React.ReactNode;
    effective: string;
    overridden: boolean;
    dim?: boolean;
  }) => (
    <div className="cortex-tier-row" style={dim ? { opacity: 0.55 } : undefined}>
      <div>
        <div className="cortex-tier-label">
          {label}
          {overridden ? (
            <span className="badge" style={{ marginLeft: 6, fontSize: 10 }}>
              {t("auto.tri.override")}
            </span>
          ) : null}
        </div>
        <div className="muted cortex-tier-help">{help}</div>
      </div>
      <div>
        <span className="cortex-tier-inline-label">{t("auto.col.global")}</span>
        {globalControl}
      </div>
      <div className="cortex-tier-agent">
        <span className="cortex-tier-inline-label">{t("auto.col.agent", { agent })}</span>
        {agentControl}
        <span className="muted cortex-tier-effective" title={t("auto.effective.title", { agent })}>
          {t("auto.effective", { state: effective })}
        </span>
      </div>
    </div>
  );

  const intensities = status.intensities || [];
  const agentIntensity = agentVal("intensity", ov.intensity) as string | null;
  const globalIntensity = globalVal("intensity", def.intensity) as string;

  // ── Background-run budget (step budget + time limit) ──────────────────────
  // Both rows are the same shape, so build them from one spec instead of
  // copy-pasting a 40-line Row twice — the copy is where the second row would
  // quietly keep the first row's bounds or write to the first row's key.
  const bounds = status.worker_budget_bounds;
  const budgetRows = !bounds
    ? null
    : (
        [
          {
            field: "worker_max_iterations" as const,
            label: t("auto.row.steps"),
            help: t("auto.row.steps.help", {
              min: String(bounds.max_iterations_min),
              max: String(bounds.max_iterations_max),
            }),
            min: bounds.max_iterations_min,
            max: bounds.max_iterations_max,
          },
          {
            field: "worker_timeout_seconds" as const,
            label: t("auto.row.timeout"),
            help: t("auto.row.timeout.help", {
              min: String(bounds.timeout_seconds_min),
              max: String(bounds.timeout_seconds_max),
            }),
            min: bounds.timeout_seconds_min,
            max: bounds.timeout_seconds_max,
          },
        ] as const
      ).map((spec) => {
        const agentValue = agentVal(spec.field, ov[spec.field]) as number | null;
        const globalValue = globalVal(spec.field, def[spec.field]) as number;
        // Resolved comes from the server (the one implementation of the rule);
        // while a click is in flight, preview it the same way the tri-state
        // knobs do rather than showing a stale number.
        const effective = agentValue ?? globalValue;
        return (
          <Row
            key={spec.field}
            label={spec.label}
            help={spec.help}
            dim={!engineOn}
            overridden={agentValue !== null && agentValue !== undefined}
            effective={String(effective)}
            global={
              <BudgetField
                value={globalValue}
                effective={globalValue}
                min={spec.min}
                max={spec.max}
                label={`${t("auto.col.global")} — ${spec.label}`}
                inheritable={false}
                onCommit={(v) => {
                  if (v === null) return; // not offered in the global column
                  setGlobal(spec.field, v, () =>
                    api.post("/api/cognition/autonomy", { [spec.field]: v }),
                  );
                }}
              />
            }
            agent={
              <BudgetField
                value={agentValue}
                effective={globalValue}
                min={spec.min}
                max={spec.max}
                label={`${agent} — ${spec.label}`}
                inheritable
                onCommit={(v) => setAgentOverride(spec.field, v, spec.label)}
              />
            }
          />
        );
      });

  return (
    // Two sub-sections of the one Cortex settings card, not a card of their
    // own: the page used to carry two peer cards that both read as "engine
    // settings" — this one and "Customize your Cortex Engine" — with no way to
    // tell from their headings which knob lived where. The deep-run meter moved
    // up to the card's header, where it stays readable while this is collapsed.
    <>
      <section className="cortex-subsection">
      <details open={howOpen}>
        <summary
          className="phase-disclosure cortex-subsection-toggle"
          onClick={(e) => {
            e.preventDefault();
            setHowOpen((o) => !o);
          }}
        >
          <ChevronRight size={15} strokeWidth={2.25} aria-hidden />
          {t("auto.sub.howItRuns")}
        </summary>
        <div className="cortex-subsection-body">
      <p className="muted" style={{ fontSize: 12.5, margin: "10px 0 14px" }}>
        {t("auto.intro", { agent })}
      </p>

      <div className="cortex-tier-head">
        <div>{t("auto.col.setting")}</div>
        <div title={t("auto.col.global.title")}>{t("auto.col.global")}</div>
        <div title={t("auto.col.agent.title", { agent })}>{t("auto.col.agent", { agent })}</div>
      </div>

      <Row
        label={t("auto.row.engine")}
        help={t("auto.row.engine.help")}
        overridden={ov.enabled !== null && ov.enabled !== undefined}
        effective={onOff(engineOn)}
        global={
          <Binary
            value={globalVal("enabled", def.enabled) as boolean}
            onChange={(v) =>
              setGlobal("enabled", v, () => api.post("/api/cognition/autonomy", { master: v }))
            }
          />
        }
        agent={
          <TriControl
            value={agentVal("enabled", ov.enabled) as Tri}
            setting={t("auto.row.engine")}
            onChange={(v) => setAgentOverride("enabled", v, t("auto.row.engine"))}
          />
        }
      />

      <Row
        label={t("auto.row.autonomous")}
        help={t("auto.row.autonomous.help")}
        dim={!engineOn}
        overridden={ov.autonomous !== null && ov.autonomous !== undefined}
        effective={onOff(
          engineOn &&
            previewResolve(
              agentVal("autonomous", ov.autonomous) as Tri,
              globalVal("autonomous", def.autonomous) as boolean,
            ),
        )}
        global={
          <Binary
            value={globalVal("autonomous", def.autonomous) as boolean}
            onChange={(v) =>
              setGlobal("autonomous", v, () => api.post("/api/cognition/autonomy", { enabled: v }))
            }
          />
        }
        agent={
          <TriControl
            value={agentVal("autonomous", ov.autonomous) as Tri}
            setting={t("auto.row.autonomous")}
            onChange={(v) => setAgentOverride("autonomous", v, t("auto.row.autonomous"))}
          />
        }
      />

      <Row
        label={t("auto.row.autoApprove")}
        help={t("auto.row.autoApprove.help")}
        dim={!engineOn}
        overridden={ov.auto_approve !== null && ov.auto_approve !== undefined}
        effective={onOff(
          engineOn &&
            previewResolve(
              agentVal("auto_approve", ov.auto_approve) as Tri,
              globalVal("auto_approve", def.auto_approve) as boolean,
            ),
        )}
        global={
          <Binary
            value={globalVal("auto_approve", def.auto_approve) as boolean}
            onChange={(v) =>
              setGlobal("auto_approve", v, () =>
                api.post("/api/cognition/auto-approve", { enabled: v, agent }),
              )
            }
          />
        }
        agent={
          <TriControl
            value={agentVal("auto_approve", ov.auto_approve) as Tri}
            setting={t("auto.row.autoApprove")}
            onChange={(v) => setAgentOverride("auto_approve", v, t("auto.row.autoApprove"))}
          />
        }
      />

      <Row
        label={t("auto.row.cadence")}
        help={t("auto.row.cadence.help")}
        dim={!engineOn}
        overridden={!!agentIntensity}
        effective={agentIntensity || globalIntensity}
        global={
          <div className="seg-group" title={t("auto.cadence.title")}>
            {intensities.map((v) => (
              <button
                key={v}
                className={`seg-btn${globalIntensity === v ? " active" : ""}`}
                aria-pressed={globalIntensity === v}
                style={{ textTransform: "capitalize" }}
                onClick={() =>
                  setGlobal("intensity", v, () =>
                    api.post("/api/cognition/autonomy", { intensity: v }),
                  )
                }
              >
                {v}
              </button>
            ))}
          </div>
        }
        agent={
          <div className="seg-group" aria-label={t("auto.row.cadence")}>
            {["inherit", ...intensities].map((v) => {
              const isInherit = v === "inherit";
              const active = isInherit ? !agentIntensity : agentIntensity === v;
              return (
                <button
                  key={v}
                  className={`seg-btn${active ? " active" : ""}`}
                  aria-pressed={active}
                  style={isInherit ? undefined : { textTransform: "capitalize" }}
                  title={isInherit ? t("auto.tri.title.inherit") : t("auto.cadence.title")}
                  onClick={() =>
                    setAgentOverride("intensity", isInherit ? null : v, t("auto.row.cadence"))
                  }
                >
                  {isInherit ? t("auto.tri.inherit") : v}
                </button>
              );
            })}
          </div>
        }
      />

      {/* The two halves of the background-run budget. They live together and
          directly under Cadence because Cadence sets how MANY runs happen and
          these two bound what ONE run may spend — and because raising the step
          budget without the time limit just moves the failure, not the ceiling. */}
      {budgetRows}

      <div
        className="row"
        style={{ justifyContent: "space-between", gap: 10, flexWrap: "wrap", marginTop: 12 }}
      >
        <span className="muted" style={{ fontSize: 12 }}>
          {overrideCount === 0
            ? t("auto.overrides.none", { agent })
            : overrideCount === 1
              ? t("auto.overrides.count", { count: "1", agent })
              : t("auto.overrides.count.plural", { count: String(overrideCount), agent })}
        </span>
        {overrideCount > 0 ? (
          <button
            className="btn"
            style={{ padding: "3px 10px", fontSize: 12 }}
            title={t("auto.overrides.reset.title", { agent })}
            onClick={() => void resetOverrides()}
          >
            {t("auto.overrides.reset")}
          </button>
        ) : null}
      </div>
        </div>
      </details>
      </section>

      {/* Peer sub-section, not a footnote of the table above: eleven phases
          each with two tiers and two actions is its own topic, and it opens on
          demand so the settings card lands on the settings. */}
      <section className="cortex-subsection">
      <details open={open}>
        <summary
          className="phase-disclosure cortex-subsection-toggle"
          onClick={(e) => {
            e.preventDefault();
            setOpen((o) => !o);
          }}
        >
          <ChevronRight size={15} strokeWidth={2.25} aria-hidden />
          {t("auto.phases", { count: String(status.phases.length) })}
        </summary>
        {open ? (
          <div style={{ marginTop: 6, opacity: engineOn ? 1 : 0.6 }}>
            {status.phases.map((p) => {
              const lastColor = statusColor(p.last_status || "");
              const phaseJob = jobs?.liveFor(agent, "phase", p.id);
              const running = runningSet.has(p.id) || !!phaseJob;
              const override = agentVal(p.feature, ov.features?.[p.feature]) as Tri;
              const globalFeature = globalVal(
                `feature:${p.feature}`,
                def.features?.[p.feature] ?? true,
              ) as boolean;
              const effective = engineOn && previewResolve(override, globalFeature);
              const sched = effective
                ? t("auto.phase.next", { cadence: cronHuman(p.cadence), when: relFuture(p.next_run) })
                : t("auto.phase.paused");
              return (
                <div key={p.id} style={{ borderTop: "1px solid var(--border)" }}>
                  {/* Four blocks, not one flat flex row: below the tier
                      breakpoint the row restacks into name / schedule /
                      controls, and a flat row can only wrap wherever it runs
                      out — which is what collapsed every phase name to a
                      single letter on a phone. */}
                  <div className="cortex-phase-row">
                    <div className="cortex-phase-title">
                      {running ? (
                        <span
                          className="spinner"
                          title={t("auto.phase.running")}
                          style={{ width: 10, height: 10, flexShrink: 0 }}
                        />
                      ) : (
                        <span
                          title={effective ? t("auto.phase.enabled") : t("auto.phase.disabled")}
                          style={{
                            width: 8,
                            height: 8,
                            borderRadius: "50%",
                            flexShrink: 0,
                            background: effective ? "var(--success)" : "var(--text-3)",
                          }}
                        />
                      )}
                      <span className="cortex-phase-name">
                        {p.label}
                        {override !== null && override !== undefined ? (
                          <span className="badge" style={{ marginLeft: 8, fontSize: 10 }}>
                            {t("auto.tri.override")}
                          </span>
                        ) : null}
                      </span>
                    </div>
                    <div className="cortex-phase-when">
                      <span className="muted cortex-phase-sched">{sched}</span>
                      <span className="cortex-phase-last">
                        {running ? (
                          <span style={{ color: "var(--accent)", fontWeight: 600 }}>
                            {t("auto.phase.running")}
                          </span>
                        ) : p.last_run ? (
                          <span style={{ color: lastColor }} title={p.last_summary || ""}>
                            {relPast(p.last_run)}
                            {p.last_status ? ` · ${p.last_status}` : ""}
                          </span>
                        ) : (
                          <span className="muted">{t("auto.phase.never")}</span>
                        )}
                      </span>
                    </div>
                    <div className="cortex-phase-controls">
                      {/* All agents */}
                      <div className="cortex-phase-tier">
                        <span className="cortex-tier-inline-label">{t("auto.col.global")}</span>
                        <Binary
                          value={globalFeature}
                          title={t("auto.phase.toggleTitle", {
                            state: globalFeature ? t("auto.phase.enabled") : t("auto.phase.disabled"),
                            feature: p.feature,
                          })}
                          onChange={(v) =>
                            setGlobal(`feature:${p.feature}`, v, () =>
                              api.post(`/api/cognition/features/${encodeURIComponent(p.feature)}`, {
                                enabled: v,
                              }),
                            )
                          }
                        />
                      </div>
                      {/* This agent */}
                      <div className="cortex-phase-tier">
                        <span className="cortex-tier-inline-label">
                          {t("auto.col.agent", { agent })}
                        </span>
                        <TriControl
                          value={override}
                          setting={p.label}
                          onChange={(v) => setAgentOverride(p.feature, v, p.label)}
                        />
                      </div>
                      <div className="cortex-phase-actions">
                        <button
                          className="btn"
                          onClick={() => setLogPhase(p)}
                          title={t("auto.phase.log.title")}
                        >
                          {t("auto.phase.log")}
                        </button>
                        {phaseJob && jobs ? (
                          <button
                            className="btn"
                            data-testid="cortex-phase-view-run"
                            onClick={() => jobs.openJob({ id: phaseJob.id, agent })}
                            title={t("auto.phase.viewRun")}
                            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                          >
                            <span className="spinner" style={{ width: 10, height: 10, borderWidth: 1.5 }} />
                            {t("jobs.status.running")}
                          </button>
                        ) : (
                          <button
                            className="btn"
                            disabled={runningPhase !== null || running}
                            onClick={() => (over ? setConfirmRun(p) : void runPhase(p))}
                            title={t("auto.phase.run.title")}
                          >
                            {runningPhase === p.id || running ? "…" : t("auto.phase.run")}
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                  {p.last_status === "error" && (p.last_summary || p.last_code) ? (
                    <div style={{ fontSize: 11.5, color: "var(--danger)", padding: "2px 0 4px 18px" }}>
                      {p.last_summary || t("auto.phase.failed")}
                      {p.last_code && p.last_code !== "vault_locked" ? ` (${p.last_code})` : ""}
                      {p.last_code === "vault_locked" ? (
                        <Link to="/vault" className="btn primary" style={{ marginLeft: 8, padding: "2px 9px", fontSize: 11 }}>
                          {t("auto.unlockVault")}
                        </Link>
                      ) : null}
                      {p.last_remediation && p.last_code !== "vault_locked" ? (
                        <ErrorRemediation remediation={p.last_remediation} />
                      ) : null}
                    </div>
                  ) : p.last_status === "skipped" && p.last_summary ? (
                    <div className="muted" style={{ fontSize: 11, padding: "1px 0 3px 18px" }}>
                      {p.last_summary}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : null}
      </details>
      </section>

      {logPhase ? (
        <ActivityDrawer
          agent={agent}
          phaseFilter={logPhase.id}
          titleOverride={t("auto.phase.log.drawerTitle", { phase: logPhase.label })}
          onClose={() => setLogPhase(null)}
        />
      ) : null}

      {confirmRun ? (
        <ConfirmModal
          title={t("auto.budget.confirm.title")}
          confirmLabel={t("auto.phase.run")}
          message={t("auto.budget.confirm.message", {
            used: String(used),
            cap: String(cap),
            phase: confirmRun.label,
          })}
          onCancel={() => setConfirmRun(null)}
          onConfirm={() => {
            const p = confirmRun;
            setConfirmRun(null);
            void runPhase(p);
          }}
        />
      ) : null}
    </>
  );
});
