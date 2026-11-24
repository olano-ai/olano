// Where the dashboard's Help row points.
//
// The user guide is published at docs.olano.ai with one page per dashboard
// tab (docs/user-guide/dashboard/*.md in the olano repository), so Help can
// open the page for the screen the person is looking at rather than the
// guide's front door. The map is keyed by the first path segment of the
// route, which is what the nav table (core/nav.ts) uses as a tab's `to`.
// A route with no page of its own, or one not listed here, opens the guide's
// home page: a wrong deep link would be worse than none.
//
// `latest` is the newest release's guide. The dashboard does not pin its own
// minor yet; when it does, the base is the one place to change.

export const DOCS_BASE = "https://docs.olano.ai/latest/";

const PAGE_FOR_ROUTE: Readonly<Record<string, string>> = {
  "/overview": "dashboard/overview/",
  "/insights": "dashboard/insights/",
  "/chat": "dashboard/chat/",
  "/agents": "dashboard/agents/",
  "/cortex": "dashboard/cortex/",
  "/scheduler": "dashboard/scheduler/",
  "/issues": "dashboard/issues/",
  "/revisions": "dashboard/revisions/",
  "/calendar": "dashboard/calendar/",
  "/files": "dashboard/files/",
  "/memory": "dashboard/memory/",
  "/notes": "dashboard/notes/",
  "/knowledge": "dashboard/knowledge/",
  "/tools": "dashboard/tools/",
  "/providers": "dashboard/providers/",
  "/connections": "dashboard/connections/",
  "/vault": "dashboard/vault/",
  "/activity": "dashboard/agent-to-agent/",
  "/orgchart": "dashboard/org-chart/",
  "/logs": "dashboard/logs/",
  "/terminal": "dashboard/terminal/",
  "/playground": "dashboard/playground/",
  "/config": "dashboard/config/",
  "/admin": "dashboard/admin/",
};

/** The docs page for the dashboard route at `pathname`, or the guide's home. */
export function docsUrlFor(pathname: string): string {
  const first = pathname.split("?")[0].split("#")[0].split("/").filter(Boolean)[0];
  const page = first ? PAGE_FOR_ROUTE[`/${first}`] : undefined;
  return page ? `${DOCS_BASE}${page}` : DOCS_BASE;
}
