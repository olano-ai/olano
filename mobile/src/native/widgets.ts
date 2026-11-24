/**
 * The Home Screen widgets (iOS, ios/App/OlanoWidgets). They draw from the
 * deployment on their own, with the session the app hands them through
 * `OlanoShell.syncWidgets()`: the address, the cookie, and whether the person
 * is an admin (Start / Stop on the Agent card). The app calls it when the
 * session is ready, when it comes back to the front (a widget added while
 * the app sat in the background is drawn the moment the app is opened), when
 * the agent list loads, when a turn ends and when it signs out, and the
 * system redraws the widgets each time. Calls are coalesced: a burst of
 * finished turns is one hand-off.
 *
 * Android has no widget extension yet, and a desktop browser nothing to
 * hand over, so everywhere but iOS this is a no-op.
 */

import { registerPlugin } from "@capacitor/core";

import { platform } from "./shell.ts";

interface WidgetsPlugin {
  syncWidgets(options: { admin?: boolean }): Promise<{ shared: boolean }>;
}

const OlanoShell = registerPlugin<WidgetsPlugin>("OlanoShell");

/** How long after a request the hand-off happens, so a burst is one call. */
const COALESCE_MS = 1500;

let timer: ReturnType<typeof setTimeout> | null = null;
let admin: boolean | undefined;

/** Tells the widgets the deployment's state changed. */
export function syncWidgets(options: { admin?: boolean } = {}): void {
  if (platform() !== "ios") return;
  if (options.admin !== undefined) admin = options.admin;
  if (timer !== null) return;
  timer = setTimeout(() => {
    timer = null;
    OlanoShell.syncWidgets(admin === undefined ? {} : { admin }).catch(() => {
      // An older build without the method.
    });
  }, COALESCE_MS);
}
