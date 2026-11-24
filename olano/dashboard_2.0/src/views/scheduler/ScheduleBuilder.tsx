// ScheduleBuilder — a no-cron-knowledge-required schedule picker.
//
// Replaces the raw "cron string + ISO datetime + timezone text" inputs with a
// friendly builder: choose a frequency (every N minutes / hours / days, weekly,
// monthly, or a one-time run on a calendar), pick a time with no seconds, and
// pick a timezone from a searchable list (defaulting to the viewer's own zone).
// It emits the backend's storage format ({schedule, run_at, timezone}) via
// onChange so the create/edit modals stay thin. All cron/translation logic lives
// in ./recurrence so this file is purely the UI.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Globe, Search } from "lucide-react";
import { useT } from "../../core/i18n";
import { AnchoredPopover } from "../../ui/AnchoredPopover";
import { Field } from "./Modal";
import { STR } from "./strings";
import {
  CADENCE_WARN_BELOW_MINUTES,
  type Freq,
  type Recurrence,
  clamp,
  describeRecurrence,
  guessTimeZone,
  isCadenceTooFrequent,
  isRecurrenceValid,
  listTimeZones,
  scheduleToSpec,
  specToSchedule,
  todayLocal,
  tzOffsetLabel,
  weekdayName,
} from "./recurrence";

const pad = (n: number) => String(n).padStart(2, "0");
// Display order for the weekday toggles: Sunday-first (matches the Calendar grid)
// as picker indexes (0=Mon … 6=Sun); recurrence.ts writes them as names.
const WEEKDAY_ORDER = [6, 0, 1, 2, 3, 4, 5];

// A schedule the backend can actually run: structurally complete AND, for a
// one-time run, dated in the future (a past run_at would never fire).
function schedulable(r: Recurrence): boolean {
  if (!isRecurrenceValid(r)) return false;
  if (r.freq === "once" && r.date && r.date < todayLocal()) return false;
  return true;
}

