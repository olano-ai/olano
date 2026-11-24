import { useT } from "../../core/i18n";
import { modelContextWindow } from "./models";
import type { ContextReport } from "./sessionStore";
import { STR } from "./strings";

// Stable, theme-independent category colours (a data-viz palette) so the grid,
// legend and section dots always agree. "Free space" uses a surface token so the
// empty cells read as background, not data.
const COLORS: Record<string, string> = {
  system_prompt: "#ff6a3d",
  system_tools: "#60a5fa",
  subagents: "#a78bfa",
  memory_files: "#e2b341",
  skills: "#3ddc97",
  mcp_tools: "#22d3ee",
  messages: "#ec4899",
  free: "var(--surface-4)",
};

// Detail rows under the breakdown link out to the matching slash command (the
// dashboard exposes /tools, /subagents, /skills, /mcps — but not memory).
const SECTIONS: { key: string; cmd: string | null }[] = [
  { key: "system_tools", cmd: "tools" },
  { key: "subagents", cmd: "subagents" },
  { key: "skills", cmd: "skills" },
  { key: "mcp_tools", cmd: "mcps" },
  { key: "memory_files", cmd: null },
];

// 28 × 10 cells — each cell is a fixed slice of the context window, coloured by
// whichever category it falls into (mirrors Claude Code's /context grid).
const GRID_COLS = 28;
const GRID_CELLS = GRID_COLS * 10;

function fmtTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

export function ContextCard({
  report,
  onRunCommand,
}: {
  report: ContextReport;
  onRunCommand?: (cmd: string) => void;
}) {
  const t = useT(STR);
  const byKey = new Map(report.categories.map((c) => [c.key, c]));
  const tokensOf = (key: string) => byKey.get(key)?.tokens ?? 0;

  const staticTokens = report.categories.reduce((sum, c) => sum + c.tokens, 0);
  // "Messages" is the live conversation: the last turn's window fill minus the
  // static footprint. Unknown (null) on a fresh thread → 0.
  const messages =
    report.usedContext != null ? Math.max(0, report.usedContext - staticTokens) : 0;
  const used = staticTokens + messages;

  const ctxWindow = modelContextWindow(report.model);
  const total = ctxWindow ?? used; // no window table for this model → fill = used
  const free = ctxWindow ? Math.max(0, ctxWindow - used) : 0;
  const pct = ctxWindow && ctxWindow > 0 ? (used / ctxWindow) * 100 : null;

  // Ordered segments that tile the grid + drive the legend.
  const segments: { key: string; tokens: number; color: string }[] = [
    ...report.categories.map((c) => ({ key: c.key, tokens: c.tokens, color: COLORS[c.key] || COLORS.free })),
    { key: "messages", tokens: messages, color: COLORS.messages },
    { key: "free", tokens: free, color: COLORS.free },
  ];

  // Paint the grid: each segment claims a proportional run of cells; rounding
  // slack is padded with "free" so the grid is always exactly full.
  const cells: string[] = [];
  for (const seg of segments) {
    if (seg.tokens <= 0) continue;
    const n = total > 0 ? Math.round((seg.tokens / total) * GRID_CELLS) : 0;
    for (let i = 0; i < n && cells.length < GRID_CELLS; i++) cells.push(seg.color);
  }
  while (cells.length < GRID_CELLS) cells.push(COLORS.free);

  const label = (key: string) => t(`context.cat.${key}`);
  const segPct = (tokens: number) =>
    total > 0 ? `${((tokens / total) * 100).toFixed(1)}%` : "";

  return (
    <div className="ctx-card">
      <div className="ctx-head">
        <span className="ctx-model">{report.model || "—"}</span>
        <span className="ctx-usage tabular">
          {ctxWindow
            ? t("context.usage", {
                used: fmtTokens(used),
                total: fmtTokens(ctxWindow),
                pct: pct != null ? pct.toFixed(1) : "0",
              })
            : t("context.usageNoWindow", { used: used.toLocaleString() })}
        </span>
      </div>

      <div
        className="ctx-grid"
        style={{ gridTemplateColumns: `repeat(${GRID_COLS}, 1fr)` }}
        aria-hidden
      >
        {cells.map((c, i) => (
          <span
            key={i}
            className={`ctx-cell${c === COLORS.free ? " is-free" : ""}`}
            style={c === COLORS.free ? undefined : { background: c }}
          />
        ))}
      </div>

      <div className="ctx-legend-title">{t("context.estimated")}</div>
      <div className="ctx-legend">
        {segments.map((seg) => (
          <div className="ctx-legend-row" key={seg.key}>
            <span className="ctx-dot" style={{ background: seg.color }} />
            <span className="ctx-legend-label">{label(seg.key)}</span>
            <span className="ctx-legend-val tabular">
              {fmtTokens(seg.tokens)} {t("context.tokens")}
              {segPct(seg.tokens) && <span className="ctx-legend-pct"> ({segPct(seg.tokens)})</span>}
            </span>
          </div>
        ))}
      </div>

      <div className="ctx-sections">
        {SECTIONS.map(({ key, cmd }) => {
          const cat = byKey.get(key);
          if (!cat) return null;
          const count = cat.count ?? 0;
          return (
            <div className="ctx-section" key={key}>
              <span className="ctx-dot" style={{ background: COLORS[key] || COLORS.free }} />
              <span className="ctx-section-label">{label(key)}</span>
              {cmd &&
                (onRunCommand ? (
                  <button type="button" className="ctx-cmd" onClick={() => onRunCommand(cmd)}>
                    /{cmd}
                  </button>
                ) : (
                  <code className="ctx-cmd">/{cmd}</code>
                ))}
              <span className="ctx-section-meta tabular">
                {t("context.items", { n: String(count) })}
                {" · "}
                {cat.onDemand ? t("context.onDemand") : `${fmtTokens(tokensOf(key))} ${t("context.tokens")}`}
              </span>
            </div>
          );
        })}
      </div>

    </div>
  );
}
