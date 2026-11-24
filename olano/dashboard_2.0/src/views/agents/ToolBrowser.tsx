// Categorized, searchable tool browser — a parity port of the original
// dashboard's TOOL BROWSER MODAL (_tb / _tbRender / _tbToolCard), rebuilt inline
// on the Aurora design system. Desktop: a left rail organizes tools by quick
// filter (All / Selected / Internal / Missing keys), curated **toolkit
// bundles**, and derived **categories** — the two long sections are
// collapsible and truncated to a screenful with a "Show all" expander, so the
// rail never becomes a 100-row scroll. Mobile: the rail is replaced by a
// compact filter bar (search first, quick-filter chips in one horizontal
// row, toolkit + category dropdowns) so the tool list starts on the first
// screen — stacking the full rail above the list used to push the actual
// tools several viewports below the fold. The main pane searches across
// name/description/category and lists checkbox cards with a readiness badge
// + required-secret chips. Selection is lifted to the parent (auto-saved by
// the Tools tab).
//
// `internal` rows (GET /api/agents/{name} → internal_tools) are first-class
// here: tools the platform grants without a `tools:` entry (memory, notes,
// graph, workspace access, the agent loop, filesystem primitives). Every
// catalog row is reported for every agent; they render as normal cards —
// searchable, listed under All tools and Selected, plus their own Internal
// quick filter — checked while on, and unchecking one records the removal in
// `disabled_tools` exactly like an auto-injected default. Rows whose state
// isn't governed by disabled_tools (a mode condition not met, or a
// top_level_* allow-list exclusion) show a lock plus a note saying what
// enables them.

import { useMemo, useState } from "react";
import { Search, Check, ChevronDown, ChevronRight, KeyRound, Package, Lock, ShieldAlert } from "lucide-react";
import { useT } from "../../core/i18n";
import { useMediaQuery, MOBILE_QUERY } from "../../core/useMediaQuery";
import { STR } from "./strings";
import type { InternalToolRow, ToolBundle, ToolEntry, ToolEnvVar } from "./types";

// A tool's required (non-optional) env vars that are not yet resolvable.
function missingEnv(t: ToolEntry): ToolEnvVar[] {
  const required = (t.requirements?.env_vars || []).filter((e) => !e.optional);
  return required.filter((e) => !e.in_env && !e.in_vault && !e.in_connector);
}
function hasMissingKeys(t: ToolEntry): boolean {
  return (t.requirements?.env_vars || []).some(
    (e) => !e.optional && !e.in_env && !e.in_vault && !e.in_connector,
  );
}

type Filter = string; // "all" | "selected" | "internal" | "missing" | "bundle:<name>" | "<category>"

