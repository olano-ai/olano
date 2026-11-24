/**
 * A long press (or a right click), without stealing taps or scrolls: moving
 * the finger more than a few pixels before the press completes is a scroll,
 * and the click that ends a long press is swallowed so a link under the
 * finger does not open as well. The handler hears what was pressed, so a
 * menu can offer what fits it (a link's own choices, say).
 */

import { useRef } from "react";
import type React from "react";

const PRESS_MS = 450;
const SLOP_PX = 8;

export function useLongPress(onLongPress: (target: Element | null) => void) {
  const timer = useRef<number | undefined>(undefined);
  const origin = useRef({ x: 0, y: 0 });
  const fired = useRef(false);
  const pressed = useRef<Element | null>(null);
  const clear = () => {
    window.clearTimeout(timer.current);
    timer.current = undefined;
  };
  return {
    onPointerDown: (e: React.PointerEvent) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      fired.current = false;
      pressed.current = e.target instanceof Element ? e.target : null;
      origin.current = { x: e.clientX, y: e.clientY };
      clear();
      timer.current = window.setTimeout(() => {
        timer.current = undefined;
        fired.current = true;
        onLongPress(pressed.current);
      }, PRESS_MS);
    },
    onPointerMove: (e: React.PointerEvent) => {
      if (timer.current === undefined) return;
      if (Math.hypot(e.clientX - origin.current.x, e.clientY - origin.current.y) > SLOP_PX) clear();
    },
    onPointerUp: clear,
    onPointerCancel: clear,
    onPointerLeave: clear,
    onContextMenu: (e: React.MouseEvent) => {
      e.preventDefault();
      clear();
      if (!fired.current) {
        fired.current = true;
        onLongPress(e.target instanceof Element ? e.target : null);
      }
    },
    onClickCapture: (e: React.MouseEvent) => {
      if (!fired.current) return;
      fired.current = false;
      e.preventDefault();
      e.stopPropagation();
    },
  };
}
