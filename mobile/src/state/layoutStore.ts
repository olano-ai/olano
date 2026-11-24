/**
 * The app layout from the deployment (`appLayout.ts`), fetched when the
 * session is ready and every time the app comes back to the foreground, so
 * an admin's change in the dashboard reaches the phone the next time it is
 * opened. The last copy is kept per deployment, so a cold start draws the
 * tabs the admin chose before the deployment has answered, never the hidden
 * ones for a moment.
 */

import { ApiError } from "../net/api.ts";
import { DEFAULT_LAYOUT, parseLayout, type AppLayout } from "./appLayout.ts";
import { currentApi, session } from "./session.ts";
import { createStore, useStore } from "./store.ts";

const KEY = "olano.layout.v1:";

function storageKey(): string | null {
  const origin = session.get().origin;
  return origin ? KEY + origin : null;
}

function readKept(): AppLayout {
  const key = storageKey();
  if (!key) return DEFAULT_LAYOUT;
  try {
    const raw = localStorage.getItem(key);
    return raw ? parseLayout(JSON.parse(raw)) : DEFAULT_LAYOUT;
  } catch {
    return DEFAULT_LAYOUT;
  }
}

function keep(raw: unknown): void {
  const key = storageKey();
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify(raw));
  } catch {
    // Not kept past this launch; it still applies now.
  }
}

export const appLayout = createStore<AppLayout>(DEFAULT_LAYOUT);

let inflight: Promise<void> | null = null;

/** Asks the deployment for its layout; a failure keeps what is held. */
export function refreshLayout(): Promise<void> {
  if (session.get().phase !== "ready") return Promise.resolve();
  if (inflight) return inflight;
  // The copy kept from last time first, so a cold start draws the right tabs.
  if (!appLayout.get().revision) {
    const kept = readKept();
    if (kept.revision) appLayout.set(kept);
  }
  inflight = currentApi()
    .mobileConfig()
    .then((raw) => {
      const next = parseLayout(raw);
      keep(raw);
      if (next.revision !== appLayout.get().revision || !next.revision) appLayout.set(next);
    })
    .catch((error: unknown) => {
      // A deployment that predates the setting shows everything.
      if (error instanceof ApiError && error.status === 404) appLayout.set(DEFAULT_LAYOUT);
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** Signed out or switched deployment: back to everything shown. */
export function resetLayout(): void {
  appLayout.set(DEFAULT_LAYOUT);
}

export function useAppLayout(): AppLayout {
  return useStore(appLayout);
}
