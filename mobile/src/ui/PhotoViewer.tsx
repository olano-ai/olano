/**
 * Photos full screen, the way the phone's own Photos app shows them: the
 * tapped photo grows out of its place in the conversation, swipe sideways for
 * the message's other photos, pinch or double-tap to zoom (and drag around a
 * zoomed photo), swipe down to put it back. A tap hides the controls.
 *
 * Mounted once (App.tsx) and opened from anywhere with `openPhotoViewer`.
 * The Android back button, Escape and the close button all close it.
 */

import { IonIcon } from "@ionic/react";
import { close as closeIcon } from "ionicons/icons";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { MessagePhoto } from "../chat/photos.ts";
import { setDarkOverlay } from "../native/lifecycle.ts";
import { createStore, useStore } from "../state/store.ts";
import { useMediaSrc } from "./mediaCache.ts";

interface ViewerState {
  photos: MessagePhoto[];
  index: number;
  /** The tile the first photo was opened from: where it grows from and returns to. */
  origin: HTMLElement | null;
  openedIndex: number;
}

const viewer = createStore<ViewerState | null>(null);

export function openPhotoViewer(photos: MessagePhoto[], index: number, origin?: HTMLElement | null): void {
  if (!photos.length) return;
  (document.activeElement as HTMLElement | null)?.blur?.();
  viewer.set({ photos, index, origin: origin ?? null, openedIndex: index });
}

const EASE = "cubic-bezier(0.2, 0.8, 0.2, 1)";
const OPEN_MS = 280;
const CLOSE_MS = 240;
const MAX_ZOOM = 4;
const DOUBLE_TAP_ZOOM = 2.5;

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Where an image of `natural` size sits when contained in `frame`. */
function containedBox(frame: DOMRect, natural: { width: number; height: number }): Box {
  const fit = Math.min(frame.width / natural.width, frame.height / natural.height);
  const width = natural.width * fit;
  const height = natural.height * fit;
  return { x: frame.left + (frame.width - width) / 2, y: frame.top + (frame.height - height) / 2, width, height };
}

/** The transform that puts the contained image over `target` (cover, like the tile draws it). */
function transformOnto(frame: DOMRect, natural: { width: number; height: number }, target: DOMRect): string {
  const box = containedBox(frame, natural);
  const scale = Math.max(target.width / box.width, target.height / box.height);
  const dx = target.left + target.width / 2 - (box.x + box.width / 2);
  const dy = target.top + target.height / 2 - (box.y + box.height / 2);
  return `translate3d(${dx}px, ${dy}px, 0) scale(${scale})`;
}

export function PhotoViewer() {
  const state = useStore(viewer);
  if (!state) return null;
  return createPortal(<Viewer key={state.openedIndex + ":" + state.photos[0]?.key} state={state} />, document.body);
}

