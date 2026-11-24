// Shared dialog behavior: Escape-to-close, Tab focus trap, and focus restore.
// Every modal/drawer shell (agents + scheduler Modal, vault Overlay, cortex
// Drawer, confirm) applies this one hook instead of hand-rolling listeners, so
// keyboard behavior is identical everywhere — including the surfaces that had
// none (the vault overlays were Escape-dead and nothing trapped focus).
//
// Dialogs register on a module-level stack; only the TOPMOST dialog responds
// to Escape/Tab. That also fixes the latent nested-dialog bug where one Escape
// closed both a modal and the confirm sitting on top of it.

import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

const stack: symbol[] = [];

export function useDialogA11y(
  panelRef: RefObject<HTMLElement | null>,
  onClose?: () => void,
  opts?: {
    // Seed focus into the dialog on open (default true). Pass false when the
    // dialog manages its own initial focus (e.g. an autofocused input).
    initialFocus?: boolean;
  },
) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const initialFocus = opts?.initialFocus !== false;

  useEffect(() => {
    const id = Symbol("dialog");
    stack.push(id);
    const restoreTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const panel = panelRef.current;
    if (initialFocus && panel && !panel.contains(document.activeElement)) {
      panel.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    }

    const onKey = (e: KeyboardEvent) => {
      if (stack[stack.length - 1] !== id) return; // not the topmost dialog
      if (e.key === "Escape") {
        closeRef.current?.();
        return;
      }
      if (e.key !== "Tab") return;
      const p = panelRef.current;
      if (!p) return;
      const focusables = Array.from(p.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        // offsetParent null ⇒ hidden (display:none subtree); keep the active
        // element regardless so the trap can't lose its anchor.
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey) {
        if (active === first || !p.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !p.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      const i = stack.indexOf(id);
      if (i !== -1) stack.splice(i, 1);
      restoreTo?.focus();
    };
  }, [panelRef, initialFocus]);
}
