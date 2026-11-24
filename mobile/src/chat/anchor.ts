/**
 * Where a conversation's scroll position goes when something changes size
 * under it, so the newest message stays where the person left it.
 *
 * Two kinds of change, answered differently:
 *
 * - The window onto the messages (the keyboard opening or closing, the
 *   composer growing a line or a photo tray): the bottom edge stays on the
 *   same message, as in every messenger, so the conversation rides up with
 *   the keyboard instead of disappearing behind it. Someone reading further
 *   up keeps their distance from the bottom; someone at the bottom stays there.
 * - The messages themselves (a reply growing, its buttons appearing once it
 *   finishes, a picture loading): followed only by someone at the bottom.
 *   Someone reading further up is left exactly where they are.
 *
 * Pure, so `node --test` covers it; `ui/useBottomAnchor.ts` applies it.
 */

export interface ScrollBox {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

export type Change = "viewport" | "messages";

export function distanceFromBottom(box: ScrollBox): number {
  return Math.max(0, box.scrollHeight - box.clientHeight - box.scrollTop);
}

/**
 * The scrollTop to set after `change`, or null to leave it. `fromBottom` is
 * the distance from the bottom measured before the change; `stuck` says the
 * person was following the newest message.
 */
export function anchoredScrollTop(box: ScrollBox, change: Change, stuck: boolean, fromBottom: number): number | null {
  const bottom = Math.max(0, box.scrollHeight - box.clientHeight);
  let target: number;
  if (stuck) target = bottom;
  else if (change === "viewport") target = Math.max(0, bottom - Math.max(0, fromBottom));
  else return null;
  return Math.abs(target - box.scrollTop) < 1 ? null : target;
}

/**
 * Whether a scroll event can be read as the person's own position: only when
 * the box is still the size the anchor last answered for. A resize clamps the
 * scroll position and the browser delivers that scroll event late, after the
 * next resize but before the ResizeObserver hears of it, so it would describe
 * the new box against the old position: a conversation at the bottom read as
 * "302px up, reading", the follow flag dropped, and the keyboard then kept
 * that distance and left the newest reply behind the composer. `seenHeight`
 * 0 means the anchor has not started, and nothing is withheld.
 */
export function scrollIsSettled(clientHeight: number, seenHeight: number): boolean {
  return seenHeight === 0 || clientHeight === seenHeight;
}
