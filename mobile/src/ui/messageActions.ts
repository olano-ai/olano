/**
 * What a long press on a message offers: copy its words (or a voice note's
 * transcript) and share them with another app. Sharing uses the phone's own
 * share sheet (the Share plugin); in a browser, the Web Share API where there
 * is one.
 */

import { Capacitor } from "@capacitor/core";
import { Share } from "@capacitor/share";

import { Clipboard } from "./clipboard.ts";

export function canShare(): boolean {
  if (Capacitor.isNativePlatform()) return Capacitor.isPluginAvailable("Share");
  return typeof navigator !== "undefined" && typeof navigator.share === "function";
}

export async function shareText(text: string): Promise<void> {
  try {
    if (Capacitor.isNativePlatform()) await Share.share({ text });
    else await navigator.share({ text });
  } catch {
    // Dismissed, or nothing to share with.
  }
}

export const copyText = (text: string) => Clipboard.write(text);
