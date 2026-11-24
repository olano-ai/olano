// Fixed-position anchoring for a popover that must escape its container.
//
// A menu rendered in flow is clipped by any ancestor with `overflow: hidden`,
// and both shells have one: the simple rail collapses to a 78px strip that
// clips, and a popover in it came out sliced into a column of half-words. The
// fix is the same every time — take the trigger's rect, position `fixed` from
// it, clamp into the viewport — so it lives here once rather than being
// re-derived per popover.
//
// Clamping is the half that matters on a phone. An in-flow menu pinned to its
// trigger with `left: 0` or `right: 0` reaches wherever its width takes it, and
// the chat header's chip row wraps rather than scrolls, so a chip can sit at
// either edge of a 390px viewport: the chat MCP popover (330px, `right: 0`) hung
// ~60px off the LEFT edge, and the model/effort menus (300px, `left: 0`) hang off
// the RIGHT one from a chip past the middle. `anchorTo` caps the width at the
// viewport and slides the box back inside, so neither can happen at any width.
//
// `fixed` means the popover does not scroll with a container it has left, so the
// anchor is re-measured on scroll (capture phase — a nested scroller's event
// never reaches document while bubbling) and it tracks its trigger instead.

import { useCallback, useEffect, useState, type RefObject } from "react";

/** Where the popover sits relative to its trigger. `auto` flips to whichever
 *  side has room, preferring below. */
export type Placement = "above" | "below" | "auto";

export type AnchorRect = {
  /** Viewport px, ready for `style={{ position: "fixed", ...rect }}`. */
  left: number;
  /** Set for `above`; the popover's bottom edge measured from the viewport's. */
  bottom?: number;
  /** Set for `below`. */
  top?: number;
  width: number;
  /** Room between the popover's anchored edge and the far viewport edge — cap
   *  the panel's `max-height` at this so a long list scrolls inside the screen
   *  instead of running off the bottom of it. */
  maxHeight: number;
};

/** How close to a viewport edge an anchored popover may sit. */
const GUTTER = 8;
/** How far the popover clears its trigger. */
const OFFSET = 6;
/** Below this a dropdown is too short to be worth opening downwards, so `auto`
 *  flips it — provided the other side is actually roomier. */
const MIN_HEIGHT = 180;

export function anchorTo(
  el: HTMLElement,
  placement: Placement,
  minWidth: number,
  rtl = document.documentElement.dir === "rtl",
): AnchorRect {
  const r = el.getBoundingClientRect();
  // Never narrower than the popover needs, never wider than the viewport can
  // hold — a 360px panel on a 320px phone would otherwise hang off the edge.
  const width = Math.min(Math.max(minWidth, r.width), window.innerWidth - GUTTER * 2);
  // Hang from the trigger's leading edge, which is the right one in RTL.
  const raw = rtl ? r.right - width : r.left;
  const left = Math.max(GUTTER, Math.min(raw, window.innerWidth - width - GUTTER));
  const below = Math.max(0, window.innerHeight - r.bottom - OFFSET - GUTTER);
  const above = Math.max(0, r.top - OFFSET - GUTTER);
  const side =
    placement === "auto" ? (below < MIN_HEIGHT && above > below ? "above" : "below") : placement;
  return side === "above"
    ? { left, bottom: window.innerHeight - r.top + OFFSET, width, maxHeight: above }
    : { left, top: r.bottom + OFFSET, width, maxHeight: below };
}

/**
 * Track a trigger's anchor rect while `open`.
 *
 * Re-measures on open, on resize and on any scroll (rAF-coalesced, so a fast
 * wheel costs one measurement per frame); returns `null` until measured, so a
 * caller renders nothing rather than a popover parked at 0,0 for one frame.
 */
export function useAnchor(
  ref: RefObject<HTMLElement | null>,
  open: boolean,
  placement: Placement,
  minWidth: number,
): { rect: AnchorRect | null; measure: () => void } {
  const [rect, setRect] = useState<AnchorRect | null>(null);
  const measure = useCallback(() => {
    const el = ref.current;
    if (el) setRect(anchorTo(el, placement, minWidth));
  }, [ref, placement, minWidth]);

  useEffect(() => {
    if (!open) return;
    measure();
    let pending = 0;
    const schedule = () => {
      if (pending) return;
      pending = window.requestAnimationFrame(() => {
        pending = 0;
        measure();
      });
    };
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, true);
    return () => {
      if (pending) window.cancelAnimationFrame(pending);
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true);
    };
  }, [open, measure]);

  return { rect, measure };
}
