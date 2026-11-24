import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import { useT } from "../../core/i18n";
import { useMediaQuery } from "../../core/useMediaQuery";
import {
  IDENTITY_VIEW,
  MULTI_TAP_SLOP_PX,
  TAP_SLOP_PX,
  TOUCH_HOLD_MS,
  classifyKey,
  framePoint,
  isMacPlatform,
  nextClickRun,
  pagePixelsPerScreenPixel,
  panView,
  pinchView,
  type ClickRun,
  type Pt,
  type ZoomView,
} from "./liveInput";
import { LiveNav, LivePrompts } from "./LiveChrome";
import { STR } from "./strings";

// The live view, shared by the agent's Browser tab and the chat's browser dock.
//
// One WebSocket per mounted `useLiveBrowser` (olano/runtime/routes/browser.py):
// binary JPEG frames out, JSON in both directions (`state`, `pointer`, `error`,
// `closed` from the server; input events and `control` from us). The dock keeps
// one connection across its small and expanded layouts, so expanding never
// reconnects.
//
// A screencast frame never contains a mouse pointer, so the server sends
// `pointer` events wherever the agent is about to act (it also glides the real
// mouse there while someone watches, so hover states show in the frames), and
// `LiveScreen` draws an animated cursor plus a ripple on each click. The cursor
// hides while a person holds control: their own pointer is the one that counts.
//
// While a person holds control the keyboard goes to a hidden text field over
// the picture rather than to the picture itself: a paste event (Command+V /
// Ctrl+V) and an input method's composed text only reach an editable element
// in every browser. Copy and cut ask the page for its selected text and put it
// on this computer's clipboard (`clipboard` frames), and so does a copy button
// on the page pressed by the person in control.

export type LivePhase = "connecting" | "open" | "closed" | "error";

export interface LivePointer {
  x: number;
  y: number;
  kind: string;
  at: number;
}

export interface LiveTab {
  id: number;
  url: string;
  title: string;
  active: boolean;
}

/** A page's alert / confirm / prompt / leave-this-page dialog, waiting for
 *  the person in control. */
export interface LiveDialog {
  id: number;
  kind: string;
  message: string;
  default: string;
}

/** A page's request for files (an `<input type=file>` was clicked). */
export interface LiveChooser {
  id: number;
  multiple: boolean;
  accept: string;
}

/** The `<select>` a click just opened: the browser draws its list outside
 *  the picture, so the dashboard shows it instead. */
export interface LiveSelect {
  label: string;
  selected: number;
  options: { label: string; disabled: boolean; group: string }[];
}

/** A recording of the browser in progress (POST .../browser/recording). */
export interface LiveRecording {
  since: number;
  seconds: number;
  name: string;
}

export interface LivePicker {
  kind: string;
  value: string;
  min: string;
  max: string;
  step: string;
  label: string;
}

export interface LiveBrowser {
  /** The agent whose browser this is ("" while idle). */
  agent: string;
  phase: LivePhase;
  error: string;
  frame: string | null;
  pointer: LivePointer | null;
  controller: string;
  attention: string;
  /** The page the browser is on (from the session state frames). */
  url: string;
  /** Text copied from the page that this browser refused to put on the
   *  clipboard by itself (no user gesture left, or not a secure page). */
  pendingCopy: string;
  clearPendingCopy: () => void;
  /** The last input the page refused or could not take, shown for a few
   *  seconds while a person holds control ("" when there is none). */
  notice: string;
  /** The page in front is loading (between navigation start and load). */
  loading: boolean;
  canBack: boolean;
  canForward: boolean;
  tabs: LiveTab[];
  dialog: LiveDialog | null;
  chooser: LiveChooser | null;
  select: LiveSelect | null;
  clearSelect: () => void;
  /** A date, time or colour field a click landed on: the browser's own picker is never in the picture. */
  picker: LivePicker | null;
  clearPicker: () => void;
  /** A recording running, for the red dot and the Stop button. */
  recording: LiveRecording | null;
  send: (event: Record<string, unknown>) => void;
  /** The picture drew the frame it was given: the deployment may send the next. */
  frameShown: () => void;
  /** The picture is `w` by `h` device pixels: frames need be no larger. */
  reportViewSize: (w: number, h: number) => void;
}

