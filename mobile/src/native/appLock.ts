/**
 * The app lock: with it on, opening Olano, and coming back to it after the
 * chosen time, asks for Face ID, Touch ID, a fingerprint or the phone's
 * passcode before anything shows. The app holds a two-week session to agents
 * that can act on your accounts; this keeps a phone left unlocked on a desk
 * from being a way in.
 *
 * The phone does the asking (OlanoShell `lockStatus` / `unlock`, over
 * LocalAuthentication on iOS and BiometricPrompt on Android), always with the
 * passcode as the fallback, so nobody is locked out by a wet finger. While the
 * app is in the background a cover hides it, so the app switcher's snapshot
 * shows nothing either.
 */

import { App as CapApp } from "@capacitor/app";
import { registerPlugin } from "@capacitor/core";

import { DEFAULT_LOCK_PREFS, locksOnReturn, parseLockPrefs, type LockKind, type LockPrefs } from "../lock/lock.ts";
import { createStore } from "../state/store.ts";
import { isNative } from "./shell.ts";

export interface LockStatus {
  /** The phone has a passcode (and so can lock the app). */
  available: boolean;
  kind: LockKind;
}

export type UnlockResult = "ok" | "cancelled" | "failed" | "unavailable";

interface LockPlugin {
  lockStatus(): Promise<LockStatus>;
  unlock(options: { reason: string }): Promise<{ ok: boolean }>;
}

const OlanoShell = registerPlugin<LockPlugin>("OlanoShell");

/** A browser has no phone lock; the end-to-end tests stand in for one through this hook. */
interface TestLock {
  status: LockStatus;
  answer: UnlockResult;
}

function testLock(): TestLock | null {
  return (window as unknown as { __olanoLock?: TestLock }).__olanoLock ?? null;
}

export async function lockStatus(): Promise<LockStatus> {
  if (!isNative()) return testLock()?.status ?? { available: false, kind: "none" };
  try {
    return await OlanoShell.lockStatus();
  } catch {
    return { available: false, kind: "none" };
  }
}

export async function unlock(reason: string): Promise<UnlockResult> {
  if (!isNative()) return testLock()?.answer ?? "unavailable";
  try {
    await OlanoShell.unlock({ reason });
    return "ok";
  } catch (error) {
    const code = (error as { code?: string } | null)?.code;
    return code === "cancelled" || code === "unavailable" ? code : "failed";
  }
}

const KEY = "olano.lock";

function readPrefs(): LockPrefs {
  try {
    return parseLockPrefs(localStorage.getItem(KEY));
  } catch {
    return DEFAULT_LOCK_PREFS;
  }
}

export const lockPrefs = createStore<LockPrefs>(readPrefs());

export function setLockPrefs(change: Partial<LockPrefs>): void {
  lockPrefs.set((prefs) => ({ ...prefs, ...change }));
  try {
    localStorage.setItem(KEY, JSON.stringify(lockPrefs.get()));
  } catch {
    // Not remembered past this launch; it still applies now.
  }
}

export interface LockState {
  /** Nothing shows until the phone says it is you. */
  locked: boolean;
  /** In the background: a plain cover, for the app switcher's snapshot. */
  covered: boolean;
}

/** Locked from the first frame when the lock is on, so nothing flashes before it. */
export const lockState = createStore<LockState>({ locked: readPrefs().enabled, covered: false });

let leftAt: number | null = null;

/** Inactive (the app switcher, a call, Control Center): cover, but do not count it as leaving. */
function cover(): void {
  if (lockPrefs.get().enabled) lockState.set((s) => ({ ...s, covered: true }));
}

/** In the background: the time it left decides whether coming back asks again. */
function left(): void {
  if (!lockPrefs.get().enabled) return;
  if (leftAt === null) leftAt = Date.now();
  cover();
}

function returned(): void {
  const lock = leftAt !== null && locksOnReturn(lockPrefs.get(), leftAt, Date.now());
  leftAt = null;
  lockState.set((s) => ({ locked: s.locked || lock, covered: false }));
}

/** Asks the phone; unlocks on success. */
export async function tryUnlock(): Promise<UnlockResult> {
  const result = await unlock("Unlock Olano");
  if (result === "ok") lockState.set({ locked: false, covered: false });
  return result;
}

/** Wires the background cover and the lock on return; returns the undo. */
export function installAppLock(): () => void {
  const onVisibility = () => (document.hidden ? left() : returned());
  document.addEventListener("visibilitychange", onVisibility);
  const cleanups = [() => document.removeEventListener("visibilitychange", onVisibility)];
  if (isNative()) {
    // "pause" comes before the snapshot is taken; the cover must be up by then.
    const pause = CapApp.addListener("pause", left);
    const resume = CapApp.addListener("resume", returned);
    const state = CapApp.addListener("appStateChange", ({ isActive }) => (isActive ? returned() : cover()));
    cleanups.push(() => void pause.then((h) => h.remove()));
    cleanups.push(() => void resume.then((h) => h.remove()));
    cleanups.push(() => void state.then((h) => h.remove()));
  }
  return () => cleanups.forEach((undo) => undo());
}
