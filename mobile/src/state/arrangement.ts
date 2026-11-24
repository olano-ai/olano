/**
 * The Chats tab's arrangement (pins, folders, order, folded sections), kept
 * per person on the deployment so it follows them to another phone or a
 * reinstall.
 *
 * Stored in the person's preferences (`GET/PUT /api/auth/me/prefs`) under one
 * key of its own, never the dashboard sidebar's: the dashboard keeps its
 * arrangement per browser on purpose, and two surfaces writing one value
 * would undo each other. A copy is kept on the phone too, so the list is
 * arranged the moment it opens; the deployment's copy replaces it once read,
 * unless the person changed something meanwhile. A runtime without the
 * preferences routes keeps the phone's copy only.
 */

import { useEffect } from "react";

import { ApiError } from "../net/api.ts";
import { EMPTY_ARRANGEMENT, parseArrangement, type Arrangement } from "../shared/dashboard.ts";
import { session } from "./session.ts";
import { createStore, useStore } from "./store.ts";

export const PREF_KEY = "mobile_chat_arrangement";
const LOCAL_PREFIX = "olano.chats.arrangement:";
const SAVE_DELAY_MS = 600;

export const arrangement = createStore<Arrangement>(EMPTY_ARRANGEMENT);

let scope = "";
let edited = false;
let localOnly = false;
let saveTimer: ReturnType<typeof setTimeout> | null = null;

function scopeOf(): string {
  const { origin, me } = session.get();
  return origin ? `${origin}|${me?.user?.id ?? "owner"}` : "";
}

function readLocal(key: string): Arrangement {
  try {
    const raw = localStorage.getItem(LOCAL_PREFIX + key);
    return raw ? parseArrangement(JSON.parse(raw)) : EMPTY_ARRANGEMENT;
  } catch {
    return EMPTY_ARRANGEMENT;
  }
}

function writeLocal(key: string, value: Arrangement): void {
  try {
    localStorage.setItem(LOCAL_PREFIX + key, JSON.stringify(value));
  } catch {
    // Private mode or full storage: the deployment's copy still holds it.
  }
}

/** Reads the arrangement for whoever is signed in now (once per person and deployment). */
export function loadArrangement(): void {
  const key = scopeOf();
  if (!key || key === scope) return;
  scope = key;
  edited = false;
  localOnly = false;
  arrangement.set(readLocal(key));
  const api = session.get().api;
  if (!api) return;
  void api
    .prefs()
    .then(({ prefs }) => {
      if (scope !== key || edited) return;
      const stored = prefs?.[PREF_KEY];
      if (stored === undefined) return;
      const value = parseArrangement(stored);
      arrangement.set(value);
      writeLocal(key, value);
    })
    .catch((error) => {
      if (error instanceof ApiError && (error.status === 404 || error.status === 405)) localOnly = true;
    });
}

/** Changes the arrangement: shown at once, saved on the phone, sent to the deployment shortly after. */
export function arrange(change: (current: Arrangement) => Arrangement): void {
  const next = change(arrangement.get());
  if (next === arrangement.get()) return;
  edited = true;
  arrangement.set(next);
  const key = scope;
  writeLocal(key, next);
  if (localOnly) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    const api = session.get().api;
    if (!api || scope !== key) return;
    void api.savePrefs({ [PREF_KEY]: arrangement.get() }).catch((error) => {
      if (error instanceof ApiError && (error.status === 404 || error.status === 405)) localOnly = true;
      // Otherwise the phone's copy holds it, and the next change sends it again.
    });
  }, SAVE_DELAY_MS);
}

export function useArrangement(): Arrangement {
  const { origin, me } = useStore(session);
  useEffect(loadArrangement, [origin, me?.user?.id]);
  return useStore(arrangement);
}

/** Forget the loaded arrangement (sign out, switch deployment): the next person reads their own. */
export function resetArrangement(): void {
  scope = "";
  edited = false;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = null;
  arrangement.set(EMPTY_ARRANGEMENT);
}
