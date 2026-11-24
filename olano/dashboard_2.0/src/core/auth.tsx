import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "./api";
import { refreshCapabilities } from "./capabilities";
import {
  DEFAULT_UI_MODE,
  type AgentAccessRow,
  type MeResponse,
  type UiMode,
  type User,
} from "./types";

/** Per-device memory of the last shell used here. See switchUiMode(). */
export const UI_MODE_HINT_KEY = "lc.uiModeHint";

/**
 * The shell to *dress the login screen as*, before anyone has identified
 * themselves.
 *
 * This is the one genuinely unanswerable moment in the whole feature: the
 * server cannot know your mode until it knows who you are, and it cannot know
 * who you are until you have logged in. So the login screen either guesses or
 * stays neutral. It guesses from this device's last sign-in, which is right
 * for the overwhelmingly common case (one person, one laptop) and harmless
 * when wrong — the correct shell still mounts immediately after auth, because
 * the resolved mode rides the same /api/auth/me response that ends the gate.
 */
export function uiModeHint(): UiMode {
  try {
    return localStorage.getItem(UI_MODE_HINT_KEY) === "simple" ? "simple" : DEFAULT_UI_MODE;
  } catch {
    return DEFAULT_UI_MODE;
  }
}

// Auth phases drive the top-level gate:
//   loading → first /api/auth/me roundtrip
//   setup   → no users yet (first-run) → bootstrap screen
//   login   → users exist, not authenticated → login screen
//   ready   → authenticated (or single-user implicit owner)
export type AuthPhase = "loading" | "setup" | "login" | "ready";