export function ScheduleBuilder({
  schedule,
  runAt,
  timezone,
  onChange,
}: {
  schedule: string;
  runAt: string;
  timezone: string;
  onChange: (next: { schedule: string; run_at: string; timezone: string }) => void;
}) {
  const t = useT(STR);
  const [spec, setSpec] = useState<Recurrence>(() => scheduleToSpec(schedule, runAt));

  // Single emission path: an incomplete spec (weekly with no day, a malformed
  // custom cron, a one-time task with no date) emits an EMPTY schedule so the
  // create/edit modal's "schedule required" guard blocks submit before the
  // backend would. The summary panel below shows the warning inline.
  const emit = useCallback(
    (next: Recurrence, tz: string) => {
      const s = schedulable(next) ? specToSchedule(next) : { schedule: "", run_at: "" };
      onChange({ schedule: s.schedule, run_at: s.run_at, timezone: tz || guessTimeZone() });
    },
    [onChange],
  );

  // Push the normalized schedule up once on mount so an untouched form still
  // submits a valid value (the daily default for create; padded run_at for a
  // calendar-prefilled one-time task; the raw cron for an unrecognized edit).
  useEffect(() => {
    emit(spec, timezone);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const update = useCallback(
    (patch: Partial<Recurrence>) => {
      const next = { ...spec, ...patch };
      setSpec(next);
      emit(next, timezone);
    },
    [spec, timezone, emit],
  );

  const setTz = useCallback((tz: string) => emit(spec, tz), [spec, emit]);

  // Remember the last recurring frequency so toggling One-time ⇄ Recurring
  // restores the user's previous choice instead of snapping back to a default.
  const lastRecurring = useRef<Freq>(spec.freq === "once" ? "daily" : spec.freq);

  // Switching to "Custom" seeds the cron box from whatever the current friendly
  // mode produces (or a daily default), so power users start from a working
  // expression instead of a blank field + "incomplete" warning.
  const setFreq = (freq: Freq) => {
    if (freq !== "once") lastRecurring.current = freq;
    if (freq === "advanced" && !spec.cron.trim()) {
      update({ freq, cron: specToSchedule(spec).schedule || "0 9 * * *" });
    } else {
      update({ freq });
    }
  };

  // The primary choice: a repeating schedule or a single future run.
  const setKind = (k: "recurring" | "once") => setFreq(k === "once" ? "once" : lastRecurring.current);

  const timeValue = `${pad(spec.hour)}:${pad(spec.minute)}`;
  const setTime = (v: string) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(v);
    if (m) update({ hour: clamp(+m[1], 0, 23), minute: clamp(+m[2], 0, 59) });
  };

  const valid = schedulable(spec);
  const built = specToSchedule(spec);
  const pastOnce = spec.freq === "once" && !!spec.date && spec.date < todayLocal();

  // Recurring frequencies only — one-time lives under the top-level toggle.
  const RECUR_FREQS: { id: Freq; label: string }[] = [
    { id: "minutes", label: t("sb.freq.minutes") },
    { id: "hourly", label: t("sb.freq.hourly") },
    { id: "daily", label: t("sb.freq.daily") },
    { id: "weekly", label: t("sb.freq.weekly") },
    { id: "monthly", label: t("sb.freq.monthly") },
    { id: "advanced", label: t("sb.freq.advanced") },
  ];
  const kind: "recurring" | "once" = spec.freq === "once" ? "once" : "recurring";

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {/* Primary choice: a repeating schedule or a single future run. */}
      <div className="field" style={{ margin: 0 }}>
        <label>{t("sb.kind")}</label>
        <div className="seg" style={{ display: "flex" }}>
          <button
            type="button"
            className={`seg-btn${kind === "recurring" ? " active" : ""}`}
            style={{ flex: 1 }}
            onClick={() => setKind("recurring")}
          >
            {t("type.recurring")}
          </button>
          <button
            type="button"
            className={`seg-btn${kind === "once" ? " active" : ""}`}
            style={{ flex: 1 }}
            onClick={() => setKind("once")}
          >
            {t("type.oneTime")}
          </button>
        </div>
      </div>

      {kind === "recurring" && (
        <div className="field" style={{ margin: 0 }}>
          <label>{t("sb.repeat")}</label>
          <div className="seg" style={{ display: "flex", flexWrap: "wrap" }}>
            {RECUR_FREQS.map((f) => (
              <button
                key={f.id}
                type="button"
                className={`seg-btn${spec.freq === f.id ? " active" : ""}`}
                onClick={() => setFreq(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* per-mode controls */}
      {spec.freq === "minutes" && (
        <Field label={t("sb.minutes.label")}>
          <Stepper value={spec.everyMinutes} min={1} max={59} onChange={(v) => update({ everyMinutes: v })} suffix={t("sb.unit.minutes")} />
          <Presets values={[5, 10, 15, 30]} active={spec.everyMinutes} onPick={(v) => update({ everyMinutes: v })} />
        </Field>
      )}

      {spec.freq === "hourly" && (
        <div style={{ display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr" }}>
          <Field label={t("sb.hourly.label")}>
            <Stepper value={spec.everyHours} min={1} max={23} onChange={(v) => update({ everyHours: v })} suffix={t("sb.unit.hours")} />
            <Presets values={[1, 2, 3, 6, 12]} active={spec.everyHours} onPick={(v) => update({ everyHours: v })} />
          </Field>
          <Field label={t("sb.hourly.atMinute")} hint={t("sb.hourly.atMinuteHint")}>
            <Stepper value={spec.atMinute} min={0} max={59} onChange={(v) => update({ atMinute: v })} prefix=":" />
          </Field>
        </div>
      )}

      {spec.freq === "daily" && (
        <div style={{ display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr" }}>
          <Field label={t("sb.daily.label")}>
            <Stepper value={spec.everyDays} min={1} max={31} onChange={(v) => update({ everyDays: v })} suffix={t("sb.unit.days")} />
            <Presets values={[1, 2, 3, 7, 14]} active={spec.everyDays} onPick={(v) => update({ everyDays: v })} />
          </Field>
          <Field label={t("sb.atTime")}>
            <input className="input" type="time" value={timeValue} onChange={(e) => setTime(e.target.value)} />
          </Field>
        </div>
      )}

      {spec.freq === "weekly" && (
        <>
          <Field label={t("sb.weekly.onDays")}>
            <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
              {WEEKDAY_ORDER.map((d) => {
                const on = spec.weekdays.includes(d);
                return (
                  <button
                    key={d}
                    type="button"
                    className="wd-btn"
                    aria-pressed={on}
                    style={
                      on
                        ? { background: "var(--accent-soft)", color: "var(--accent)", borderColor: "var(--accent)" }
                        : undefined
                    }
                    onClick={() =>
                      update({
                        weekdays: on ? spec.weekdays.filter((x) => x !== d) : [...spec.weekdays, d],
                      })
                    }
                  >
                    {weekdayName(d)}
                  </button>
                );
              })}
            </div>
          </Field>
          <Field label={t("sb.atTime")}>
            <input className="input" type="time" value={timeValue} onChange={(e) => setTime(e.target.value)} style={{ maxWidth: 160 }} />
          </Field>
        </>
      )}

      {spec.freq === "monthly" && (
        <div style={{ display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr" }}>
          <Field label={t("sb.monthly.onDay")} hint={t("sb.monthly.onDayHint")}>
            <Stepper value={spec.dayOfMonth} min={1} max={31} onChange={(v) => update({ dayOfMonth: v })} />
          </Field>
          <Field label={t("sb.atTime")}>
            <input className="input" type="time" value={timeValue} onChange={(e) => setTime(e.target.value)} />
          </Field>
        </div>
      )}

      {spec.freq === "once" && (
        <div style={{ display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr" }}>
          <Field label={t("sb.once.date")} hint={t("sb.once.dateHint")}>
            <input
              className="input"
              type="date"
              min={todayLocal()}
              value={spec.date}
              onChange={(e) => update({ date: e.target.value })}
            />
          </Field>
          <Field label={t("sb.once.time")}>
            <input className="input" type="time" value={spec.time} onChange={(e) => update({ time: e.target.value })} />
          </Field>
        </div>
      )}

      {spec.freq === "advanced" && (
        <Field label={t("sb.advanced.label")} hint={t("sb.advanced.hint")}>
          <input
            className="input"
            style={{ fontFamily: "var(--font-mono)" }}
            value={spec.cron}
            onChange={(e) => update({ cron: e.target.value })}
            placeholder="0 9 * * 1-5"
          />
        </Field>
      )}

      {/* timezone — searchable, defaults to the viewer's zone */}
      <TimezonePicker value={timezone} onChange={setTz} t={t} />

      {/* live, plain-language summary + the underlying cron for the curious */}
      <div
        style={{
          padding: "10px 12px",
          borderRadius: "var(--r-sm)",
          background: valid ? "var(--info-soft)" : "var(--warn-soft)",
          color: valid ? "var(--info)" : "var(--warn)",
          fontSize: 13,
          display: "flex",
          flexDirection: "column",
          gap: 4,
        }}
      >
        <span style={{ fontWeight: 600 }}>{pastOnce ? t("sb.sum.past") : describeRecurrence(spec, t)}</span>
        {valid && spec.freq !== "once" && built.schedule ? (
          <span className="muted" style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
            cron: {built.schedule}
          </span>
        ) : null}
      </div>
      {/* Advisory, never a block: a fast cadence is legitimate for a cheap
          task, but the run-dropping behaviour it causes is invisible otherwise
          (the runtime records a skipped run, but only after it happens). */}
      {valid && !pastOnce && isCadenceTooFrequent(spec) ? (
        <div
          role="status"
          style={{
            marginTop: 8,
            padding: "10px 12px",
            borderRadius: "var(--r-sm)",
            background: "var(--warn-soft)",
            color: "var(--warn)",
            fontSize: 12.5,
            lineHeight: 1.5,
          }}
        >
          {t("sb.sum.tooFrequent", { n: String(CADENCE_WARN_BELOW_MINUTES) })}
        </div>
      ) : null}
    </div>
  );
}

// ── number stepper (−  N  +) ─────────────────────────────────────────────────
function Stepper({
  value,
  min,
  max,
  onChange,
  suffix,
  prefix,
}: {
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
  suffix?: string;
  prefix?: string;
}) {
  const t = useT(STR);
  return (
    <div className="row" style={{ gap: 6, alignItems: "center" }}>
      <button
        type="button"
        className="btn"
        style={{ padding: "6px 11px", lineHeight: 1 }}
        onClick={() => onChange(clamp(value - 1, min, max))}
        disabled={value <= min}
        aria-label={t("sb.stepper.decrease")}
      >
        −
      </button>
      <input
        className="input"
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(clamp(parseInt(e.target.value || String(min), 10), min, max))}
        style={{ width: 76, textAlign: "center" }}
      />
      <button
        type="button"
        className="btn"
        style={{ padding: "6px 11px", lineHeight: 1 }}
        onClick={() => onChange(clamp(value + 1, min, max))}
        disabled={value >= max}
        aria-label={t("sb.stepper.increase")}
      >
        +
      </button>
      {prefix ? <span className="muted">{prefix}</span> : null}
      {suffix ? <span className="muted">{suffix}</span> : null}
    </div>
  );
}

// Quick-pick chips for common interval values.
function Presets({ values, active, onPick }: { values: number[]; active: number; onPick: (v: number) => void }) {
  return (
    <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 8 }}>
      {values.map((v) => (
        <button
          key={v}
          type="button"
          className="wd-btn"
          aria-pressed={active === v}
          style={active === v ? { background: "var(--accent-soft)", color: "var(--accent)", borderColor: "var(--accent)" } : undefined}
          onClick={() => onPick(v)}
        >
          {v}
        </button>
      ))}
    </div>
  );
}

// ── timezone picker (searchable popover, fixed-positioned to escape the modal's
// overflow clip) ─────────────────────────────────────────────────────────────
function TimezonePicker({
  value,
  onChange,
  t,
}: {
  value: string;
  onChange: (tz: string) => void;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const btnRef = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);

  const zones = useMemo(() => listTimeZones(), []);
  const offsets = useMemo(() => {
    const m = new Map<string, string>();
    for (const z of zones) m.set(z, tzOffsetLabel(z));
    return m;
  }, [zones]);
  const local = useMemo(() => guessTimeZone(), []);

  // Reset the query whenever the popover closes so it reopens clean.
  useEffect(() => {
    if (!open) setQ("");
  }, [open]);

  const norm = (s: string) => s.toLowerCase().replace(/[_/]/g, " ");
  const query = norm(q.trim());
  const filtered = useMemo(
    () => (query ? zones.filter((z) => norm(z).includes(query) || (offsets.get(z) || "").toLowerCase().includes(query)) : zones),
    [query, zones, offsets],
  );
  const suggested = query ? [] : Array.from(new Set([local, "UTC"])).filter(Boolean);

  const pick = (tz: string) => {
    onChange(tz);
    setOpen(false);
  };

  const Item = (z: string) => (
    <button key={z} type="button" className={`model-item${z === value ? " selected" : ""}`} onClick={() => pick(z)}>
      <span className="model-item-name">{z}</span>
      <span className="model-item-sub">{offsets.get(z)}</span>
    </button>
  );

  return (
    <Field label={t("sb.timezone")} hint={value === local ? t("sb.timezone.yours") : undefined}>
      <button
        ref={btnRef}
        type="button"
        className="input row"
        style={{ justifyContent: "space-between", alignItems: "center", cursor: "pointer", textAlign: "left" }}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="row" style={{ gap: 7, minWidth: 0 }}>
          <Globe size={14} strokeWidth={1.75} style={{ flexShrink: 0, color: "var(--text-3)" }} />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value || "UTC"}</span>
          <span className="muted" style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
            {offsets.get(value) || tzOffsetLabel(value)}
          </span>
        </span>
        <ChevronDown size={14} strokeWidth={2} style={{ opacity: 0.7, flexShrink: 0 }} />
      </button>
      {/* Above --z-modal: this popover is opened from inside the create-modal
          and is portaled out to <body>, so it has to clear the modal it left. */}
      <AnchoredPopover
        anchorRef={btnRef}
        open={open}
        onClose={close}
        minWidth={260}
        className="tz-pop"
        style={{ zIndex: "var(--z-confirm)" }}
      >
        <div style={{ position: "relative" }}>
          <Search
            size={13}
            strokeWidth={1.75}
            style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)", color: "var(--text-4)" }}
          />
          <input
            className="input model-search"
            style={{ paddingLeft: 26 }}
            autoFocus
            placeholder={t("sb.timezone.search")}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div className="model-list">
          {suggested.length > 0 && (
            <>
              <div className="muted" style={{ fontSize: 10, padding: "4px 9px 2px", textTransform: "uppercase", letterSpacing: 0.4 }}>
                {t("sb.timezone.suggested")}
              </div>
              {suggested.map(Item)}
              <div style={{ height: 1, background: "var(--border)", margin: "4px 0" }} />
            </>
          )}
          {filtered.slice(0, 300).map(Item)}
          {filtered.length === 0 && <div className="model-empty">{t("sb.timezone.noMatch")}</div>}
        </div>
      </AnchoredPopover>
    </Field>
  );
}
