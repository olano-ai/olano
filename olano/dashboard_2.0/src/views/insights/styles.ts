// Scoped CSS for the Insights view — injected once (we can't touch globals.css).
// Topbar control cluster, work journal, drill-down rows, and the spin keyframe;
// everything else is driven by recharts + design tokens.

const STYLE_ID = "lc-insights-styles";

const CSS = `
@keyframes lc-insights-spin { to { transform: rotate(360deg); } }
.lc-spin { animation: lc-insights-spin 0.9s linear infinite; }

/* ── topbar controls ─────────────────────────────────────────────────────── */
.lc-in-topbar { container-type: inline-size; }
.lc-in-controls {
  display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap;
  gap: 8px; flex: 1 1 auto; min-width: 0;
}
.lc-in-seg { display: flex; align-items: center; gap: 2px; padding: 2px; background: var(--surface-2); border-radius: var(--r-sm); }
.lc-in-seg .btn { padding: 4px 10px; font-size: 12.5px; white-space: nowrap; justify-content: center; }
.lc-in-seg-sm .btn { padding: 2px 8px; font-size: 11.5px; }
.lc-in-refresh { white-space: nowrap; }

/* Once the bar is too narrow for one line (mobile, or a squeezed desktop
   window), restack into two flush rows: [view toggle | refresh] over a
   full-width window picker with equal segments. The bar's own wordmark is
   dropped — the brand stays visible in the mobile topbar / sidebar — so the
   controls get the full width. Queried on the bar (not the viewport) because
   the sidebar state changes how wide the bar really is; browsers without
   container queries keep the plain wrap above. */
@container (max-width: 720px) {
  .lc-in-topbar > .brand-wordmark { display: none; }
  .lc-in-controls { padding-block: 10px; }
  .lc-in-tabs { flex: 1 1 auto; }
  .lc-in-windows { order: 3; flex: 1 1 100%; }
  .lc-in-tabs .btn, .lc-in-windows .btn { flex: 1 1 0; min-width: 0; padding-inline: 6px; }
  .lc-in-refresh { align-self: stretch; padding: 6px 9px; }
  .lc-in-refresh-label { display: none; }
}

/* ── billing-book sections ───────────────────────────────────────────────── */
/* Each accounting book (Olano credits / BYOK) gets a titled band so the two
   sets of numbers can never be read as one running total. */
.lc-in-book { margin-top: 22px; }
.lc-in-book-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.lc-in-book-title { font-size: 15px; font-weight: 650; display: inline-flex; align-items: center; gap: 7px; }
.lc-in-book-desc { font-size: 12px; }
.lc-in-stats { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
.lc-in-charts { display: grid; gap: 12px; margin-top: 12px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.lc-in-charts-wide { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }

/* ── work journal ────────────────────────────────────────────────────────── */
.lc-j-day-select { width: auto; min-width: 150px; padding: 5px 30px 5px 10px; font-size: 12.5px; font-variant-numeric: tabular-nums; }
.lc-j-day-step { display: inline-flex; align-items: center; justify-content: center; padding: 5px 6px; }
.lc-j-agent-row {
  position: relative; overflow: hidden; display: flex; align-items: center; gap: 10px;
  padding: 7px 10px; border-radius: var(--r-sm); background: var(--surface-2); font-size: 12.5px;
}
.lc-j-agent-fill { position: absolute; left: 0; top: 0; bottom: 0; z-index: 0; background: var(--accent-soft); border-radius: var(--r-sm); }
.lc-j-agent-row > span { position: relative; z-index: 1; }
.lc-j-agent-name { font-weight: 600; min-width: 72px; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lc-j-agent-tools { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lc-j-agent-value { margin-left: auto; font-weight: 600; font-variant-numeric: tabular-nums; }
.lc-j-agent-share { min-width: 36px; text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 560px) { .lc-j-agent-tools { display: none; } }

/* ── drill-down ──────────────────────────────────────────────────────────── */
.lc-rows { display: flex; flex-direction: column; gap: 2px; }
.lc-cost-row {
  position: relative; overflow: hidden; display: flex; align-items: center; gap: 12px;
  width: 100%; text-align: left; background: transparent; border: 0;
  border-radius: var(--r-sm); padding: 9px 10px; cursor: pointer; color: inherit;
  font: inherit; transition: background 0.12s ease;
}
.lc-cost-row:disabled { cursor: default; }
.lc-cost-row:not(:disabled):hover { background: var(--surface-2); }
.lc-cost-fill {
  position: absolute; left: 0; top: 0; bottom: 0; z-index: 0;
  background: var(--accent-soft); border-radius: var(--r-sm);
}
.lc-cost-main { position: relative; z-index: 1; display: flex; flex-direction: column; gap: 1px; flex: 1; min-width: 0; }
.lc-cost-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.lc-cost-sub { font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: flex; align-items: center; gap: 6px; }
.lc-cost-metrics { position: relative; z-index: 1; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.lc-cost-chev { color: var(--text-3); }

.lc-crumb { background: transparent; border: 0; padding: 2px 4px; cursor: pointer; color: var(--accent); font: inherit; font-size: 13px; border-radius: 4px; }
.lc-crumb:disabled, .lc-crumb.is-current { color: var(--text-2); cursor: default; font-weight: 600; }

.lc-origin { font-size: 10px; padding: 1px 6px; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.3px; background: var(--surface-2); color: var(--text-3); }
.lc-origin-interactive { color: var(--accent); background: var(--accent-soft); }
.lc-origin-autonomous, .lc-origin-scheduled { color: var(--warn); background: color-mix(in srgb, var(--warn) 16%, transparent); }
.lc-origin-peer { color: var(--success); background: color-mix(in srgb, var(--success) 16%, transparent); }

.lc-tree-node { align-items: center; padding: 5px 8px; border-radius: var(--r-sm); transition: background 0.12s ease; }
.lc-tree-node:hover { background: var(--surface-2); }

.lc-chip { display: inline-flex; align-items: center; gap: 4px; font-family: var(--font-mono, ui-monospace, monospace); font-size: 11.5px; padding: 2px 7px; border-radius: var(--r-sm); background: var(--surface-2); color: var(--text-2); }

.lc-raw-overlay { position: fixed; inset: 0; z-index: 1000; background: color-mix(in srgb, #000 45%, transparent); display: flex; justify-content: flex-end; }
.lc-raw-drawer { width: min(620px, 92vw); height: 100%; border-radius: 0; overflow-y: auto; padding: 18px; }
.lc-raw-msg { background: var(--surface-2); border-radius: var(--r-sm); padding: 8px 10px; }
.lc-raw-pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: var(--font-mono, ui-monospace, monospace); font-size: 11.5px; line-height: 1.5; max-height: 280px; overflow-y: auto; color: var(--text-2); }
`;

export function ensureInsightsStyles(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(STYLE_ID)) return;
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}
