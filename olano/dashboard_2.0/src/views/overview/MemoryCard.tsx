import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Activity, ChevronRight, Cpu } from "lucide-react";
import { api } from "../../core/api";
import { usePersistentState } from "../../core/usePersistentState";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

// Live process-memory card for the Overview. Polls the runtime's
// /api/diagnostics/memory (gc off — cheap) so an operator can watch RSS and the
// leak-indicator structure counts, and flip the background monitor (sampler +
// tracemalloc) on/off without leaving the dashboard. "Off" also stops
// tracemalloc, so the monitor stops costing memory on a constrained box.
// Collapsed by default (like the Agents section) — a single snapshot fills the
// compact summary line; the 5s poll only runs while the card is expanded.

interface MemProcess {
  rss_mb: number | null;
  vms_mb: number | null;
  pss_mb: number | null;
  threads: number | null;
  uptime_seconds: number | null;
}
interface MemAgent {
  boot_rss_mb: number | null;
  hook_contexts: number | null;
  config_tools: number | null;
  mcp_servers: number | null;
}
interface MemStructures {
  tool_args_registry: number | null;
  hook_contexts_total: number | null;
  processed_offsets_total: number | null;
  runners_count: number | null;
  per_agent?: Record<string, MemAgent>;
  rag?: { embedding_model_resident: boolean | null; vectors_total?: number | null };
}
interface MemChildProc {
  pid: number;
  label: string;
  comm: string;
  rss_mb: number | null;
}
interface MemChildren {
  supported: boolean;
  total_rss_mb: number | null;
  processes: MemChildProc[];
}
interface MemMonitor {
  enabled: boolean;
  interval_seconds: number;
  tracemalloc: boolean;
}
interface MemReport {
  source: string;
  running: boolean;
  process: MemProcess | null;
  structures: MemStructures | null;
  children?: MemChildren | null;
  monitor: MemMonitor | null;
}

function fmtMb(v: number | null | undefined): string {
  return typeof v === "number" ? `${Math.round(v).toLocaleString()} MB` : "—";
}
function fmtUptime(s: number | null | undefined): string {
  if (typeof s !== "number") return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// A dotted underline + help cursor signals "hover me for an explanation".
const hintStyle = { cursor: "help", textDecoration: "underline dotted", textUnderlineOffset: 2 };

function MemStat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="card" style={{ padding: "8px 10px" }} title={hint}>
      <div
        className="muted"
        style={{
          fontSize: 11,
          textTransform: "uppercase",
          letterSpacing: 0.4,
          ...(hint ? hintStyle : {}),
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 16, fontWeight: 600, marginTop: 2 }}>{value}</div>
    </div>
  );
}

// One inline "label value" pair from the structure-count row, with its own
// hover explanation on the (dotted-underlined) label.
function Term({ label, hint, children }: { label: string; hint: string; children: ReactNode }) {
  return (
    <span>
      <span title={hint} style={hintStyle}>
        {label}
      </span>{" "}
      <b>{children}</b>
    </span>
  );
}

