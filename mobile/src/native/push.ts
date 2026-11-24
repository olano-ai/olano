/**
 * Push notifications: the phone's token goes to the deployment
 * (`POST /api/push/devices`), the deployment decides what is worth a
 * notification, and Olano Cloud delivers it through APNs / FCM. Apple and
 * Google credentials never reach the deployment or the app.
 *
 * Deployments without the push backend answer `/api/push/*` with 404; the
 * Settings screen then says push needs a newer Olano.
 */

import { Capacitor } from "@capacitor/core";
import { PushNotifications } from "@capacitor/push-notifications";

import { ApiError } from "../net/api.ts";
import { say } from "../state/notice.ts";
import { createStore } from "../state/store.ts";
import { session } from "../state/session.ts";
import { answerFor, answerNotice, routeFor, type NotificationAnswer } from "./pushActions.ts";

export { routeFor };
import { appInfo, isNative, platform } from "./shell.ts";

export type PushKind = "replies" | "approvals" | "cortex" | "alerts";
export const PUSH_KINDS: { kind: PushKind; label: string; help: string }[] = [
  { kind: "replies", label: "Replies", help: "When an agent finishes answering and you are not looking." },
  { kind: "approvals", label: "Approvals", help: "When an agent needs your OK before it acts." },
  { kind: "cortex", label: "Cortex", help: "Urgent updates the agents post on their own." },
  { kind: "alerts", label: "Alerts", help: "Problems on the deployment (admins only)." },
];

export type EnableResult =
  | { ok: true; events: PushKind[] }
  | { ok: false; reason: "unsupported" | "denied" | "prompt" | "token" | "server" | "old_runtime"; detail?: string };

const DEVICE_KEY = "olano.pushDevice";

/** Where a tapped notification wants to go; the router component consumes it. */
export const pendingRoute = createStore<string | null>(null);

export function pushSupported(): boolean {
  return isNative() && Capacitor.isPluginAvailable("PushNotifications");
}

function deviceKey(): string {
  return `${DEVICE_KEY}:${session.get().origin ?? ""}`;
}

export function savedDevice(): { id: string; events: PushKind[] } | null {
  try {
    const raw = localStorage.getItem(deviceKey());
    return raw ? (JSON.parse(raw) as { id: string; events: PushKind[] }) : null;
  } catch {
    return null;
  }
}

function saveDevice(device: { id: string; events: PushKind[] } | null): void {
  try {
    if (device) localStorage.setItem(deviceKey(), JSON.stringify(device));
    else localStorage.removeItem(deviceKey());
  } catch {
    // Best effort: the next launch registers again and learns the id.
  }
}

export async function permission(): Promise<"granted" | "denied" | "prompt" | "unsupported"> {
  if (!pushSupported()) return "unsupported";
  try {
    const result = await PushNotifications.checkPermissions();
    if (result.receive === "granted") return "granted";
    if (result.receive === "denied") return "denied";
    return "prompt";
  } catch {
    return "unsupported";
  }
}

function requestToken(): Promise<string> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const handles: Promise<{ remove: () => Promise<void> }>[] = [];
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      for (const handle of handles) void handle.then((h) => h.remove());
      fn();
    };
    handles.push(PushNotifications.addListener("registration", (t) => finish(() => (t.value ? resolve(t.value) : reject(new Error("empty token"))))));
    handles.push(PushNotifications.addListener("registrationError", (e) => finish(() => reject(new Error(e.error || "registration failed")))));
    const timer = setTimeout(() => finish(() => reject(new Error("The phone did not return a push token in time."))), 15000);
    PushNotifications.register().catch((error: unknown) => finish(() => reject(error instanceof Error ? error : new Error(String(error)))));
  });
}

/**
 * Makes sure this phone gets this deployment's notifications. `prompt: true`
 * (a button the person pressed) may show the system dialog; `prompt: false`
 * (every launch) only refreshes a permission already given.
 */
