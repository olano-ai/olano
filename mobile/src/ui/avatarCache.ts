/**
 * Avatars kept on the phone between launches.
 *
 * The deployment serves an agent's avatar at a stamped address
 * (`/api/agents/<name>/avatar?v=<stamp>`, the stamp changing with the file)
 * and marks it immutable for a year, but the app fetches it through its
 * native plugin as base64 and the web view's own cache never sees it, so a
 * cold launch downloaded every avatar again before the roster had faces.
 * A stamped address is content-addressed, so its bytes can be kept for good:
 * a new picture gets a new address. Bare addresses (no stamp) are never kept.
 *
 * Filed under the deployment, since the same path means another picture on
 * another one; dropped on sign-out with the other kept copies. A bounded set
 * (`MAX_AVATARS`), oldest out first. localStorage can be full, missing or
 * broken, so every read and write tolerates that.
 *
 * Pure: no DOM beyond the store handed in, so `node --test` covers it.
 */

export const AVATAR_PREFIX = "olano.avatar.v1:";
const INDEX_KEY = `${AVATAR_PREFIX}index`;
export const MAX_AVATARS = 40;

export type AvatarStore = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key" | "length">;

/** Whether an avatar address names one version of the picture for good. */
export function stamped(path: string): boolean {
  return /[?&]v=[^&]+/.test(path);
}

function itemKey(origin: string, path: string): string {
  return `${AVATAR_PREFIX}${origin}\u0000${path}`;
}

function readIndex(store: AvatarStore): string[] {
  try {
    const raw = JSON.parse(store.getItem(INDEX_KEY) ?? "[]") as unknown;
    return Array.isArray(raw) ? raw.filter((k): k is string => typeof k === "string") : [];
  } catch {
    return [];
  }
}

export function readAvatar(store: AvatarStore | null, origin: string, path: string): string | null {
  if (!store || !origin || !stamped(path)) return null;
  try {
    const value = store.getItem(itemKey(origin, path));
    return value && value.startsWith("data:") ? value : null;
  } catch {
    return null;
  }
}

export function writeAvatar(store: AvatarStore | null, origin: string, path: string, dataUrl: string): void {
  if (!store || !origin || !stamped(path) || !dataUrl.startsWith("data:")) return;
  try {
    const key = itemKey(origin, path);
    const index = readIndex(store).filter((k) => k !== key);
    index.push(key);
    while (index.length > MAX_AVATARS) {
      const doomed = index.shift();
      if (doomed) store.removeItem(doomed);
    }
    store.setItem(key, dataUrl);
    store.setItem(INDEX_KEY, JSON.stringify(index));
  } catch {
    // Full or unavailable: the next launch downloads it instead.
  }
}

/** Drops every kept avatar (sign out, switch deployment). */
export function clearAvatars(store: AvatarStore | null): void {
  if (!store) return;
  try {
    const doomed: string[] = [];
    for (let i = 0; i < store.length; i += 1) {
      const name = store.key(i);
      if (name?.startsWith(AVATAR_PREFIX)) doomed.push(name);
    }
    for (const name of doomed) store.removeItem(name);
  } catch {
    // Nothing more can be done about a store that will not answer.
  }
}
