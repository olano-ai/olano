// Unit tests for the window→CSS-variable rules. Run with `npm run test:unit`
// (Node's built-in runner with type stripping — no test framework in the
// bundle).
//
// The readings below are the real ones. An iPhone 16 Pro Max is 956pt tall
// with a 62pt top inset and a 34pt home indicator; installed, WebKit reports
// its visual viewport (and `dvh`) as 894 — the window minus the top inset —
// which is the defect these rules exist to absorb.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { KEYBOARD_MIN_PX, viewportVars } from "./viewportVars.ts";

const IPHONE = 956;
const TOP_INSET = 62;

describe("viewportVars", () => {
  it("leaves --app-vh to the stylesheet in a browser tab", () => {
    // A tab's layout viewport is the toolbars-HIDDEN height, so pinning the
    // shell to it would put the composer under Safari's bottom bar — exactly
    // what the stylesheet's `dvh` avoids. Publishing nothing keeps that rule.
    const vars = viewportVars({ layout: IPHONE, visual: 860, offsetTop: 0, standalone: false });
    assert.equal(vars.appVh, null);
    assert.equal(vars.modalVh, 860);
  });

  it("pins --app-vh to the window when installed", () => {
    const vars = viewportVars({
      layout: IPHONE,
      visual: IPHONE - TOP_INSET,
      offsetTop: 0,
      standalone: true,
    });
    assert.equal(vars.appVh, IPHONE);
  });

  it("reads a sub-keyboard shortfall as the iOS under-report, not a keyboard", () => {
    // The bug: the app ended one status-bar inset short of the screen, with a
    // dead strip under the composer and every bottom sheet floating in it.
    const vars = viewportVars({
      layout: IPHONE,
      visual: IPHONE - TOP_INSET,
      offsetTop: 0,
      standalone: true,
    });
    assert.equal(vars.modalVh, IPHONE, "a bottom sheet must reach the bottom of the screen");
    assert.equal(vars.modalTop, 0);
  });

  it("still yields to the software keyboard", () => {
    // 336pt of keyboard: the sheet has to shrink or its buttons are under it.
    const vars = viewportVars({ layout: IPHONE, visual: 620, offsetTop: 0, standalone: true });
    assert.equal(vars.modalVh, 620);
  });

  it("draws the line between the two at the keyboard threshold", () => {
    const under = viewportVars({
      layout: IPHONE,
      visual: IPHONE - (KEYBOARD_MIN_PX - 1),
      offsetTop: 0,
      standalone: true,
    });
    const over = viewportVars({
      layout: IPHONE,
      visual: IPHONE - KEYBOARD_MIN_PX,
      offsetTop: 0,
      standalone: true,
    });
    assert.equal(under.modalVh, IPHONE);
    assert.equal(over.modalVh, IPHONE - KEYBOARD_MIN_PX);
  });

  it("counts a keyboard that pushed the view down as occlusion", () => {
    // iOS scrolls the visual viewport to keep the focused field visible: the
    // occluded strip is what is left AFTER the offset, not the height alone.
    const vars = viewportVars({ layout: IPHONE, visual: 600, offsetTop: 120, standalone: true });
    assert.equal(vars.modalVh, 600);
    assert.equal(vars.modalTop, 120);
  });

  it("never publishes a zero height", () => {
    // A page between layouts (backgrounded tab, display swap) measures 0; a
    // shell pinned to that would collapse to nothing on screen. Both fall back
    // to the stylesheet instead.
    const blank = viewportVars({ layout: 0, visual: 0, offsetTop: 0, standalone: true });
    assert.equal(blank.appVh, null);
    assert.equal(blank.modalVh, null);

    // Only the visual viewport unreadable: the window is still an answer.
    const half = viewportVars({ layout: IPHONE, visual: 0, offsetTop: 0, standalone: false });
    assert.equal(half.modalVh, IPHONE);
  });

  it("floors fractional measurements", () => {
    // A hairline of page showing under a sheet is the rounding-up failure.
    const vars = viewportVars({ layout: 800.6, visual: 745.4, offsetTop: 0.4, standalone: false });
    assert.equal(vars.modalVh, 745);
    assert.equal(vars.modalTop, 0);
  });

  it("ignores a visual viewport TALLER than the layout one", () => {
    // Reported on desktop Safari mid-pinch-zoom; a negative occlusion must not
    // read as "no keyboard, use the window" and shrink the overlay.
    const vars = viewportVars({ layout: 800, visual: 900, offsetTop: 0, standalone: true });
    assert.equal(vars.modalVh, 900);
  });
});
