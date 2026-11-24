/**
 * Handing a file to another app: the phone's own sheet, with "Save to Files",
 * Mail, Word, Numbers and whatever else can open it. The file is written to
 * the app's cache first (the Filesystem plugin), because the share sheet takes
 * files, not bytes. In a browser it downloads instead.
 */

import { Capacitor } from "@capacitor/core";
import { Directory, Filesystem } from "@capacitor/filesystem";
import { Share } from "@capacitor/share";

import { shareName } from "../chat/files.ts";

export async function openWithOtherApp(name: string, base64: string, type: string): Promise<void> {
  const fileName = shareName(name);
  if (Capacitor.isNativePlatform() && Capacitor.isPluginAvailable("Filesystem") && Capacitor.isPluginAvailable("Share")) {
    const written = await Filesystem.writeFile({
      path: `shared/${fileName}`,
      data: base64,
      directory: Directory.Cache,
      recursive: true,
    });
    try {
      await Share.share({ title: fileName, files: [written.uri] });
    } catch {
      // Dismissed.
    }
    return;
  }
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30000);
}
