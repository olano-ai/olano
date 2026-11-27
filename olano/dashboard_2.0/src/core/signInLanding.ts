// Where a sign-in lands. A sign-in renders the app at whatever URL the browser
// is on, which is right when the same person's session lapsed (they go back to
// the page they were reading) and wrong when it is somebody else: after one
// person signed out from /chat/<agent>, the next person to sign in on that
// browser landed on that path and got "Agent ... is not in your roster" and
// "Could not load the conversation list" for a page that was never theirs.
//
// The rule: a deliberate sign-out, or a sign-in as a different account than
// the one this browser last saw, starts at home. A first sign-in (nobody seen
// yet) keeps the URL, so a shared deep link still opens what it names.
//
// Pure, with the storage passed in, so node's --test can drive it.

export const LAST_USER_KEY = "olano.lastSignedInUser";
export const SIGNED_OUT_KEY = "olano.signedOut";

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function shouldResetRoute(previousUser: string | null, currentUser: string, signedOut: boolean): boolean {
  if (signedOut) return true;
  return !!previousUser && previousUser !== currentUser;
}

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

/** Read-only: what the landing for `currentUser` should be. */
export function landingFor(store: KeyValueStore | null, currentUser: string): { reset: boolean } {
  if (!store) return { reset: false };
  const prev = safe(() => store.getItem(LAST_USER_KEY), null);
  const signedOut = safe(() => store.getItem(SIGNED_OUT_KEY), null) === "1";
  return { reset: shouldResetRoute(prev, currentUser, signedOut) };
}

/** Record that `currentUser` is now the one signed in on this browser. */
export function recordSignIn(store: KeyValueStore | null, currentUser: string): void {
  if (!store) return;
  safe(() => {
    store.setItem(LAST_USER_KEY, currentUser);
    store.removeItem(SIGNED_OUT_KEY);
  }, undefined);
}

/** A person signed out on purpose: whoever signs in next starts at home. */
export function recordSignOut(store: KeyValueStore | null): void {
  if (!store) return;
  safe(() => store.setItem(SIGNED_OUT_KEY, "1"), undefined);
}

export function browserStore(): KeyValueStore | null {
  return safe(() => (typeof localStorage === "undefined" ? null : localStorage), null);
}
