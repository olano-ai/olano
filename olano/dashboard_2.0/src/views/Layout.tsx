import {
  Fragment,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type MouseEvent as ReactMouseEvent,
  type RefObject,
} from "react";
import type { LucideIcon } from "lucide-react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { confirmNavigation, hasNavGuard, isGuardedLinkClick } from "../core/navGuard";
import { api, ApiError } from "../core/api";
import { useAuth } from "../core/auth";
import { usePrefs } from "../core/prefs";
import { useCapabilities, useCapability } from "../core/capabilities";
import { prefetchRoute } from "../core/routeChunks";
import { useRouteScrollMemory } from "../core/usePersistentState";
import { useCurrentAgent } from "../core/currentAgent";
import { RouteCanonical } from "../core/RouteCanonical";
import { agentDetailPath, shellKey } from "../core/routeUrls";
import { BotNav, type BotNavHandle } from "./nav/BotNav";
import { NewAgentButton } from "./nav/NewAgentButton";
import { PwaBanners } from "./pwa/PwaBanners";
import { useI18n } from "../core/i18n";
import { cycleTheme } from "../core/theme";
import { useTheme } from "../core/useTheme";
import { useToast } from "../ui/toast";
import { BrandIcon, BrandWordmark } from "../ui/BrandMark";
import { ThemeSelect, LanguageSelect } from "../ui/PreferenceSelects";
import { themeIcon } from "../ui/ThemePicker";
import { UiModeSwitch } from "../ui/UiModeSwitch";
// Only the icons this shell draws itself — every nav item's icon now travels
// with its entry in core/nav.ts.
import {
  AgentsIcon,
  SchedulerIcon,
  ChatIcon,
  SearchIcon,
  KeyIcon,
  SignOutIcon,
  CheckIcon,
  HelpIcon,
  ChevronDownIcon,
  SidebarCollapseIcon,
  SidebarExpandIcon,
  MenuIcon,
  SettingsSectionIcon,
  SettingsFieldIcon,
} from "../ui/icons";
import { useMediaQuery, MOBILE_QUERY } from "../core/useMediaQuery";
import { RouteErrorBoundary } from "../ui/RouteErrorBoundary";
import { useDialogA11y } from "../ui/dialogA11y";
import { useSettingFocus } from "../core/settingFocus";
import { search, highlightParts, type Range } from "../core/settingsSearch";
import {
  loadSettingsIndex,
  resolveEntries,
  entryHref,
  type SettingsEntry,
} from "../core/settingsIndex";
import { UsernameAnchor } from "./vault/components";
import { useSchedulerActiveCount } from "./scheduler/schedulerStore";
import { useApprovalsCount } from "./approvals/approvalsStore";
import { useUnreadCount } from "./notifications/notificationsStore";
import { NotificationBell } from "./notifications/NotificationBell";
import { useActivityIndicator } from "./activity/activityStore";
import type { User } from "../core/types";
import { docsUrlFor } from "../core/docsLinks";

// The nav table lives in core/nav.ts so the simple shell's More section can
// read it without importing this module (and the whole console with it).
// Re-exported here because this is where every existing caller looks for it.
import {
  NAV,
  ADMIN_ONLY_NAV,
  ACCOUNT_MENU_NAV,
  ACCOUNT_MENU_SECTIONS,
  placeItem,
  resolveMoreOrder,
} from "../core/nav";
export { NAV, ADMIN_ONLY_NAV, ACCOUNT_MENU_NAV };

const linkClass = ({ isActive }: { isActive: boolean }) =>
  isActive ? "nav-link active" : "nav-link";
const SIDEBAR_COLLAPSED_KEY = "lc-sidebar-collapsed";
// Settings rows shown per query. Enough to cover a vague word ("model",
// "disk") without pushing agents and tasks off the visible list.
const SETTING_RESULTS = 8;

// ── sidebar width (px) ───────────────────────────────────────────────────
// The left nav is drag-resizable within ±25% of its default, persisted to
// localStorage so the chosen width survives reloads (mirrors the FilesView
// tree pane + ChatView threads rail). Applied as the --sb-w custom property on
// .app-shell; the collapsed rail uses --sb-w-collapsed and is unaffected.
const SIDEBAR_W_KEY = "lc.sidebar.width";
const SB_W_DEFAULT = 244; // keep in sync with --sb-w in tokens.css
const SB_W_MIN = Math.round(SB_W_DEFAULT * 0.75); // 183
const SB_W_MAX = Math.round(SB_W_DEFAULT * 1.25); // 305

/** Clamp to the allowed ±25% band around the default width. */
function clampSidebarWidth(px: number): number {
  return Math.round(Math.min(SB_W_MAX, Math.max(SB_W_MIN, px)));
}

function readSidebarWidth(): number {
  try {
    const raw = localStorage.getItem(SIDEBAR_W_KEY);
    if (raw) return clampSidebarWidth(parseFloat(raw));
  } catch {
    // storage disabled — fall through to the default.
  }
  return SB_W_DEFAULT;
}

function writeSidebarWidth(px: number): void {
  try {
    localStorage.setItem(SIDEBAR_W_KEY, String(px));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

// Draggable grab strip on the left nav's right edge. Mirrors the FilesView /
// ChatView handles: a thin vertical line that brightens while hovered or
// dragging. Positioned over the gap between the floating nav panel and the main
// content via the live --sb-w property, so it tracks the panel edge mid-drag.
// Local hover state keeps hovering from re-rendering the whole nav.
function SidebarResizeHandle({
  active,
  title,
  onMouseDown,
  onDoubleClick,
}: {
  active: boolean;
  title: string;
  onMouseDown: (e: ReactMouseEvent) => void;
  onDoubleClick: () => void;
}) {
  const [hover, setHover] = useState(false);
  const lit = active || hover;
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      title={title}
      onMouseDown={onMouseDown}
      onDoubleClick={onDoubleClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        position: "absolute",
        top: 8,
        bottom: 8,
        left: "calc(var(--sb-w) - 14px)",
        width: 12,
        zIndex: 6,
        cursor: "col-resize",
        display: "flex",
        justifyContent: "center",
        touchAction: "none",
      }}
    >
      {/* Visual line over the panel's right edge — brightens while hovered/dragging. */}
      <span
        style={{
          width: 2,
          alignSelf: "stretch",
          borderRadius: 2,
          background: lit ? "var(--accent)" : "transparent",
          transition: "background var(--t-fast)",
        }}
      />
    </div>
  );
}

