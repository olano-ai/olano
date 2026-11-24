/** A light tap of the Taptic Engine / vibration motor, where the platform has one. */

import { Haptics, ImpactStyle, NotificationType } from "@capacitor/haptics";

import { isNative } from "./shell.ts";

export async function haptic(kind: "light" | "medium" | "success" | "warning"): Promise<void> {
  if (!isNative()) return;
  try {
    if (kind === "success") await Haptics.notification({ type: NotificationType.Success });
    else if (kind === "warning") await Haptics.notification({ type: NotificationType.Warning });
    else await Haptics.impact({ style: kind === "medium" ? ImpactStyle.Medium : ImpactStyle.Light });
  } catch {
    // No haptics on this device.
  }
}
