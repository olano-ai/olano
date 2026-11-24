// What a person's mouse and keyboard mean to the agent's remote browser.
//
// Pure (no React, no DOM beyond plain numbers and strings) so node's --test can
// import it. LiveScreen.tsx is the only caller.

/** Where a point on the screen element falls on the page, as 0..1 fractions,
 *  or `null` when it is outside the picture.
 *
 *  The frame is drawn with `object-fit: contain`, so whenever the element's
 *  shape differs from the page's the picture is letterboxed and the element's
 *  own box is the wrong reference: the bands are not the page, and inside the
 *  picture a fraction of the box is not a fraction of the page. With `clamp`
 *  a point outside is pulled to the nearest edge instead (a drag that leaves
 *  the picture still has to release somewhere on the page). */
export function framePoint(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  frameWidth: number,
  frameHeight: number,
  clamp = false,
): { x: number; y: number } | null {
  if (rect.width <= 0 || rect.height <= 0) return null;
  let left = rect.left;
  let top = rect.top;
  let width = rect.width;
  let height = rect.height;
  if (frameWidth > 0 && frameHeight > 0) {
    const scale = Math.min(rect.width / frameWidth, rect.height / frameHeight);
    width = frameWidth * scale;
    height = frameHeight * scale;
    left += (rect.width - width) / 2;
    top += (rect.height - height) / 2;
  }
  const x = (clientX - left) / width;
  const y = (clientY - top) / height;
  if (clamp) return { x: Math.min(Math.max(x, 0), 1), y: Math.min(Math.max(y, 0), 1) };
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x, y };
}

// ── Zoom ─────────────────────────────────────────────────────────────────────
//
// On a phone the whole page is drawn about a third of its size, too small to
// hit a button, and the picture has to own its touches (a drag on it is a drag
// on the page), so the phone's own pinch-zoom never starts there. The picture
// zooms itself instead: two fingers pinch and move it, one finger acts on the
// page. The view is a CSS transform on the layer holding the picture,
// `translate(x, y) scale(scale)` from its top-left corner, in pixels of the
// screen element. A point is mapped through the picture's transformed box, so
// `framePoint` needs nothing else to stay exact while zoomed.

export interface ZoomView {
  scale: number;
  x: number;
  y: number;
}

export interface Pt {
  x: number;
  y: number;
}

export const ZOOM_MAX = 4;
export const IDENTITY_VIEW: ZoomView = { scale: 1, x: 0, y: 0 };

/** Keep a zoomed picture covering the element: no empty band opens on any
 *  side, and a scale back at 1 is exactly the unzoomed view. */
export function clampView(view: ZoomView, width: number, height: number): ZoomView {
  const scale = Math.min(Math.max(view.scale, 1), ZOOM_MAX);
  if (scale < 1.01) return IDENTITY_VIEW;
  const x = Math.min(0, Math.max(width - width * scale, view.x));
  const y = Math.min(0, Math.max(height - height * scale, view.y));
  return { scale, x, y };
}

/** The view while two fingers pinch, from where they started (`a0`, `b0`,
 *  over `start`) to where they are (`a1`, `b1`). The picture point that was
 *  under the fingers' midpoint stays under it, so moving both fingers pans. */
export function pinchView(
  start: ZoomView,
  a0: Pt,
  b0: Pt,
  a1: Pt,
  b1: Pt,
  width: number,
  height: number,
): ZoomView {
  const d0 = Math.hypot(a0.x - b0.x, a0.y - b0.y);
  const d1 = Math.hypot(a1.x - b1.x, a1.y - b1.y);
  const scale = Math.min(Math.max(start.scale * (d0 > 0 ? d1 / d0 : 1), 1), ZOOM_MAX);
  const m0 = { x: (a0.x + b0.x) / 2, y: (a0.y + b0.y) / 2 };
  const m1 = { x: (a1.x + b1.x) / 2, y: (a1.y + b1.y) / 2 };
  const cx = (m0.x - start.x) / start.scale;
  const cy = (m0.y - start.y) / start.scale;
  return clampView({ scale, x: m1.x - scale * cx, y: m1.y - scale * cy }, width, height);
}

/** A one-finger drag of (`dx`, `dy`) screen pixels: a zoomed picture moves
 *  first, as far as it can, and whatever is left over (`rest`) scrolls the
 *  page, the way a phone pans a zoomed page before scrolling it. */
export function panView(
  view: ZoomView,
  dx: number,
  dy: number,
  width: number,
  height: number,
): { view: ZoomView; rest: Pt } {
  if (view.scale <= 1) return { view, rest: { x: dx, y: dy } };
  const next = clampView({ scale: view.scale, x: view.x + dx, y: view.y + dy }, width, height);
  return { view: next, rest: { x: dx - (next.x - view.x), y: dy - (next.y - view.y) } };
}

/** How many page pixels one screen pixel of the picture covers, through the
 *  picture's (possibly zoomed) box and its letterboxing. */
export function pagePixelsPerScreenPixel(
  rect: { width: number; height: number },
  frameWidth: number,
  frameHeight: number,
): number {
  if (frameWidth <= 0 || frameHeight <= 0 || rect.width <= 0 || rect.height <= 0) return 1;
  return 1 / Math.min(rect.width / frameWidth, rect.height / frameHeight);
}

/** What the address bar shows for a page: nothing for the blank page, the
 *  page's own address otherwise. */
export function addressFor(url: string): string {
  return !url || url === "about:blank" ? "" : url;
}

