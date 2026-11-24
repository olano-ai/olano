// How one reading of the window becomes the CSS variables `ui/viewportInset.ts`
// publishes. A pure leaf module (no DOM, no React) so `npm run test:unit` can
// exercise the rules directly — the geometry is the part that is easy to get
// subtly wrong and impossible to eyeball on a device you do not have.

/**
 * The smallest occlusion treated as a software keyboard.
 *
 * Two things make the visual viewport shorter than the layout one: a keyboard
 * (hundreds of px — the shortest iOS keyboard, landscape and undocked, is
 * still ~190) and, in an installed iOS app, the WebKit under-report described
 * in `viewportInset.ts` (exactly one safe-area inset: 62px on the tallest
 * iPhone, ~24 on an iPad). Nothing lands between them, so a single threshold
 * separates "something is covering the app" from "the browser is describing
 * the same window in two different sizes".
 */
export const KEYBOARD_MIN_PX = 120;

export type ViewportReading = {
  /** Layout-viewport height — `document.documentElement.clientHeight`. */
  layout: number;
  /** `visualViewport.height`, or the layout height where the API is missing. */
  visual: number;
  /** `visualViewport.offsetTop` — how far a keyboard pushed the view down. */
  offsetTop: number;
  /** Running from the home screen rather than a browser tab. */
  standalone: boolean;
};

export type ViewportVars = {
  /** Window height in px, or null to leave `--app-vh` to the stylesheet. */
  appVh: number | null;
  /** Visible height for a fixed overlay in px, or null to leave the CSS one. */
  modalVh: number | null;
  /** Offset of the visible box from the layout origin, in px. */
  modalTop: number;
};

/** Fold one reading into the variables to publish. */
export function viewportVars(reading: ViewportReading): ViewportVars {
  const layout = Math.floor(reading.layout);
  // Rounded down: a fractional height (common while the keyboard animates)
  // otherwise leaves a hairline of page showing under the sheet.
  const visual = Math.floor(reading.visual);
  const offsetTop = Math.max(0, Math.round(reading.offsetTop));
  const occluded = layout - (visual + offsetTop);

  // In a browser tab the layout viewport is the *toolbars-hidden* height, so it
  // is never the answer — the whole point of reading visualViewport is that it
  // comes back smaller. Installed there is no chrome to hide, so a visual
  // viewport shorter than the window by less than a keyboard is the WebKit
  // under-report, and the window is the honest number.
  const quirk = reading.standalone && occluded > 0 && occluded < KEYBOARD_MIN_PX;
  const visible = quirk ? layout : visual;

  // A measurement of 0 means the page is between layouts (a backgrounded tab,
  // a display swap). Publishing it would collapse the shell — and every
  // overlay — to nothing, so both fall back to the stylesheet, which is right
  // far more often than zero is.
  return {
    appVh: reading.standalone && layout > 0 ? layout : null,
    modalVh: visible > 0 ? visible : layout > 0 ? layout : null,
    modalTop: offsetTop,
  };
}