type NavItem = (typeof NAV)[number];

// The sidebar, top to bottom: brand, search, New agent, then the agents (BotNav
// — pinned, the reader's own groups, all agents, group chats), a rule, and ONE
// collapsible More group holding every day-to-day tab. Overview, Insights,
// Cortex and Issues lead it by default (core/nav.ts DEFAULT_MORE_HEAD); the rest
// follow in NAV order. Users drag to reorder and the order persists per user
// (nav_more). The always-visible Primary group this replaced is folded into
// More the first time a saved two-group split is loaded — see resolveMoreOrder.
//
// Chat has no tab of its own: an agent row IS the way into a conversation, and
// a tab that duplicated it was one home too many. The route stays (and stays in
// the palette) — it is where the rows go.

function initials(user: User | null): string {
  const base = user?.name || user?.email || "?";
  const parts = base.split(/[\s@.]+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
}

export function Layout() {
  const { user, multiUser, logout } = useAuth();
  const { t } = useI18n();
  // Remember each route's scroll position so a left-nav round-trip lands you back
  // where you left off (the <main> scroller is shared across routes — only its
  // Outlet content swaps — so the memory is keyed by path, not by component).
  const { pathname } = useLocation();
  // The page identity behind the raw path (core/routeUrls.ts): /chat/<a> and
  // /chat/<b> are one page, /agents/<a>/mcp and /agents/<a>/tools too. Scroll
  // memory, the error boundary's remount and the drawer's close-on-navigate
  // key on it, so a sub-tab click or an agent switch behaves exactly as it
  // did when neither was in the path.
  const pageKey = shellKey(pathname);
  // For the guarded-link interception on the shell root (core/navGuard): a
  // click the guard approves is re-issued here as a programmatic navigation.
  const navigate = useNavigate();
  const mainRef = useRef<HTMLElement>(null);
  useRouteScrollMemory(mainRef, pageKey);
  // Turns the palette's ?focus=<setting id> into scroll + highlight. Mounted
  // once here rather than per view — it only queries the document.
  useSettingFocus();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const theme = useTheme();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true",
  );

  // ── mobile shell ─────────────────────────────────────────────────────────
  // Below the breakpoint the sidebar becomes an off-canvas drawer behind a
  // hamburger (a fixed 244px column ate ~65% of a phone screen). The collapsed
  // preference is desktop-only — the drawer always shows the full nav.
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  // The agent list's door for a right-click that lands anywhere else on the
  // sidebar (see the <aside>'s onContextMenu below).
  const botNavRef = useRef<BotNavHandle>(null);
  useEffect(() => {
    // Navigating (tap on a nav item, deep link) closes the drawer.
    //
    // Keyed on the page key, not the raw path: picking another agent from the
    // drawer (the chevron) moves /chat/<a> to /chat/<b> and must leave the
    // drawer open, and RouteCanonical's rewrites (/chat → /chat/<agent>) never
    // change the key either, so they cannot shut a drawer opened meanwhile.
    //
    // It is keyed on a *value*, so it cannot tell a navigation the
    // user just made from one that was already in flight when the drawer
    // opened — it would shut a drawer the user opened a moment ago. Nothing
    // redirects into the shell's landing route for exactly that reason: "/" →
    // "/overview" is matched before <Layout> mounts (see app.tsx), so by the
    // time the hamburger exists the router has settled. Keep it that way.
    setMobileNavOpen(false);
  }, [pageKey]);

  // Drag-resizable nav width. The chosen width drives the --sb-w custom property
  // on .app-shell; we apply it imperatively (not via the style prop) so the
  // frequent store-driven re-renders below can't clobber an in-flight drag.
  const shellRef = useRef<HTMLDivElement>(null);
  const [sidebarWidth, setSidebarWidth] = useState<number>(readSidebarWidth);
  const [resizing, setResizing] = useState(false);

  useLayoutEffect(() => {
    shellRef.current?.style.setProperty("--sb-w", `${sidebarWidth}px`);
  }, [sidebarWidth]);

  const startResize = useCallback(
    (e: ReactMouseEvent) => {
      e.preventDefault();
      const shell = shellRef.current;
      const startX = e.clientX;
      const startW = sidebarWidth;
      let latest = startW;
      setResizing(true);
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      // Suppress the collapse transition so the panel tracks the cursor 1:1.
      if (shell) shell.style.transition = "none";
      const onMove = (ev: globalThis.MouseEvent) => {
        latest = clampSidebarWidth(startW + (ev.clientX - startX));
        shell?.style.setProperty("--sb-w", `${latest}px`);
      };
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        if (shell) shell.style.transition = "";
        setResizing(false);
        setSidebarWidth(latest);
        writeSidebarWidth(latest);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [sidebarWidth],
  );

  const resetResize = useCallback(() => {
    const w = clampSidebarWidth(SB_W_DEFAULT);
    setSidebarWidth(w);
    writeSidebarWidth(w);
  }, []);
  // Terminal is admin-only / grant-gated — hide its nav item for users without
  // access (the capability folds in the feature flag + per-user grant). Config
  // edits the global config.yaml and is admin-only (its API is gated under the
  // /api/system prefix), so hide it from non-admin members too.
  const showTerminal = useCapability("terminal_enabled") === true;
  // Playground is a home-wide opt-in (config.yaml playground.enabled, default
  // off). Hide its nav item until the capability is known true to avoid a flash.
  const showPlayground = useCapability("playground_enabled") === true;
  const isAdmin = !!user?.is_admin;
  // Deployment settings can hide any tab via a tab_<name> capability. These
  // are default-ON: only an explicit false hides an item (undefined while
  // loading must not flash tabs out — see useCapabilities' tri-state contract).
  const caps = useCapabilities();
  const navItems = useMemo(
    () =>
      NAV.filter(
        (n) =>
          // Chat has no tab in this sidebar: a bot row is the door to a
          // conversation (BotNav), and a tab that duplicated it was one home
          // too many. Filtered here rather than dropped from the table, which
          // the simple shell's More section reads as its map of the console.
          n.to !== "/chat" &&
          (n.to !== "/terminal" || showTerminal) &&
          (n.to !== "/playground" || showPlayground) &&
          (!ADMIN_ONLY_NAV.includes(n.to) || isAdmin) &&
          caps?.[`tab_${n.to.slice(1)}`] !== false,
      ),
    [showTerminal, showPlayground, isAdmin, caps],
  );
  // Setup and admin surfaces never sit in the Primary/More groups: they are the
  // profile menu's rows (ACCOUNT_MENU_NAV). Both lists derive from the same
  // gated `navItems`, so the admin and capability checks apply exactly once.
  const sidebarItems = useMemo(
    () => navItems.filter((n) => !ACCOUNT_MENU_NAV.includes(n.to)),
    [navItems],
  );
  const accountLinks = useMemo(
    () => navItems.filter((n) => ACCOUNT_MENU_NAV.includes(n.to)),
    [navItems],
  );

  // The palette's nav rows. Derived from the same gated list the sidebar and the
  // profile menu draw from, so the three can never disagree — the palette used
  // to filter NAV directly, which let it offer a tab the deployment had hidden
  // and TabGuard then bounced. Config and Admin are in here too: they left the
  // sidebar groups for the profile menu, but "type its name" must still work.
  const paletteNav = useMemo<PaletteItem[]>(
    () => [
      ...navItems.map((n) => ({
        key: n.to,
        group: "nav" as const,
        label: t(n.key),
        to: n.to,
        Icon: n.Icon,
      })),
      // Chat has no sidebar tab (the bot rows are the door), but it is still a
      // place a person types "chat" to reach.
      ...(caps?.tab_chat !== false
        ? [{ key: "/chat", group: "nav" as const, label: t("nav.chat"), to: "/chat", Icon: ChatIcon }]
        : []),
    ],
    [navItems, t, caps],
  );

  // Live count of active (enabled) scheduled jobs for the Scheduler nav badge.
  // Driven by its own app-level store so it stays current on any tab.
  const schedulerActive = useSchedulerActiveCount();
  // Live count of pending HITL approvals. The approvals inbox now lives as a
  // section inside Overview (no standalone tab), so its badge rides the Overview
  // nav item — keeping the app-wide "something needs you" signal.
  const approvalsPending = useApprovalsCount();
  // Operator notifications (missing connector credential, locked vault, disk
  // pressure). The bell is the interrupt; this count also rides the Overview
  // nav badge alongside approvals, since the section lives there.
  const notificationsUnread = useUnreadCount();
  // Live A2A indicator for the Activity nav item: a working pulse while any
  // agent-to-agent exchange is in flight, plus the count of new messages that
  // arrived while the Activity panel wasn't open.
  const { unread: activityUnread, busy: activityBusy } = useActivityIndicator();

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function toggleSidebar() {
    setSidebarCollapsed((collapsed) => {
      const next = !collapsed;
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
      return next;
    });
  }

  const sidebarToggleLabel = sidebarCollapsed ? t("nav.sidebar.expand") : t("nav.sidebar.collapse");

  const shellClass = [
    "app-shell",
    !isMobile && sidebarCollapsed ? "sidebar-collapsed" : "",
    isMobile && mobileNavOpen ? "mobile-nav-open" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      ref={shellRef}
      className={shellClass}
      onClickCapture={(e) => {
        // A page with unsaved work (core/navGuard) gets to answer before any
        // in-app link — sidebar, account menu, or a link inside the page —
        // takes the person away. Capture phase, so this runs before
        // react-router's own <Link> handler pushes history. Nothing here
        // touches a click when no guard is registered, which is every page
        // but a dirty form.
        if (!hasNavGuard()) return;
        const anchor = (e.target as HTMLElement).closest?.("a[href]") as HTMLAnchorElement | null;
        if (!anchor) return;
        if (
          !isGuardedLinkClick({
            href: anchor.href,
            target: anchor.target,
            download: anchor.getAttribute("download") ?? "",
            origin: anchor.origin,
            currentOrigin: window.location.origin,
            metaKey: e.metaKey,
            ctrlKey: e.ctrlKey,
            shiftKey: e.shiftKey,
            altKey: e.altKey,
            button: e.button,
          })
        )
          return;
        const to = anchor.pathname + anchor.search + anchor.hash;
        if (to === location.pathname + location.search + location.hash) return;
        // preventDefault alone: react-router's <Link> skips its own navigate
        // when `defaultPrevented` is set, and the event must keep bubbling so
        // the sidebar's "a link tap closes the mobile drawer" handler still
        // runs — otherwise the dialog would open underneath an open drawer.
        e.preventDefault();
        void confirmNavigation().then((ok) => {
          if (ok) navigate(to);
        });
      }}
    >
      {/* Mobile-only topbar (CSS hides it above the breakpoint): hamburger +
          brand. The sidebar itself becomes the off-canvas drawer below. */}
      <header className="mobile-topbar">
        <button
          className="btn ghost mobile-nav-btn"
          onClick={() => setMobileNavOpen((o) => !o)}
          aria-label={t("nav.mobile.menu")}
          aria-expanded={mobileNavOpen}
        >
          <MenuIcon size={19} strokeWidth={1.75} />
        </button>
        <span className="row" style={{ gap: 8, fontWeight: 700 }}>
          <BrandIcon size={16} />
          <BrandWordmark />
        </span>
        {/* Pushed to the far edge so the bell is thumb-reachable and never
            collides with the hamburger. */}
        <span style={{ marginLeft: "auto" }}>
          <NotificationBell />
        </span>
      </header>
      {isMobile && mobileNavOpen && (
        <>
          <div className="mobile-nav-backdrop" onClick={() => setMobileNavOpen(false)} />
          {/* Escape + focus trap/restore for the open drawer — same shared
              dialog behavior as every modal (conditional mount because hooks
              can't be conditional inside Layout itself). */}
          <DrawerA11y panelRef={sidebarRef} onClose={() => setMobileNavOpen(false)} />
        </>
      )}
      <aside
        className="sidebar"
        ref={sidebarRef}
        aria-label="Primary"
        onClick={(e) => {
          // Tapping any link/button that navigates should also dismiss the
          // drawer; harmless no-op on desktop.
          if (isMobile && (e.target as HTMLElement).closest("a")) setMobileNavOpen(false);
        }}
        onContextMenu={(e) => {
          // A right-click nothing else claimed — the brand row, the gap under
          // More, the profile footer — opens the agent list's own menu ("New
          // group…", "New group chat…"): to a person the whole rail IS the
          // agent list, so the gesture works wherever it lands, as it does on
          // the simple shell's rail. Rows and headings with a menu of their own
          // stop the event before it gets here, the list's empty space marks it
          // handled (`defaultPrevented`), and links, fields and buttons keep
          // the browser's menu — open in a new tab, paste.
          if (e.defaultPrevented) return;
          if ((e.target as HTMLElement).closest("a, button, input, textarea, select, [role='menu']")) return;
          e.preventDefault();
          botNavRef.current?.openSectionMenu(e.clientX, e.clientY);
        }}
      >
        <div className="brand">
          <BrandIcon size={18} />
          <BrandWordmark className="brand-label" />
          <div className="brand-actions">
            <NotificationBell />
            <button
              className="btn ghost sidebar-icon-btn theme-toggle"
              onClick={() => cycleTheme()}
              aria-label="Switch theme"
              title="Switch theme"
            >
              {themeIcon(theme, 17)}
            </button>
            <button
              className="btn ghost sidebar-icon-btn sidebar-toggle"
              onClick={toggleSidebar}
              aria-label={sidebarToggleLabel}
              title={sidebarToggleLabel}
              aria-expanded={!sidebarCollapsed}
            >
              {sidebarCollapsed ? <SidebarExpandIcon size={17} strokeWidth={1.75} /> : <SidebarCollapseIcon size={17} strokeWidth={1.75} />}
            </button>
          </div>
        </div>
        <button
          className="btn ghost sidebar-search"
          style={{ justifyContent: "space-between", marginBottom: 6, color: "var(--text-4)", fontWeight: 400 }}
          onClick={() => setPaletteOpen(true)}
          title={t("action.search")}
        >
          <span className="row" style={{ gap: 8 }}>
            <SearchIcon size={15} /> <span className="sidebar-label">{t("action.search")}</span>
          </span>
          <kbd className="sidebar-kbd" style={{ fontSize: 11 }}>⌘K</kbd>
        </button>
        {/* Directly under Search, the way the simple shell's rail lays out its
            head: find something, or make an agent. There is no app-wide agent
            dropdown here any more — the agent list below is the selector, and
            the agent-scoped views (Memory, Notes, Knowledge, Terminal) carry
            their own inline picker in their top bar. */}
        <NewAgentButton />
        <SidebarNav
          navItems={sidebarItems}
          botNavRef={botNavRef}
          // The agent list draws itself differently in the strip — bigger
          // marks, no unfolded chats — and CSS alone cannot resize a mark whose
          // size is a component prop. The mobile drawer is the expanded layout,
          // so this is the desktop collapse only.
          collapsed={!isMobile && sidebarCollapsed}
          schedulerActive={schedulerActive}
          approvalsPending={approvalsPending}
          notificationsUnread={notificationsUnread}
          activityBusy={activityBusy}
          activityUnread={activityUnread}
        />
        <ProfileButton
          user={user}
          multiUser={multiUser}
          onLogout={logout}
          links={accountLinks}
          pathname={pathname}
        />
      </aside>
      <main className="main" ref={mainRef}>
        {/* All routed views are lazy chunks (see app.tsx) — the Suspense
            boundary lives here so a first visit to a view spins only the
            content pane while the shell stays interactive, and the error
            boundary catches failed chunk fetches (deploy skew, network blip)
            instead of white-screening the app. Keyed by path so navigating
            away clears the error and the next route retries its import. */}
        <RouteErrorBoundary key={pageKey}>
          <Suspense
            fallback={
              <div className="page">
                <div className="spinner" style={{ marginTop: 40 }} />
              </div>
            }
          >
            <RouteCanonical>
              <Outlet />
            </RouteCanonical>
          </Suspense>
        </RouteErrorBoundary>
      </main>
      {/* Fixed-position prompts (new build ready / add to home screen). Mounted
          here rather than per-route so navigating never dismisses one. */}
      <PwaBanners />
      {/* Resizing only makes sense for the expanded desktop nav; the collapsed
          rail is a fixed icon strip and the mobile drawer has a fixed width. */}
      {!sidebarCollapsed && !isMobile && (
        <SidebarResizeHandle
          active={resizing}
          title={t("nav.sidebar.resize")}
          onMouseDown={startResize}
          onDoubleClick={resetResize}
        />
      )}
      {paletteOpen && (
        <CommandPalette
          onClose={() => setPaletteOpen(false)}
          isAdmin={isAdmin}
          navItems={paletteNav}
        />
      )}
    </div>
  );
}

// The nav: the agent list, a rule, then the one collapsible More group holding
// every day-to-day tab. The tabs are drag-and-drop sortable and the order
// persists per user (server-side, via usePrefs) so it follows the human across
// devices. Config and Admin are not in it: they are rows of the profile menu
// below (see ACCOUNT_MENU_NAV), so the operator tools sit with the account
// controls instead of competing with the day-to-day tabs.
function SidebarNav({
  navItems,
  botNavRef,
  collapsed,
  schedulerActive,
  approvalsPending,
  notificationsUnread,
  activityBusy,
  activityUnread,
}: {
  navItems: typeof NAV;
  botNavRef: RefObject<BotNavHandle | null>;
  /** The sidebar is the collapsed icon strip (desktop only). */
  collapsed: boolean;
  schedulerActive: number;
  approvalsPending: number;
  notificationsUnread: number;
  activityBusy: boolean;
  activityUnread: number;
}) {
  const { t } = useI18n();
  const { prefs, setPref, loaded: prefsLoaded } = usePrefs();
  const [dragKey, setDragKey] = useState<string | null>(null);
  // Transient working order during a drag; null when not dragging.
  const [work, setWork] = useState<string[] | null>(null);

  const moreCollapsed = prefs.nav_more_collapsed ?? true;

  const available = useMemo(() => navItems.map((n) => n.to), [navItems]);
  const saved = useMemo(
    () => resolveMoreOrder(available, { primary: prefs.nav_primary, more: prefs.nav_more }),
    [available, prefs.nav_primary, prefs.nav_more],
  );

  // Fold a saved Primary group into More the first time this human loads the
  // one-group sidebar. Gated on `prefsLoaded`: before the fetch lands `prefs`
  // is `{}`, which reads as "nothing saved", and a write on that would clobber
  // the split still on the wire.
  const persist = saved.persist;
  useEffect(() => {
    if (!prefsLoaded || !persist) return;
    setPref("nav_primary", persist.primary);
    setPref("nav_more", persist.more);
    // `saved` is recomputed from the prefs this writes, and a split whose
    // Primary is empty folds to nothing, so the guard closes the loop after one
    // pass.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefsLoaded, persist]);

  // While dragging, render from the live transient order so the move is visible;
  // otherwise render the saved order.
  const byTo = useMemo(() => new Map(navItems.map((n) => [n.to, n] as const)), [navItems]);
  const rendered = useMemo(
    () => (work ?? saved.order).map((to) => byTo.get(to)).filter(Boolean) as NavItem[],
    [work, saved.order, byTo],
  );

  // The current order as `to`-paths — the base for the next placeItem call.
  const baseOrder = (): string[] => work ?? saved.order;

  // While dragging, force the More group open so the drop target is on screen
  // even when it's collapsed.
  const showMore = !moreCollapsed || dragKey != null;

  // Roll-up of attention signals (pending approvals, unread chats) hidden inside
  // a collapsed More group, so a collapse never buries something actionable.
  const moreSet = useMemo(() => new Set(rendered.map((n) => n.to)), [rendered]);
  // Both Overview signals roll up into one badge: a pending approval and an
  // unread notification are the same message to the user — this tab needs you.
  const overviewAttention = approvalsPending + notificationsUnread;

  const moreAttention =
    (moreSet.has("/overview") ? overviewAttention : 0) +
    (moreSet.has("/activity") ? activityUnread : 0);
  const moreBusy = moreSet.has("/activity") && activityBusy;

  function handleDragStart(e: DragEvent, to: string) {
    setDragKey(to);
    setWork(baseOrder());
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", to); // Firefox needs data set to start a drag
  }

  // `placeItem` is the two-group primitive both shells share; with one group
  // here the Primary side is simply always empty.
  function place(cur: string[] | null, key: string, beforeKey: string | null): string[] {
    return placeItem({ primary: [], more: cur ?? baseOrder() }, key, "more", beforeKey).more;
  }

  // Hovering a specific item drops `dragKey` just before it. stopPropagation
  // keeps the surrounding group zone from also firing (which would append).
  function handleItemDragOver(e: DragEvent, overTo: string) {
    if (!dragKey) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "move";
    if (overTo === dragKey) return;
    setWork((cur) => place(cur, dragKey, overTo));
  }

  // Hovering the group's empty area (padding / an emptied group) appends.
  function handleZoneDragOver(e: DragEvent) {
    if (!dragKey) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setWork((cur) => place(cur, dragKey, null));
  }

  // A completed drop persists the order. `drop` fires before `dragend` (per
  // spec) and is the only place that persists, so a cancelled drag (ESC /
  // dropped outside) skips `drop`, hits only `dragend`, and reverts to saved.
  function handleDrop(e: DragEvent) {
    e.preventDefault();
    if (work && dragKey) setPref("nav_more", work);
    reset();
  }

  function reset() {
    setDragKey(null);
    setWork(null);
  }

  function renderLink(n: NavItem) {
    return (
      <NavLink
        key={n.to}
        to={n.to}
        draggable
        // Intent-to-navigate beats the idle warmer: pointing at a tab starts
        // its chunk a few hundred ms before the click lands, which is often
        // the whole of the chunk's transfer time. No-op once already loaded.
        onMouseEnter={() => prefetchRoute(n.to)}
        onFocus={() => prefetchRoute(n.to)}
        onDragStart={(e) => handleDragStart(e, n.to)}
        onDragOver={(e) => handleItemDragOver(e, n.to)}
        onDrop={handleDrop}
        onDragEnd={reset}
        className={({ isActive }) =>
          `${linkClass({ isActive })}${n.to === "/cortex" ? " cortex-nav" : ""}${dragKey === n.to ? " dragging" : ""}`
        }
        title={t(n.key)}
      >
        <n.Icon size={17} strokeWidth={1.75} />
        <span className="nav-label">{t(n.key)}</span>
        {n.to === "/scheduler" && schedulerActive > 0 && (
          <span className="nav-indicators">
            <span className="thread-badge" title={t("nav.scheduler.active", { n: String(schedulerActive) })}>
              {schedulerActive > 99 ? "99+" : schedulerActive}
            </span>
          </span>
        )}
        {n.to === "/overview" && overviewAttention > 0 && (
          <span className="nav-indicators">
            <span className="thread-badge" title={t("nav.overview.attention", { n: String(overviewAttention) })}>
              {overviewAttention > 99 ? "99+" : overviewAttention}
            </span>
          </span>
        )}
        {n.to === "/activity" && (activityBusy || activityUnread > 0) && (
          <span className="nav-indicators">
            {activityBusy && (
              <span
                className="thread-dot"
                title={t("nav.activity.working")}
                aria-label={t("nav.activity.working")}
              />
            )}
            {activityUnread > 0 && (
              <span className="thread-badge" title={t("nav.activity.new", { n: String(activityUnread) })}>
                {activityUnread > 99 ? "99+" : activityUnread}
              </span>
            )}
          </span>
        )}
      </NavLink>
    );
  }

  function toggleMore() {
    setPref("nav_more_collapsed", !moreCollapsed);
  }

  return (
    <nav className="sidebar-nav" aria-label={t("nav.reorder.hint")}>
      <BotNav ref={botNavRef} collapsed={collapsed} />

      {/* A rule, not a heading: everything above is an agent (or a chat with
          several), everything below is a place. The simple shell's rail draws
          the same line over its own More. */}
      <div className="nav-divider" role="presentation" />

      <button
        type="button"
        className={`nav-group-header${showMore ? "" : " collapsed"}`}
        onClick={toggleMore}
        aria-expanded={showMore}
        title={moreCollapsed ? t("nav.more.expand") : t("nav.more.collapse")}
        onContextMenu={(e) => {
          // A reserved heading, like PINNED and ALL AGENTS above it: the same
          // "New group…" menu the agent list offers on its own headings.
          e.preventDefault();
          e.stopPropagation();
          botNavRef.current?.openSectionMenu(e.clientX, e.clientY);
        }}
      >
        <ChevronDownIcon className="nav-group-chev" size={15} strokeWidth={2} />
        <span className="nav-label">{t("nav.more")}</span>
        {!showMore && (moreAttention > 0 || moreBusy) && (
          <span className="nav-indicators">
            {moreBusy && moreAttention === 0 && (
              <span className="thread-dot" title={t("nav.chat.thinking")} aria-label={t("nav.chat.thinking")} />
            )}
            {moreAttention > 0 && (
              <span className="thread-badge" title={t("nav.more.hidden", { n: String(moreAttention) })}>
                {moreAttention > 99 ? "99+" : moreAttention}
              </span>
            )}
          </span>
        )}
      </button>

      {showMore && (
        <div className="nav-group nav-group-more" onDragOver={handleZoneDragOver} onDrop={handleDrop}>
          {rendered.map(renderLink)}
          {rendered.length === 0 && <div className="nav-dropzone" aria-hidden />}
        </div>
      )}
    </nav>
  );
}

// The signed-in person's menu, and the way into the setup and administration
// surfaces: Providers, Connections and Tools for everyone the routes admit;
// Vault, Logs, Config and Admin for admins. They used to be nav tabs (Admin
// pinned under More, the rest inside it); none is a place anyone visits during
// the day, so they now sit here with the other account-level controls, in the
// two groups ACCOUNT_MENU_SECTIONS names. `links` is already gated (admin +
// tab_* capability) by Layout, so a member simply gets no Administration group
// rather than rows that 403, and a group with nothing left in it is not drawn.
function ProfileButton({
  user,
  multiUser,
  onLogout,
  links,
  pathname,
}: {
  user: User | null;
  multiUser: boolean;
  onLogout: () => Promise<void>;
  links: NavItem[];
  pathname: string;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // While Config or Admin is the open page nothing in the nav groups is
  // highlighted, so the trigger itself carries the active state: the person can
  // still see where the page they are on lives.
  const isOn = (to: string) => pathname === to || pathname.startsWith(`${to}/`);
  const onAccountPage = links.some((n) => isOn(n.to));

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  return (
    <div
      ref={ref}
      className="profile-menu-wrap"
      style={{ position: "relative", borderTop: "1px solid var(--border)", paddingTop: 10, marginTop: 4 }}
    >
      <button
        className={`btn ghost profile-btn${onAccountPage ? " is-active" : ""}`}
        style={{ width: "100%", justifyContent: "flex-start", gap: 10 }}
        onClick={() => setOpen((o) => !o)}
        title={user?.name || user?.email || "Owner"}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-current={onAccountPage ? "location" : undefined}
      >
        <span className="avatar-sm">{initials(user)}</span>
        <span className="profile-name" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, textAlign: "left" }}>
          {user?.name || user?.email || "Owner"}
        </span>
      </button>

      {open && (
        <div className="menu" style={{ bottom: "100%", left: 0, right: 0, marginBottom: 6 }}>
          <div className="menu-head">
            <div style={{ fontWeight: 600 }}>{user?.name || "Owner"}</div>
            {!!(user?.title || "").trim() && <div style={{ fontSize: 12 }}>{user?.title}</div>}
            <div className="muted" style={{ fontSize: 12 }}>{user?.email}</div>
            {user?.is_admin && <span className="badge admin" style={{ marginTop: 6 }}>admin</span>}
          </div>

          {/* Setup + Administration. Closing the menu on click is load-bearing:
              Layout closes the mobile *drawer* on navigation, but this menu's
              own open state would otherwise survive and greet the next drawer
              opening. */}
          {ACCOUNT_MENU_SECTIONS.map((section) => {
            const rows = section.items
              .map((to) => links.find((n) => n.to === to))
              .filter((n): n is NavItem => !!n);
            if (rows.length === 0) return null;
            return (
              <Fragment key={section.key}>
                <div className="menu-section" role="group" aria-label={t(section.key)}>
                  <div className="menu-section-label">{t(section.key)}</div>
                  {rows.map((n) => (
                    <NavLink
                      key={n.to}
                      to={n.to}
                      className={`menu-item menu-link${isOn(n.to) ? " active" : ""}`}
                      onMouseEnter={() => prefetchRoute(n.to)}
                      onFocus={() => prefetchRoute(n.to)}
                      onClick={() => setOpen(false)}
                    >
                      <n.Icon size={15} strokeWidth={1.75} />
                      <span>{t(n.key)}</span>
                      {isOn(n.to) && <CheckIcon size={14} className="menu-link-check" />}
                    </NavLink>
                  ))}
                </div>
                <div className="menu-sep" />
              </Fragment>
            );
          })}

          {/* Two rows, not thirteen: three themes and ten languages as lists
              made the menu taller than the viewport on a laptop. The native
              popups draw outside the menu box, so neither can be clipped. */}
          <ThemeSelect label={t("menu.theme")} />
          <LanguageSelect label={t("menu.language")} />

          {/* The user guide, opened at the page for the tab the person is on
              (core/docsLinks.ts). An external link in a new tab: the nav guard
              ignores other origins, and the dashboard stays where it was. */}
          <div className="menu-sep" />
          <div className="menu-section" role="group" aria-label={t("menu.help")}>
            <div className="menu-section-label">{t("menu.help")}</div>
            <a
              className="menu-item menu-link"
              href={docsUrlFor(pathname)}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => setOpen(false)}
            >
              <HelpIcon size={15} strokeWidth={1.75} />
              <span>{t("menu.help.docs")}</span>
            </a>
          </div>
          <div className="menu-sep" />

          {/* The way into the simple view, for admins only. Sits above the
              account actions because it changes the whole screen, which is a
              bigger move than changing a password. */}
          <UiModeSwitch onDone={() => setOpen(false)} />

          {multiUser && (
            <>
              <div style={{ height: 1, background: "var(--border)", margin: "6px 0" }} />
              <button className="menu-item" onClick={() => { setPwOpen(true); setOpen(false); }}>
                <KeyIcon size={15} /> {t("action.changePassword")}
              </button>
              <button className="menu-item" style={{ color: "var(--danger)" }} onClick={() => void onLogout()}>
                <SignOutIcon size={15} /> {t("action.signout")}
              </button>
            </>
          )}
        </div>
      )}
      {pwOpen && <ChangePasswordModal onClose={() => setPwOpen(false)} />}
    </div>
  );
}

// Hook host for the mobile nav drawer: mounted only while the drawer is open,
// so the shared dialog behavior (Escape, focus trap, focus restore) applies
// exactly for the drawer's lifetime.
function DrawerA11y({
  panelRef,
  onClose,
}: {
  panelRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  useDialogA11y(panelRef, onClose);
  return null;
}

function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const { user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  // Shared dialog behavior — this modal previously had no Escape handling at
  // all. Not dismissible while the change-password POST is in flight.
  const panelRef = useRef<HTMLFormElement>(null);
  useDialogA11y(panelRef, busy ? undefined : onClose);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      toast.push("New passwords don't match.", "error");
      return;
    }
    setBusy(true);
    try {
      await api.post("/api/auth/password", { current_password: current, new_password: next });
      toast.push("Password changed.");
      onClose();
    } catch (err) {
      toast.push(
        err instanceof ApiError && err.detail === "wrong_password" ? "Current password is wrong." : "Could not change password.",
        "error",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={busy ? undefined : onClose}>
      <form
        className="card auth-card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        onSubmit={submit}
      >
        <h1 style={{ fontSize: 17 }}>Change password</h1>
        {/* This panel is where "Suggest password" already worked, but only by
            accident: current + new + confirm is the one shape a manager can
            classify as a change-password form with no attributes at all. Say it
            outright instead, so the offer does not depend on that heuristic. */}
        <UsernameAnchor value={user?.email ?? "admin"} />
        <div className="field">
          <label>Current password</label>
          <input className="input" type="password" name="current-password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} autoFocus />
        </div>
        <div className="field">
          <label>New password</label>
          <input className="input" type="password" name="new-password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        </div>
        <div className="field">
          <label>Confirm new password</label>
          <input className="input" type="password" name="confirm-password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !current || !next}>{busy ? "Saving…" : "Save"}</button>
        </div>
      </form>
    </div>
  );
}

// One palette row: a navigation target, an agent, a scheduled task, or a
// setting. `to` is the full href (path + params + ?focus=), not just a path —
// a setting needs the tab and anchor a bare route can't carry.
interface PaletteItem {
  key: string;
  group: "nav" | "setting" | "agent" | "task";
  label: string;
  /** Breadcrumb shown under the label ("Config › Speech"). Settings only. */
  sub?: string;
  /** Spans of `label` the query matched, for bolding. */
  ranges?: Range[];
  to: string;
  Icon: LucideIcon;
}

function CommandPalette({
  onClose,
  isAdmin,
  navItems,
}: {
  onClose: () => void;
  isAdmin: boolean;
  navItems: PaletteItem[];
}) {
  const navigate = useNavigate();
  const { lang, t } = useI18n();
  const { agents, current } = useCurrentAgent();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  // Register on the shared dialog stack: Escape then closes ONLY the palette
  // (topmost), instead of also reaching a stack-registered dialog underneath.
  const panelRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  useDialogA11y(panelRef, onClose, { initialFocus: false });
  // Scheduled tasks make the palette a real global search, not just a nav
  // filter. Best-effort: fetched once per open, and the palette works fine
  // without them if the request fails.
  const [tasks, setTasks] = useState<{ id: string; name: string }[]>([]);
  useEffect(() => {
    let alive = true;
    api
      .cachedGet<{ id: string; name: string }[]>("/api/scheduler/tasks")
      .then((rows) => {
        if (alive) setTasks(rows.map(({ id, name }) => ({ id, name })));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // The settings index. Loaded on open, not at module scope: the catalog pulls
  // in every indexed view's strings.ts, which must not sit in the shell chunk.
  const [settings, setSettings] = useState<SettingsEntry[] | null>(null);
  useEffect(() => {
    let alive = true;
    loadSettingsIndex().then((entries) => {
      if (alive) setSettings(entries);
    });
    return () => {
      alive = false;
    };
  }, []);

  const caps = useCapabilities();
  // Resolve labels once per index/language, not once per keystroke.
  const resolvedSettings = useMemo(() => {
    if (!settings) return [];
    const visible = settings.filter(
      (e) =>
        (!e.adminOnly || isAdmin) &&
        (!e.capability || caps?.[e.capability] !== false),
    );
    return resolveEntries(visible, lang, t);
  }, [settings, isAdmin, caps, lang, t]);

  const ql = q.trim();
  const filtered: PaletteItem[] = useMemo(() => {
    const lower = ql.toLowerCase();
    return [
      ...navItems.filter((i) => i.label.toLowerCase().includes(lower)),
      // Everything below only joins in once the user types — an empty palette
      // stays the familiar quick-nav list.
      ...(ql
        ? search(resolvedSettings, ql)
            .slice(0, SETTING_RESULTS)
            .map(({ item, ranges }) => ({
              key: `setting:${item.entry.id}`,
              group: "setting" as const,
              label: item.label,
              sub: item.breadcrumb,
              ranges,
              to: entryHref(item.entry, current?.name),
              Icon: item.entry.kind === "section" ? SettingsSectionIcon : SettingsFieldIcon,
            }))
        : []),
      ...(ql
        ? agents
            .filter((a) => a.name.toLowerCase().includes(lower))
            .slice(0, 8)
            .map((a) => ({
              key: `agent:${a.name}`,
              group: "agent" as const,
              label: a.name,
              to: agentDetailPath(a.name),
              Icon: AgentsIcon,
            }))
        : []),
      ...(ql
        ? tasks
            .filter((task) => task.name.toLowerCase().includes(lower))
            .slice(0, 8)
            .map((task) => ({
              key: `task:${task.id}`,
              group: "task" as const,
              label: task.name,
              to: "/scheduler",
              Icon: SchedulerIcon,
            }))
        : []),
    ];
  }, [navItems, ql, resolvedSettings, agents, current, tasks]);

  function go(to: string) {
    // The palette is the one programmatic exit from the shell; it asks the
    // same guard the link interception does (core/navGuard).
    onClose();
    void confirmNavigation().then((ok) => {
      if (ok) navigate(to);
    });
  }

  // Keep the highlighted row visible: with settings in the list the results
  // routinely overflow the 50vh scroller, and arrowing past the fold used to
  // move a selection nobody could see.
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(".cmdk-item.active")
      ?.scrollIntoView({ block: "nearest" });
  }, [active, filtered]);

  function onKey(e: React.KeyboardEvent) {
    // Escape is handled by useDialogA11y (stack-aware) — not here.
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter" && filtered[active]) {
      go(filtered[active].to);
    }
  }

  const groupLabel: Record<PaletteItem["group"], string> = {
    nav: "",
    setting: t("cmdk.group.settings"),
    agent: t("nav.agents"),
    task: t("nav.scheduler"),
  };

  return (
    <div className="cmdk-overlay" onClick={onClose}>
      <div className="cmdk" ref={panelRef} role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <input
          autoFocus
          placeholder={t("action.search")}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKey}
        />
        <div className="cmdk-list" ref={listRef}>
          {filtered.map((i, idx) => {
            const prev = idx > 0 ? filtered[idx - 1] : null;
            const showHead = i.group !== "nav" && i.group !== prev?.group;
            return (
              <Fragment key={i.key}>
                {showHead && (
                  <div
                    className="muted"
                    style={{ padding: "6px 10px 2px", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4 }}
                  >
                    {groupLabel[i.group]}
                  </div>
                )}
                <div
                  className={`cmdk-item ${idx === active ? "active" : ""}`}
                  onMouseEnter={() => setActive(idx)}
                  onClick={() => go(i.to)}
                >
                  <i.Icon size={16} strokeWidth={1.75} />
                  <span className="cmdk-item-text">
                    <span>
                      {highlightParts(i.label, i.ranges ?? []).map((part, n) =>
                        part.hit ? <mark key={n}>{part.text}</mark> : <Fragment key={n}>{part.text}</Fragment>,
                      )}
                    </span>
                    {i.sub && <span className="cmdk-item-sub">{i.sub}</span>}
                  </span>
                </div>
              </Fragment>
            );
          })}
          {filtered.length === 0 && (
            <div className="cmdk-item muted">{settings ? t("cmdk.noMatches") : t("cmdk.loading")}</div>
          )}
        </div>
      </div>
    </div>
  );
}