export async function enablePush(options: { prompt: boolean; events?: PushKind[] }): Promise<EnableResult> {
  if (!pushSupported()) return { ok: false, reason: "unsupported" };
  const api = session.get().api;
  if (!api) return { ok: false, reason: "server" };
  let state = await permission();
  if (state === "prompt" && options.prompt) {
    try {
      const result = await PushNotifications.requestPermissions();
      state = result.receive === "granted" ? "granted" : result.receive === "denied" ? "denied" : "prompt";
    } catch {
      state = "denied";
    }
  }
  if (state !== "granted") return { ok: false, reason: state === "unsupported" ? "unsupported" : state };
  let token: string;
  try {
    token = await requestToken();
  } catch (error) {
    return { ok: false, reason: "token", detail: (error as Error).message };
  }
  const info = await appInfo();
  try {
    const events = options.events ?? savedDevice()?.events;
    const { device } = await api.registerDevice({
      token,
      platform: platform() === "android" ? "android" : "ios",
      name: platform() === "android" ? "Android phone" : /iPad/.test(navigator.userAgent) ? "iPad" : "iPhone",
      app_version: info.version,
      ...(events ? { events } : {}),
    });
    const saved = { id: device.id, events: (device.events ?? []) as PushKind[] };
    saveDevice(saved);
    return { ok: true, events: saved.events };
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return { ok: false, reason: "old_runtime" };
    return { ok: false, reason: "server", detail: (error as Error).message };
  }
}

export async function setPushEvents(events: PushKind[]): Promise<boolean> {
  const device = savedDevice();
  const api = session.get().api;
  if (!device || !api) return false;
  try {
    const result = await api.updateDevice(device.id, events);
    saveDevice({ id: device.id, events: (result.device.events ?? events) as PushKind[] });
    return true;
  } catch {
    return false;
  }
}

/** Stops this phone's notifications from this deployment (sign out, switch, the toggle). */
export async function disablePush(): Promise<void> {
  const device = savedDevice();
  const api = session.get().api;
  saveDevice(null);
  if (device && api) await api.removeDevice(device.id).catch(() => undefined);
}

let tapsInstalled = false;

/** Resolves once the app is signed in to its deployment (a button can launch it cold). */
function signedIn(timeoutMs = 20000): Promise<boolean> {
  if (session.get().phase === "ready") return Promise.resolve(true);
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      stop();
      resolve(false);
    }, timeoutMs);
    const stop = session.subscribe(() => {
      if (session.get().phase !== "ready") return;
      clearTimeout(timer);
      stop();
      resolve(true);
    });
  });
}

/** Sends the answer a notification's Approve or Decline button gave. */
async function answer(decision: NotificationAnswer): Promise<void> {
  if (!(await signedIn())) {
    say(answerNotice(decision, "failed"));
    return;
  }
  const api = session.get().api;
  if (!api) return;
  try {
    if (decision.kind === "chat") {
      const type = decision.approve ? ("approve" as const) : ("reject" as const);
      await api.answerApproval(decision.threadId, Array.from({ length: decision.count }, () => ({ type })));
    } else {
      await api.hitlResolve(decision.id, decision.approve);
    }
    say(answerNotice(decision, "done"));
  } catch (error) {
    const gone = error instanceof ApiError && (error.status === 404 || error.status === 409);
    say(answerNotice(decision, gone ? "gone" : "failed"));
  }
}

/**
 * Taps on notifications open the conversation or the Inbox; an approval's
 * Approve or Decline button answers it too (pushActions.ts). Once per launch.
 */
export function installPushTaps(): void {
  if (tapsInstalled || !pushSupported()) return;
  tapsInstalled = true;
  void PushNotifications.addListener("pushNotificationActionPerformed", (action) => {
    const data = (action.notification.data ?? {}) as Record<string, unknown>;
    const decision = answerFor(action.actionId, data);
    if (decision) void answer(decision);
    const route = routeFor(data);
    if (route) pendingRoute.set(route);
  });
}
