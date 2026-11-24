// A searchable popover multi-select over the agent roster — "which agents'
// things am I looking at" — for views that show the whole fleet at once
// (Calendar, Cortex).
//
// Lifted out of the Calendar view the day Cortex needed the same control.
// Mirrors the chat ModelPicker (.model-picker / .model-menu / .model-search /
// .model-list / .model-item) so it stays usable with dozens of agents, unlike
// an inline checkbox row. Toggling a row keeps the popover open (it is a
// multi-select); search + Select all / Clear scale the long-list case; Escape
// and an outside click close it and reset the query.
//
// The state is a HIDDEN set, never a shown set: an empty set means "everyone",
// so an agent created after the choice was made is visible by default rather
// than silently filtered out. Text comes in through `labels` so each view
// keeps its own dictionary and this component has none.
//
// The popover is portaled out of this wrapper by `AnchoredPopover` (so it is
// clamped into the viewport instead of running off the edge from a trigger near
// one), which is why its rows are addressed through `agent-filter-menu` rather
// than as descendants of `agent-filter`.

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Search, Users } from "lucide-react";
import { AnchoredPopover } from "./AnchoredPopover";

/** Natural width of the popover; `AnchoredPopover` caps it at the viewport. */
const MENU_MIN_W = 300;

export interface AgentFilterItem {
  key: string;
  label: string;
  /** A swatch before the name — the Calendar's per-agent colour. */
  color?: string | null;
  /** Something drawn before the name instead of the swatch (an avatar). */
  mark?: ReactNode;
}

export interface AgentFilterLabels {
  /** The trigger's tooltip: what the control filters. */
  title: string;
  /** Trigger text while nothing is hidden. */
  allShown: string;
  /** Trigger text while everything is hidden. */
  noneShown: string;
  /** Trigger text otherwise; also the count line inside the popover. */
  summary: (shown: number, total: number) => string;
  search: string;
  selectAll: string;
  clear: string;
  noMatch: string;
}

export function AgentFilter({
  items,
  hidden,
  onToggle,
  onAll,
  onNone,
  labels,
  className,
}: {
  items: AgentFilterItem[];
  hidden: Set<string>;
  onToggle: (key: string) => void;
  onAll: () => void;
  onNone: () => void;
  labels: AgentFilterLabels;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);

  // Reset the query whenever the popover closes so it reopens clean.
  useEffect(() => {
    if (!open) setQ("");
  }, [open]);

  const total = items.length;
  const shown = items.filter((it) => !hidden.has(it.key)).length;
  const query = q.trim().toLowerCase();
  const filtered = query ? items.filter((it) => it.label.toLowerCase().includes(query)) : items;

  const summary =
    shown >= total ? labels.allShown : shown === 0 ? labels.noneShown : labels.summary(shown, total);
  const active = shown < total;

  return (
    <div className={`model-picker${className ? ` ${className}` : ""}`} ref={ref} data-testid="agent-filter">
      <button
        type="button"
        className={`chat-chip${active ? " is-on" : ""}`}
        style={shown === 0 ? { background: "var(--danger-soft)", color: "var(--danger)" } : undefined}
        title={labels.title}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Users size={12} strokeWidth={2} /> {summary}
        <ChevronDown size={11} strokeWidth={2} style={{ marginLeft: 2, opacity: 0.7 }} />
      </button>
      <AnchoredPopover
        anchorRef={ref}
        open={open}
        onClose={close}
        minWidth={MENU_MIN_W}
        className="model-menu"
        testId="agent-filter-menu"
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
            placeholder={labels.search}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div className="row" style={{ justifyContent: "space-between", padding: "0 4px", fontSize: 11 }}>
          <span className="muted">{labels.summary(shown, total)}</span>
          <span className="row" style={{ gap: 8 }}>
            <button type="button" className="btn ghost" style={{ padding: "2px 7px", fontSize: 11 }} onClick={onAll}>
              {labels.selectAll}
            </button>
            <button type="button" className="btn ghost" style={{ padding: "2px 7px", fontSize: 11 }} onClick={onNone}>
              {labels.clear}
            </button>
          </span>
        </div>
        <div className="model-list" role="listbox" aria-multiselectable>
          {filtered.map((it) => {
            const checked = !hidden.has(it.key);
            return (
              <button
                key={it.key || "__default"}
                type="button"
                role="option"
                aria-selected={checked}
                className={`model-item${checked ? " selected" : ""}`}
                style={{ alignItems: "center" }}
                onClick={() => onToggle(it.key)}
              >
                <input type="checkbox" checked={checked} readOnly tabIndex={-1} style={{ pointerEvents: "none" }} />
                {it.mark ??
                  (it.color ? (
                    <span style={{ width: 9, height: 9, borderRadius: "50%", background: it.color, flexShrink: 0 }} />
                  ) : null)}
                <span className="model-item-name">{it.label}</span>
              </button>
            );
          })}
          {filtered.length === 0 && <div className="model-empty">{labels.noMatch}</div>}
        </div>
      </AnchoredPopover>
    </div>
  );
}
