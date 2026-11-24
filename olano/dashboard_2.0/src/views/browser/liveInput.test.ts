import { test } from "node:test";
import assert from "node:assert/strict";
import {
  IDENTITY_VIEW,
  ZOOM_MAX,
  addressFor,
  clampView,
  pagePixelsPerScreenPixel,
  panView,
  classifyKey,
  framePoint,
  isMacPlatform,
  pinchView,
  remoteKeyName,
} from "./liveInput.ts";

const box = { left: 100, top: 50, width: 1280, height: 800 };

test("a point maps to the same fraction of the page when the shapes match", () => {
  assert.deepEqual(framePoint(100 + 640, 50 + 200, box, 1280, 800), { x: 0.5, y: 0.25 });
});

test("a letterboxed picture maps from the picture, not the element", () => {
  // A 1000x800 element showing a 1280x800 page: the picture is 1000x625,
  // centred, so 87.5px bands above and below are not the page.
  const tall = { left: 0, top: 0, width: 1000, height: 800 };
  assert.equal(framePoint(500, 40, tall, 1280, 800), null);
  const p = framePoint(500, 87.5 + 625 / 2, tall, 1280, 800);
  assert.ok(p);
  assert.equal(p.x, 0.5);
  assert.equal(p.y, 0.5);
});

test("a point outside the picture is not a click on the page", () => {
  assert.equal(framePoint(99, 60, box, 1280, 800), null);
  assert.equal(framePoint(200, 851, box, 1280, 800), null);
});

test("clamped, a point outside lands on the nearest edge", () => {
  assert.deepEqual(framePoint(0, 1000, box, 1280, 800, true), { x: 0, y: 1 });
});

test("with no frame yet the element's own box is the reference", () => {
  assert.deepEqual(framePoint(100 + 320, 50 + 400, box, 0, 0), { x: 0.25, y: 0.5 });
});

const key = (k: string, mods: Partial<{ metaKey: boolean; ctrlKey: boolean; altKey: boolean; isComposing: boolean }> = {}) => ({
  key: k,
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  ...mods,
});

test("Command+V on a Mac and Ctrl+V elsewhere paste", () => {
  assert.deepEqual(classifyKey(key("v", { metaKey: true }), true), { kind: "paste" });
  assert.deepEqual(classifyKey(key("v", { ctrlKey: true }), false), { kind: "paste" });
  assert.deepEqual(classifyKey(key("V", { ctrlKey: true }), false), { kind: "paste" });
});

test("copy and cut bring the selection back", () => {
  assert.deepEqual(classifyKey(key("c", { metaKey: true }), true), { kind: "copy", cut: false });
  assert.deepEqual(classifyKey(key("x", { ctrlKey: true }), false), { kind: "copy", cut: true });
});

test("Ctrl+V on a Mac is not a paste: it is replayed like any other key", () => {
  assert.deepEqual(classifyKey(key("v", { ctrlKey: true }), true), { kind: "key", key: "v" });
});

test("Command is replayed as Control on a Mac and dropped elsewhere", () => {
  assert.equal(remoteKeyName("Meta", true), "Control");
  assert.equal(remoteKeyName("Meta", false), null);
  assert.deepEqual(classifyKey(key("Meta", { metaKey: true }), true), { kind: "key", key: "Control" });
  assert.deepEqual(classifyKey(key("Meta", { metaKey: true }), false), { kind: "skip" });
  assert.deepEqual(classifyKey(key("a", { metaKey: true }), true), { kind: "key", key: "a" });
});

test("printable characters and editing keys are replayed as they are", () => {
  for (const k of ["a", "A", "é", "@", " ", "Enter", "Backspace", "Tab", "ArrowLeft", "Shift"]) {
    assert.deepEqual(classifyKey(key(k), false), { kind: "key", key: k });
  }
});

test("dead keys and input methods are left to the text field", () => {
  assert.deepEqual(classifyKey(key("Dead"), true), { kind: "compose" });
  assert.deepEqual(classifyKey(key("Process"), false), { kind: "compose" });
  assert.deepEqual(classifyKey(key("Unidentified"), false), { kind: "compose" });
  assert.deepEqual(classifyKey(key("e", { isComposing: true }), true), { kind: "compose" });
});