/** Put text on the clipboard: the async API where the page may use it, else the
 *  older copy command (which works inside a click, including on plain http). */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the copy command */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

const NOTICE_MS = 6000;

function liveUrl(agentName: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/agents/${encodeURIComponent(agentName)}/browser/live`;
}

/** Connect to an agent's live browser; `null` keeps the hook idle (no socket). */
export function useLiveBrowser(agentName: string | null): LiveBrowser {
  const wsRef = useRef<WebSocket | null>(null);
  const urls = useRef<string[]>([]);
  const [phase, setPhase] = useState<LivePhase>("connecting");
  const [error, setError] = useState("");
  const [frame, setFrame] = useState<string | null>(null);
  const [pointer, setPointer] = useState<LivePointer | null>(null);
  const [controller, setController] = useState("agent");
  const [attention, setAttention] = useState("");
  const [url, setUrl] = useState("");
  const urlRef = useRef("");
  const [pendingCopy, setPendingCopy] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [canBack, setCanBack] = useState(false);
  const [canForward, setCanForward] = useState(false);
  const [tabs, setTabs] = useState<LiveTab[]>([]);
  const [dialog, setDialog] = useState<LiveDialog | null>(null);
  const [chooser, setChooser] = useState<LiveChooser | null>(null);
  const [select, setSelect] = useState<LiveSelect | null>(null);
  const [picker, setPicker] = useState<LivePicker | null>(null);
  const [recording, setRecording] = useState<LiveRecording | null>(null);
  const viewSizeRef = useRef<{ w: number; h: number } | null>(null);

  useEffect(() => {
    if (!agentName) return;
    let alive = true;
    setPhase("connecting");
    setError("");
    const ws = new WebSocket(liveUrl(agentName));
    ws.binaryType = "blob";
    wsRef.current = ws;
    ws.onopen = () => {
      if (!alive) return;
      setPhase("open");
      // This viewer paces the stream: the next frame comes once the last was
      // drawn (`shown`, sent from the picture's onLoad), and no larger than
      // the picture (`view`, sent as it is measured).
      ws.send(JSON.stringify({ t: "view", ack: true, ...(viewSizeRef.current ?? {}) }));
    };
    ws.onmessage = (ev) => {
      if (!alive) return;
      if (typeof ev.data === "string") {
        try {
          const msg = JSON.parse(ev.data) as {
            type?: string;
            message?: string;
            text?: string;
            session?: {
              controller?: string;
              attention?: string;
              url?: string;
              loading?: boolean;
              can_back?: boolean;
              can_forward?: boolean;
              tabs?: LiveTab[];
              dialog?: LiveDialog | null;
              chooser?: LiveChooser | null;
              recording?: LiveRecording | null;
            };
          } & Partial<LivePointer> &
            Partial<LiveSelect> &
            Partial<LivePicker>;
          if (msg.type === "error" && msg.message) {
            setError(msg.message);
            setNotice(msg.message);
          }
          else if (msg.type === "notice" && msg.message) setNotice(msg.message);
          else if (msg.type === "clipboard" && typeof msg.text === "string") {
            const text = msg.text;
            if (!text) return;
            void copyToClipboard(text).then((ok) => setPendingCopy(ok ? "" : text));
          }
          else if (msg.type === "closed") setPhase("closed");
          else if (msg.type === "state" && msg.session) {
            const st = msg.session;
            setController(st.controller || "agent");
            setAttention(st.attention || "");
            const nextUrl = st.url || "";
            // A list opened on the previous page belongs to it.
            if (nextUrl !== urlRef.current) {
              setSelect(null);
              setPicker(null);
            }
            urlRef.current = nextUrl;
            setUrl(nextUrl);
            setLoading(!!st.loading);
            setCanBack(!!st.can_back);
            setCanForward(!!st.can_forward);
            setTabs(Array.isArray(st.tabs) ? st.tabs : []);
            setDialog(st.dialog || null);
            setChooser(st.chooser || null);
            setRecording(st.recording || null);
          } else if (msg.type === "select" && Array.isArray(msg.options)) {
            setSelect({ label: msg.label || "", selected: msg.selected ?? -1, options: msg.options });
          } else if (msg.type === "picker" && typeof msg.kind === "string") {
            setPicker({ kind: msg.kind, value: msg.value || "", min: msg.min || "", max: msg.max || "", step: msg.step || "", label: msg.label || "" });
          } else if (msg.type === "pointer" && typeof msg.x === "number" && typeof msg.y === "number") {
            setPointer({ x: msg.x, y: msg.y, kind: msg.kind || "move", at: msg.at || Date.now() / 1000 });
          }
        } catch {
          /* ignore malformed control frames */
        }
        return;
      }
      const next = URL.createObjectURL(ev.data as Blob);
      // Keep the previous URL alive one more frame: an <img> may still be
      // decoding it when the next one arrives.
      urls.current.push(next);
      while (urls.current.length > 2) URL.revokeObjectURL(urls.current.shift() as string);
      setFrame(next);
    };
    ws.onclose = (ev) => {
      if (!alive) return;
      if (ev.code === 4404 || ev.code === 1000) setPhase("closed");
      else if (ev.code >= 4000 || ev.code === 1008) {
        setPhase("error");
        setError(ev.reason || String(ev.code));
      } else setPhase("closed");
    };
    return () => {
      alive = false;
      ws.close();
      wsRef.current = null;
      for (const u of urls.current) URL.revokeObjectURL(u);
      urls.current = [];
      setFrame(null);
      setPointer(null);
    };
  }, [agentName]);

  const send = useCallback((event: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(event));
  }, []);

  const clearPendingCopy = useCallback(() => setPendingCopy(""), []);
  const clearSelect = useCallback(() => setSelect(null), []);
  const clearPicker = useCallback(() => setPicker(null), []);

  /** The picture drew the frame it was given: the next may come. */
  const frameShown = useCallback(() => send({ t: "shown" }), [send]);

  /** The picture is `w` by `h` device pixels: frames need be no larger. Told when it changes by more than a tenth. */
  const reportViewSize = useCallback(
    (w: number, h: number) => {
      if (w <= 0 || h <= 0) return;
      const last = viewSizeRef.current;
      if (last && Math.abs(w - last.w) <= last.w * 0.1 && Math.abs(h - last.h) <= last.h * 0.1) return;
      viewSizeRef.current = { w: Math.ceil(w), h: Math.ceil(h) };
      send({ t: "view", ack: true, ...viewSizeRef.current });
    },
    [send],
  );

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  return {
    agent: agentName ?? "",
    phase,
    error,
    frame,
    pointer,
    controller,
    attention,
    url,
    pendingCopy,
    clearPendingCopy,
    notice,
    loading,
    canBack,
    canForward,
    tabs,
    dialog,
    chooser,
    select,
    clearSelect,
    picker,
    clearPicker,
    recording,
    frameShown,
    reportViewSize,
    send,
  };
}

/** "example.com/path" for display: no scheme, no query, ellipsis-friendly. */
export function displayUrl(raw: string): string {
  if (!raw || raw === "about:blank") return "";
  try {
    const u = new URL(raw);
    const path = u.pathname === "/" ? "" : u.pathname;
    return `${u.host}${path}`;
  } catch {
    return raw;
  }
}

export function liveStatusKey(live: LiveBrowser): string {
  if (live.phase === "connecting") return "br.live.connecting";
  if (live.phase === "closed") return "br.live.closed";
  if (live.phase === "error") return "br.live.error";
  return live.controller === "user" ? "br.live.controlling" : "br.live.watching";
}

function Cursor({ pointer }: { pointer: LivePointer }) {
  return (
    <div
      className="browser-cursor"
      data-testid="browser-cursor"
      style={{ left: `${pointer.x * 100}%`, top: `${pointer.y * 100}%` }}
      aria-hidden="true"
    >
      {pointer.kind === "click" && <span key={pointer.at} className="browser-ripple" />}
      <svg width="20" height="22" viewBox="0 0 20 22">
        <path
          d="M2 1.5 L2 17.5 L6.4 13.4 L9.3 20 L12.2 18.7 L9.4 12.2 L15.4 12.2 Z"
          fill="#fff"
          stroke="#111"
          strokeWidth="1.4"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}

const IS_MAC =
  typeof navigator !== "undefined" && isMacPlatform(navigator.platform || "", navigator.userAgent || "");

/** A finger or the mouse held on the page. */
interface Press {
  id: number;
  button: string;
  /** Which click of a run it is (2: a double click), see `nextClickRun`. */
  count: number;
  /** Where it went down, as page fractions, and in screen pixels (for the
   *  tap slop). */
  start: Pt;
  startClient: Pt;
  /** The newest page position sent, where a release that never came ends. */
  last: Pt;
  /** Whether the press has reached the page yet (a touch waits, see below). */
  sent: boolean;
  /** A finger that moved before it was held: it scrolls, and never presses. */
  scrolling: boolean;
  /** Where the scrolling finger was at the last move, in screen pixels. */
  lastClient: Pt;
  timer: number | undefined;
}

interface Pinch {
  ids: [number, number];
  a: Pt;
  b: Pt;
  view: ZoomView;
}

/**
 * The browser picture with the agent's cursor drawn over it. `interactive`
 * forwards pointer, wheel and keys to the page, and only while a person holds
 * control (the server refuses input otherwise anyway).
 *
 * On a touch screen it behaves like a page on a phone: a tap clicks, a
 * one-finger drag scrolls (moving a zoomed picture first, `panView`), a touch
 * held still and then moved drags, and two fingers zoom and move the picture
 * (`pinchView`). A one-finger press is held back until it is clearly a press:
 * a tap goes out as press and release together when the finger lifts, a long
 * press once it has been held. So a second finger arriving turns the gesture
 * into a pinch with nothing sent to the page, and a touch the phone cancels
 * (it does, when its keyboard opens or closes under the finger) sends nothing
 * either, instead of a press whose release never comes. A press that did go
 * out is always released.
 */
export function LiveScreen({
  live,
  interactive,
  rounded = 8,
  onActivate,
  label,
}: {
  live: LiveBrowser;
  interactive: boolean;
  rounded?: number;
  onActivate?: () => void;
  label?: string;
}) {
  const t = useT(STR);
  const lastMove = useRef(0);
  // Scroll owed to the page by a dragging finger, sent ~20 times a second.
  const wheel = useRef({ x: 0, y: 0, at: 0 });
  const imgRef = useRef<HTMLImageElement | null>(null);
  const keysRef = useRef<HTMLTextAreaElement | null>(null);
  // Keys replayed as down on the page, so the matching up goes out even when
  // the down was renamed (Command -> Control) and a release is never lost.
  const held = useRef(new Map<string, string>());
  const press = useRef<Press | null>(null);
  const clicks = useRef<ClickRun | null>(null);
  // Fingers on the picture, in screen pixels, and the pinch they make.
  const touches = useRef(new Map<number, Pt>());
  const pinch = useRef<Pinch | null>(null);
  const [view, setViewState] = useState<ZoomView>(IDENTITY_VIEW);
  const viewRef = useRef<ZoomView>(IDENTITY_VIEW);
  const controlling = interactive && live.controller === "user";
  const send = live.send;

  function setView(next: ZoomView) {
    viewRef.current = next;
    setViewState(next);
  }

  function point(e: PointerEvent<HTMLDivElement>, clamp: boolean) {
    // The picture's own box: it is the one that moves when zoomed.
    const img = imgRef.current;
    return framePoint(
      e.clientX,
      e.clientY,
      (img || e.currentTarget).getBoundingClientRect(),
      img?.naturalWidth || 0,
      img?.naturalHeight || 0,
      clamp,
    );
  }
  function screenPoint(e: PointerEvent<HTMLDivElement>): Pt {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  const button = (b: number) => (b === 1 ? "middle" : b === 2 ? "right" : "left");

  const commitPress = useCallback(() => {
    const pr = press.current;
    if (!pr || pr.sent || pr.scrolling) return;
    window.clearTimeout(pr.timer);
    pr.timer = undefined;
    pr.sent = true;
    send({ t: "down", ...pr.start, button: pr.button, count: pr.count });
  }, [send]);

  /** End the press: a release where it is now, or a whole tap if the press
   *  was still held back. */
  function releasePress(at: Pt | null) {
    const pr = press.current;
    if (!pr) return;
    press.current = null;
    window.clearTimeout(pr.timer);
    if (!pr.sent) {
      send({ t: "down", ...pr.start, button: pr.button, count: pr.count });
      send({ t: "up", ...pr.start, button: pr.button, count: pr.count });
      return;
    }
    send({ t: "up", ...(at || pr.last), button: pr.button, count: pr.count });
  }

  function flushWheel(force: boolean) {
    const w = wheel.current;
    if (Math.abs(w.x) < 0.5 && Math.abs(w.y) < 0.5) return;
    const now = Date.now();
    if (!force && now - w.at < 50) return;
    send({ t: "wheel", dx: Math.round(w.x), dy: Math.round(w.y) });
    wheel.current = { x: 0, y: 0, at: now };
  }

  /** A finger dragged by (`dx`, `dy`) screen pixels: move a zoomed picture,
   *  then scroll the page by what is left, in the page's own pixels. */
  function scrollBy(e: PointerEvent<HTMLDivElement>, dx: number, dy: number) {
    const r = e.currentTarget.getBoundingClientRect();
    const { view: next, rest } = panView(viewRef.current, dx, dy, r.width, r.height);
    if (next !== viewRef.current) setView(next);
    const img = imgRef.current;
    const ratio = pagePixelsPerScreenPixel(
      (img || e.currentTarget).getBoundingClientRect(),
      img?.naturalWidth || 0,
      img?.naturalHeight || 0,
    );
    // A finger moving up scrolls the page down.
    wheel.current.x -= rest.x * ratio;
    wheel.current.y -= rest.y * ratio;
    flushWheel(false);
  }

  /** Abandon the press: nothing if it never reached the page, else the
   *  release it is owed, where it last was. Never a click nobody meant. */
  const dropPress = useCallback(() => {
    const pr = press.current;
    if (!pr) return;
    press.current = null;
    window.clearTimeout(pr.timer);
    if (pr.sent) send({ t: "up", ...pr.last, button: pr.button, count: pr.count });
  }, [send]);

  function onDown(e: PointerEvent<HTMLDivElement>) {
    if (!controlling) return;
    e.preventDefault();
    const touch = e.pointerType === "touch";
    // Captured, so its release reaches us wherever it happens: a finger left
    // in `touches` would turn every later tap into a pinch.
    e.currentTarget.setPointerCapture?.(e.pointerId);
    if (touch) {
      touches.current.set(e.pointerId, screenPoint(e));
      if (touches.current.size >= 2) {
        dropPress();
        clicks.current = null;
        const [[ia, a], [ib, b]] = [...touches.current.entries()];
        pinch.current = { ids: [ia, ib], a, b, view: viewRef.current };
        return;
      }
    }
    // Measure before focusing: focusing can scroll the view under the pointer,
    // and a point measured after that lands somewhere else on the page.
    const p = point(e, false);
    // The keyboard field takes focus for a mouse or a pen only. On a phone,
    // focusing it opens the on-screen keyboard over half the picture on every
    // tap and shifts the page under the finger; text goes through the box
    // below the address bar there instead.
    if (!touch) keysRef.current?.focus({ preventScroll: true });
    if (!p) return;
    dropPress();
    const run = nextClickRun(
      clicks.current,
      Date.now(),
      e.clientX,
      e.clientY,
      button(e.button),
      touch ? MULTI_TAP_SLOP_PX : undefined,
    );
    clicks.current = run;
    press.current = {
      id: e.pointerId,
      button: run.button,
      count: run.count,
      start: p,
      startClient: { x: e.clientX, y: e.clientY },
      last: p,
      sent: false,
      scrolling: false,
      lastClient: { x: e.clientX, y: e.clientY },
      timer: undefined,
    };
    if (touch) press.current.timer = window.setTimeout(commitPress, TOUCH_HOLD_MS);
    else commitPress();
  }
  function onUp(e: PointerEvent<HTMLDivElement>) {
    if (!controlling) {
      if (onActivate) onActivate();
      return;
    }
    e.preventDefault();
    if (e.pointerType === "touch") {
      touches.current.delete(e.pointerId);
      if (pinch.current) {
        // The gesture stays a pinch until every finger has lifted.
        if (touches.current.size === 0) pinch.current = null;
        return;
      }
    }
    const pr = press.current;
    if (!pr || pr.id !== e.pointerId) return;
    if (pr.scrolling) {
      flushWheel(true);
      press.current = null;
      return;
    }
    releasePress(pr.sent ? point(e, true) : null);
  }
  function onCancel(e: PointerEvent<HTMLDivElement>) {
    flushWheel(true);
    touches.current.delete(e.pointerId);
    if (pinch.current && touches.current.size === 0) pinch.current = null;
    if (press.current?.id === e.pointerId) dropPress();
  }
  function onMove(e: PointerEvent<HTMLDivElement>) {
    if (!controlling) return;
    const touch = e.pointerType === "touch";
    if (touch) {
      if (!touches.current.has(e.pointerId)) return;
      touches.current.set(e.pointerId, screenPoint(e));
      const pz = pinch.current;
      if (pz) {
        const a = touches.current.get(pz.ids[0]);
        const b = touches.current.get(pz.ids[1]);
        if (a && b) {
          const r = e.currentTarget.getBoundingClientRect();
          setView(pinchView(pz.view, pz.a, pz.b, a, b, r.width, r.height));
        }
        return;
      }
    }
    const pr = press.current && press.current.id === e.pointerId ? press.current : null;
    if (pr && !pr.sent) {
      if (!pr.scrolling) {
        const moved = Math.hypot(e.clientX - pr.startClient.x, e.clientY - pr.startClient.y);
        if (moved < TAP_SLOP_PX) return;
        if (!touch) {
          commitPress();
        } else {
          // Moved before it was held: a scroll. The page scrolls whatever is
          // under the mouse, so the mouse goes where the finger went down.
          pr.scrolling = true;
          // A scroll ends any run of taps: the next tap is a first one.
          clicks.current = null;
          window.clearTimeout(pr.timer);
          pr.timer = undefined;
          send({ t: "move", ...pr.start });
        }
      }
      if (pr.scrolling) {
        scrollBy(e, e.clientX - pr.lastClient.x, e.clientY - pr.lastClient.y);
        pr.lastClient = { x: e.clientX, y: e.clientY };
        return;
      }
    }
    // A finger only acts on the page while pressed; a mouse also hovers.
    if (touch && !pr) return;
    const now = Date.now();
    if (now - lastMove.current < 50) return; // ~20 moves a second is plenty
    lastMove.current = now;
    const p = point(e, true);
    if (!p) return;
    if (pr) pr.last = p;
    send({ t: "move", ...p });
  }

  function onScreenKey(e: KeyboardEvent<HTMLDivElement>) {
    if (controlling || !onActivate) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onActivate();
    }
  }
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (!controlling) return;
    const action = classifyKey(e.nativeEvent, IS_MAC);
    // Paste and composed text arrive as their own events on the text field.
    if (action.kind === "paste" || action.kind === "compose") return;
    e.preventDefault();
    if (action.kind === "copy") {
      if (!e.repeat) send({ t: action.cut ? "cut" : "copy" });
      return;
    }
    if (action.kind === "key") {
      held.current.set(e.key, action.key);
      send({ t: "keydown", key: action.key });
    }
  }
  function onKeyUp(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (!controlling) return;
    const remote = held.current.get(e.key);
    if (remote === undefined) return;
    held.current.delete(e.key);
    e.preventDefault();
    send({ t: "keyup", key: remote });
  }
  function releaseHeld() {
    // Focus left the picture with keys down (Command+Tab away, say): release
    // them on the page too, or it keeps a stuck Control/Shift.
    for (const remote of held.current.values()) send({ t: "keyup", key: remote });
    held.current.clear();
  }
  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    if (!controlling) return;
    e.preventDefault();
    const text = e.clipboardData.getData("text/plain");
    if (text) send({ t: "text", text });
  }
  function onInput(e: FormEvent<HTMLTextAreaElement>) {
    // Text an input method, a dead key or a phone keyboard produced without a
    // key we replayed. Composition still running: wait for its end.
    const el = e.currentTarget;
    if ((e.nativeEvent as InputEvent).isComposing) return;
    if (controlling && el.value) send({ t: "text", text: el.value });
    el.value = "";
  }

  useEffect(() => {
    if (controlling) return;
    // Control handed back (or never held): the page no longer takes input,
    // so forget anything half done, and show the whole page again.
    held.current.clear();
    touches.current.clear();
    pinch.current = null;
    clicks.current = null;
    if (press.current) {
      window.clearTimeout(press.current.timer);
      press.current = null;
    }
    viewRef.current = IDENTITY_VIEW;
    setViewState(IDENTITY_VIEW);
  }, [controlling]);
  useEffect(() => () => window.clearTimeout(press.current?.timer), []);

  const zoomed = view.scale > 1;
  return (
    <div
      className="browser-screen"
      data-testid="browser-screen"
      role={onActivate && !controlling ? "button" : undefined}
      aria-label={label}
      tabIndex={controlling ? -1 : 0}
      onPointerDown={onDown}
      onPointerUp={onUp}
      onPointerMove={onMove}
      onPointerCancel={onCancel}
      onWheel={(e) => controlling && send({ t: "wheel", dx: e.deltaX, dy: e.deltaY })}
      onKeyDown={onScreenKey}
      onContextMenu={(e) => controlling && e.preventDefault()}
      style={{
        borderRadius: rounded,
        border: controlling ? "2px solid var(--accent)" : "1px solid var(--border)",
        cursor: controlling ? "default" : onActivate ? "zoom-in" : "not-allowed",
        touchAction: controlling ? "none" : "auto",
      }}
    >
      <div
        className="browser-screen-layer"
        data-testid="browser-screen-layer"
        style={zoomed ? { transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` } : undefined}
      >
        {live.frame && (
          <img
            ref={imgRef}
            src={live.frame}
            alt=""
            draggable={false}
            onLoad={(e) => {
              // Drawn: the next frame may come, no larger than this picture
              // is shown (device pixels, more when zoomed in).
              const box = e.currentTarget.getBoundingClientRect();
              const scale = (window.devicePixelRatio || 1) * Math.max(1, view.scale);
              live.reportViewSize(box.width * scale, box.height * scale);
              live.frameShown();
            }}
            onError={() => live.frameShown()}
          />
        )}
        {live.pointer && live.controller !== "user" && <Cursor pointer={live.pointer} />}
      </div>
      {live.loading && <div className="browser-progress" role="progressbar" aria-label={t("br.live.loading")} />}
      {zoomed && (
        <button
          type="button"
          className="btn sm browser-zoom-reset"
          data-testid="browser-zoom-reset"
          // The picture's own handlers would take this tap to the page.
          onPointerDown={(e) => e.stopPropagation()}
          onPointerUp={(e) => e.stopPropagation()}
          onClick={() => setView(IDENTITY_VIEW)}
        >
          {t("br.live.zoomReset")}
        </button>
      )}
      {controlling && (
        <textarea
          ref={keysRef}
          className="browser-keys"
          data-testid="browser-keys"
          aria-label={t("br.live.keys")}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          spellCheck={false}
          onKeyDown={onKeyDown}
          onKeyUp={onKeyUp}
          onBlur={releaseHeld}
          onPaste={onPaste}
          onInput={onInput}
          onCompositionEnd={(e) => {
            const el = e.currentTarget;
            if (el.value) send({ t: "text", text: el.value });
            el.value = "";
          }}
        />
      )}
    </div>
  );
}