interface AuthState {
  phase: AuthPhase;
  user: User | null;
  multiUser: boolean;
  // Show "Langfuse" links (reply, Insights thread). Resolved server-side.
  langfuseLinks: boolean;
  // Per-agent access rows from /api/auth/me ({agent_name, role}). For admins
  // every visible agent carries role "admin"; for members only granted agents
  // appear. Refreshed with the rest of the session state.
  agents: AgentAccessRow[];
  // Which shell to render: "advanced" (the full dashboard) or "simple" (the
  // bot chat app). Resolved server-side and delivered on /api/auth/me, which
  // this provider already awaits before phase becomes "ready" — so the right
  // shell mounts on the first paint, with no flash of the wrong one.
  //
  // Presentation only. It is never an authorization input: every real guard
  // (is_admin, _member_forbidden, assert_agent_access) is enforced on the
  // server regardless of which shell the browser happens to be showing.
  uiMode: UiMode;
  // True for the moment between "switch requested" and "the other shell has
  // mounted". The app paints a cover over that gap: the two shells share no
  // layout, so an uncovered swap is a hard cut from a 25-route console to a
  // chat app, which reads as a crash rather than a choice.
  switchingUiMode: boolean;
  /** Switch YOUR OWN shell. Admin-only server-side; resolves false if refused. */
  switchUiMode(mode: UiMode): Promise<boolean>;
  // True while the first-run setup wizard must gate the app (admin on a
  // deployed instance, tour not yet completed/skipped). Cleared by refresh()
  // after the wizard posts its completion.
  onboardingRequired: boolean;
  // False while the deployment's provisioned temporary admin password is still
  // in play. True once any rotation is recorded — including the owner creating
  // the first account themselves, where the temporary password was never
  // usable. The wizard's profile step drops its password half when true.
  adminPasswordRotated: boolean;
  // Its vault twin: false while the provisioned vault passphrase is still in
  // play; true once a rekey is recorded. The profile step drops its vault
  // half when true (and the whole step when both are).
  vaultPasswordRotated: boolean;
  // Whether the current human may MANAGE an agent (global admin or that
  // agent's owner) — the gate for owner surfaces like the Variables tab.
  canAdminAgent(name: string): boolean;
  refresh(): Promise<void>;
  login(email: string, password: string): Promise<void>;
  bootstrap(
    email: string,
    password: string,
    name: string,
    username?: string,
    setupCode?: string,
  ): Promise<void>;
  logout(): Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<AuthPhase>("loading");
  const [user, setUser] = useState<User | null>(null);
  const [multiUser, setMultiUser] = useState(false);
  const [langfuseLinks, setLangfuseLinks] = useState(false);
  const [agents, setAgents] = useState<AgentAccessRow[]>([]);
  const [onboardingRequired, setOnboardingRequired] = useState(false);
  const [adminPasswordRotated, setAdminPasswordRotated] = useState(false);
  const [vaultPasswordRotated, setVaultPasswordRotated] = useState(false);
  const [uiMode, setUiMode] = useState<UiMode>(DEFAULT_UI_MODE);
  const [switchingUiMode, setSwitchingUiMode] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const me = await api.get<MeResponse>("/api/auth/me");
      setUser(me.user);
      setMultiUser(me.multi_user);
      setLangfuseLinks(me.langfuse_links === true);
      setAgents(me.agents ?? []);
      setOnboardingRequired(Boolean(me.onboarding?.required));
      setAdminPasswordRotated(Boolean(me.onboarding?.admin_password_rotated));
      setVaultPasswordRotated(Boolean(me.onboarding?.vault_password_rotated));
      // Fall back rather than trusting the wire: an older backend omits the
      // key, and an unknown future value must not blank the UI.
      const resolved: UiMode = me.ui_mode === "simple" ? "simple" : DEFAULT_UI_MODE;
      setUiMode(resolved);
      // Also record it per-device: a member never calls switchUiMode (an admin
      // assigned their mode), so without this their login screen would be
      // dressed as the shell they do not use.
      try {
        localStorage.setItem(UI_MODE_HINT_KEY, resolved);
      } catch {
        /* see switchUiMode */
      }
      setPhase("ready");
    } catch (e) {
      setUser(null);
      setLangfuseLinks(false);
      setAgents([]);
      setOnboardingRequired(false);
      setAdminPasswordRotated(false);
      setVaultPasswordRotated(false);
      setUiMode(DEFAULT_UI_MODE);
      setPhase(e instanceof ApiError && e.detail === "setup_required" ? "setup" : "login");
    }
    refreshCapabilities();
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const switchUiMode = useCallback(async (mode: UiMode) => {
    if (mode === uiMode) return true;
    setSwitchingUiMode(true);
    try {
      await api.put("/api/auth/me/ui-mode", { ui_mode: mode });
      // Remember the choice for THIS DEVICE so the next sign-in screen can be
      // dressed as the shell this person actually uses. The server is still
      // the source of truth — this is only a hint for the pre-auth moment,
      // where nobody's identity is known yet and so neither is their mode.
      try {
        localStorage.setItem(UI_MODE_HINT_KEY, mode);
      } catch {
        // Private windows and blocked site data throw. A missing hint costs a
        // neutral login screen, which is a fine outcome.
      }
      setUiMode(mode);
      // Hold the cover for one paint after the swap. Clearing it in the same
      // tick lets the incoming shell's first, unstyled frame through.
      requestAnimationFrame(() => setTimeout(() => setSwitchingUiMode(false), 180));
      return true;
    } catch {
      setSwitchingUiMode(false);
      return false;
    }
  }, [uiMode]);

  const login = useCallback(
    async (email: string, password: string) => {
      await api.post("/api/auth/login", { email, password });
      await refresh();
    },
    [refresh],
  );

  const bootstrap = useCallback(
    async (email: string, password: string, name: string, username = "", setupCode = "") => {
      await api.post("/api/auth/bootstrap", {
        email,
        password,
        name,
        username,
        setup_code: setupCode.trim(),
      });
      await refresh();
    },
    [refresh],
  );

  const logout = useCallback(async () => {
    await api.post("/api/auth/logout");
    await refresh();
  }, [refresh]);

  // Mirrors the backend's can_admin_agent: global admin, or an explicit
  // per-agent "owner" grant. (Single-user installs report is_admin=true for
  // the implicit owner, so this stays true there.) The server re-checks on
  // every call — this only decides what UI to show.
  const canAdminAgent = useCallback(
    (name: string) =>
      !!user?.is_admin || agents.some((a) => a.agent_name === name && a.role === "owner"),
    [user, agents],
  );

  return (
    <Ctx.Provider
      value={{
        phase,
        user,
        multiUser,
        langfuseLinks,
        agents,
        uiMode,
        switchingUiMode,
        switchUiMode,
        onboardingRequired,
        adminPasswordRotated,
        vaultPasswordRotated,
        canAdminAgent,
        refresh,
        login,
        bootstrap,
        logout,
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