// A finger that lifts within this distance of where it went down is a tap,
// sent as one press and release where it went down. Fingers wobble, and a
// press sent at once could not be taken back when a second finger turns the
// gesture into a pinch.
export const TAP_SLOP_PX = 10;
// A finger held still this long is a press (a long press, the start of a drag
// that has not moved yet): the press goes to the page without waiting for it
// to lift, and moving it afterwards drags (selects text, moves a slider). A
// finger that moves before then scrolls instead, as on any phone.
export const TOUCH_HOLD_MS = 250;

// ── Double and triple clicks ─────────────────────────────────────────────────
//
// The page tells a double click from two single ones by the press's click
// count, never by timing: two presses of count 1, however close, select
// nothing and fire no `dblclick`. Pointer events carry no click count of their
// own, so it is counted here: a press of the same button, this soon after the
// last one and this close to it, continues the run.

export const MULTI_CLICK_MS = 500;
/** How far a press may land from the last one and still continue the run, in
 *  screen pixels: a mouse barely moves, a finger lands wherever it lands. */
export const MULTI_CLICK_SLOP_PX = 6;
export const MULTI_TAP_SLOP_PX = 24;

export interface ClickRun {
  at: number;
  x: number;
  y: number;
  button: string;
  count: number;
}

/** The run a press at (`x`, `y`) screen pixels, time `at` ms, belongs to:
 *  `count` 2 is a double click, 3 a triple. */
export function nextClickRun(
  prev: ClickRun | null,
  at: number,
  x: number,
  y: number,
  button: string,
  slop = MULTI_CLICK_SLOP_PX,
): ClickRun {
  const continues =
    prev !== null &&
    prev.button === button &&
    at - prev.at >= 0 &&
    at - prev.at <= MULTI_CLICK_MS &&
    Math.hypot(x - prev.x, y - prev.y) <= slop;
  return { at, x, y, button, count: continues && prev ? prev.count + 1 : 1 };
}

export type KeyAction =
  /** Let the browser deliver a paste event; its text goes to the page. */
  | { kind: "paste" }
  /** Bring the page's selected text back to this computer's clipboard. */
  | { kind: "copy"; cut: boolean }
  /** Replay this key on the page (key name as the remote browser knows it). */
  | { kind: "key"; key: string }
  /** Leave it to the text field: a dead key or an input method composing
   *  text, whose result arrives as an input event. */
  | { kind: "compose" }
  /** Nothing to send (a key the remote browser has no use for). */
  | { kind: "skip" };

export interface KeyLike {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  isComposing?: boolean;
}

/** The remote browser runs on Linux, where the shortcut modifier is Control.
 *  On a Mac the person presses Command, so Command is replayed as Control and
 *  Command+A selects all, Command+Z undoes. Elsewhere the Windows/Super key has
 *  no meaning to a page and is not sent. */
export function remoteKeyName(key: string, mac: boolean): string | null {
  if (key === "Meta" || key === "OS") return mac ? "Control" : null;
  if (!key || key === "Unidentified" || key === "Dead" || key === "Process") return null;
  if (key === "Fn" || key === "FnLock" || key === "Hyper" || key === "Super") return null;
  return key;
}

/** What a keydown on the live screen should do. */
export function classifyKey(e: KeyLike, mac: boolean): KeyAction {
  if (e.isComposing || e.key === "Dead" || e.key === "Process" || e.key === "Unidentified") {
    return { kind: "compose" };
  }
  const shortcut = mac ? e.metaKey : e.ctrlKey;
  if (shortcut && !e.altKey) {
    const k = e.key.toLowerCase();
    if (k === "v") return { kind: "paste" };
    if (k === "c" || k === "x") return { kind: "copy", cut: k === "x" };
  }
  const key = remoteKeyName(e.key, mac);
  return key ? { kind: "key", key } : { kind: "skip" };
}

export function isMacPlatform(platform: string, userAgent: string): boolean {
  return /Mac|iPhone|iPad|iPod/i.test(platform || userAgent || "");
}

// ── Browser chrome ───────────────────────────────────────────────────────────

/** Whether an address is private on the wire: `https` reads as secure,
 *  plain `http` as not, anything else (blank, `about:`, `data:`) as neither. */
export function addressSecurity(url: string): "secure" | "insecure" | "none" {
  try {
    const scheme = new URL(url).protocol;
    if (scheme === "https:" || scheme === "wss:") return "secure";
    if (scheme === "http:" || scheme === "ws:") return "insecure";
  } catch {
    /* not an address */
  }
  return "none";
}

/** What a tab shows: the page's title, else its host and path, else "". */
export function tabLabel(tab: { title: string; url: string }): string {
  const title = tab.title.trim();
  if (title) return title;
  const url = addressFor(tab.url);
  if (!url) return "";
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname === "/" ? "" : u.pathname}`;
  } catch {
    return url;
  }
}

/** The options of a long list that match what was typed, by label or group,
 *  ignoring case and accents, with each option's own index kept. */
export function filterOptions<T extends { label: string; group: string }>(
  options: T[],
  query: string,
): { option: T; index: number }[] {
  const fold = (s: string) =>
    s
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase();
  const q = fold(query.trim());
  const all = options.map((option, index) => ({ option, index }));
  if (!q) return all;
  return all.filter(({ option }) => fold(option.label).includes(q) || fold(option.group).includes(q));
}

/** "1.2 MB" style sizes for the file sheet. */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Files a person sends to a page in one go (mirrors live.MAX_UPLOAD_BYTES
 *  and live.MAX_UPLOAD_FILES). */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_UPLOAD_FILES = 20;
