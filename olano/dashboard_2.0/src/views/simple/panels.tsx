// The dashboard views the simple shell also offers, under the rail's **More**
// section.
//
// The shell is still a chat app: agents and conversations are what the rail is
// for, and these five sit below a divider precisely because they are a second
// tier. But "simple" was never meant to mean "cannot see your own deployment" —
// someone who lives in this shell still needs the overview, what their agents
// cost, the issue list, what Cortex has been doing and the files it wrote. The
// alternative was telling them to switch to the full dashboard, which is the
// one thing this shell exists to avoid.
//
// They are the *same* views the dashboard routes to, not simplified copies:
// one definition each, so a fix lands in both shells at once.
//
// Selection is driven by the URL, not by component state, so Back works, a
// reload keeps the panel, a deep link (`/files?path=…`) opens where it points,
// and the notification bell's "View all" — a plain <Link to="/overview"> — lands
// somewhere real in this shell too. A path outside this table falls back to the
// conversation, which is what the shell shows at "/".

import { lazy, type LazyExoticComponent, type ComponentType } from "react";
import type { LucideIcon } from "lucide-react";
import { loadRoute } from "../../core/routeChunks";
import { NAV } from "../../core/nav";

// Overview alone is NOT route-split in the dashboard (it is the landing page and
// the recovery surface), so it has no ROUTE_LOADERS entry to reuse; importing
// the module directly keeps that property here.
const OverviewPanel = lazy(() =>
  import("../overview/OverviewView").then((m) => ({ default: m.OverviewView })),
);
const InsightsPanel = lazy(() =>
  loadRoute("/insights").then((m) => ({ default: m.InsightsView })),
);
const IssuesPanel = lazy(() => loadRoute("/issues").then((m) => ({ default: m.IssuesView })));
const CortexPanel = lazy(() => loadRoute("/cortex").then((m) => ({ default: m.CortexView })));
const FilesPanel = lazy(() => loadRoute("/files").then((m) => ({ default: m.FilesView })));

export type SimplePanel = {
  /** The dashboard route this panel is. Also the shell's selector. */
  to: string;
  /** i18n key for the label — the central dictionary's, so both shells agree. */
  key: string;
  Icon: LucideIcon;
  View: LazyExoticComponent<ComponentType>;
  /**
   * The deployment-settings gate (`tab_<name>`), or null for a panel that has
   * none. Same rule the dashboard's TabGuard applies: default-on, and only an
   * explicit `false` hides it. Overview is never gated — it is the recovery
   * surface in both shells.
   */
  capability: string | null;
};

// Label and icon come from the dashboard's own NAV table rather than being
// retyped, so the two shells can never drift on either.
function fromNav(to: string, View: SimplePanel["View"], capability: string | null): SimplePanel {
  const item = NAV.find((n) => n.to === to);
  if (!item) throw new Error(`simple panel ${to} has no NAV entry`);
  return { to, key: item.key, Icon: item.Icon, View, capability };
}

export const SIMPLE_PANELS: SimplePanel[] = [
  fromNav("/overview", OverviewPanel, null),
  fromNav("/insights", InsightsPanel, "tab_insights"),
  fromNav("/issues", IssuesPanel, "tab_issues"),
  fromNav("/cortex", CortexPanel, "tab_cortex"),
  fromNav("/files", FilesPanel, "tab_files"),
];

/** The panel a path selects, or null for "show the conversation". Matched on
 *  the first segment: a panel's own address can go deeper (/cortex/<agent>/ideas,
 *  /files/<agent>, /insights/drill) and is still that panel. */
export function panelForPath(pathname: string): SimplePanel | null {
  const first = `/${pathname.split("/").filter(Boolean)[0] ?? ""}`;
  return SIMPLE_PANELS.find((p) => p.to === first) ?? null;
}