test("platform detection", () => {
  assert.equal(isMacPlatform("MacIntel", ""), true);
  assert.equal(isMacPlatform("", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"), true);
  assert.equal(isMacPlatform("Win32", ""), false);
  assert.equal(isMacPlatform("Linux x86_64", ""), false);
});

test("a zoomed view never opens an empty band, and zooming out past 1 is the plain view", () => {
  assert.deepEqual(clampView({ scale: 2, x: 50, y: -1000 }, 400, 250), { scale: 2, x: 0, y: -250 });
  assert.deepEqual(clampView({ scale: 0.5, x: -30, y: -30 }, 400, 250), IDENTITY_VIEW);
  assert.equal(clampView({ scale: 9, x: 0, y: 0 }, 400, 250).scale, ZOOM_MAX);
});

test("a pinch keeps the point under the fingers in place while it scales", () => {
  // Fingers 100px apart around (200, 125) spread to 200px: twice the size,
  // and the picture point that was at (200, 125) is still there.
  const v = pinchView(IDENTITY_VIEW, { x: 150, y: 125 }, { x: 250, y: 125 }, { x: 100, y: 125 }, { x: 300, y: 125 }, 400, 250);
  assert.equal(v.scale, 2);
  assert.equal(v.x + 2 * 200, 200);
  assert.equal(v.y + 2 * 125, 125);
});

test("moving both fingers together pans a zoomed picture", () => {
  const start = { scale: 2, x: -200, y: -125 };
  const v = pinchView(start, { x: 150, y: 100 }, { x: 250, y: 100 }, { x: 120, y: 80 }, { x: 220, y: 80 }, 400, 250);
  assert.equal(v.scale, 2);
  assert.equal(v.x, -230);
  assert.equal(v.y, -145);
});

test("a point on a zoomed picture maps through the picture's own box", () => {
  // A 400x250 screen at 2x, panned to its centre: the picture's box is
  // 800x500 starting at (-200, -125), so the screen's centre is the page's.
  const p = framePoint(200, 125, { left: -200, top: -125, width: 800, height: 500 }, 1280, 800);
  assert.deepEqual(p, { x: 0.5, y: 0.5 });
});

test("a drag on the unzoomed picture is all scroll", () => {
  const { view, rest } = panView(IDENTITY_VIEW, 0, -30, 400, 250);
  assert.equal(view, IDENTITY_VIEW);
  assert.deepEqual(rest, { x: 0, y: -30 });
});

test("a drag on a zoomed picture moves it to its edge, then scrolls the rest", () => {
  // At 2x the picture can move up by at most 250px; it starts 240px up.
  const { view, rest } = panView({ scale: 2, x: -100, y: -240 }, 0, -30, 400, 250);
  assert.deepEqual(view, { scale: 2, x: -100, y: -250 });
  assert.deepEqual(rest, { x: 0, y: -20 });
});

test("screen pixels convert to page pixels through the drawn picture", () => {
  // A 1280x800 page drawn 400px wide: one screen pixel is 3.2 page pixels;
  // zoomed 2x it is half that.
  assert.equal(pagePixelsPerScreenPixel({ width: 400, height: 250 }, 1280, 800), 3.2);
  assert.equal(pagePixelsPerScreenPixel({ width: 800, height: 500 }, 1280, 800), 1.6);
  assert.equal(pagePixelsPerScreenPixel({ width: 400, height: 250 }, 0, 0), 1);
});

test("the address bar shows the page, and nothing for a blank one", () => {
  assert.equal(addressFor("https://github.com/login"), "https://github.com/login");
  assert.equal(addressFor("about:blank"), "");
  assert.equal(addressFor(""), "");
});

test("the address bar says whether a page is private on the wire", async () => {
  const { addressSecurity } = await import("./liveInput.ts");
  assert.equal(addressSecurity("https://github.com/login"), "secure");
  assert.equal(addressSecurity("http://example.com/"), "insecure");
  assert.equal(addressSecurity("about:blank"), "none");
  assert.equal(addressSecurity(""), "none");
});

test("a tab shows its title, else where it is", async () => {
  const { tabLabel } = await import("./liveInput.ts");
  assert.equal(tabLabel({ title: " GitHub ", url: "https://github.com/" }), "GitHub");
  assert.equal(tabLabel({ title: "", url: "https://github.com/login?x=1" }), "github.com/login");
  assert.equal(tabLabel({ title: "", url: "about:blank" }), "");
});

test("a long list filters by label or group, accents and case aside, keeping indexes", async () => {
  const { filterOptions } = await import("./liveInput.ts");
  const opts = [
    { label: "Costa Rica", group: "Americas" },
    { label: "Côte d'Ivoire", group: "Africa" },
    { label: "Canada", group: "Americas" },
  ];
  assert.deepEqual(
    filterOptions(opts, "cote").map((m) => m.index),
    [1],
  );
  assert.deepEqual(
    filterOptions(opts, "AMERICAS").map((m) => m.index),
    [0, 2],
  );
  assert.equal(filterOptions(opts, "  ").length, 3);
});

test("sizes read as a person would say them", async () => {
  const { formatBytes } = await import("./liveInput.ts");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(3 * 1024 * 1024), "3.0 MB");
});

test("presses close in time and place make a double, then a triple click", async () => {
  const { nextClickRun, MULTI_CLICK_MS } = await import("./liveInput.ts");
  const one = nextClickRun(null, 1000, 200, 100, "left");
  assert.equal(one.count, 1);
  const two = nextClickRun(one, 1000 + 180, 202, 101, "left");
  assert.equal(two.count, 2);
  const three = nextClickRun(two, 1000 + 360, 201, 100, "left");
  assert.equal(three.count, 3);
  // Each gap is measured from the press before it, not from the first.
  const late = nextClickRun(three, 1000 + 360 + MULTI_CLICK_MS + 1, 201, 100, "left");
  assert.equal(late.count, 1);
});

test("a press elsewhere, with another button, or a finger too far starts a new run", async () => {
  const { nextClickRun, MULTI_CLICK_SLOP_PX, MULTI_TAP_SLOP_PX } = await import("./liveInput.ts");
  const one = nextClickRun(null, 0, 200, 100, "left");
  assert.equal(nextClickRun(one, 100, 200 + MULTI_CLICK_SLOP_PX + 1, 100, "left").count, 1);
  assert.equal(nextClickRun(one, 100, 200, 100, "right").count, 1);
  // A finger lands less precisely than a mouse, so a tap's run is wider.
  assert.equal(nextClickRun(one, 100, 220, 100, "left").count, 1);
  assert.equal(nextClickRun(one, 100, 220, 100, "left", MULTI_TAP_SLOP_PX).count, 2);
  // A clock that went backwards never continues a run.
  assert.equal(nextClickRun(one, -5, 200, 100, "left").count, 1);
});