export function ToolBrowser({
  all,
  bundles,
  selected,
  internal = [],
  onChange,
}: {
  all: ToolEntry[];
  bundles: ToolBundle[];
  selected: string[];
  internal?: InternalToolRow[];
  onChange: (next: string[]) => void;
}) {
  const t = useT(STR);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const sel = useMemo(() => new Set(selected), [selected]);

  const internalByName = useMemo(() => new Map(internal.map((r) => [r.name, r])), [internal]);
  // Internal rows with no registry/catalog entry (memory, workspace_context,
  // agent-loop, filesystem…) become synthesized cards so every tool the agent
  // can run with is visible and searchable — not just the catalog.
  const combined = useMemo(() => {
    const catalogNames = new Set(all.map((tl) => tl.name));
    const synth: ToolEntry[] = internal
      .filter((r) => !catalogNames.has(r.name))
      .map((r) => ({
        name: r.name,
        description: r.description || "",
        category: r.category,
        bundles: [],
      }));
    return [...all, ...synth];
  }, [all, internal]);

  // Derived categories (name → count), sorted by size then alpha — mirrors
  // _tbCategories so the rail reads the same as the original.
  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const tl of combined) counts.set(tl.category || "other", (counts.get(tl.category || "other") || 0) + 1);
    return [...counts.entries()]
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
  }, [combined]);

  const missingCount = useMemo(() => combined.filter(hasMissingKeys).length, [combined]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = combined.filter((tl) => {
      if (filter === "selected") {
        if (!sel.has(tl.name)) return false;
      } else if (filter === "internal") {
        if (!internalByName.has(tl.name)) return false;
      } else if (filter === "missing") {
        if (!hasMissingKeys(tl)) return false;
      } else if (filter.startsWith("bundle:")) {
        if (!(tl.bundles || []).includes(filter.slice(7))) return false;
      } else if (filter !== "all" && (tl.category || "other") !== filter) {
        return false;
      }
      if (!q) return true;
      return (
        tl.name.toLowerCase().includes(q) ||
        (tl.description || "").toLowerCase().includes(q) ||
        (tl.category || "").toLowerCase().includes(q)
      );
    });
    if (filter === "internal") {
      // Present internal rows in catalog order (grouped by category server-side),
      // not catalog-then-synthesized order.
      const order = new Map(internal.map((r, i) => [r.name, i]));
      return [...list].sort((a, b) => (order.get(a.name) ?? 0) - (order.get(b.name) ?? 0));
    }
    return list;
  }, [combined, filter, query, sel, internal, internalByName]);

  function toggle(name: string) {
    if (internalByName.get(name)?.toggleable === false) return;
    onChange(sel.has(name) ? selected.filter((x) => x !== name) : [...selected, name]);
  }
  function addAllVisible() {
    const next = new Set(selected);
    // Locked internal rows aren't user-controllable — leave their state untouched.
    for (const tl of filtered) if (internalByName.get(tl.name)?.toggleable !== false) next.add(tl.name);
    onChange([...next]);
  }
  function toggleBundle(b: ToolBundle, on: boolean) {
    const next = new Set(selected);
    for (const n of b.tools) on ? next.add(n) : next.delete(n);
    onChange([...next]);
  }

  const activeBundle = filter.startsWith("bundle:") ? bundles.find((b) => b.name === filter.slice(7)) : undefined;
  const isCategoryView =
    filter !== "all" && filter !== "selected" && filter !== "internal" && filter !== "missing" && !filter.startsWith("bundle:");
  const title =
    filter === "all"
      ? t("tb.allTools")
      : filter === "selected"
        ? t("tb.selectedTools")
        : filter === "internal"
          ? t("tb.internalTools")
          : filter === "missing"
            ? t("tb.missingTools")
            : activeBundle
              ? t("tb.toolkitTools", { name: activeBundle.name })
              : filter;

  const isMobile = useMediaQuery(MOBILE_QUERY);

  // One definition drives both presentations: desktop rail items and mobile
  // chips must expose the same accessible shape (a button named by the label,
  // holding a `span.badge` count) — the e2e suite locates them by that shape
  // at both viewports.
  const quickFilters: { key: Filter; label: string; count: number; accent?: boolean; warn?: boolean }[] = [
    { key: "all", label: t("tb.all"), count: combined.length },
    { key: "selected", label: t("tb.selected"), count: selected.length, accent: true },
    ...(internal.length > 0 ? [{ key: "internal", label: t("tb.internal"), count: internal.length }] : []),
    { key: "missing", label: t("tb.missingKeys"), count: missingCount, warn: missingCount > 0 },
  ];

  const searchBox = (
    <div style={{ position: "relative" }}>
      <input
        className="input"
        placeholder={t("tb.searchPlaceholder")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{ paddingLeft: 32 }}
      />
      <span style={{ position: "absolute", left: 11, top: "50%", transform: "translateY(-50%)", color: "var(--text-4)", display: "inline-flex" }}>
        <Search size={14} strokeWidth={1.75} />
      </span>
    </div>
  );

  return (
    <div className="tool-browser">
      {isMobile ? (
        /* ── mobile filter bar: search first, then one chip row + two
              dropdowns — the tool list starts on the first screen ──────── */
        <div className="tool-browser-filters">
          {searchBox}
          <div className="tool-browser-chips" role="group" aria-label={t("tb.quickFilters")}>
            {quickFilters.map((f) => (
              <button
                key={f.key}
                className="tool-browser-chip"
                data-active={filter === f.key || undefined}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
                <span
                  className="badge"
                  style={{ padding: "0 6px", color: f.warn ? "var(--warn)" : f.accent ? "var(--accent)" : "var(--text-3)" }}
                >
                  {f.count}
                </span>
              </button>
            ))}
          </div>
          <div className="row" style={{ gap: 8 }}>
            {bundles.length > 0 && (
              <select
                className="input"
                aria-label={t("tb.toolkits")}
                value={activeBundle ? activeBundle.name : ""}
                data-filled={activeBundle ? true : undefined}
                onChange={(e) => setFilter(e.target.value ? `bundle:${e.target.value}` : "all")}
                style={{ flex: 1, minWidth: 0 }}
              >
                <option value="">{t("tb.allToolkits")}</option>
                {bundles.map((b) => (
                  <option key={b.name} value={b.name}>
                    {b.name} ({b.tools.length})
                  </option>
                ))}
              </select>
            )}
            <select
              className="input"
              aria-label={t("tb.categories")}
              value={isCategoryView ? filter : ""}
              data-filled={isCategoryView ? true : undefined}
              onChange={(e) => setFilter(e.target.value || "all")}
              style={{ flex: 1, minWidth: 0 }}
            >
              <option value="">{t("tb.allCategories")}</option>
              {categories.map((c) => (
                <option key={c.key} value={c.key}>
                  {c.key} ({c.count})
                </option>
              ))}
            </select>
          </div>
        </div>
      ) : (
        /* ── desktop left rail ─────────────────────────────────────── */
        <div className="tool-browser-rail">
          <RailSection label={t("tb.quickFilters")}>
            {quickFilters.map((f) => (
              <RailItem
                key={f.key}
                label={f.label}
                count={f.count}
                accent={f.accent}
                warn={f.warn}
                active={filter === f.key}
                onClick={() => setFilter(f.key)}
              />
            ))}
          </RailSection>

          {bundles.length > 0 && (
            <CollapsibleRailList
              label={t("tb.toolkits")}
              hint={t("tb.toolkitsHint")}
              items={bundles.map((b) => ({ key: `bundle:${b.name}`, label: b.name, count: b.tools.length }))}
              activeKey={filter}
              onSelect={setFilter}
            />
          )}

          <CollapsibleRailList
            label={t("tb.categories")}
            items={categories.map((c) => ({ key: c.key, label: c.key, count: c.count }))}
            activeKey={filter}
            onSelect={setFilter}
          />
        </div>
      )}

      {/* ── main pane ──────────────────────────────────────────────── */}
      <div className="tool-browser-main">
        {!isMobile && <div style={{ marginBottom: 12 }}>{searchBox}</div>}

        {activeBundle && (
          <div className="card" style={{ padding: "12px 14px", marginBottom: 12 }}>
            <div className="row" style={{ justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
              <div>
                <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  <strong style={{ fontSize: 14 }}>{t("tb.toolkitName", { name: activeBundle.name })}</strong>
                  {activeBundle.privileged && (
                    <span className="badge row" style={{ gap: 3, padding: "0 6px", color: "var(--warn)" }}>
                      <ShieldAlert size={11} strokeWidth={1.75} /> {t("tb.privilegedChip")}
                    </span>
                  )}
                </div>
                <div className="muted" style={{ fontSize: 12, marginTop: 1 }}>
                  {t("count.tool" + (activeBundle.tools.length === 1 ? "" : "s"), { n: String(activeBundle.tools.length) })}
                </div>
                {activeBundle.privileged && (
                  <div className="muted" style={{ fontSize: 11, marginTop: 4, lineHeight: 1.4, color: "var(--warn)" }}>
                    {t("tb.privilegedNote")}
                  </div>
                )}
              </div>
              <div className="row" style={{ gap: 6 }}>
                <button className="btn" style={{ padding: "4px 10px", fontSize: 12 }} onClick={() => toggleBundle(activeBundle, false)}>
                  {t("tb.clearToolkit")}
                </button>
                <button
                  className="btn primary"
                  style={{ padding: "4px 10px", fontSize: 12 }}
                  disabled={activeBundle.tools.every((n) => sel.has(n))}
                  onClick={() => toggleBundle(activeBundle, true)}
                >
                  {activeBundle.tools.every((n) => sel.has(n)) ? t("tb.alreadyAdded") : t("tb.addAll")}
                </button>
              </div>
            </div>
            {activeBundle.requirements?.guidance && (
              <div className="muted" style={{ fontSize: 12, marginTop: 8, lineHeight: 1.5 }}>
                {activeBundle.requirements.guidance}
              </div>
            )}
          </div>
        )}

        {filter === "internal" && (
          <p className="muted" style={{ fontSize: 11, margin: "0 0 10px", lineHeight: 1.45 }}>
            {t("tb.internalHint")}
          </p>
        )}

        <div className="row" style={{ justifyContent: "space-between", marginBottom: 10, gap: 8 }}>
          <div className="row" style={{ gap: 8 }}>
            <strong style={{ fontSize: 14 }}>{title}</strong>
            <span className="muted" style={{ fontSize: 12 }}>
              {t("tb.matches", { n: String(filtered.length) })}
            </span>
          </div>
          {isCategoryView && filtered.length > 0 && (
            <button className="btn ghost" style={{ padding: "3px 9px", fontSize: 12 }} onClick={addAllVisible}>
              {t("tb.addAllCategory")}
            </button>
          )}
        </div>

        {filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: "40px 20px", color: "var(--text-3)" }}>
            <div style={{ fontWeight: 600, color: "var(--text-2)" }}>{t("tb.noMatchTitle")}</div>
            <div style={{ fontSize: 13, marginTop: 4 }}>{t("tb.noMatchSub")}</div>
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))", gap: 10 }}>
            {filtered.map((tl) => (
              <ToolCard
                key={tl.name}
                tool={tl}
                internalRow={internalByName.get(tl.name)}
                checked={sel.has(tl.name)}
                onToggle={() => toggle(tl.name)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function RailSection({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 3, fontWeight: 600 }}>
        {label}
        {hint ? <span style={{ textTransform: "none", letterSpacing: 0, fontWeight: 400, marginLeft: 6 }}>{hint}</span> : null}
      </div>
      {children}
    </div>
  );
}

// How many rows a long rail section shows before folding behind "Show all".
// Sections at most 2 over the cutoff render whole — a "Show all (14)" hiding
// two rows is worse than the rows.
const RAIL_TRUNCATE = 12;

// A rail section for the unbounded lists (toolkits, categories): header
// collapses the whole section, and past RAIL_TRUNCATE rows the tail folds
// behind a "Show all (N)" toggle. The active item is always kept visible even
// when it lives in the folded tail, so collapsing never hides the filter the
// list is currently showing.
function CollapsibleRailList({
  label,
  hint,
  items,
  activeKey,
  onSelect,
}: {
  label: string;
  hint?: string;
  items: { key: string; label: string; count: number }[];
  activeKey: string;
  onSelect: (key: string) => void;
}) {
  const t = useT(STR);
  const [open, setOpen] = useState(true);
  const [showAll, setShowAll] = useState(false);
  const truncatable = items.length > RAIL_TRUNCATE + 2;
  let visible = items;
  if (truncatable && !showAll) {
    visible = items.slice(0, RAIL_TRUNCATE);
    const active = items.find((i) => i.key === activeKey);
    if (active && !visible.includes(active)) visible = [...visible, active];
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <button
        className="muted row"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{
          gap: 4,
          fontSize: 11,
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          marginBottom: 3,
          fontWeight: 600,
          cursor: "pointer",
          background: "transparent",
          border: "none",
          padding: 0,
          textAlign: "left",
        }}
      >
        {open ? <ChevronDown size={12} strokeWidth={2} /> : <ChevronRight size={12} strokeWidth={2} />}
        {label}
        {hint ? <span style={{ textTransform: "none", letterSpacing: 0, fontWeight: 400 }}>{hint}</span> : null}
        {!open && (
          <span className="badge" style={{ padding: "0 6px", color: "var(--text-3)" }}>
            {items.length}
          </span>
        )}
      </button>
      {open &&
        visible.map((i) => (
          <RailItem key={i.key} label={i.label} count={i.count} active={activeKey === i.key} onClick={() => onSelect(i.key)} />
        ))}
      {open && truncatable && (
        <button
          className="btn ghost"
          style={{ padding: "3px 9px", fontSize: 12, alignSelf: "flex-start" }}
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? t("tb.showFewer") : t("tb.showAllN", { n: String(items.length) })}
        </button>
      )}
    </div>
  );
}

