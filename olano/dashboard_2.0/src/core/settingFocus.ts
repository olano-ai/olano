// Land on a setting, not just on the page that holds it.
//
// The palette navigates to `?focus=<setting id>`; this hook (mounted once in
// Layout — it only queries the document, so no per-view wiring) turns that into
// scroll + flash + focus.
//
// Three things make it more than a querySelector:
//
//  1. The element usually does NOT exist when the location changes. Every view
//     except Overview is route-split, so the chunk is still downloading, and
//     the section that holds the field is often behind its own data fetch. We
//     watch for it with a MutationObserver plus a rAF tick, up to a deadline.
//  2. It may exist inside a collapsed <details> (Cortex settings, a connector
//     form's secondary group). Scrolling to a collapsed shell looks like a
//     miss, so every <details> ancestor is opened first.
//  3. The param is consumed. Leaving it in the URL means Back re-flashes and a
//     reload re-flashes; stripping it with replace:true means selecting the
//     same target from the palette twice re-adds it and it fires again.

import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { settingSelector } from "./settingAnchor";

export const FOCUS_PARAM = "focus";

/** How long to keep waiting for a lazily-rendered target before giving up. */
const DEADLINE_MS = 5000;
/** How long the highlight stays on. Matches the CSS animation duration. */
const FLASH_MS = 2000;
export const FLASH_CLASS = "setting-flash";

function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

/** Open every <details> between the target and the document root. */
function revealAncestors(el: HTMLElement): void {
  let node: HTMLElement | null = el.parentElement;
  while (node) {
    if (node instanceof HTMLDetailsElement) node.open = true;
    node = node.parentElement;
  }
}

/**
 * Scroll to the target and mark it.
 *
 * The highlight owns its own lifetime and is deliberately NOT tied to the
 * effect's cleanup: committing to a target immediately strips the focus param,
 * which re-runs the effect, whose cleanup would then remove the class in the
 * same tick it was added — a flash nobody could see. An element that unmounts
 * mid-flash takes the class with it, so nothing leaks.
 */
function reveal(el: HTMLElement): void {
  revealAncestors(el);
  el.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
  el.classList.add(FLASH_CLASS);
  // Focus so keyboard and screen-reader users land where sighted users do.
  // preventScroll because scrollIntoView above already chose the position.
  if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
  el.focus({ preventScroll: true });
  window.setTimeout(() => el.classList.remove(FLASH_CLASS), FLASH_MS);
}

export function useSettingFocus(): void {
  const [params, setParams] = useSearchParams();
  const target = params.get(FOCUS_PARAM);

  useEffect(() => {
    if (!target) return;
    const selector = settingSelector(target);
    let done = false;
    let raf = 0;

    // Drop the param as soon as we commit to a target, so Back and reload don't
    // replay the flash. Done via the functional form to avoid clobbering any
    // other param (?tab= in particular) the view wrote in the same tick.
    function clearParam() {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete(FOCUS_PARAM);
          return next;
        },
        { replace: true },
      );
    }

    function attempt(): boolean {
      const el = document.querySelector<HTMLElement>(selector);
      if (!el) return false;
      done = true;
      reveal(el);
      clearParam();
      return true;
    }

    if (attempt()) return;

    // Not there yet: watch the tree until it appears or the deadline passes.
    // The rAF tick covers a target that is added and laid out in the same frame
    // the observer fires, where an immediate measure would still read zero.
    const observer = new MutationObserver(() => {
      if (done) return;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (!done && attempt()) observer.disconnect();
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // A target that never arrives (stale link, a section gated off on this box)
    // fails silently — the user is still on the right page, which is the useful
    // half of the navigation.
    const deadline = window.setTimeout(() => {
      if (done) return;
      done = true;
      observer.disconnect();
      clearParam();
    }, DEADLINE_MS);

    return () => {
      done = true;
      observer.disconnect();
      cancelAnimationFrame(raf);
      window.clearTimeout(deadline);
    };
    // setParams is stable across renders in react-router v7; re-running this on
    // every render would restart the observer mid-wait.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);
}
