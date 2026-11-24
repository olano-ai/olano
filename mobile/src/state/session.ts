/**
 * Which deployment the app is connected to and who is signed in.
 *
 * Phases, in order: `starting` (reading the saved address), `connect` (no
 * address: the connect screen), `signin` (an address but no session),
 * `setup` (the deployment has no accounts yet: finish setup in a browser),
 * `ready` (signed in, or a single-user deployment with no sign-in at all).
 */

import { Api, ApiError, isNetworkError, onSignedOut } from "../net/api.ts";
import { clearNativeCookies } from "../net/transport.ts";
import type { Me } from "../net/types.ts";
import { clearServer, getServer, messageOf, setServer } from "../native/shell.ts";
import { syncWidgets } from "../native/widgets.ts";
import { clearCached, defaultCacheStore } from "./persisted.ts";
import { clearAvatars } from "../ui/avatarCache.ts";
import { isDemoOrigin } from "./appConfig.ts";
import { createStore } from "./store.ts";

export type Phase = "starting" | "connect" | "signin" | "setup" | "ready" | "offline";

export interface Session {
  phase: Phase;
  origin: string | null;
  api: Api | null;
  me: Me | null;
  /** Why the app is on the offline screen. */
  error: string | null;
}

export const session = createStore<Session>({ phase: "starting", origin: null, api: null, me: null, error: null });

export function currentApi(): Api {
  const api = session.get().api;
  if (!api) throw new Error("Not connected to a deployment.");
  return api;
}

/** Reads `/api/auth/me` and moves to the phase it implies. */
export async function resolveSession(origin: string): Promise<void> {
  const api = new Api(origin);
  session.set((s) => ({ ...s, origin, api, error: null }));
  try {
    const me = await api.me();
    session.set((s) => ({ ...s, phase: "ready", me }));
    syncWidgets({ admin: me.user.is_admin });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      session.set((s) => ({ ...s, phase: error.code === "setup_required" ? "setup" : "signin", me: null }));
    } else {
      const message = isNetworkError(error)
        ? `Couldn't reach ${hostOf(origin)}. Check your connection.`
        : messageOf(error, "The deployment did not answer as expected.");
      session.set((s) => ({ ...s, phase: "offline", error: message }));
    }
  }
}

export async function startSession(): Promise<void> {
  const origin = await getServer().catch(() => null);
  if (!origin) {
    session.set({ phase: "connect", origin: null, api: null, me: null, error: null });
    return;
  }
  await resolveSession(origin);
}

/** Saves a probed deployment and continues to sign in (or straight in). */
export async function connectTo(origin: string): Promise<void> {
  const saved = await setServer(origin);
  await resolveSession(saved);
}

/** Where the phone keeps the origin of a demo it entered, so an expired demo session can be renewed. */
export const DEMO_ORIGIN_KEY = "olano.demoOrigin";

function storage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function rememberDemo(origin: string | null): void {
  try {
    if (origin) storage()?.setItem(DEMO_ORIGIN_KEY, origin);
    else storage()?.removeItem(DEMO_ORIGIN_KEY);
  } catch {
    // A convenience: without it an expired demo session asks for a password.
  }
}

/** True when the app is connected to the demo deployment (entered through "Try the demo"). */
export function isDemo(origin: string | null): boolean {
  if (!origin) return false;
  try {
    if (storage()?.getItem(DEMO_ORIGIN_KEY) === origin) return true;
  } catch {
    // Fall through to the config.
  }
  return isDemoOrigin(origin);
}

/** Why the demo could not be entered, in words for the connect screen. */
export function describeDemoFailure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return "The demo is busy right now. Try again in a minute.";
    if (error.status === 404 || error.status === 403 || error.status === 409) {
      return "The demo is not available right now. Try again later, or connect to your own deployment.";
    }
  }
  if (isNetworkError(error)) return "Couldn't reach the demo. Check your connection and try again.";
  return messageOf(error, "The demo did not open. Try again in a minute.");
}

/**
 * Enters the demo deployment: saves it, asks it for a demo session
 * (`POST /api/auth/demo`, no password: the demo box signs every visitor in as
 * its one demo member) and continues as after a sign-in. On failure nothing
 * is kept and the error propagates for `describeDemoFailure`.
 */
export async function enterDemo(origin: string): Promise<void> {
  const saved = await setServer(origin);
  try {
    await new Api(saved).demoSignIn();
  } catch (error) {
    if (!session.get().origin) await clearServer().catch(() => undefined);
    throw error;
  }
  rememberDemo(saved);
  await resolveSession(saved);
}

/** Renews an expired demo session from the sign-in screen. */
export async function renewDemo(): Promise<void> {
  const api = currentApi();
  await api.demoSignIn();
  const me = await api.me();
  session.set((s) => ({ ...s, phase: "ready", me }));
  syncWidgets({ admin: me.user.is_admin });
}

export async function signIn(email: string, password: string): Promise<void> {
  const api = currentApi();
  await api.login(email.trim(), password);
  const me = await api.me();
  session.set((s) => ({ ...s, phase: "ready", me }));
  syncWidgets({ admin: me.user.is_admin });
}

export async function signOut(): Promise<void> {
  const api = session.get().api;
  await api?.logout().catch(() => undefined);
  await clearNativeCookies();
  // The lists kept for a fast launch (state/persisted.ts) were this person's;
  // a deliberate sign-out leaves none of them on the phone. A session that
  // merely expired keeps them: they are filed under the same person, who is
  // about to sign in again.
  clearCached(defaultCacheStore());
  clearAvatars(defaultCacheStore());
  session.set((s) => ({ ...s, phase: "signin", me: null }));
  // The widgets must not keep a session the person ended.
  syncWidgets({ admin: false });
}

/** Forgets the deployment (and its session on this device): back to the connect screen. */
export async function switchDeployment(): Promise<void> {
  const api = session.get().api;
  await api?.logout().catch(() => undefined);
  await clearNativeCookies();
  clearCached(defaultCacheStore());
  clearAvatars(defaultCacheStore());
  await clearServer().catch(() => undefined);
  rememberDemo(null);
  session.set({ phase: "connect", origin: null, api: null, me: null, error: null });
}

export function hostOf(origin: string): string {
  try {
    return new URL(origin).host;
  } catch {
    return origin;
  }
}

// A 401 from any call means the session is gone.
onSignedOut(() => {
  const s = session.get();
  if (s.phase === "ready" && s.me?.multi_user !== false) {
    session.set({ ...s, phase: "signin", me: null });
  }
});
