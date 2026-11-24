/** Pulse updates: what the agents found and want you to know (lane "pulse", pending). */

import type { PulseItem } from "../../net/types.ts";
import { useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";

/** Under "cortex" so the live stream's cortex events refresh it. */
export const PULSE_KEY = "cortex:pulse";

export function usePulseUpdates() {
  return useResource<PulseItem[]>(PULSE_KEY, async () => (await currentApi().pulse("pulse")).items ?? [], 60000);
}