// A touch screen is the one place a typed-out text box earns its row: a phone
// keyboard is what fails to reach a page most often. With a mouse and a
// keyboard, typing goes straight into the picture.
const TOUCH_QUERY = "(pointer: coarse)";

/** What a person in control works with besides the picture: tabs and the
 *  address bar (`LiveNav`), on a touch screen the text box and keys, and the
 *  sheets for a page's dialogs, file requests and dropdowns (`LivePrompts`). */
export function LiveControls({ live }: { live: LiveBrowser }) {
  const t = useT(STR);
  const [text, setText] = useState("");
  const textRef = useRef<HTMLInputElement | null>(null);
  const touch = useMediaQuery(TOUCH_QUERY);
  if (live.controller !== "user") return null;
  const pressKey = (key: string) => {
    live.send({ t: "keydown", key });
    live.send({ t: "keyup", key });
  };
  return (
    <>
      {live.notice && (
        <div className="browser-live-notice" role="status" data-testid="browser-live-notice">
          {live.notice}
        </div>
      )}
      {touch && (
        <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
          {t("br.live.touchHint")}
        </p>
      )}
      {live.pendingCopy && (
        <div className="row browser-copy-ready" role="status" style={{ gap: 6, flexWrap: "wrap" }}>
          <span className="muted grow" style={{ fontSize: 12.5 }}>
            {t("br.live.copyReady")}
          </span>
          <button
            type="button"
            className="btn primary sm"
            data-testid="browser-copy-ready"
            onClick={() => {
              void copyToClipboard(live.pendingCopy).then((ok) => ok && live.clearPendingCopy());
            }}
          >
            {t("br.live.copy")}
          </button>
          <button type="button" className="btn ghost sm" onClick={live.clearPendingCopy}>
            {t("br.live.dismiss")}
          </button>
        </div>
      )}
      <LiveNav live={live} />
      {touch && (
        <form
          className="row"
          style={{ gap: 6, flexWrap: "wrap" }}
          onSubmit={(e) => {
            e.preventDefault();
            if (text) {
              live.send({ t: "text", text });
              setText("");
            }
            // Close the phone's keyboard: left open it covers half the
            // picture, and the next tap on the page is the one it eats.
            textRef.current?.blur();
          }}
        >
          <input
            ref={textRef}
            className="input"
            style={{ flex: "1 1 200px", minWidth: 0 }}
            placeholder={t("br.live.typePlaceholder")}
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoComplete="off"
            data-testid="browser-type-text"
          />
          <button type="submit" className="btn sm" data-testid="browser-send-text">
            {t("br.live.send")}
          </button>
          {/* Kept together so a narrow screen wraps them as one pair. */}
          <span className="row" style={{ gap: 6 }}>
            <button
              type="button"
              className="btn ghost sm"
              title={t("br.live.backspaceHint")}
              data-testid="browser-key-backspace"
              onClick={() => pressKey("Backspace")}
            >
              {t("br.live.backspace")}
            </button>
            <button
              type="button"
              className="btn ghost sm"
              title={t("br.live.tabHint")}
              data-testid="browser-key-tab"
              onClick={() => pressKey("Tab")}
            >
              {t("br.live.tab")}
            </button>
            <button
              type="button"
              className="btn ghost sm"
              title={t("br.live.enterHint")}
              data-testid="browser-key-enter"
              onClick={() => pressKey("Enter")}
            >
              {t("br.live.enter")}
            </button>
          </span>
        </form>
      )}
      <LivePrompts live={live} />
    </>
  );
}
