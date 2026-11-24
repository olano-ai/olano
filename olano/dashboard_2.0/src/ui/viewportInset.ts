// Publishes the window as CSS variables — the app's height (`--app-vh`) and
// the *visual* viewport (`--modal-vh`, `--modal-vtop`) — so full-window shells
// and fixed overlays size themselves to what the user can actually see.
//
// Why it exists: `position: fixed; inset: 0` and `100vh` both resolve against
// the layout viewport, which on iOS never shrinks — not for the browser's
// bottom bar and not for the software keyboard. A dialog sized that way puts
// its footer underneath the browser chrome, where nothing can scroll it back
// into view. `100dvh` fixes the browser-chrome half in pure CSS; only
// `visualViewport` knows about the keyboard, hence this module.
//
// And the mirror image of that, which is why the module also publishes
// `--app-vh`: launched from the home screen there is no browser chrome to
// track, and iOS then reports a viewport SHORTER than the window — `dvh`, and
// with it `visualViewport.height`, come back one status-bar inset short of the
// screen. That left a dead strip of page background along the bottom edge,
// with the composer and every bottom sheet floating above it. tokens.css and
// globals.css answer the CSS half (`--app-vh` resolves to `vh` when
// installed); this module answers the measured half, and refuses to publish a
// `--modal-vh` shorter than the window when nothing is actually covering it.
//
// Installed once at startup and left running: the variables are read by
// `.app-shell`, `.simple-shell` and `.modal-overlay`, so keeping them current
// costs a few passive listeners and nothing else changes when no dialog is
// open.

import { isStandalone } from "../core/pwa";
import { viewportVars } from "./viewportVars";

const ROOT_VH = "--modal-vh";
const ROOT_VTOP = "--modal-vtop";
const ROOT_APP_VH = "--app-vh";

function setPx(root: HTMLElement, name: string, value: number | null): void {
  if (value === null) root.style.removeProperty(name);
  else root.style.setProperty(name, `${value}px`);
}

/** True when the app runs from the home screen rather than a browser tab. */
function installedShell(): boolean {
  // The dataset marker is what CSS keys its standalone block on (core/pwa sets
  // it at boot from the same predicate), so reading it here keeps the two
  // halves of the inset from disagreeing about which mode this is.
  return document.documentElement.dataset.standalone === "1" || isStandalone();
}

export function installViewportInset(): () => void {
  if (typeof window === "undefined") return () => {};

  const vv = window.visualViewport;
  const root = document.documentElement;
  let frame = 0;

  const apply = () => {
    frame = 0;
    const vars = viewportVars({
      layout: root.clientHeight,
      // No visualViewport (older engines): the layout viewport is all this can
      // know, and it is what the CSS fallbacks already assume.
      visual: vv ? vv.height : root.clientHeight,
      offsetTop: vv ? vv.offsetTop : 0,
      standalone: installedShell(),
    });
    // A null is "the stylesheet knows better": in a browser tab its `dvh`
    // tracks chrome this module cannot see, and a degenerate reading must not
    // pin anything at all. Removed rather than left stale, because an inline
    // value on the root element outranks every rule in the cascade.
    setPx(root, ROOT_APP_VH, vars.appVh);
    setPx(root, ROOT_VH, vars.modalVh);
    // The keyboard can also push the visual viewport down the page; a fixed
    // overlay stays at the layout origin, so mirror the offset back onto it.
    root.style.setProperty(ROOT_VTOP, `${vars.modalTop}px`);
  };

  const schedule = () => {
    if (frame) return;
    frame = requestAnimationFrame(apply);
  };

  apply();
  vv?.addEventListener("resize", schedule);
  vv?.addEventListener("scroll", schedule);
  // Rotation, split view and a desktop window drag change the *layout*
  // viewport, which the visual viewport's own events do not reliably report.
  window.addEventListener("resize", schedule);
  window.addEventListener("orientationchange", schedule);
  return () => {
    if (frame) cancelAnimationFrame(frame);
    vv?.removeEventListener("resize", schedule);
    vv?.removeEventListener("scroll", schedule);
    window.removeEventListener("resize", schedule);
    window.removeEventListener("orientationchange", schedule);
    root.style.removeProperty(ROOT_VH);
    root.style.removeProperty(ROOT_VTOP);
    root.style.removeProperty(ROOT_APP_VH);
  };
}