function Viewer({ state }: { state: ViewerState }) {
  const root = useRef<HTMLDivElement>(null);
  const pager = useRef<HTMLDivElement>(null);
  const [index, setIndex] = useState(state.index);
  const [chrome, setChrome] = useState(true);
  const [zoomed, setZoomed] = useState(false);
  const [closing, setClosing] = useState(false);
  const closeRef = useRef<(via?: "button" | "swipe", dy?: number) => void>(() => undefined);
  const panes = useRef(new Map<number, PaneHandle>());
  const count = state.photos.length;

  useEffect(() => {
    setDarkOverlay(true);
    return () => setDarkOverlay(false);
  }, []);

  // Start on the tapped photo.
  useLayoutEffect(() => {
    const el = pager.current;
    if (el) el.scrollLeft = state.index * el.clientWidth;
  }, [state.index]);

  const setBackdrop = useCallback((progress: number, animate: boolean) => {
    const el = root.current;
    if (!el) return;
    el.style.transition = animate ? `background-color ${CLOSE_MS}ms ${EASE}` : "none";
    el.style.backgroundColor = `rgba(0, 0, 0, ${Math.max(0, 1 - progress)})`;
  }, []);

  const close = useCallback(
    (via: "button" | "swipe" = "button", dy = 0) => {
      if (closing) return;
      setClosing(true);
      setChrome(false);
      setBackdrop(1, true);
      const rect = index === state.openedIndex && state.origin?.isConnected ? state.origin.getBoundingClientRect() : null;
      // Back into its tile only when the tile is on screen; otherwise it fades.
      const origin = rect && rect.bottom > 0 && rect.top < window.innerHeight && rect.width > 0 ? rect : null;
      panes.current.get(index)?.leave(origin, via === "swipe" ? dy : 0);
      window.setTimeout(() => viewer.set(null), CLOSE_MS);
    },
    [closing, index, setBackdrop, state.openedIndex, state.origin],
  );
  closeRef.current = close;

  // Android back button (Ionic's overlays sit at 100) and Escape / arrows on a keyboard.
  useEffect(() => {
    const onBack = (event: Event) => {
      (event as CustomEvent<{ register: (priority: number, handler: () => void) => void }>).detail.register(110, () =>
        closeRef.current(),
      );
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
      const el = pager.current;
      if (!el) return;
      if (event.key === "ArrowRight") el.scrollBy({ left: el.clientWidth, behavior: "smooth" });
      if (event.key === "ArrowLeft") el.scrollBy({ left: -el.clientWidth, behavior: "smooth" });
    };
    document.addEventListener("ionBackButton", onBack);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("ionBackButton", onBack);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  const onScroll = () => {
    const el = pager.current;
    if (!el || !el.clientWidth) return;
    const next = Math.round(el.scrollLeft / el.clientWidth);
    if (next !== index && next >= 0 && next < count) {
      panes.current.get(index)?.reset(false);
      setIndex(next);
    }
  };

  return (
    <div
      ref={root}
      className={chrome ? "olano-pv" : "olano-pv bare"}
      role="dialog"
      aria-modal="true"
      aria-label="Photo viewer"
    >
      <div
        ref={pager}
        className="olano-pv-pager"
        style={{ overflowX: zoomed || count < 2 ? "hidden" : "auto" }}
        onScroll={onScroll}
      >
        {state.photos.map((photo, i) => (
          <Pane
            key={photo.key}
            photo={photo}
            hero={i === state.index ? state.origin : null}
            register={(handle) => {
              if (handle) panes.current.set(i, handle);
              else panes.current.delete(i);
            }}
            onZoom={(z) => i === index && setZoomed(z)}
            onTap={() => setChrome((c) => !c)}
            onDismissDrag={(progress) => setBackdrop(progress, false)}
            onDismissCancel={() => setBackdrop(0, true)}
            onDismiss={(dy) => close("swipe", dy)}
          />
        ))}
      </div>
      <div className="olano-pv-bar">
        <button type="button" className="olano-pv-close" aria-label="Close" onClick={() => close()}>
          <IonIcon icon={closeIcon} />
        </button>
        {count > 1 && (
          <span className="olano-pv-count" aria-live="polite">
            {index + 1} of {count}
          </span>
        )}
        <span className="olano-pv-spacer" />
      </div>
      {count > 1 && count <= 12 && (
        <div className="olano-pv-dots" aria-hidden="true">
          {state.photos.map((p, i) => (
            <span key={p.key} className={i === index ? "on" : ""} />
          ))}
        </div>
      )}
    </div>
  );
}

interface PaneHandle {
  reset(animate: boolean): void;
  leave(origin: DOMRect | null, dy: number): void;
}

interface Pointer {
  x: number;
  y: number;
}

type Gesture =
  | { kind: "pending"; x: number; y: number; t: number }
  | { kind: "pan"; x: number; y: number }
  | { kind: "pinch"; dist: number; scale: number; tx: number; ty: number; mx: number; my: number }
  | { kind: "dismiss"; x: number; y: number; lastY: number; lastT: number; vy: number };

function Pane({
  photo,
  hero,
  register,
  onZoom,
  onTap,
  onDismissDrag,
  onDismissCancel,
  onDismiss,
}: {
  photo: MessagePhoto;
  hero: HTMLElement | null;
  register: (handle: PaneHandle | null) => void;
  onZoom: (zoomed: boolean) => void;
  onTap: () => void;
  onDismissDrag: (progress: number) => void;
  onDismissCancel: () => void;
  onDismiss: (dy: number) => void;
}) {
  const src = useMediaSrc(photo);
  const frame = useRef<HTMLDivElement>(null);
  const img = useRef<HTMLImageElement>(null);
  const view = useRef({ scale: 1, x: 0, y: 0 });
  const pointers = useRef(new Map<number, Pointer>());
  const gesture = useRef<Gesture | null>(null);
  const lastTap = useRef({ t: 0, x: 0, y: 0 });
  const tapTimer = useRef<number | undefined>(undefined);
  const heroPending = useRef(!!hero);
  const [zoomed, setZoomed] = useState(false);

  const natural = () => {
    const el = img.current;
    return el && el.naturalWidth ? { width: el.naturalWidth, height: el.naturalHeight } : null;
  };

  const apply = useCallback((animate: boolean, extra = "") => {
    const el = img.current;
    if (!el) return;
    const { scale, x, y } = view.current;
    el.style.transition = animate ? `transform ${OPEN_MS}ms ${EASE}, opacity ${OPEN_MS}ms ${EASE}` : "none";
    el.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${scale})${extra}`;
  }, []);

  const setZoom = useCallback(
    (z: boolean) => {
      setZoomed(z);
      onZoom(z);
    },
    [onZoom],
  );

  /** Keeps a zoomed photo's edges from leaving the screen's edges. */
  const clamp = useCallback(() => {
    const box = frame.current?.getBoundingClientRect();
    const size = natural();
    const v = view.current;
    if (!box || !size) return;
    const contained = containedBox(box, size);
    const maxX = Math.max(0, (contained.width * v.scale - box.width) / 2);
    const maxY = Math.max(0, (contained.height * v.scale - box.height) / 2);
    v.x = Math.min(maxX, Math.max(-maxX, v.x));
    v.y = Math.min(maxY, Math.max(-maxY, v.y));
  }, []);

  const reset = useCallback(
    (animate: boolean) => {
      view.current = { scale: 1, x: 0, y: 0 };
      apply(animate);
      setZoom(false);
    },
    [apply, setZoom],
  );

  useEffect(() => {
    register({
      reset,
      leave(origin, dy) {
        const el = img.current;
        const box = frame.current?.getBoundingClientRect();
        const size = natural();
        if (!el) return;
        el.style.transition = `transform ${CLOSE_MS}ms ${EASE}, opacity ${CLOSE_MS}ms ${EASE}`;
        if (origin && box && size && view.current.scale <= 1.01) {
          el.style.transform = transformOnto(box, size, origin);
          el.style.opacity = "0.98";
        } else {
          const away = dy ? Math.sign(dy) * window.innerHeight : 0;
          el.style.transform = `translate3d(0, ${away}px, 0) scale(${dy ? 0.9 : 0.92})`;
          el.style.opacity = "0";
        }
      },
    });
    return () => register(null);
  }, [register, reset]);

  // Grow out of the tile it was opened from (unless the phone asks for less motion).
  const onLoad = () => {
    if (!heroPending.current) return;
    heroPending.current = false;
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const el = img.current;
    const box = frame.current?.getBoundingClientRect();
    const size = natural();
    if (!el || !box || !size || !hero?.isConnected) return;
    el.style.transition = "none";
    el.style.transform = transformOnto(box, size, hero.getBoundingClientRect());
    void el.offsetWidth;
    apply(true);
  };

  const zoomAt = (clientX: number, clientY: number) => {
    const box = frame.current?.getBoundingClientRect();
    if (!box) return;
    const v = view.current;
    if (v.scale > 1.01) {
      reset(true);
      return;
    }
    // Keep the tapped point under the finger.
    const px = clientX - (box.left + box.width / 2);
    const py = clientY - (box.top + box.height / 2);
    v.x = px - px * DOUBLE_TAP_ZOOM;
    v.y = py - py * DOUBLE_TAP_ZOOM;
    v.scale = DOUBLE_TAP_ZOOM;
    clamp();
    apply(true);
    setZoom(true);
  };

  const center = () => {
    const box = frame.current!.getBoundingClientRect();
    return { cx: box.left + box.width / 2, cy: box.top + box.height / 2 };
  };

  const startPinch = () => {
    const [a, b] = [...pointers.current.values()];
    const { cx, cy } = center();
    gesture.current = {
      kind: "pinch",
      dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
      scale: view.current.scale,
      tx: view.current.x,
      ty: view.current.y,
      mx: (a.x + b.x) / 2 - cx,
      my: (a.y + b.y) / 2 - cy,
    };
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (!src) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      // Not every pointer can be captured (a synthetic one); the gesture still works.
    }
    if (pointers.current.size === 2) startPinch();
    else if (pointers.current.size === 1) gesture.current = { kind: "pending", x: e.clientX, y: e.clientY, t: performance.now() };
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const p = pointers.current.get(e.pointerId);
    const g = gesture.current;
    if (!p || !g) return;
    const dx = e.clientX - p.x;
    const dy = e.clientY - p.y;
    p.x = e.clientX;
    p.y = e.clientY;
    const v = view.current;
    if (g.kind === "pinch" && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()];
      const { cx, cy } = center();
      const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const scale = Math.min(MAX_ZOOM * 1.25, Math.max(0.7, (g.scale * dist) / g.dist));
      const mx = (a.x + b.x) / 2 - cx;
      const my = (a.y + b.y) / 2 - cy;
      // The point under the fingers when the pinch began stays under them.
      v.x = mx - (g.mx - g.tx) * (scale / g.scale);
      v.y = my - (g.my - g.ty) * (scale / g.scale);
      v.scale = scale;
      apply(false);
      return;
    }
    if (g.kind === "pending") {
      const mx = e.clientX - g.x;
      const my = e.clientY - g.y;
      if (Math.hypot(mx, my) < 8) return;
      if (v.scale > 1.01) gesture.current = { kind: "pan", x: e.clientX, y: e.clientY };
      else if (Math.abs(my) > Math.abs(mx)) gesture.current = { kind: "dismiss", x: g.x, y: g.y, lastY: e.clientY, lastT: performance.now(), vy: 0 };
      else return; // Sideways: the pager scrolls.
    }
    const now = gesture.current!;
    if (now.kind === "pan") {
      v.x += dx;
      v.y += dy;
      apply(false);
    } else if (now.kind === "dismiss") {
      const t = performance.now();
      now.vy = (e.clientY - now.lastY) / Math.max(1, t - now.lastT);
      now.lastY = e.clientY;
      now.lastT = t;
      const offset = e.clientY - now.y;
      v.x = (e.clientX - now.x) * 0.6;
      v.y = offset;
      v.scale = 1 - Math.min(0.25, Math.abs(offset) / 1600);
      apply(false);
      onDismissDrag(Math.min(1, Math.abs(offset) / 360));
    }
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const g = gesture.current;
    pointers.current.delete(e.pointerId);
    const v = view.current;
    if (g?.kind === "pinch") {
      if (pointers.current.size === 1) {
        const [rest] = [...pointers.current.values()];
        gesture.current = { kind: "pan", x: rest.x, y: rest.y };
      } else {
        gesture.current = null;
      }
      if (v.scale < 1.02) {
        reset(true);
      } else {
        if (v.scale > MAX_ZOOM) {
          // Settle back to the limit around where the fingers were.
          const k = MAX_ZOOM / v.scale;
          v.x = g.mx - (g.mx - v.x) * k;
          v.y = g.my - (g.my - v.y) * k;
          v.scale = MAX_ZOOM;
        }
        clamp();
        apply(true);
        setZoom(true);
      }
      return;
    }
    if (pointers.current.size) return;
    gesture.current = null;
    if (!g) return;
    if (g.kind === "pending" && e.type === "pointerup") {
      // A tap: two in quick succession zoom, one alone shows or hides the controls.
      const now = performance.now();
      const last = lastTap.current;
      window.clearTimeout(tapTimer.current);
      if (now - last.t < 300 && Math.hypot(e.clientX - last.x, e.clientY - last.y) < 40) {
        lastTap.current = { t: 0, x: 0, y: 0 };
        zoomAt(e.clientX, e.clientY);
      } else {
        lastTap.current = { t: now, x: e.clientX, y: e.clientY };
        tapTimer.current = window.setTimeout(onTap, 300);
      }
      return;
    }
    if (g.kind === "pan") {
      clamp();
      apply(true);
    } else if (g.kind === "dismiss") {
      const offset = v.y;
      if (Math.abs(offset) > 110 || Math.abs(g.vy) > 0.6) {
        onDismiss(offset || g.vy);
      } else {
        view.current = { scale: 1, x: 0, y: 0 };
        apply(true);
        onDismissCancel();
      }
    }
  };

  useEffect(() => () => window.clearTimeout(tapTimer.current), []);

  return (
    <div
      ref={frame}
      className="olano-pv-slide"
      style={{ touchAction: zoomed ? "none" : "pan-x" }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      {src ? (
        <img ref={img} src={src} alt={photo.name} draggable={false} onLoad={onLoad} />
      ) : (
        <div className="olano-pv-loading" aria-label="Loading photo" />
      )}
    </div>
  );
}
