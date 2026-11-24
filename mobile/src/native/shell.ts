/**
 * The app's own native plugins and what the rest of the code asks of the
 * platform. `OlanoShell` remembers which deployment the app talks to
 * (OlanoShellPlugin.swift / .java); in a desktop browser (`npm run dev`, the
 * end-to-end tests) the same contract is kept in localStorage.
 */

import { Capacitor, registerPlugin } from "@capacitor/core";

export interface AppInfo {
  /** Marketing version, e.g. "1.0.0" (versionName / CFBundleShortVersionString). */
  version: string;
  /** Build number, e.g. "7" (versionCode / CFBundleVersion). */
  build: string;
  platform: "android" | "ios" | "web";
}

interface OlanoShellPlugin {
  getServer(): Promise<{ url: string | null }>;
  /** Validates and saves; resolves the origin it saved. Rejects with a readable message. */
  setServer(options: { url: string }): Promise<{ url: string }>;
  clearServer(): Promise<void>;
  getAppInfo(): Promise<AppInfo>;
}

const OlanoShell = registerPlugin<OlanoShellPlugin>("OlanoShell");

const WEB_SERVER_KEY = "olano.server";

/** True inside the app with the native bridge (not in a desktop browser). */
export function isNative(): boolean {
  return Capacitor.isNativePlatform();
}

export function platform(): "ios" | "android" | "web" {
  const p = Capacitor.getPlatform();
  return p === "ios" || p === "android" ? p : "web";
}

/**
 * Debug builds only (plain http:// to a box on the local network). The native
 * side refuses http:// in release builds regardless; this only decides what
 * the connect screen offers.
 */
export function isDebugBuild(): boolean {
  if (!isNative()) return true;
  return (Capacitor as unknown as { DEBUG?: boolean }).DEBUG === true;
}

export async function getServer(): Promise<string | null> {
  if (isNative()) return (await OlanoShell.getServer()).url;
  try {
    return localStorage.getItem(WEB_SERVER_KEY);
  } catch {
    return null;
  }
}

export async function setServer(origin: string): Promise<string> {
  if (isNative()) return (await OlanoShell.setServer({ url: origin })).url;
  try {
    localStorage.setItem(WEB_SERVER_KEY, origin);
  } catch {
    // A desktop browser without storage: the address lasts until reload.
  }
  return origin;
}

export async function clearServer(): Promise<void> {
  if (isNative()) {
    await OlanoShell.clearServer();
    return;
  }
  try {
    localStorage.removeItem(WEB_SERVER_KEY);
  } catch {
    // Nothing stored.
  }
}

export async function appInfo(): Promise<AppInfo> {
  if (isNative()) {
    try {
      return await OlanoShell.getAppInfo();
    } catch {
      // Fall through to the web answer.
    }
  }
  return { version: "dev", build: "", platform: "web" };
}

/** The message of a rejected plugin call or thrown error, without the "Error: " prefix. */
export function messageOf(error: unknown, fallback = "Something went wrong. Try again."): string {
  if (error && typeof error === "object" && "message" in error) {
    const text = String((error as { message: unknown }).message ?? "").trim();
    if (text) return text;
  }
  return fallback;
}
