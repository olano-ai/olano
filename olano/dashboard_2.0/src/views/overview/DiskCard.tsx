import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRight, HardDrive, Play, ShieldCheck } from "lucide-react";
import { api } from "../../core/api";
import { usePersistentState } from "../../core/usePersistentState";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

// Live disk-usage card for the Overview. Reads /api/diagnostics/disk (a fresh
// shutil.disk_usage measurement + the monitor's settings/state) so an operator
// can see pressure at a glance, flip automatic cleanup on/off, and run a
// cleanup right now without leaving the dashboard. Collapsed by default like
// MemoryCard; the poll only runs while expanded — except while a cleanup is in
// flight, when we keep polling so the button un-busies itself when it's done.

interface DiskActionInfo {
  name: string;
  description: string;
  safe: boolean;
  selected: boolean;
  available: boolean;
  reason: string | null;
}
interface DiskCleanupCfg {
  enabled: boolean;
  actions: string[];
  cooldown_minutes: number;
  force_interval_days: number;
  journald_vacuum_size: string;
}
interface DiskSettings {
  enabled: boolean;
  path: string;
  check_interval_minutes: number;
  warn_percent: number;
  cleanup_percent: number;
  cleanup: DiskCleanupCfg;
  env_disabled?: boolean;
}
interface DiskActionResult {
  name: string;
  status: string; // ok | skipped | failed
  reason: string | null;
  freed_human: string;
  reported: string | null;
}
interface DiskCleanupReport {
  started_at: string;
  trigger: string;
  freed_human: string;
  percent_before: number;
  percent_after: number;
  results: DiskActionResult[];
}
interface DiskBreakdownGroup {
  key: string;
  label: string;
  detail: string;
  bytes: number | null;
  reclaimable_bytes: number | null;
  note: string | null;
}
interface DiskBreakdown {
  path: string;
  generated_at: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  attributed_bytes: number;
  groups: DiskBreakdownGroup[];
  complete: boolean;
  warnings: string[];
}
interface DiskScanEntry {
  path: string;
  depth: number;
  bytes: number;
  files: number;
}
interface DiskScanSnapshot {
  root: string;
  scanned_at: string;
  duration_seconds: number;
  total_bytes: number;
  total_files: number;
  truncated: boolean;
  entries: DiskScanEntry[];
}
interface DiskReport {
  source: string;
  path?: string;
  percent?: number;
  level?: string; // ok | warn | critical
  free_human?: string;
  total_human?: string;
  error?: string;
  settings?: DiskSettings;
  state?: { last_cleanup_at?: string | null; last_report?: DiskCleanupReport | null };
  actions?: DiskActionInfo[];
  cleanup_running?: boolean;
  scan?: DiskScanSnapshot | null;
  scan_running?: boolean;
  breakdown?: DiskBreakdown | null;
}

function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

// One colour per breakdown group, so the stacked bar and the list read as the
// same object. Unknown keys (a future group) fall back to the neutral tone
// rather than rendering an invisible segment.
const GROUP_COLORS: Record<string, string> = {
  docker_images: "var(--accent)",
  docker_containers: "var(--accent-2)",
  docker_volumes: "var(--info)",
  docker_build_cache: "var(--cortex)",
  olano_home: "var(--success)",
  swap: "var(--warn)",
  system: "var(--text-3)",
};
const groupColor = (key: string): string => GROUP_COLORS[key] ?? "var(--border-strong)";


const hintStyle = { cursor: "help", textDecoration: "underline dotted", textUnderlineOffset: 2 };

function levelColor(level?: string): string {
  if (level === "critical") return "var(--danger)";
  if (level === "warn") return "var(--warn)";
  return "var(--success)";
}