function RailItem({
  label,
  count,
  active,
  accent,
  warn,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  accent?: boolean;
  warn?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="row"
      style={{
        justifyContent: "space-between",
        gap: 8,
        padding: "6px 9px",
        borderRadius: "var(--r-sm)",
        border: "1px solid transparent",
        background: active ? "var(--surface-3)" : "transparent",
        color: active ? "var(--text)" : "var(--text-2)",
        cursor: "pointer",
        fontSize: 13,
        textAlign: "left",
        width: "100%",
        fontWeight: active ? 600 : 400,
      }}
    >
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
      <span
        className="badge"
        style={{
          padding: "0 7px",
          color: warn ? "var(--warn)" : accent ? "var(--accent)" : "var(--text-3)",
        }}
      >
        {count}
      </span>
    </button>
  );
}

function ToolCard({
  tool,
  internalRow,
  checked,
  onToggle,
}: {
  tool: ToolEntry;
  internalRow?: InternalToolRow;
  checked: boolean;
  onToggle: () => void;
}) {
  const t = useT(STR);
  const envVars = tool.requirements?.env_vars || [];
  const missing = missingEnv(tool);
  const ready = envVars.length === 0 || missing.length === 0;
  const locked = internalRow?.toggleable === false;
  const description = tool.description || internalRow?.description || "";

  return (
    // A div, deliberately NOT a <label>: a label wrapping its checkbox
    // re-dispatches clicks to the input (and the forwarded click bubbles back
    // through the label), so one tap toggled the tool twice — off and straight
    // back on — on touch devices. With a plain div there is no native
    // forwarding: the card body is one click target and the checkbox itself
    // (propagation-stopped) is the other, each firing onToggle exactly once.
    <div
      className="card"
      data-tool-card={tool.name}
      title={locked ? internalRow?.note || t("tb.lockedTitle") : undefined}
      onClick={() => {
        if (!locked) onToggle();
      }}
      style={{
        padding: "11px 12px",
        display: "flex",
        gap: 9,
        alignItems: "flex-start",
        cursor: locked ? "default" : "pointer",
        borderColor: checked ? "var(--accent)" : !ready ? "color-mix(in srgb, var(--warn) 40%, var(--border))" : undefined,
        background: checked ? "color-mix(in srgb, var(--accent) 8%, var(--surface))" : undefined,
        opacity: internalRow && !checked ? 0.75 : 1,
      }}
    >
      {locked ? (
        <Lock size={14} strokeWidth={1.75} style={{ marginTop: 3, flexShrink: 0, color: "var(--text-4)" }} />
      ) : (
        <input
          type="checkbox"
          checked={checked}
          aria-label={tool.name}
          onChange={onToggle}
          onClick={(e) => e.stopPropagation()}
          style={{ marginTop: 3, flexShrink: 0, accentColor: "var(--accent)" }}
        />
      )}
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <span style={{ fontWeight: 600, fontSize: 13, fontFamily: "var(--font-mono)" }}>{tool.name}</span>
          {tool.category && <span className="badge" style={{ padding: "0 6px" }}>{tool.category}</span>}
          {internalRow && (
            <span className="badge" style={{ padding: "0 6px", color: "var(--accent)" }}>
              {t("tb.internalChip")}
            </span>
          )}
          {tool.privileged && (
            <span
              className="badge row"
              style={{ gap: 3, padding: "0 6px", color: "var(--warn)" }}
              title={t("tb.privilegedNote")}
            >
              <ShieldAlert size={11} strokeWidth={1.75} /> {t("tb.privilegedChip")}
            </span>
          )}
          {envVars.length > 0 &&
            (ready ? (
              <span className="badge row" style={{ gap: 3, color: "var(--success)", padding: "0 6px" }}>
                <Check size={11} strokeWidth={2} /> {t("tb.ready")}
              </span>
            ) : (
              <span className="badge row" style={{ gap: 3, color: "var(--warn)", padding: "0 6px" }}>
                {t("tb.nMissing", { n: String(missing.length) })}
              </span>
            ))}
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 3, lineHeight: 1.45 }}>
          {description || t("tb.noDescription")}
        </div>
        {internalRow?.note && (
          <div className="muted" style={{ fontSize: 11, marginTop: 4, lineHeight: 1.4, fontStyle: "italic" }}>
            {internalRow.note}
          </div>
        )}
        {/* Spelled out under the row, not only in the badge's title: the reason
            a fleet-wide tool must not be a stray click does not fit in a chip,
            and a tooltip is invisible on touch. */}
        {tool.privileged && (
          <div className="muted" style={{ fontSize: 11, marginTop: 4, lineHeight: 1.4, color: "var(--warn)" }}>
            {t("tb.privilegedNote")}
          </div>
        )}
        {(envVars.length > 0 || (tool.requirements?.packages || []).length > 0) && (
          <div className="row" style={{ gap: 4, marginTop: 6, flexWrap: "wrap" }}>
            {envVars.map((e) => (
              <EnvChip key={e.name} env={e} />
            ))}
            {(tool.requirements?.packages || []).map((p) => (
              <span key={p} className="badge row" style={{ gap: 3, padding: "0 6px", color: "var(--text-3)" }}>
                <Package size={11} strokeWidth={1.75} /> {p}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function EnvChip({ env }: { env: ToolEnvVar }) {
  const t = useT(STR);
  const set = env.in_env || env.in_vault || env.in_connector;
  const color = set ? "var(--success)" : env.optional ? "var(--text-3)" : "var(--danger)";
  const titleParts: string[] = [];
  if (env.in_env) titleParts.push(t("tb.envSetInEnv"));
  if (env.in_vault) titleParts.push(t("tb.envStoredInVault"));
  if (env.in_connector) titleParts.push(t("tb.envInConnector"));
  if (!set && env.optional) titleParts.push(t("tb.envOptional"));
  if (!set && !env.optional) titleParts.push(t("tb.envRequiredVault"));
  return (
    <span
      className="badge row"
      title={titleParts.join(" · ")}
      style={{ gap: 3, padding: "0 6px", color, fontFamily: "var(--font-mono)", fontSize: 11 }}
    >
      {set ? <Check size={11} strokeWidth={2} /> : <KeyRound size={10} strokeWidth={1.75} />}
      {env.name}
      {env.optional ? <span style={{ opacity: 0.7 }}>{t("tb.envOptTag")}</span> : null}
    </span>
  );
}
