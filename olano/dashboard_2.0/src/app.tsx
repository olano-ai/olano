import { Suspense, lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./core/auth";
import { PrefsProvider } from "./core/prefs";
import { CurrentAgentProvider } from "./core/currentAgent";
import { ServerMonitorProvider } from "./core/serverMonitor/ServerMonitorProvider";
import { Layout } from "./views/Layout";
import { LoginView } from "./views/auth/LoginView";
import { BootstrapView } from "./views/auth/BootstrapView";
import { AcceptInviteView } from "./views/auth/AcceptInviteView";
import { ConsentView } from "./views/oauth/ConsentView";
// Overview stays eager: it's the default landing page and the recovery surface,
// so it must paint without waiting on a second chunk fetch.
import { OverviewView } from "./views/overview/OverviewView";
import { VaultMonitorProvider } from "./views/vault/VaultMonitor";
import { useCapability } from "./core/capabilities";
import { loadRoute, useRouteChunkPrefetch } from "./core/routeChunks";
import { useIdlePrefetch } from "./core/catalogResource";
import { useConnectNotices } from "./views/chat/connectNotices";
import { useThreadSync } from "./views/chat/threadSync";
import { prefetchArchetypes } from "./views/agents/archetypes";
import { importChunk } from "./core/chunkRecovery";
import type { ReactNode } from "react";

// Every other view is route-split: each chunk (and heavy deps like recharts on
// /insights or xterm on /terminal) loads on first navigation instead of riding
// in the initial bundle. Layout wraps its <Outlet> in the Suspense boundary, so
// the shell stays put and only the content pane shows the loading spinner.
//
// The import factories live in core/routeChunks.ts so the idle prefetcher (and
// the sidebar's hover prefetch) can warm exactly the same module promises these
// lazy() calls await — otherwise the chunk fetch doesn't even start until the
// route renders, at the tail of the boot waterfall. loadRoute() is that same
// factory plus stale-build recovery: a tab left open across a deploy asks for
// hashed chunks the server replaced, and reloads once instead of erroring.
const InsightsView = lazy(() => loadRoute("/insights").then((m) => ({ default: m.InsightsView })));
const ChatView = lazy(() => loadRoute("/chat").then((m) => ({ default: m.ChatView })));
const AgentsView = lazy(() => loadRoute("/agents").then((m) => ({ default: m.AgentsView })));
const AgentDetailPage = lazy(() =>
  loadRoute("/agents/:name").then((m) => ({ default: m.AgentDetailPage })),
);
const GroupChatPage = lazy(() =>
  loadRoute("/groups/:id").then((m) => ({ default: m.GroupChatPage })),
);
const CortexView = lazy(() => loadRoute("/cortex").then((m) => ({ default: m.CortexView })));
const SchedulerView = lazy(() =>
  loadRoute("/scheduler").then((m) => ({ default: m.SchedulerView })),
);
const CalendarView = lazy(() => loadRoute("/calendar").then((m) => ({ default: m.CalendarView })));
const FilesView = lazy(() => loadRoute("/files").then((m) => ({ default: m.FilesView })));
const MemoryView = lazy(() => loadRoute("/memory").then((m) => ({ default: m.MemoryView })));
const NotesView = lazy(() => loadRoute("/notes").then((m) => ({ default: m.NotesView })));
const KnowledgeView = lazy(() =>
  loadRoute("/knowledge").then((m) => ({ default: m.KnowledgeView })),
);
const ToolsView = lazy(() => loadRoute("/tools").then((m) => ({ default: m.ToolsView })));
const IssuesView = lazy(() => loadRoute("/issues").then((m) => ({ default: m.IssuesView })));
const RevisionsView = lazy(() =>
  loadRoute("/revisions").then((m) => ({ default: m.RevisionsView })),
);
const ProvidersView = lazy(() =>
  loadRoute("/providers").then((m) => ({ default: m.ProvidersView })),
);
const ConnectionsView = lazy(() =>
  loadRoute("/connections").then((m) => ({ default: m.ConnectionsView })),
);
const VaultView = lazy(() => loadRoute("/vault").then((m) => ({ default: m.VaultView })));
const ActivityView = lazy(() => loadRoute("/activity").then((m) => ({ default: m.ActivityView })));
const OrgChartView = lazy(() => loadRoute("/orgchart").then((m) => ({ default: m.OrgChartView })));
const LogsView = lazy(() => loadRoute("/logs").then((m) => ({ default: m.LogsView })));
const PlaygroundView = lazy(() =>
  loadRoute("/playground").then((m) => ({ default: m.PlaygroundView })),
);
const ConfigView = lazy(() => loadRoute("/config").then((m) => ({ default: m.ConfigView })));
const TerminalView = lazy(() => loadRoute("/terminal").then((m) => ({ default: m.TerminalView })));
const AdminView = lazy(() => loadRoute("/admin").then((m) => ({ default: m.AdminView })));
// First-run setup wizard: a one-time full-screen gate, so it's route-split —
// every later visit skips its chunk entirely. It renders instead of the app
// tree (like Login/Bootstrap), so it carries its own Suspense fallback.
const OnboardingView = lazy(() =>
  importChunk(() => import("./views/onboarding/OnboardingView")).then((m) => ({
    default: m.OnboardingView,
  })),
);
// The simple (bot chat) shell. Like the onboarding wizard it renders INSTEAD
// of the dashboard tree rather than inside it, so it carries its own Suspense
// fallback and never mounts Layout, the route table or the nav.
const SimpleApp = lazy(() => loadRoute("/simple").then((m) => ({ default: m.SimpleApp })));
import { UiModeSwitchCover } from "./ui/UiModeSwitch";

/** Deployment-settings tab gate: hiding a nav item is UX only — the route
 * itself must also refuse direct navigation. Default-on: only an explicit
 * `tab_<name>: false` capability redirects (undefined while loading renders
 * normally, so enabled tabs never flash away). Overview is never gated — it
 * is the recovery surface. The APIs behind each view carry their own
 * server-side guards regardless. */
function TabGuard({ tab, children }: { tab: string; children: ReactNode }) {
  const enabled = useCapability(`tab_${tab}`);
  if (enabled === false) return <Navigate to="/overview" replace />;
  return <>{children}</>;
}

/**
 * The Playground answers to two switches: the plan may allow the tab, but the
 * deployment's own opt-in (config.yaml `playground.enabled`, default off)
 * decides whether it exists here. Both must say yes.
 *
 * Unlike TabGuard, an *unknown* answer renders nothing rather than the view.
 * This flag defaults to off, so optimistically mounting would flash the
 * playground — and pay for its chunk and its render loop — on every box that
 * never asked for it.
 */
function PlaygroundGuard({ children }: { children: ReactNode }) {
  const tab = useCapability("tab_playground");
  const enabled = useCapability("playground_enabled");
  if (tab === false || enabled === false) return <Navigate to="/overview" replace />;
  if (enabled === undefined) return null;
  return <>{children}</>;
}

/** Wraps children in the vault monitor only when it can actually be used. */
function MaybeVaultMonitor({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  if (!enabled) return <>{children}</>;
  return <VaultMonitorProvider>{children}</VaultMonitorProvider>;
}

function GatedApp() {
  const { phase, user, uiMode, switchingUiMode, onboardingRequired } = useAuth();
  const simple = uiMode === "simple";

  // Warm the route chunks in the background once we're past the auth gate, so
  // the first click on a tab doesn't pay for its own download. Hooks can't sit
  // below the early returns, so the gating is a parameter, not an `if`.
  useRouteChunkPrefetch(phase === "ready" && !onboardingRequired && !simple);
  // Same idea for catalog *data*: the archetype/integration lists used to start
  // loading only when the "New agent" modal first mounted, so the picker's
  // first paint was always an empty list. Warm them on idle instead.
  useIdlePrefetch(phase === "ready" && !onboardingRequired && !simple, prefetchArchetypes);
  // Magic-link outcomes land here rather than in either chat view: a sign-in
  // finishes minutes after the turn that started it, on a thread that may not
  // be the one on screen, and both shells share one chat store. `!simple` is
  // deliberately absent — the simple shell is where most connect flows start.
  useConnectNotices(phase === "ready" && !onboardingRequired);
  // Same shape, wider scope: every transcript row and turn that happens on a
  // thread from ANY surface (Telegram, scheduler, connector, another device)
  // lands in the shared chat store as it happens, so a thread open here is
  // never behind what a reload would show.
  useThreadSync(phase === "ready" && !onboardingRequired);

  if (phase === "loading") {
    return (
      <div className="auth-wrap">
        <div className="spinner" />
      </div>
    );
  }
  if (phase === "setup") return <BootstrapView />;
  if (phase === "login") return <LoginView />;
  // First admin sign-in on a deployed instance → the guided setup wizard
  // replaces the whole app until completed or skipped (server-side flag via
  // /api/auth/me, so it follows the deployment, not this browser).
  if (onboardingRequired && user?.is_admin) {
    return (
      // ServerMonitor wraps the wizard too: a runtime restart mid-setup gets
      // the one calm offline overlay instead of per-step fetch errors.
      <ServerMonitorProvider>
        <Suspense
          fallback={
            <div className="auth-wrap">
              <div className="spinner" />
            </div>
          }
        >
          <OnboardingView />
        </Suspense>
      </ServerMonitorProvider>
    );
  }

  // Simple mode: an entirely different shell replaces the dashboard, the same
  // way the onboarding wizard above does. Nothing about the dashboard renders
  // — no Layout, no sidebar, no route table, no tab chunks.
  //
  // This is a DISPLAY choice and nothing more. It grants no permission and
  // removes none: every endpoint the simple shell calls is the same one the
  // dashboard calls, behind the same server-side guards (`_member_forbidden`,
  // `assert_agent_access`, `filter_visible_agents`). A user who reached the
  // other shell somehow would see a different arrangement of exactly the same
  // data they are already entitled to.
  if (simple) {
    return (
      <ServerMonitorProvider>
        {switchingUiMode && <UiModeSwitchCover to="simple" />}
        {/* VaultMonitor is deliberately admin-only here. `/api/vault/` sits in
            _ADMIN_ONLY_PREFIXES, so for a member its 15s poll is a 403 loop and
            its unlock prompt is an action they cannot take — a modal about
            daemon key material is exactly the kind of thing this shell exists
            to keep away from a non-technical user. Admins keep it, because an
            admin previewing simple mode still needs to know the vault locked. */}
        <MaybeVaultMonitor enabled={!!user?.is_admin}>
          <PrefsProvider>
            <CurrentAgentProvider shell="simple">
              <Suspense
                fallback={
                  <div className="auth-wrap">
                    <div className="spinner" />
                  </div>
                }
              >
                <SimpleApp />
              </Suspense>
            </CurrentAgentProvider>
          </PrefsProvider>
        </MaybeVaultMonitor>
      </ServerMonitorProvider>
    );
  }

  return (
    // ServerMonitor watches runtime connectivity app-wide: if :8765 goes down
    // (stop/restart) it shows one calm overlay + suppresses the error-toast
    // flood, instead of every view failing loudly. Outermost so its overlay
    // covers the vault prompt and all views.
    <ServerMonitorProvider>
      {switchingUiMode && <UiModeSwitchCover to="advanced" />}
      {/* The vault holds every LLM/connector API key and re-locks on a server or
          dashboard restart. The monitor watches its status app-wide and pops the
          unlock prompt the moment it's locked — at load and in real time — so it
          must wrap every authenticated view, not just /vault. */}
      <VaultMonitorProvider>
        <PrefsProvider>
        <CurrentAgentProvider>
        <Routes>
          {/* The landing redirect sits OUTSIDE the shell route on purpose.
              As an `index` route it rendered *inside* <Layout>, so the whole
              shell painted at "/" and was still on screen while the redirect
              was in flight — React writes the history entry synchronously but
              delivers the resulting re-render and effects several scheduler
              hops later, and input events can preempt those hops. Anything the
              user did inside that window raced the redirect's own effects: a
              tap on the mobile hamburger opened the drawer, then Layout's
              "navigation closes the drawer" effect shut it again in the same
              task, before the browser ever painted it open. Matching the
              redirect before the shell mounts closes the window — and skips a
              full render of the nav, its subscriptions and its badge fetches
              for a URL that is about to be replaced anyway. */}
          <Route index element={<Navigate to="/overview" replace />} />
          {/* OAuth consent for an outside MCP/A2A client (olano/mcp_server/oauth.py
              sends the browser here). Behind the login, outside the shell: one
              decision, nothing else on screen. */}
          <Route path="oauth/consent" element={<ConsentView />} />
          <Route element={<Layout />}>
            <Route path="overview/:agent?" element={<OverviewView />} />
            <Route path="insights/:tab?/:agent?" element={<TabGuard tab="insights"><InsightsView /></TabGuard>} />
            <Route path="chat/:agent?" element={<TabGuard tab="chat"><ChatView /></TabGuard>} />
            <Route path="agents" element={<TabGuard tab="agents"><AgentsView /></TabGuard>} />
            <Route path="agents/:name/:tab?" element={<TabGuard tab="agents"><AgentDetailPage /></TabGuard>} />
            {/* A group chat. No tab of its own — the sidebar's GROUP CHATS rows and
                the chat header's "Add a bot" lead here — and no TabGuard: the
                page redirects itself when the groups feature is off. */}
            <Route path="groups/:id" element={<GroupChatPage />} />
            <Route path="cortex/:agent?/:section?" element={<TabGuard tab="cortex"><CortexView /></TabGuard>} />
            <Route path="scheduler/:agent?" element={<TabGuard tab="scheduler"><SchedulerView /></TabGuard>} />
            <Route path="calendar/:mode?" element={<TabGuard tab="calendar"><CalendarView /></TabGuard>} />
            <Route path="files/:agent?" element={<TabGuard tab="files"><FilesView /></TabGuard>} />
            <Route path="memory/:agent?" element={<TabGuard tab="memory"><MemoryView /></TabGuard>} />
            <Route path="notes/:agent?" element={<TabGuard tab="notes"><NotesView /></TabGuard>} />
            <Route path="knowledge/:agent?/:section?" element={<TabGuard tab="knowledge"><KnowledgeView /></TabGuard>} />
            <Route path="tools" element={<TabGuard tab="tools"><ToolsView /></TabGuard>} />
            <Route path="issues" element={<TabGuard tab="issues"><IssuesView /></TabGuard>} />
            <Route path="revisions" element={<TabGuard tab="revisions"><RevisionsView /></TabGuard>} />
            <Route path="providers" element={<TabGuard tab="providers"><ProvidersView /></TabGuard>} />
            <Route path="connections/:agent?" element={<TabGuard tab="connections"><ConnectionsView /></TabGuard>} />
            {user?.is_admin && (
              <Route path="vault" element={<TabGuard tab="vault"><VaultView /></TabGuard>} />
            )}
            <Route path="activity" element={<TabGuard tab="activity"><ActivityView /></TabGuard>} />
            <Route path="orgchart" element={<TabGuard tab="orgchart"><OrgChartView /></TabGuard>} />
            {user?.is_admin && (
              <Route path="logs/:agent?" element={<TabGuard tab="logs"><LogsView /></TabGuard>} />
            )}
            <Route path="terminal/:agent?" element={<TabGuard tab="terminal"><TerminalView /></TabGuard>} />
            <Route path="playground" element={<PlaygroundGuard><PlaygroundView /></PlaygroundGuard>} />
            {user?.is_admin && (
              <Route path="config" element={<TabGuard tab="config"><ConfigView /></TabGuard>} />
            )}
            {user?.is_admin && (
              <Route path="admin" element={<TabGuard tab="admin"><AdminView /></TabGuard>} />
            )}
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Route>
        </Routes>
        </CurrentAgentProvider>
        </PrefsProvider>
      </VaultMonitorProvider>
    </ServerMonitorProvider>
  );
}

export function App() {
  return (
    // Navigations are plain state updates, not transitions. react-router wraps
    // each history change in startTransition by default, and the chat views
    // re-render synchronously off their stores (useSyncExternalStore) while a
    // thread loads. When one of those landed during a navigation's render, the
    // navigation was occasionally dropped: the URL moved, React went idle with
    // nothing pending, and from then on the router ignored every history event,
    // Back included, so the address and the page disagreed until a reload.
    // tests/e2e/test_url_routing.py caught it about one run in five.
    <BrowserRouter useTransitions={false}>
      <Routes>
        {/* Public — reachable without a session, before the auth gate. */}
        <Route path="/invite/:token" element={<AcceptInviteView />} />
        <Route path="/*" element={<GatedApp />} />
      </Routes>
    </BrowserRouter>
  );
}
