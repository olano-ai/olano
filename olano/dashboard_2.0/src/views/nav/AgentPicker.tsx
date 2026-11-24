// The app-wide agent selector.
//
// One store, any number of pickers: every instance reads and writes
// `useCurrentAgent`, so a picker at the top of an agent-scoped view (Memory,
// Notes, Knowledge, Terminal) and the sidebar's agent list (BotNav, which
// writes the same selection when a row is clicked) can never disagree. The
// sidebar no longer mounts the `sidebar` variant — the list of agents IS the
// selector there — so the `inline` form is the one on screen; the variant
// stays for any shell that wants a compact dropdown.
//
// Agents are split into an **Online** (running) group and a collapsible
// **Offline** group (everything not running) so a long bench of idle teammates
// never buries the handful that are live. Offline starts collapsed; when nothing
// is online it's shown flat (there'd be nothing left to reveal otherwise). The
// collapse preference is shared by every picker — it is one preference about
// the roster, not about a place in the UI.
//
// Lives under views/nav rather than ui/ because it depends on the agents view's
// status helpers; ui/ importing from views/ would invert the layering.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useCurrentAgent } from "../../core/currentAgent";
import { useI18n } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import type { Agent } from "../../core/types";
import { AgentsIcon, CheckIcon, ChevronDownIcon } from "../../ui/icons";
import { agentStatus, statusColor } from "../agents/helpers";

export interface AgentPickerProps {
  /**
   * `sidebar` (default) is the left nav's row: an agents icon, then the
   * trigger, menu anchored below and spanning the sidebar. `inline` is the
   * compact form a view's topbar hosts: no leading icon, a 34px trigger, the
   * menu anchored below-left and sized to its content.
   */
  variant?: "sidebar" | "inline";
  /** Trailing content per row, drawn before the selected check — the Terminal
   *  uses it for a per-agent count of open shells. */
  rowExtra?: (a: Agent) => ReactNode;
  /** Inline only: with an empty roster render a disabled trigger carrying this
   *  label instead of nothing, so the topbar keeps its shape. */
  emptyLabel?: string;
  className?: string;
}

export function AgentPicker({ variant = "sidebar", rowExtra, emptyLabel, className }: AgentPickerProps) {
  const { agents, agent, setAgent, current } = useCurrentAgent();
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [offlineCollapsed, setOfflineCollapsed] = usePersistentState<boolean>(
    "agentPicker.offlineCollapsed",
    true,
  );
  const ref = useRef<HTMLDivElement>(null);
  const inline = variant === "inline";

  // Close on outside click + Escape, mirroring the profile menu.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const { online, offline } = useMemo(() => {
    const on: Agent[] = [];
    const off: Agent[] = [];
    for (const a of agents) (agentStatus(a) === "running" ? on : off).push(a);
    return { online: on, offline: off };
  }, [agents]);

  const rootClass = [inline ? "agent-picker agent-picker-inline" : "sidebar-agent", className]
    .filter(Boolean)
    .join(" ");
  const triggerClass = inline ? "agent-picker-trigger agent-picker-trigger-inline" : "agent-picker-trigger";
  const menuClass = inline ? "menu agent-menu agent-menu-inline" : "menu agent-menu";

  if (!agents.length) {
    if (!inline || !emptyLabel) return null;
    return (
      <div className={rootClass} data-testid="agent-picker" data-variant={variant}>
        <button type="button" className={triggerClass} disabled aria-label={t("nav.agents")} data-agent="">
          <span className="agent-picker-current">
            <span className="agent-picker-name">{emptyLabel}</span>
          </span>
        </button>
      </div>
    );
  }

  function choose(name: string) {
    setAgent(name);
    setOpen(false);
  }

  // The Offline group only collapses when there's an Online group above it to
  // fall back to; with nothing online it stays open so the menu is never empty.
  const collapsible = online.length > 0 && offline.length > 0;
  const showOffline = !collapsible || !offlineCollapsed;
  const currentStatus = current ? agentStatus(current) : "configured";

  function renderRow(a: Agent) {
    const selected = a.name === agent;
    return (
      <button
        key={a.name}
        className={`menu-item agent-item${selected ? " active" : ""}`}
        role="option"
        aria-selected={selected}
        onClick={() => choose(a.name)}
      >
        <span className="agent-status-dot" style={{ background: statusColor(agentStatus(a)) }} />
        {a.emoji && <span className="agent-item-emoji">{a.emoji}</span>}
        <span className="agent-item-name">{a.name}</span>
        {rowExtra?.(a)}
        {selected && <CheckIcon size={14} className="agent-item-check" />}
      </button>
    );
  }

  function groupLabel(key: string, count: number) {
    return (
      <div className="agent-group-label">
        <span className="agent-group-chev-spacer" />
        <span>{t(key)}</span>
        <span className="agent-group-count">{count}</span>
      </div>
    );
  }

  return (
    <div
      className={rootClass}
      ref={ref}
      title={inline ? undefined : t("nav.agents")}
      data-testid="agent-picker"
      data-variant={variant}
    >
      {!inline && <AgentsIcon size={15} />}
      <button
        type="button"
        className={triggerClass}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t("nav.agents")}
        data-agent={agent}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="agent-picker-current">
          <span className="agent-status-dot" style={{ background: statusColor(currentStatus) }} />
          {current?.emoji && <span className="agent-item-emoji">{current.emoji}</span>}
          <span className="agent-picker-name">{agent || t("nav.agents")}</span>
        </span>
        <ChevronDownIcon className="agent-picker-chev" size={14} strokeWidth={2} />
      </button>

      {open && (
        <div className={menuClass} role="listbox" aria-label={t("nav.agents")}>
          {online.length > 0 && (
            <div className="agent-group">
              {groupLabel("agent.online", online.length)}
              {online.map(renderRow)}
            </div>
          )}
          {offline.length > 0 && (
            <div className="agent-group">
              {collapsible ? (
                <button
                  type="button"
                  className={`agent-group-label agent-group-toggle${showOffline ? "" : " collapsed"}`}
                  onClick={() => setOfflineCollapsed((c) => !c)}
                  aria-expanded={showOffline}
                  title={showOffline ? t("agent.offline.collapse") : t("agent.offline.expand")}
                >
                  <ChevronDownIcon className="agent-group-chev" size={13} strokeWidth={2} />
                  <span>{t("agent.offline")}</span>
                  <span className="agent-group-count">{offline.length}</span>
                </button>
              ) : (
                groupLabel("agent.offline", offline.length)
              )}
              {showOffline && offline.map(renderRow)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
