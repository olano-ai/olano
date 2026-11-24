/**
 * The app lock (Settings > Lock with Face ID): what is remembered on this
 * phone, when coming back asks to unlock again, and what the phone's unlock is
 * called. Pure, so node's test runner can import it; native/appLock.ts does
 * the asking.
 */

/** How the phone proves it is you. */
export type LockKind = "face" | "fingerprint" | "optic" | "biometric" | "passcode" | "none";

export interface LockPrefs {
  enabled: boolean;
  /** Seconds the app may sit in the background before it asks again (0: every time). */
  afterSeconds: number;
}

export const LOCK_AFTER_CHOICES: { seconds: number; label: string }[] = [
  { seconds: 0, label: "Immediately" },
  { seconds: 60, label: "After 1 minute" },
  { seconds: 300, label: "After 5 minutes" },
  { seconds: 900, label: "After 15 minutes" },
];

export const DEFAULT_LOCK_PREFS: LockPrefs = { enabled: false, afterSeconds: 0 };

export function parseLockPrefs(raw: string | null): LockPrefs {
  let value: Record<string, unknown> = {};
  try {
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (parsed && typeof parsed === "object") value = parsed as Record<string, unknown>;
  } catch {
    // Unreadable: off.
  }
  const after = Number(value.afterSeconds);
  return {
    enabled: value.enabled === true,
    afterSeconds: LOCK_AFTER_CHOICES.some((c) => c.seconds === after) ? after : 0,
  };
}

/** Coming back from the background at `now`: ask to unlock again? */
export function locksOnReturn(prefs: LockPrefs, leftAt: number | null, now: number): boolean {
  if (!prefs.enabled) return false;
  if (leftAt === null) return true;
  return now - leftAt >= prefs.afterSeconds * 1000;
}

/** The phone's unlock by name, as its own Settings call it. */
export function lockName(kind: LockKind, platform: string): string {
  switch (kind) {
    case "face":
      return platform === "ios" ? "Face ID" : "face unlock";
    case "fingerprint":
      return platform === "ios" ? "Touch ID" : "fingerprint";
    case "optic":
      return "Optic ID";
    case "biometric":
      return platform === "ios" ? "Face ID" : "fingerprint or face unlock";
    default:
      return "passcode";
  }
}
