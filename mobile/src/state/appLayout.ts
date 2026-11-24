/**
 * The app's layout as the deployment's admin set it (dashboard Config >
 * Mobile app, `GET /api/mobile/config`): which tabs, Inbox sections and
 * Settings rows this app shows, and where voice notes are transcribed by
 * default.
 *
 * Pure: no DOM, no network, so `node --test` covers it. The store that
 * fetches and keeps it is `layoutStore.ts`.
 */

import type { TranscribeOn, VoicePrefs } from "./voicePrefs.ts";

export type InboxSection = "approvals" | "alerts" | "issues";

export interface AppLayout {
  revision: string;
  cortexTab: boolean;
  inbox: { enabled: boolean; approvals: boolean; alerts: boolean; issues: boolean };
  settingsTab: { files: boolean; creditsUsage: boolean; transcribeOnPhone: boolean };
  transcribeOnPhoneDefault: boolean;
}

/**
 * Before the deployment has answered, and on a deployment too old to have
 * the setting: everything shown, voice notes transcribed on the deployment
 * (the deployment's own default).
 */
export const DEFAULT_LAYOUT: AppLayout = {
  revision: "",
  cortexTab: true,
  inbox: { enabled: true, approvals: true, alerts: true, issues: true },
  settingsTab: { files: true, creditsUsage: true, transcribeOnPhone: true },
  transcribeOnPhoneDefault: false,
};

function flag(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function group(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

/** The deployment's answer (snake_case, possibly partial or from another version) as a layout. */
export function parseLayout(raw: unknown): AppLayout {
  const body = group(raw);
  const inbox = group(body.inbox);
  const settings = group(body.settings_tab);
  const d = DEFAULT_LAYOUT;
  return {
    revision: typeof body.revision === "string" ? body.revision : "",
    cortexTab: flag(body.cortex_tab, d.cortexTab),
    inbox: {
      enabled: flag(inbox.enabled, d.inbox.enabled),
      approvals: flag(inbox.approvals, d.inbox.approvals),
      alerts: flag(inbox.alerts, d.inbox.alerts),
      issues: flag(inbox.issues, d.inbox.issues),
    },
    settingsTab: {
      files: flag(settings.files, d.settingsTab.files),
      creditsUsage: flag(settings.credits_usage, d.settingsTab.creditsUsage),
      transcribeOnPhone: flag(settings.transcribe_on_phone, d.settingsTab.transcribeOnPhone),
    },
    transcribeOnPhoneDefault: flag(body.transcribe_on_phone_default, d.transcribeOnPhoneDefault),
  };
}

/** The Inbox sections to show, in order. Empty when the Inbox is off. */
export function inboxSections(layout: AppLayout): InboxSection[] {
  if (!layout.inbox.enabled) return [];
  return (["approvals", "alerts", "issues"] as const).filter((s) => layout.inbox[s]);
}

/** Whether the Inbox tab is in the tab bar: on, with at least one section to show. */
export function showsInbox(layout: AppLayout): boolean {
  return inboxSections(layout).length > 0;
}

/** The first tab segment of `path` ("cortex", "inbox", "settings", ...). */
function tabOf(path: string): string {
  return path.split("/")[1] || "";
}

/**
 * Where a path the layout hides should go instead, or null when it is
 * shown. A phone that was on a tab when the admin hid it lands on Chats; a
 * hidden Settings screen falls back to Settings itself.
 */
export function redirectFor(path: string, layout: AppLayout): string | null {
  const tab = tabOf(path);
  if (tab === "cortex" && !layout.cortexTab) return "/chats";
  if (tab === "inbox" && !showsInbox(layout)) return "/chats";
  if (/^\/inbox\/issues\//.test(path) && !inboxSections(layout).includes("issues")) return "/chats";
  if (/^\/settings\/files(\/|$)/.test(path) && !layout.settingsTab.files) return "/settings";
  if (/^\/settings\/usage(\/|$)/.test(path) && !layout.settingsTab.creditsUsage) return "/settings";
  if (/^\/chats\/[^/]+\/_files(\/|$)/.test(path) && !layout.settingsTab.files) {
    return path.replace(/\/_files(\/.*)?$/, "/_info");
  }
  return null;
}

/**
 * Where voice notes become words on this phone. The person's own choice
 * counts only while the admin shows the switch and only once they have made
 * it; otherwise the deployment's default applies.
 */
export function transcribeOn(prefs: Pick<VoicePrefs, "transcribeOn" | "transcribeChosen">, layout: AppLayout): TranscribeOn {
  if (layout.settingsTab.transcribeOnPhone && prefs.transcribeChosen) return prefs.transcribeOn;
  return layout.transcribeOnPhoneDefault ? "phone" : "deployment";
}
