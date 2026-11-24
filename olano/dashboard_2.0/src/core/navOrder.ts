// The pure half of the navigation table: how the nav groups are arranged.
//
// No React, no icons, no DOM — `core/nav.ts` re-exports everything here beside
// the NAV table itself, so callers import from there; this file exists so the
// rules run under Node's test runner (`npm run test:unit`, see nav.test.ts)
// exactly as they run in the browser. nav.ts imports lucide icons, which the
// runner cannot resolve.

/** Which of the two drag-and-drop groups an item sits in. */
export type NavSection = "primary" | "more";

/** Ordered `to` paths for each group. */
export type NavSplit = { primary: string[]; more: string[] };

/**
 * Move `dragKey` into `section`, before `beforeKey` (or to the end when null).
 *
 * Pure, and shared by both shells' nav: the console's sidebar and the simple
 * rail's More group implement the same gesture over the same shape, and having
 * two copies of "remove from wherever it was, splice it in here" is how the two
 * would end up disagreeing about what a drop onto an item's own row does.
 */
export function placeItem(
  work: NavSplit,
  dragKey: string,
  section: NavSection,
  beforeKey: string | null,
): NavSplit {
  const primary = work.primary.filter((t) => t !== dragKey);
  const more = work.more.filter((t) => t !== dragKey);
  const target = section === "primary" ? primary : more;
  let i = beforeKey == null ? target.length : target.indexOf(beforeKey);
  if (i === -1) i = target.length;
  target.splice(i, 0, dragKey);
  return { primary, more };
}

// The tabs that lead the console sidebar's More group for a brand-new user:
// the four that used to be the always-visible Primary group, now the head of
// the one list under the agents. Everything else follows in NAV order.
export const DEFAULT_MORE_HEAD = ["/overview", "/insights", "/cortex", "/issues"];

/**
 * The console sidebar's More order for one human, from what they saved.
 *
 * The sidebar has one group of tabs now — the agents are the top shelf, and
 * every tab sits under More — but the saved preference still has the shape of
 * the two-group nav it replaced (`nav_primary` + `nav_more`). This folds the
 * two into one list, once: a saved Primary is put at the head of More, the four
 * default head tabs leading it in their shipped order, and `persist` carries
 * the folded split to write back so the fold never re-runs and the human's
 * later drags stand. A split whose Primary is already empty is theirs as
 * saved; nothing saved at all reads as the shipped order.
 *
 * `available` is the gated list this human can see, in NAV order. Paths saved
 * but not available (an admin-only tab, a capability still resolving) are kept
 * in the write-back and simply not rendered, so a member's saved split never
 * loses an entry they will see again as an admin; paths available but not saved
 * (a freshly shipped tab) are appended so they surface without being persisted
 * until the human next drags something.
 */
export function resolveMoreOrder(
  available: readonly string[],
  saved: { primary?: readonly string[]; more?: readonly string[] } | undefined,
): { order: string[]; persist: NavSplit | null } {
  const seen = new Set<string>();
  const take = (paths: readonly string[] | undefined, out: string[]) => {
    for (const to of paths ?? []) {
      if (seen.has(to)) continue;
      seen.add(to);
      out.push(to);
    }
    return out;
  };
  const primary = saved?.primary ?? [];
  const more = saved?.more ?? [];
  if (primary.length === 0 && more.length === 0) {
    const order = take(DEFAULT_MORE_HEAD.filter((to) => available.includes(to)), []);
    return { order: take(available, order), persist: null };
  }
  const folded: string[] = [];
  if (primary.length > 0) {
    // A Primary to fold: the shipped head leads, then whatever else the human
    // kept on the top shelf, then their More — three lists, one order.
    take(DEFAULT_MORE_HEAD, folded);
    take(primary, folded);
  }
  take(more, folded);
  const persist = primary.length > 0 ? { primary: [], more: [...folded] } : null;
  // Render only what this human can see; `seen` already holds every saved
  // path, so the second pass appends just the never-saved tabs.
  const visible = new Set(available);
  const order = folded.filter((to) => visible.has(to));
  take(available, order);
  return { order, persist };
}
