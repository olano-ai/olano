/**
 * What this deployment offers beyond plain chat (`GET /api/status` →
 * `capabilities`), such as group chats. Read once and kept for a while; a
 * capability the deployment does not report reads as off.
 */

import type { Status } from "../net/types.ts";
import { useResource } from "./resources.ts";
import { currentApi } from "./session.ts";

export function useCapabilities(): Record<string, unknown> | undefined {
  const [status] = useResource<Status>("status", () => currentApi().status(), 5 * 60 * 1000);
  return status.data?.capabilities;
}

/** Group chats: `undefined` until the deployment has answered, then on or off. */
export function useGroupsEnabled(): boolean | undefined {
  const capabilities = useCapabilities();
  if (capabilities === undefined) return undefined;
  return capabilities.groups_enabled === true;
}
