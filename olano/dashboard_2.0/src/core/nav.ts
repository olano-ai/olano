// The dashboard's navigation table.
//
// It lives here rather than in `views/Layout.tsx` because two shells read it and
// only one of them wants the dashboard shell: the simple view's **More** section
// takes its labels and icons from these entries, and importing them from Layout
// would drag the whole operator console — the command palette, every badge
// store, the notification bell — into the simple chunk, which is exactly what
// route-splitting the two shells exists to prevent. Layout re-exports both
// bindings, so `views/Layout` remains a valid import site for anything that is
// already paying for that module.

import type { LucideIcon } from "lucide-react";
import {
  OverviewIcon,
  InsightsIcon,
  ChatIcon,
  AgentsIcon,
  CortexIcon,
  SchedulerIcon,
  CalendarIcon,
  FilesIcon,
  MemoryIcon,
  NotesIcon,
  KnowledgeIcon,
  ToolsIcon,
  ProvidersIcon,
  ConnectionsIcon,
  VaultIcon,
  ActivityIcon,
  LogsIcon,
  PlaygroundIcon,
  OrgChartIcon,
  TerminalIcon,
  ConfigIcon,
  IssuesIcon,
  RevisionsIcon,
  AdminIcon,
} from "../ui/icons";

export const NAV: { to: string; key: string; Icon: LucideIcon }[] = [
  { to: "/overview", key: "nav.overview", Icon: OverviewIcon },
  { to: "/insights", key: "nav.insights", Icon: InsightsIcon },
  { to: "/chat", key: "nav.chat", Icon: ChatIcon },
  { to: "/agents", key: "nav.agents", Icon: AgentsIcon },
  { to: "/cortex", key: "nav.cortex", Icon: CortexIcon },
  { to: "/scheduler", key: "nav.scheduler", Icon: SchedulerIcon },
  { to: "/issues", key: "nav.issues", Icon: IssuesIcon },
  { to: "/calendar", key: "nav.calendar", Icon: CalendarIcon },
  { to: "/files", key: "nav.files", Icon: FilesIcon },
  { to: "/memory", key: "nav.memory", Icon: MemoryIcon },
  { to: "/notes", key: "nav.notes", Icon: NotesIcon },
  { to: "/knowledge", key: "nav.knowledge", Icon: KnowledgeIcon },
  { to: "/tools", key: "nav.tools", Icon: ToolsIcon },
  { to: "/providers", key: "nav.providers", Icon: ProvidersIcon },
  { to: "/connections", key: "nav.connections", Icon: ConnectionsIcon },
  { to: "/vault", key: "nav.vault", Icon: VaultIcon },
  { to: "/activity", key: "nav.activity", Icon: ActivityIcon },
  { to: "/orgchart", key: "nav.orgchart", Icon: OrgChartIcon },
  { to: "/logs", key: "nav.logs", Icon: LogsIcon },
  { to: "/terminal", key: "nav.terminal", Icon: TerminalIcon },
  { to: "/playground", key: "nav.playground", Icon: PlaygroundIcon },
  { to: "/revisions", key: "nav.revisions", Icon: RevisionsIcon },
  { to: "/config", key: "nav.config", Icon: ConfigIcon },
  { to: "/admin", key: "nav.admin", Icon: AdminIcon },
];

// Vault (its whole API is admin-prefixed), Logs (the global runtime log buffer
// spans every agent, unfilterable free text), Config (edits the deployment's
// config.yaml) and Admin (team accounts) are admin-only surfaces — hide their
// nav items from non-admin members instead of showing tabs that 403.
export const ADMIN_ONLY_NAV = ["/config", "/vault", "/logs", "/admin"];

// Setup and administration surfaces are not places a person visits during the
// day, so the sidebar's Primary/More groups never show them: they live in the
// profile menu at the bottom of the sidebar, next to the other account-level
// controls, in two labelled groups. **Setup** (model providers, connected
// services, the tool catalog) is for everyone the routes admit; **Administration**
// (the vault, runtime logs, Config, Admin) is admin-only by ADMIN_ONLY_NAV. They
// stay in NAV — one table drives the ⌘K palette, the admin/capability gating
// and the simple shell's labels — and are filtered out of the groups by these
// lists instead of being dropped from the table. Order here is display order.
export const ACCOUNT_MENU_SECTIONS: { key: string; items: string[] }[] = [
  { key: "menu.setup", items: ["/providers", "/connections", "/tools"] },
  { key: "menu.admin", items: ["/vault", "/logs", "/config", "/admin"] },
];
export const ACCOUNT_MENU_NAV = ACCOUNT_MENU_SECTIONS.flatMap((s) => s.items);

// The arrangement rules — `placeItem` (the drag primitive both shells share),
// the console's More order and its one-time fold of a saved Primary group —
// live in navOrder.ts, which has no React or icon imports so the unit tests
// can load it under Node. Re-exported here because this is where callers look.
export {
  DEFAULT_MORE_HEAD,
  placeItem,
  resolveMoreOrder,
  type NavSection,
  type NavSplit,
} from "./navOrder";
