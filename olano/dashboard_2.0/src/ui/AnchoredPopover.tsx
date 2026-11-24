// The one anchored popover: portaled to <body>, positioned from its trigger's
// rect, clamped into the viewport, closing on outside click / Escape.
//
// Every dropdown in the app used to hand-roll this, and each copy got a
// different subset right. The chat header's three chip menus were the worst
// case because the row WRAPS on a phone: a chip can end up at either edge of a
// 390px viewport, so `right: 0` (the MCP status popover, 330px wide) put most of
// the panel off the left edge of the screen, and `left: 0` (the model and effort
// menus, 300px wide) does the same off the right one from any chip past the
// middle. Neither is fixable by the popover's own CSS — where it lands depends
// on where the trigger wrapped to — so the position has to be measured, and
// measuring it once here is what keeps the next dropdown from re-introducing it.
//
// `useAnchor` owns the geometry (clamping, `auto` flipping, tracking the trigger
// on scroll/resize); this owns the portal and the dismissal.

import { useEffect, useRef, type CSSProperties, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useAnchor, type Placement } from "./anchoredMenu";

export function AnchoredPopover({
  anchorRef,
  open,
  onClose,
  minWidth,
  placement = "auto",
  className,
  style,
  role,
  ariaLabel,
  ariaMultiSelectable,
  testId,
  children,
}: {
  /** The trigger's wrapper — the rect the panel hangs from, and the region an
   *  outside click must exclude (so the trigger's own click toggles rather than
   *  closing-then-reopening). */
  anchorRef: RefObject<HTMLElement | null>;
  open: boolean;
  onClose: () => void;
  /** The panel's natural width. Capped at the viewport, so passing a desktop
   *  width is safe — a 360px panel becomes 374px-wide-viewport-minus-gutters on
   *  a small phone rather than hanging off it. */
  minWidth: number;
  placement?: Placement;
  className?: string;
  /** Merged after the measured position, so a caller can add `zIndex` (a panel
   *  opened from inside a modal needs to clear it) without restating geometry. */
  style?: CSSProperties;
  role?: string;
  ariaLabel?: string;
  ariaMultiSelectable?: boolean;
  testId?: string;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const { rect } = useAnchor(anchorRef, open, placement, minWidth);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      const target = e.target as Node;
      // The panel lives outside the trigger's subtree now, so both have to be
      // excluded explicitly or the first click inside the panel closes it.
      if (anchorRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      onClose();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose, anchorRef]);

  if (!open || !rect) return null;

  return createPortal(
    <div
      ref={panelRef}
      className={className}
      role={role}
      aria-label={ariaLabel}
      aria-multiselectable={ariaMultiSelectable}
      data-testid={testId}
      style={{
        position: "fixed",
        left: rect.left,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        maxHeight: rect.maxHeight,
        ...style,
      }}
    >
      {children}
    </div>,
    document.body,
  );
}
