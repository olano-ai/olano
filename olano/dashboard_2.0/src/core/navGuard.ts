// A page with unsaved work can ask the shell to check before it navigates away.
//
// Why this exists instead of react-router's `useBlocker`: the app mounts a
// declarative `<BrowserRouter>` (app.tsx), and `useBlocker` only works under a
// data router (`createBrowserRouter`) — it throws otherwise. Converting the
// router is a far bigger change than the problem warrants, so the guard is a
// module-level registry the shell consults at its two exits: a capture-phase
// click handler over every internal link in `Layout`, and the command
// palette's programmatic `navigate`. Tab close and refresh are the page's own
// business (`beforeunload`, see core/unsavedChanges.ts). The browser Back
// button is the one exit this does NOT intercept — that genuinely needs the
// history-level blocking only a data router provides — and the docs say so.
//
// Pure module, no React: node's `--test` imports it directly (navGuard.test.ts),
// the way turnText.ts and liveTurn.ts are tested.

/** Resolve `true` to let the navigation proceed, `false` to stay. */
export type NavGuard = () => Promise<boolean>;

let current: NavGuard | null = null;

/** Register the active guard (one at a time — the page being left owns it).
 *  Returns an unregister function that only clears *this* guard, so a page
 *  unmounting late cannot clear a guard a newer page has since installed. */
export function setNavGuard(guard: NavGuard): () => void {
  current = guard;
  return () => {
    if (current === guard) current = null;
  };
}

export function clearNavGuard(): void {
  current = null;
}

export function hasNavGuard(): boolean {
  return current !== null;
}

/** Ask the active guard, if any, whether navigation may proceed. No guard ⇒
 *  proceed. A guard that throws is treated as "proceed": a broken guard must
 *  never trap someone on a page. */
export async function confirmNavigation(): Promise<boolean> {
  const guard = current;
  if (!guard) return true;
  try {
    return await guard();
  } catch {
    return true;
  }
}

/** Whether a click on an anchor is an in-app navigation the guard should see.
 *  Modifier clicks open a new tab (nothing is left), `target=_blank` and
 *  `download` never leave the page, and an external origin is the browser's
 *  `beforeunload` territory, not ours. Exported so the rule is testable without
 *  a DOM: the caller passes what it read off the element and the event. */
export function isGuardedLinkClick(link: {
  href: string;
  target: string;
  download: string;
  origin: string;
  currentOrigin: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  button: number;
}): boolean {
  if (link.button !== 0) return false;
  if (link.metaKey || link.ctrlKey || link.shiftKey || link.altKey) return false;
  if (link.target && link.target !== "_self") return false;
  if (link.download) return false;
  if (!link.href) return false;
  if (link.origin !== link.currentOrigin) return false;
  return true;
}
