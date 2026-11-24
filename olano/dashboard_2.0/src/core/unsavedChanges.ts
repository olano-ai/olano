// The unsaved-work contract for a form page, in one hook.
//
// A page with a Save button has three ways to lose edits: an in-app click on
// any link (sidebar, palette, a link in the page), the tab closing or reloading,
// and the person simply forgetting — leaving the tab open, assuming the change
// took. This hook covers all three in order:
//
//   * while `dirty`, it registers a `navGuard` so the shell asks `onLeave`
//     before any in-app navigation (Layout intercepts the click; the palette
//     awaits the same answer);
//   * while `dirty`, it arms `beforeunload`, so a close/refresh gets the
//     browser's own "leave site?" prompt;
//   * while `dirty` and idle — no change to the form for `nudgeAfterMs` — it
//     fires `onIdle` ONCE per dirty episode, for a gentle reminder. It re-arms
//     only after the page is saved or discarded (dirty goes false), so a person
//     who is still typing is never nagged, and one who dismissed the reminder
//     is not nagged again for the same edits.
//
// Every listener comes off the moment `dirty` is false, so a saved page costs
// nothing and traps nobody.

import { useEffect, useRef } from "react";
import { setNavGuard } from "./navGuard";

export interface UnsavedChangesOptions {
  /** True while the form differs from what is saved. */
  dirty: boolean;
  /** Asked before an in-app navigation while dirty. Resolve `true` to leave
   *  (the caller has saved or chosen to discard), `false` to stay. */
  onLeave: () => Promise<boolean>;
  /** Fired once per dirty episode after `nudgeAfterMs` without an edit. */
  onIdle?: () => void;
  /** Idle time before `onIdle`; default 45 s. `0` disables the nudge. */
  nudgeAfterMs?: number;
  /** Anything that changes when the form is edited — the form value itself
   *  works. Resets the idle timer on every edit. */
  editSignal?: unknown;
}

export const DEFAULT_NUDGE_MS = 45_000;

export function useUnsavedChanges({
  dirty,
  onLeave,
  onIdle,
  nudgeAfterMs = DEFAULT_NUDGE_MS,
  editSignal,
}: UnsavedChangesOptions): void {
  // Latest callbacks without re-registering the guard on every render.
  const leaveRef = useRef(onLeave);
  leaveRef.current = onLeave;
  const idleRef = useRef(onIdle);
  idleRef.current = onIdle;
  // "Nudged already for these edits" — cleared when dirty goes false.
  const nudgedRef = useRef(false);

  // In-app navigation guard.
  useEffect(() => {
    if (!dirty) return;
    return setNavGuard(() => leaveRef.current());
  }, [dirty]);

  // Tab close / reload: the browser's own prompt. Setting `returnValue` is
  // what still triggers it in current Chromium; `preventDefault` is the
  // standard. Both, because the two disagree across versions.
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  // Idle nudge — one per dirty episode, timer restarted by every edit.
  useEffect(() => {
    if (!dirty) {
      nudgedRef.current = false;
      return;
    }
    if (!nudgeAfterMs || nudgedRef.current || !idleRef.current) return;
    const timer = window.setTimeout(() => {
      if (nudgedRef.current) return;
      nudgedRef.current = true;
      idleRef.current?.();
    }, nudgeAfterMs);
    return () => window.clearTimeout(timer);
    // `editSignal` is here on purpose: an edit restarts the countdown.
  }, [dirty, nudgeAfterMs, editSignal]);
}