export function MemoryCard() {
  const t = useT(STR);
  const [report, setReport] = useState<MemReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = usePersistentState("overview.memoryCollapsed", true);
  const aliveRef = useRef(true);

  const load = useCallback(() => {
    api
      .get<MemReport>("/api/diagnostics/memory?gc=false&tracemalloc=false")
      .then((r) => {
        if (!aliveRef.current) return;
        setReport(r);
        setErr(null);
      })
      .catch((e: unknown) => {
        if (!aliveRef.current) return;
        setErr(e instanceof Error ? e.message : t("memory.unavailableFallback"));
      });
  }, [t]);

  useEffect(() => {
    aliveRef.current = true;
    load();
    // Collapsed: the single load above fills the summary line — no polling.
    if (collapsed) {
      return () => {
        aliveRef.current = false;
      };
    }
    // Overview is the default landing page, often left open in a background
    // tab — don't keep polling diagnostics while nobody is looking; the
    // visibility listener refreshes the card the moment the tab returns.
    const id = window.setInterval(() => {
      if (!document.hidden) load();
    }, 5000);
    const onVisible = () => {
      if (!document.hidden) load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      aliveRef.current = false;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load, collapsed]);

  const toggle = useCallback(
    async (action: "on" | "off") => {
      setBusy(true);
      try {
        const state = await api.post<MemMonitor>("/api/diagnostics/memory/monitor", { action });
        setReport((prev) => (prev ? { ...prev, monitor: state } : prev));
      } catch {
        // surfaced by the next poll
      } finally {
        setBusy(false);
        load();
      }
    },
    [load],
  );

  const p = report?.process;
  const s = report?.structures;
  const mon = report?.monitor;
  const monOn = !!mon?.enabled;
  const agents = Object.entries(s?.per_agent ?? {})
    .filter(([, a]) => typeof a.boot_rss_mb === "number")
    .sort((a, b) => (b[1].boot_rss_mb ?? 0) - (a[1].boot_rss_mb ?? 0));
  const children = (report?.children?.processes ?? []).filter((c) => (c.rss_mb ?? 0) > 0);

  return (
    <div className="card" style={{ marginTop: 14, padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <button
            type="button"
            className="btn ghost"
            onClick={() => setCollapsed((c) => !c)}
            aria-expanded={!collapsed}
            title={collapsed ? t("memory.expand") : t("memory.collapse")}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "2px 6px",
              margin: "-2px -6px",
            }}
          >
            <ChevronRight
              size={16}
              strokeWidth={1.75}
              style={{
                transition: "transform var(--t-fast)",
                transform: collapsed ? "none" : "rotate(90deg)",
              }}
            />
            <span
              style={{
                fontWeight: 600,
                fontSize: 14,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <Cpu size={15} strokeWidth={1.75} /> {t("memory.title")}
            </span>
          </button>
          <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5, maxWidth: 560 }}>
            {err ? (
              t("memory.unavailable", { err })
            ) : !report ? (
              t("memory.loading")
            ) : collapsed ? (
              <>
                <span title={t("memory.hint.rss")} style={hintStyle}>
                  {t("memory.stat.rss")}
                </span>{" "}
                {fmtMb(p?.rss_mb)}
                {t("memory.summary.monitorSuffix", {
                  state: monOn ? t("memory.word.on") : t("memory.word.off"),
                })}
              </>
            ) : (
              t("memory.summary.source", { source: report.source }) +
              (report.running ? "" : t("memory.noDaemonSuffix"))
            )}
          </p>
        </div>
        {!collapsed && (
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <button
              className={monOn ? "btn danger" : "btn primary"}
              disabled={busy || !report?.running}
              onClick={() => void toggle(monOn ? "off" : "on")}
              title={t("memory.monitorToggleHint")}
              style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              {busy ? <span className="spinner" /> : <Activity size={14} strokeWidth={1.75} />}
              {monOn ? t("memory.monitor.on") : t("memory.monitor.off")}
            </button>
          </div>
        )}
      </div>

      {!collapsed && p && (
        <div
          style={{
            display: "grid",
            gap: 10,
            marginTop: 12,
            gridTemplateColumns: "repeat(auto-fill,minmax(120px,1fr))",
          }}
        >
          <MemStat label={t("memory.stat.rss")} value={fmtMb(p.rss_mb)} hint={t("memory.hint.rss")} />
          <MemStat label={t("memory.stat.pss")} value={fmtMb(p.pss_mb)} hint={t("memory.hint.pss")} />
          <MemStat label={t("memory.stat.vmSize")} value={fmtMb(p.vms_mb)} hint={t("memory.hint.vms")} />
          <MemStat label={t("memory.stat.threads")} value={p.threads ?? "—"} hint={t("memory.hint.threads")} />
          <MemStat
            label={t("memory.stat.uptime")}
            value={fmtUptime(p.uptime_seconds)}
            hint={t("memory.hint.uptime")}
          />
        </div>
      )}

      {!collapsed && s && (
        <div
          className="muted"
          style={{ marginTop: 12, fontSize: 12.5, display: "flex", flexWrap: "wrap", gap: "4px 14px" }}
        >
          <Term label={t("memory.term.toolArgs")} hint={t("memory.hint.toolArgs")}>
            {s.tool_args_registry ?? "—"}
          </Term>
          <Term label={t("memory.term.hookCtx")} hint={t("memory.hint.hookCtx")}>
            {s.hook_contexts_total ?? "—"}
          </Term>
          <Term label={t("memory.term.offsets")} hint={t("memory.hint.offsets")}>
            {s.processed_offsets_total ?? "—"}
          </Term>
          <Term label={t("memory.term.runners")} hint={t("memory.hint.runners")}>
            {s.runners_count ?? "—"}
          </Term>
          <Term label={t("memory.term.embedModel")} hint={t("memory.hint.embedModel")}>
            {s.rag?.embedding_model_resident ? t("memory.embedModel.resident") : t("memory.embedModel.no")}
          </Term>
          {mon && (
            <span>
              <Term label={t("memory.term.sampler")} hint={t("memory.hint.sampler")}>
                {monOn ? t("memory.sampler.interval", { n: String(mon.interval_seconds) }) : t("memory.word.off")}
              </Term>
              {mon.tracemalloc ? t("memory.tracemallocOn") : ""}
            </span>
          )}
        </div>
      )}

      {!collapsed && agents.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            className="muted"
            title={t("memory.hint.bootRss")}
            style={{
              fontSize: 11,
              textTransform: "uppercase",
              letterSpacing: 0.4,
              marginBottom: 4,
              ...hintStyle,
              display: "inline-block",
            }}
          >
            {t("memory.perAgentHeading")}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", fontSize: 12.5 }}>
            {agents.slice(0, 8).map(([name, a]) => (
              <span key={name}>
                {name} <b>{fmtMb(a.boot_rss_mb)}</b>
              </span>
            ))}
          </div>
        </div>
      )}

      {!collapsed && children.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            className="muted"
            title={t("memory.hint.subprocesses")}
            style={{
              fontSize: 11,
              textTransform: "uppercase",
              letterSpacing: 0.4,
              marginBottom: 4,
              ...hintStyle,
              display: "inline-block",
            }}
          >
            {t("memory.subprocessesHeading", { mb: fmtMb(report?.children?.total_rss_mb) })}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", fontSize: 12.5 }}>
            {children.slice(0, 6).map((c) => (
              <span key={c.pid}>
                {c.label} <b>{fmtMb(c.rss_mb)}</b>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