// Horizontal usage meter with tick marks at the warn / auto-clean thresholds.
function UsageMeter({
  percent,
  warn,
  cleanup,
  level,
}: {
  percent: number;
  warn: number;
  cleanup: number;
  level?: string;
}) {
  const t = useT(STR);
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div title={t("disk.hint.usage")} style={{ flex: "1 1 260px", minWidth: 200 }}>
      <div
        style={{
          position: "relative",
          height: 10,
          borderRadius: 999,
          background: "var(--surface-2)",
          border: "1px solid var(--border)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            position: "absolute",
            inset: 0,
            width: `${clamped}%`,
            background: levelColor(level),
            borderRadius: 999,
            transition: "width var(--t-fast), background var(--t-fast)",
          }}
        />
        {[
          { pct: warn, label: t("disk.warnPct", { pct: String(warn) }) },
          { pct: cleanup, label: t("disk.autoCleanPct", { pct: String(cleanup) }) },
        ].map((m) => (
          <div
            key={m.label}
            title={m.label}
            style={{
              position: "absolute",
              left: `${m.pct}%`,
              top: -2,
              bottom: -2,
              width: 2,
              background: "var(--text-3)",
              opacity: 0.7,
            }}
          />
        ))}
      </div>
      <div
        className="muted"
        style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginTop: 3 }}
      >
        <span>0%</span>
        <span>{t("disk.legend", { warn: String(warn), cleanup: String(cleanup) })}</span>
        <span>100%</span>
      </div>
    </div>
  );
}

function DiskStat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
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

// "Where the space went" — the whole disk, not just the olano home. The card
// used to show a 759 MB directory list next to a 14 GB usage meter with nothing
// explaining the gap; these groups add up to the used space, so the gap has a
// name (Docker images, swap, the operating system) instead of being a mystery.
function SpaceBreakdown({ breakdown }: { breakdown: DiskBreakdown }) {
  const t = useT(STR);
  const total = breakdown.total_bytes > 0 ? breakdown.total_bytes : 0;
  // Largest first, but the residual always last — it is "everything else", so
  // it reads as the tail of the bar rather than as another named consumer.
  // `olano disk` orders it the same way; the two surfaces must not disagree.
  const sized = breakdown.groups
    .filter((g) => typeof g.bytes === "number" && (g.bytes as number) > 0)
    .sort(
      (a, b) =>
        Number(a.key === "system") - Number(b.key === "system") ||
        (b.bytes as number) - (a.bytes as number),
    );
  const unmeasured = breakdown.groups.filter((g) => typeof g.bytes !== "number");
  const pct = (n: number) => (total > 0 ? (n / total) * 100 : 0);

  return (
    <div style={{ marginTop: 14 }}>
      <div
        className="muted"
        title={t("disk.hint.breakdown")}
        style={{
          fontSize: 11,
          textTransform: "uppercase",
          letterSpacing: 0.4,
          marginBottom: 5,
          ...hintStyle,
          display: "inline-block",
        }}
      >
        {t("disk.breakdown.title", {
          used: fmtBytes(breakdown.used_bytes),
          total: fmtBytes(breakdown.total_bytes),
        })}
      </div>

      {/* One stacked bar: every group plus free space, to scale. */}
      <div
        style={{
          display: "flex",
          height: 12,
          borderRadius: 999,
          overflow: "hidden",
          border: "1px solid var(--border)",
          background: "var(--surface-2)",
        }}
      >
        {sized.map((g) => (
          <span
            key={g.key}
            title={`${g.label} — ${fmtBytes(g.bytes as number)}`}
            style={{
              width: `${pct(g.bytes as number)}%`,
              background: groupColor(g.key),
              opacity: g.key === "system" ? 0.55 : 1,
            }}
          />
        ))}
      </div>

      <div style={{ display: "grid", gap: 3, marginTop: 6 }}>
        {sized.map((g) => (
          <div
            key={g.key}
            title={g.key === "system" ? t("disk.hint.breakdownSystem") : g.detail}
            style={{
              display: "grid",
              gridTemplateColumns: "10px minmax(90px, 1fr) 78px",
              alignItems: "center",
              gap: 8,
              fontSize: 12.5,
              cursor: "help",
            }}
          >
            <span
              aria-hidden
              style={{
                width: 8,
                height: 8,
                borderRadius: 2,
                background: groupColor(g.key),
                opacity: g.key === "system" ? 0.55 : 1,
              }}
            />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {g.label}
              {typeof g.reclaimable_bytes === "number" && g.reclaimable_bytes > 0 && (
                <span className="muted">
                  {" "}
                  · {t("disk.breakdown.reclaimable", { bytes: fmtBytes(g.reclaimable_bytes) })}
                </span>
              )}
              {g.note && <span className="muted"> · {g.note}</span>}
            </span>
            <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              {fmtBytes(g.bytes as number)}
            </span>
          </div>
        ))}
        <div
          className="muted"
          style={{
            display: "grid",
            gridTemplateColumns: "10px minmax(90px, 1fr) 78px",
            alignItems: "center",
            gap: 8,
            fontSize: 12.5,
          }}
        >
          <span
            aria-hidden
            style={{
              width: 8,
              height: 8,
              borderRadius: 2,
              border: "1px solid var(--border-strong)",
            }}
          />
          <span>{t("disk.breakdown.free")}</span>
          <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
            {fmtBytes(breakdown.free_bytes)}
          </span>
        </div>
      </div>

      {unmeasured.length > 0 && (
        <p className="muted" style={{ margin: "5px 0 0", fontSize: 12 }}>
          {t("disk.breakdown.notMeasured")}{" "}
          {unmeasured.map((g, i) => (
            <span key={g.key} title={g.detail}>
              {i > 0 ? ", " : ""}
              {g.label}
              {g.note ? ` (${g.note})` : ""}
            </span>
          ))}
        </p>
      )}
      {breakdown.warnings.map((w) => (
        <p key={w} className="muted" style={{ margin: "4px 0 0", fontSize: 12, color: "var(--warn)" }}>
          {w}
        </p>
      ))}
    </div>
  );
}

