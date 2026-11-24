/**
 * Files and text shared into Olano from another app.
 *
 * iOS: "Olano" in the share sheet is the share extension
 * (ios/App/ShareExtension), which keeps links, photos, files and text in the
 * App Group and opens `olano://share`; the screens ask `OlanoShell.takeShared()`
 * for them then, at launch and on every resume (in case the extension could
 * not open the app). The app also opens any document from Files ("Open in",
 * Info.plist `CFBundleDocumentTypes`, not in place): it is copied into the
 * app's Inbox folder and its file:// URL arrives as `appUrlOpen`.
 *
 * Android: MainActivity takes ACTION_SEND / ACTION_SEND_MULTIPLE; the screens
 * ask `OlanoShell.takeShared()` for what came with it at launch and on every
 * resume, and the plugin forgets it once read.
 *
 * Either way the files are read here like a file picked in the composer
 * (`ingestFile`), held in `incomingShare`, and the app asks which agent gets
 * them (App.tsx); the conversation then takes them into its composer
 * (`takeSharedFor`), so nothing is sent until the person sends it.
 */

import { App as CapApp } from "@capacitor/app";
import { Capacitor, registerPlugin } from "@capacitor/core";
import { Filesystem } from "@capacitor/filesystem";

import { fmtSize, ingestFile, MAX_ATTACHMENT_BYTES, type FileAttachment } from "../shared/dashboard.ts";
import { say } from "../state/notice.ts";
import { createStore } from "../state/store.ts";
import { fileNameOf, mimeOf } from "./shareNames.ts";
import { platform } from "./shell.ts";

export interface SharedItem {
  text: string;
  files: FileAttachment[];
  /** Only for a conversation with no messages yet ("Talk it over" on a Cortex item). */
  onlyIfNew?: boolean;
}

/**
 * What arrived and has not been placed in a conversation yet, with the agent
 * the person picked for it (null while the app is still asking).
 */
export const incomingShare = createStore<(SharedItem & { agent: string | null }) | null>(null);

interface SharedFile {
  name: string;
  mime: string;
  /** base64; empty when the file was too large to read (`size` says how large) */
  data: string;
  size?: number;
}

interface SharePlugin {
  takeShared(): Promise<{ text?: string; files?: SharedFile[] }>;
}

const OlanoShell = registerPlugin<SharePlugin>("OlanoShell");

function base64ToFile(data: string, name: string, mime: string): File {
  const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
  return new File([bytes], name, { type: mime });
}

/** Reads what arrived and holds it for the agent picker. */
export async function receive(text: string, raw: File[]): Promise<void> {
  const files: FileAttachment[] = [];
  const refused: string[] = [];
  for (const file of raw) {
    const result = await ingestFile(file);
    if (result.ok) files.push(result.attachment);
    else
      refused.push(
        result.reason === "too_large" ? `${file.name} is larger than ${fmtSize(MAX_ATTACHMENT_BYTES)}` : `${file.name} could not be read`,
      );
  }
  if (refused.length) say(`${refused.join(". ")}.`);
  if (!files.length && !text.trim()) return;
  // Something shared while an earlier share still waits joins it.
  incomingShare.set((current) => ({
    text: [current?.text, text.trim()].filter(Boolean).join("\n"),
    files: [...(current?.files ?? []), ...files],
    agent: null,
  }));
}

async function fromUrl(url: string): Promise<void> {
  if (!url.startsWith("file:")) return;
  const name = fileNameOf(url);
  try {
    const read = await Filesystem.readFile({ path: url });
    const data = typeof read.data === "string" ? read.data : await blobToBase64(read.data);
    await receive("", [base64ToFile(data, name, mimeOf(name))]);
  } catch {
    say(`${name} could not be read.`);
  } finally {
    // The system copied it into the app's Inbox for us; the copy is ours to remove.
    void Filesystem.deleteFile({ path: url }).catch(() => undefined);
  }
}

async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

let taking: Promise<void> | null = null;

/** One read at a time: on iOS `olano://share` and the resume that comes with it both ask. */
function fromNativeShare(): Promise<void> {
  taking ??= takeNativeShare().finally(() => {
    taking = null;
  });
  return taking;
}

async function takeNativeShare(): Promise<void> {
  let shared: { text?: string; files?: SharedFile[] };
  try {
    shared = await OlanoShell.takeShared();
  } catch {
    return; // An older build without the method.
  }
  const all = shared.files ?? [];
  const tooLarge = all.filter((f) => !f.data && (f.size ?? 0) > MAX_ATTACHMENT_BYTES);
  if (tooLarge.length) say(`${tooLarge.map((f) => f.name).join(", ")}: larger than ${fmtSize(MAX_ATTACHMENT_BYTES)}, the most a message can carry.`);
  const files = all.filter((f) => f.data).map((f) => base64ToFile(f.data, f.name || "shared file", f.mime || mimeOf(f.name)));
  if (files.length || shared.text) await receive(shared.text ?? "", files);
}

/**
 * Puts `text` in the composer of the next conversation with `agent` that
 * opens, unsent (an alert's "Ask the agent to fix it"). The caller opens that
 * conversation. With `onlyIfNew`, a conversation that already has messages
 * is left as it is: returning to a discussion continues it.
 */
export function startDraft(agent: string, text: string, options: { onlyIfNew?: boolean } = {}): void {
  incomingShare.set({ text, files: [], agent, onlyIfNew: options.onlyIfNew });
}

/** The person picked `agent`: its conversation takes the share when it opens. */
export function claimShare(agent: string): void {
  incomingShare.set((current) => (current ? { ...current, agent } : current));
}

/** Drops a share the person decided not to send. */
export function discardShare(): void {
  incomingShare.set(null);
}

/** Called by a conversation as it opens: the share, if it was meant for this agent. */
export function takeSharedFor(agent: string): SharedItem | null {
  const item = incomingShare.get();
  if (!item || item.agent !== agent) return null;
  incomingShare.set(null);
  return { text: item.text, files: item.files, onlyIfNew: item.onlyIfNew };
}

/** Wires the platform's way in; returns the undo. */
export function installShareIn(): () => void {
  if (!Capacitor.isNativePlatform()) {
    // A desktop browser has no other app to share from; the end-to-end tests
    // stand in for one through this hook.
    const hook = window as unknown as { __olanoShareIn?: (text: string, files: File[]) => Promise<void> };
    hook.__olanoShareIn = receive;
    return () => {
      delete hook.__olanoShareIn;
    };
  }
  const cleanups: (() => void)[] = [];
  if (platform() === "ios") {
    const opened = CapApp.addListener("appUrlOpen", (event) =>
      void (event.url.startsWith("olano://share") ? fromNativeShare() : fromUrl(event.url)),
    );
    cleanups.push(() => void opened.then((h) => h.remove()));
  }
  void fromNativeShare();
  const resumed = CapApp.addListener("resume", () => void fromNativeShare());
  cleanups.push(() => void resumed.then((h) => h.remove()));
  return () => {
    for (const cleanup of cleanups) cleanup();
  };
}
