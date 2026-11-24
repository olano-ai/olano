import { Brain, ChevronRight } from "lucide-react";
import { usePersistentState } from "../../core/usePersistentState";
import { useT } from "../../core/i18n";
import { AutonomyPanel, type AutonomyHandle } from "./AutonomyPanel";
import { CortexGuidancePanel } from "./CortexGuidancePanel";
import { countOverrides } from "./helpers";
import { ProgressBar } from "./ui";
import { STR } from "./strings";
import type { CognitionStatusResponse } from "./types";
import type { RefObject } from "react";

/**
 * Everything you can *configure* about Cortex, in one collapsed card.
 *
 * The page carried two peer cards before this: "Autonomous Cortex" (always
 * expanded, ~1,400px of two-tier table + an eleven-row phase list) and
 * "Customize your Cortex Engine". Both headings promise engine settings, so
 * finding a knob meant opening both and comparing — and on a phone the pair
 * pushed the tabs the page actually exists for (Pulse, Ideas, Projects…) three
 * screens below the fold, every visit, whether or not you came to change
 * anything.
 *
 * One card now, collapsed by default, with three named parts inside: **how it
 * runs** (the two-tier settings table), **phases & schedule**, and **creative
 * direction** (CORTEX.md). The header carries the live state — engine on/off,
 * pace, today's deep-run spend, and whether this agent deviates from the fleet
 * — so the common case (checking, not changing) needs no click at all.
 *
 * The body stays MOUNTED while collapsed, hidden by `<details>` rather than
 * unmounted: `AutonomyPanel` owns the `/api/cognition/status` poll that feeds
 * the header's own summary, the "Think now" button's live state, and the
 * over-budget confirmations in CortexView. Unmounting it would silently take
 * all three away for anyone who never opens the card.
 */
export function CortexSettingsPanel({
  agent,
  isAdmin,
  status,
  autonomyRef,
  onPhaseRan,
  onStatus,
  onStatusError,
}: {
  agent: string;
  isAdmin: boolean;
  /** The latest cognition status, lifted by CortexView from `onStatus`. */
  status: CognitionStatusResponse | null;
  autonomyRef: RefObject<AutonomyHandle | null>;
  onPhaseRan: () => void;
  onStatus: (s: CognitionStatusResponse) => void;
  onStatusError: () => void;
}) {
  const t = useT(STR);
  const [open, setOpen] = usePersistentState("cortex.settings.open", false);

  // Header summary — the resolved answer for THIS agent, straight from the
  // backend's own gate functions (`resolved`), never re-derived here.
  const res = status?.resolved;
  const cap = status?.deep_run_cap ?? 0;
  const used = status?.deep_runs_today ?? 0;
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  const over = cap > 0 && used >= cap;
  const overrideCount = countOverrides(status?.overrides);

  const summary: string[] = [];
  if (res) {
    summary.push(res.enabled ? t("set.sum.on") : t("set.sum.off"));
    // Pace and spend only mean anything while the engine is on.
    if (res.enabled) {
      summary.push(res.intensity);
      if (!res.autonomous) summary.push(t("set.sum.manual"));
    }
  }

  return (
    <section className="card cortex-settings">
      <details open={open}>
        <summary
          className="phase-disclosure cortex-settings-head"
          title={t("set.title.help")}
          onClick={(e) => {
            e.preventDefault();
            setOpen((o) => !o);
          }}
        >
          <ChevronRight size={15} strokeWidth={2.25} aria-hidden />
          <Brain size={15} strokeWidth={1.75} aria-hidden />
          <span className="cortex-settings-title">{t("set.title")}</span>
          {summary.length > 0 ? (
            <span className="cortex-settings-summary muted">{summary.join(" · ")}</span>
          ) : null}
          {overrideCount > 0 ? (
            <span className="badge" title={t("set.badge.overrides.title", { agent })}>
              {overrideCount === 1
                ? t("set.badge.override")
                : t("set.badge.overrides", { count: String(overrideCount) })}
            </span>
          ) : null}
          {/* Live spend, readable without opening the card — the one number
              here that changes on its own. */}
          {isAdmin && cap > 0 ? (
            <span className="cortex-settings-meter" title={t("auto.budget.title")}>
              <span
                className="muted"
                style={{
                  fontSize: 11,
                  color: over ? "var(--accent)" : undefined,
                  fontWeight: over ? 600 : undefined,
                }}
              >
                {t("auto.budget.label", {
                  used: String(used),
                  cap: String(cap),
                  left: String(status?.deep_runs_remaining ?? 0),
                })}
              </span>
              <ProgressBar pct={pct} />
            </span>
          ) : null}
        </summary>

        <div className="cortex-settings-body">
          {isAdmin ? (
            <AutonomyPanel
              ref={autonomyRef}
              agent={agent}
              onPhaseRan={onPhaseRan}
              onStatus={onStatus}
              onStatusError={onStatusError}
            />
          ) : null}
          <CortexGuidancePanel agent={agent} sectionActive={open} />
        </div>
      </details>
    </section>
  );
}