function statusGlyph(status: string): string {
  if (status === "ok") return "✓";
  if (status === "skipped") return "–";
  return "✕";
}

export function DiskCard() {
  const t = useT(STR);
  const [report, setReport] = useState<DiskReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = usePersistentState("overview.diskCollapsed", true);
  const aliveRef = useRef(true);

  const load = useCallback(() => {
    api
      .get<DiskReport>("/api/diagnostics/disk")
      .then((r) => {
        if (!aliveRef.current) return;
        setReport(r);
        setErr(null);
      })
      .catch((e: unknown) => {
        if (!aliveRef.current) return;
        setErr(e instanceof Error ? e.message : t("disk.unavailableFallback"));
      });
  }, [t]);

  const cleaning = !!report?.cleanup_running;
  const scanning = !!report?.scan_running;

  useEffect(() => {
    aliveRef.current = true;
    load();
    // Collapsed and idle: the single load above fills the summary line.
    // Expanded (or mid-cleanup/scan): poll, but never while the tab is hidden.
    if (collapsed && !cleaning && !scanning) {
      return () => {
        aliveRef.current = false;
      };
    }
    const id = window.setInterval(() => {
      if (!document.hidden) load();
    }, cleaning || scanning ? 3000 : 5000);
    const onVisible = () => {
      if (!document.hidden) load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      aliveRef.current = false;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load, collapsed, cleaning, scanning]);

  const act = useCallback(
    async (action: "on" | "off" | "run" | "scan") => {
      setBusy(true);
      try {
        await api.post("/api/diagnostics/disk/monitor", { action });
      } catch {
        // surfaced by the next poll
      } finally {
        setBusy(false);
        load();
      }
    },
    [load],
  );

  const settings = report?.settings;
  const autoOn = !!settings?.cleanup?.enabled;
  const last = report?.state?.last_report ?? null;
  const percent = typeof report?.percent === "number" ? report.percent : null;
  const actions = report?.actions ?? [];
  const scan = report?.scan ?? null;
  const breakdown = report?.breakdown ?? null;
  const [openDir, setOpenDir] = useState<string | null>(null);
  const topDirs = (scan?.entries ?? []).filter((e) => e.depth === 1).slice(0, 10);
  const maxDirBytes = topDirs.reduce((m, e) => Math.max(m, e.bytes), 0);
  const childrenOf = (parent: string) =>
    (scan?.entries ?? [])
      .filter(
        (e) =>
          e.path.startsWith(`${parent}/`) && !e.path.slice(parent.length + 1).includes("/"),
      )
      .sort((a, b) => b.bytes - a.bytes)
      .slice(0, 8);

  return (
    <div className="card" style={{ marginTop: 14, padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <button
            type="button"
            className="btn ghost"
            onClick={() => setCollapsed((c) => !c)}
            aria-expanded={!collapsed}
            title={collapsed ? t("disk.expand") : t("disk.collapse")}
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
              <HardDrive size={15} strokeWidth={1.75} /> {t("disk.title")}
            </span>
            {percent != null && (
              <span
                className="badge"
                style={{
                  background: "transparent",
                  border: `1px solid ${levelColor(report?.level)}`,
                  color: levelColor(report?.level),
                }}
              >
                {percent}%
              </span>
            )}
          </button>
          <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5, maxWidth: 560 }}>
            {err ? (
              t("disk.unavailable", { err })
            ) : !report ? (
              t("disk.loading")
            ) : report.error ? (
              report.error
            ) : collapsed ? (
              <>
                {t("disk.summary.collapsed", {
                  free: report.free_human ?? "",
                  total: report.total_human ?? "",
                  path: report.path ?? "",
                  state: autoOn ? t("disk.word.on") : t("disk.word.off"),
                })}
                {cleaning ? t("disk.cleaningSuffix") : ""}
              </>
            ) : (
              <>
                {t("disk.summary.expanded", {
                  path: report.path ?? "",
                  free: report.free_human ?? "",
                  total: report.total_human ?? "",
                  interval: String(settings?.check_interval_minutes ?? "?"),
                })}
                {settings?.env_disabled ? t("disk.envDisabledSuffix") : ""}
              </>
            )}
          </p>
        </div>
        {!collapsed && (
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <button
              className={autoOn ? "btn danger" : "btn primary"}
              disabled={busy}
              onClick={() => void act(autoOn ? "off" : "on")}
              title={t("disk.hint.autoClean")}
              style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              <ShieldCheck size={14} strokeWidth={1.75} />
              {autoOn ? t("disk.autoClean.on") : t("disk.autoClean.off")}
            </button>
            <button
              className="btn"
              disabled={busy || cleaning}
              onClick={() => void act("run")}
              title={t("disk.hint.cleanNow")}
              style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              {cleaning ? <span className="spinner" /> : <Play size={14} strokeWidth={1.75} />}
              {cleaning ? t("disk.clean.busy") : t("disk.clean.now")}
            </button>
          </div>
        )}
      </div>

      {!collapsed && !err && percent != null && settings && (
        <div
          className="row"
          style={{ marginTop: 14, gap: 16, alignItems: "center", flexWrap: "wrap" }}
        >
          <UsageMeter
            percent={percent}
            warn={settings.warn_percent}
            cleanup={settings.cleanup_percent}
            level={report?.level}
          />
          <div
            style={{
              display: "grid",
              gap: 10,
              gridTemplateColumns: "repeat(auto-fill,minmax(110px,1fr))",
              flex: "2 1 320px",
            }}
          >
            <DiskStat label={t("disk.stat.used")} value={`${percent}%`} hint={t("disk.hint.usage")} />
            <DiskStat label={t("disk.stat.free")} value={report?.free_human ?? "—"} />
            <DiskStat
              label={t("disk.stat.lastCleanup")}
              value={last ? last.freed_human : t("disk.never")}
              hint={t("disk.hint.lastCleanup")}
            />
          </div>
        </div>
      )}

      {!collapsed && !err && breakdown && <SpaceBreakdown breakdown={breakdown} />}

      {!collapsed && !err && (
        <div style={{ marginTop: 12 }}>
          <div
            className="row"
            style={{ justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap" }}
          >
            <div
              className="muted"
              title={t("disk.hint.largest")}
              style={{
                fontSize: 11,
                textTransform: "uppercase",
                letterSpacing: 0.4,
                marginBottom: 4,
                ...hintStyle,
                display: "inline-block",
              }}
            >
              {t("disk.largestDirectories")}
              {scan
                ? t("disk.scanSuffix", {
                    bytes: fmtBytes(scan.total_bytes),
                    time: new Date(scan.scanned_at).toLocaleString(),
                  }) + (scan.truncated ? t("disk.partialSuffix") : "")
                : ""}
            </div>
            <button
              className="btn ghost"
              disabled={busy || scanning}
              onClick={() => void act("scan")}
              title={t("disk.hint.scanNow")}
              style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}
            >
              {scanning ? <span className="spinner" /> : <Play size={12} strokeWidth={1.75} />}
              {scanning ? t("disk.scan.busy") : t("disk.scan.now")}
            </button>
          </div>
          {topDirs.length === 0 ? (
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 12.5 }}>
              {scanning ? t("disk.scan.firstRunning") : t("disk.scan.noneYet")}
            </p>
          ) : (
            <div style={{ display: "grid", gap: 3, marginTop: 4 }}>
              {topDirs.map((e) => {
                const open = openDir === e.path;
                const kids = open ? childrenOf(e.path) : [];
                return (
                  <div key={e.path}>
                    <button
                      type="button"
                      onClick={() => setOpenDir(open ? null : e.path)}
                      title={t("disk.dirRow.title", {
                        files: e.files.toLocaleString(),
                        action: open ? t("disk.action.collapse") : t("disk.action.seeInside"),
                      })}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "minmax(90px, 180px) 1fr 72px",
                        alignItems: "center",
                        gap: 8,
                        width: "100%",
                        padding: "1px 4px",
                        border: "none",
                        background: "transparent",
                        color: "inherit",
                        cursor: "pointer",
                        fontSize: 12.5,
                        textAlign: "left",
                      }}
                    >
                      <span
                        style={{
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        <ChevronRight
                          size={11}
                          strokeWidth={1.75}
                          style={{
                            transition: "transform var(--t-fast)",
                            transform: open ? "rotate(90deg)" : "none",
                            verticalAlign: -1,
                            marginRight: 3,
                          }}
                        />
                        {e.path}
                      </span>
                      <span
                        aria-hidden
                        style={{
                          height: 7,
                          borderRadius: 999,
                          background: "var(--surface-2)",
                          border: "1px solid var(--border)",
                          overflow: "hidden",
                        }}
                      >
                        <span
                          style={{
                            display: "block",
                            height: "100%",
                            width: `${maxDirBytes > 0 ? Math.max(2, (e.bytes / maxDirBytes) * 100) : 0}%`,
                            background: "var(--accent)",
                            borderRadius: 999,
                          }}
                        />
                      </span>
                      <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                        {fmtBytes(e.bytes)}
                      </span>
                    </button>
                    {open && kids.length > 0 && (
                      <div style={{ display: "grid", gap: 2, margin: "1px 0 4px" }}>
                        {kids.map((k) => (
                          <div
                            key={k.path}
                            title={t("disk.filesCount", { files: k.files.toLocaleString() })}
                            style={{
                              display: "grid",
                              gridTemplateColumns: "minmax(90px, 180px) 1fr 72px",
                              gap: 8,
                              padding: "0 4px 0 26px",
                              fontSize: 12,
                            }}
                            className="muted"
                          >
                            <span
                              style={{
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {k.path.slice(e.path.length + 1)}
                            </span>
                            <span />
                            <span
                              style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}
                            >
                              {fmtBytes(k.bytes)}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {!collapsed && actions.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            className="muted"
            title={t("disk.hint.actions")}
            style={{
              fontSize: 11,
              textTransform: "uppercase",
              letterSpacing: 0.4,
              marginBottom: 4,
              ...hintStyle,
              display: "inline-block",
            }}
          >
            {t("disk.cleanupActions")}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", fontSize: 12.5 }}>
            {actions.map((a) => (
              <span key={a.name} title={a.description} style={{ opacity: a.selected ? 1 : 0.55 }}>
                <b>{a.selected ? "✓" : "–"}</b> {a.name}
                {!a.safe && (
                  <span className="badge" style={{ marginLeft: 5, background: "var(--warn-soft)", color: "var(--warn)" }}>
                    {t("disk.aggressive")}
                  </span>
                )}
                {!a.available && (
                  <span className="muted"> {t("disk.reasonWrapped", { reason: a.reason ?? "" })}</span>
                )}
              </span>
            ))}
          </div>
        </div>
      )}

      {!collapsed && last && (
        <div style={{ marginTop: 12 }}>
          <div
            className="muted"
            title={t("disk.hint.lastCleanup")}
            style={{
              fontSize: 11,
              textTransform: "uppercase",
              letterSpacing: 0.4,
              marginBottom: 4,
              ...hintStyle,
              display: "inline-block",
            }}
          >
            {t("disk.lastCleanupHeading", {
              trigger: last.trigger,
              before: String(last.percent_before),
              after: String(last.percent_after),
            })}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", fontSize: 12.5 }}>
            {last.results.map((r) => (
              <span key={r.name} title={r.reported ?? r.reason ?? ""}>
                {statusGlyph(r.status)} {r.name}{" "}
                <b>{r.status === "ok" ? r.reported ?? r.freed_human : r.reason ?? r.status}</b>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
