/**
 * The agent's browser, drawn from its live frames, and the gestures that
 * drive it once the person has taken control:
 *
 * - tap: a click where the finger lifted from (two or three quick taps are a
 *   double or triple click);
 * - rest the finger for a moment, then move: press and drag (selecting text,
 *   a slider);
 * - swipe: scroll the page;
 * - two fingers: zoom the picture on the phone (nothing is sent), as does a
 *   double tap while only watching.
 *
 * Touches become fractions of the picture (browser/model.ts), which is what
 * the runtime takes. While the agent has control, its next click or keystroke
 * shows as a dot on the picture.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { LiveBrowser, LiveView } from "../../../browser/live.ts";
import { containSize, HOLD_MS, scrollDelta, TAP_SLOP_PX, tapCount, toFraction, type TapMemory } from "../../../browser/model.ts";
import { viewSizeFor } from "../../../browser/viewSize.ts";

interface Zoom {
  scale: number;
  x: number;
  y: number;
}

const NO_ZOOM: Zoom = { scale: 1, x: 0, y: 0 };
const MAX_ZOOM = 4;

export function LivePicture({ browser, view, interactive }: { browser: LiveBrowser; view: LiveView; interactive: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const picture = useRef<HTMLImageElement>(null);
  const [boxSize, setBoxSize] = useState({ w: 0, h: 0 });
  const [frameSize, setFrameSize] = useState({ w: 1280, h: 800 });
  const [zoom, setZoom] = useState<Zoom>(NO_ZOOM);
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setBoxSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Taking or handing back control starts from the whole page again.
  useEffect(() => setZoom(NO_ZOOM), [interactive]);

  const size = containSize(boxSize, frameSize);

  // The deployment sends frames no larger than the picture: its size in
  // device pixels, and more when zoomed in, told once it has settled.
  const zoomScale = zoom.scale;
  useEffect(() => {
    const timer = setTimeout(() => browser.setViewSize(viewSizeFor(size, window.devicePixelRatio, zoomScale)), 200);
    return () => clearTimeout(timer);
  }, [browser, size.w, size.h, zoomScale]);
  const gesture = useGestures(browser, picture, interactive, zoomRef, setZoom, frameSize);
  const pointer = view.pointer;
  const showPointer = !interactive && pointer && Date.now() / 1000 - pointer.at < 6;

  return (
    <div ref={box} className={interactive ? "olano-live-box interactive" : "olano-live-box"} {...gesture}>
      {view.frame && size.w > 0 && (
        <div
          className="olano-live-zoom"
          style={{ width: size.w, height: size.h, transform: `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})` }}
        >
          <img
            ref={picture}
            className="olano-live-picture"
            src={view.frame}
            alt="The agent's browser"
            draggable={false}
            onLoad={(e) => {
              const img = e.currentTarget;
              if (img.naturalWidth && (img.naturalWidth !== frameSize.w || img.naturalHeight !== frameSize.h)) {
                setFrameSize({ w: img.naturalWidth, h: img.naturalHeight });
              }
              browser.frameShown();
            }}
            onError={() => browser.frameShown()}
          />
          {showPointer && (
            <span
              key={pointer.at}
              className={pointer.kind === "click" ? "olano-live-pointer click" : "olano-live-pointer"}
              style={{ left: `${pointer.x * 100}%`, top: `${pointer.y * 100}%` }}
              aria-hidden="true"
            />
          )}
        </div>
      )}
    </div>
  );
}

type Mode = "pending" | "drag" | "scroll" | "pan" | "pinch";

/** Pointer handlers for the picture: page input while in control, local zoom always. */
function useGestures(
  browser: LiveBrowser,
  picture: React.RefObject<HTMLImageElement | null>,
  interactive: boolean,
  zoomRef: React.RefObject<Zoom>,
  setZoom: (zoom: Zoom) => void,
  frameSize: { w: number; h: number },
) {
  const touches = useRef(new Map<number, { x: number; y: number }>());
  const start = useRef({ x: 0, y: 0, at: 0 });
  const last = useRef({ x: 0, y: 0 });
  const mode = useRef<Mode>("pending");
  const holdTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastTap = useRef<TapMemory | null>(null);
  const pinch = useRef({ distance: 0, scale: 1, midX: 0, midY: 0, x: 0, y: 0 });
  const wheel = useRef({ dx: 0, dy: 0, timer: null as ReturnType<typeof setTimeout> | null });
  const lastMove = useRef(0);

  const fraction = (x: number, y: number) => {
    const img = picture.current;
    return img ? toFraction({ x, y }, img.getBoundingClientRect()) : null;
  };
  const sendAt = (t: string, x: number, y: number, extra: Record<string, unknown> = {}) => {
    const f = fraction(x, y);
    if (f) browser.send({ t, x: f.x, y: f.y, ...extra });
  };
  const flushWheel = () => {
    const w = wheel.current;
    if (w.timer) clearTimeout(w.timer);
    w.timer = null;
    if (w.dx || w.dy) browser.send({ t: "wheel", dx: w.dx, dy: w.dy });
    w.dx = 0;
    w.dy = 0;
  };
  const clearHold = () => {
    if (holdTimer.current) clearTimeout(holdTimer.current);
    holdTimer.current = null;
  };

  return {
    onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
      e.currentTarget.setPointerCapture?.(e.pointerId);
      touches.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (touches.current.size === 2) {
        // A second finger: zoom the picture, and forget the first finger's tap.
        clearHold();
        if (mode.current === "drag") sendAt("up", last.current.x, last.current.y, { button: "left", count: 1 });
        mode.current = "pinch";
        const [a, b] = [...touches.current.values()];
        const z = zoomRef.current;
        pinch.current = { distance: Math.hypot(a.x - b.x, a.y - b.y) || 1, scale: z.scale, midX: (a.x + b.x) / 2, midY: (a.y + b.y) / 2, x: z.x, y: z.y };
        return;
      }
      if (touches.current.size > 2) return;
      start.current = { x: e.clientX, y: e.clientY, at: Date.now() };
      last.current = { x: e.clientX, y: e.clientY };
      mode.current = "pending";
      if (interactive) {
        holdTimer.current = setTimeout(() => {
          holdTimer.current = null;
          if (mode.current !== "pending") return;
          mode.current = "drag";
          sendAt("move", start.current.x, start.current.y);
          sendAt("down", start.current.x, start.current.y, { button: "left", count: 1 });
        }, HOLD_MS);
      }
    },
    onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
      if (!touches.current.has(e.pointerId)) return;
      touches.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (mode.current === "pinch" && touches.current.size >= 2) {
        const [a, b] = [...touches.current.values()];
        const p = pinch.current;
        const scale = Math.max(1, Math.min(MAX_ZOOM, (p.scale * Math.hypot(a.x - b.x, a.y - b.y)) / p.distance));
        const midX = (a.x + b.x) / 2;
        const midY = (a.y + b.y) / 2;
        setZoom(scale === 1 ? NO_ZOOM : { scale, x: p.x + midX - p.midX, y: p.y + midY - p.midY });
        return;
      }
      const dx = e.clientX - last.current.x;
      const dy = e.clientY - last.current.y;
      const moved = Math.hypot(e.clientX - start.current.x, e.clientY - start.current.y);
      if (mode.current === "pending" && moved > TAP_SLOP_PX) {
        clearHold();
        if (!interactive || zoomRef.current.scale > 1) {
          mode.current = "pan";
        } else {
          mode.current = "scroll";
          sendAt("move", start.current.x, start.current.y);
        }
      }
      last.current = { x: e.clientX, y: e.clientY };
      if (mode.current === "pan") {
        const z = zoomRef.current;
        if (z.scale > 1) setZoom({ ...z, x: z.x + dx, y: z.y + dy });
      } else if (mode.current === "scroll") {
        const width = picture.current?.getBoundingClientRect().width ?? 0;
        wheel.current.dx += scrollDelta(dx, width, frameSize.w);
        wheel.current.dy += scrollDelta(dy, width, frameSize.w);
        if (!wheel.current.timer) wheel.current.timer = setTimeout(flushWheel, 50);
      } else if (mode.current === "drag") {
        const now = Date.now();
        if (now - lastMove.current > 30) {
          lastMove.current = now;
          sendAt("move", e.clientX, e.clientY);
        }
      }
    },
    onPointerUp: (e: React.PointerEvent<HTMLDivElement>) => {
      touches.current.delete(e.pointerId);
      if (mode.current === "pinch") {
        if (touches.current.size === 0) mode.current = "pending";
        return;
      }
      clearHold();
      const was = mode.current;
      mode.current = "pending";
      if (was === "drag") {
        sendAt("up", e.clientX, e.clientY, { button: "left", count: 1 });
      } else if (was === "scroll") {
        flushWheel();
      } else if (was === "pending") {
        const at = Date.now();
        const count = tapCount(lastTap.current, at, start.current.x, start.current.y);
        lastTap.current = { at, x: start.current.x, y: start.current.y, count };
        if (interactive) {
          sendAt("move", start.current.x, start.current.y);
          sendAt("down", start.current.x, start.current.y, { button: "left", count });
          sendAt("up", start.current.x, start.current.y, { button: "left", count });
        } else if (count === 2) {
          // Watching: a double tap zooms in on that spot, or back out.
          const z = zoomRef.current;
          const img = picture.current?.getBoundingClientRect();
          if (z.scale > 1 || !img) setZoom(NO_ZOOM);
          else {
            const cx = img.left + img.width / 2;
            const cy = img.top + img.height / 2;
            setZoom({ scale: 2, x: (cx - start.current.x) * 2 - (cx - start.current.x), y: (cy - start.current.y) * 2 - (cy - start.current.y) });
          }
        }
      }
    },
    onPointerCancel: (e: React.PointerEvent<HTMLDivElement>) => {
      touches.current.delete(e.pointerId);
      clearHold();
      if (mode.current === "drag") sendAt("up", last.current.x, last.current.y, { button: "left", count: 1 });
      if (mode.current === "scroll") flushWheel();
      mode.current = "pending";
    },
  };
}
