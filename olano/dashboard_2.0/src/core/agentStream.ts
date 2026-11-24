// Single shared, reference-counted, multiplexed EventSource for the global
// `/api/stream` channel.
//
// WHY THIS EXISTS — the "Connection lost" flap fix.
// Every view used to open its own `new EventSource("/api/stream")`, and the
// currentAgent provider + the scheduler/approvals badge stores each opened
// another. A logged-in session therefore held 4+ (and, under React StrictMode
// double-mounts, view navigation, or multiple tabs, often 6+) simultaneous
// long-lived SSE connections to the same host. Browsers cap connections at
// ~6 per host over HTTP/1.1 (the Vite dev server and any non-h2 proxy), so the
// `/api/status` health probe in useServerHealth got starved in the browser's
// connection queue and tripped its 4s abort — surfacing the app-wide
// "Connection lost / Reconnecting" overlay even though the server was healthy.
// Behind a CDN rewrite (the since-retired split deploy shape) a ~120s SSE cap cut all of those
// streams in sync, producing periodic reconnect storms with the same effect.
//
// Multiplexing every consumer onto ONE EventSource removes both failure modes:
// one connection instead of N, one reconnect instead of N. Consumers call
// `subscribeStream(handler)` and receive every parsed frame, filtering by
// `frame.type` exactly as they did before. The connection opens on the first
// subscriber and closes when the last unsubscribes. `getStreamState()` exposes
// the live connection state so useServerHealth can treat an open stream as a
// positive liveness signal (the server is reachable) and a connected-badge view
// can reflect drops.

export interface StreamFrame {
  type?: string;
  data?: unknown;
  ts?: string;
  [key: string]: unknown;
}

export type StreamConnState = "connecting" | "open" | "closed";

type FrameHandler = (frame: StreamFrame) => void;
type StateHandler = (state: StreamConnState) => void;

// Backoff before we re-open the stream ourselves after the browser gives up on
// it (readyState CLOSED — see onerror). Mirrors EventSource's own ~3s default.
const RECONNECT_MS = 3000;

const frameListeners = new Set<FrameHandler>();
const stateListeners = new Set<StateHandler>();
let es: EventSource | null = null;
let refCount = 0;
let connState: StreamConnState = "closed";
let reopenTimer: ReturnType<typeof setTimeout> | null = null;

function setConnState(next: StreamConnState): void {
  if (next === connState) return;
  connState = next;
  for (const l of stateListeners) l(next);
}

function clearReopenTimer(): void {
  if (reopenTimer !== null) {
    clearTimeout(reopenTimer);
    reopenTimer = null;
  }
}

function open(): void {
  if (es) return;
  clearReopenTimer();
  setConnState("connecting");
  // Capture the instance so stale handlers from a superseded connection (a
  // self-reconnect below replaces `es`) can no-op via the `es === source` guard.
  const source = new EventSource("/api/stream", { withCredentials: true });
  es = source;
  source.onopen = () => {
    if (es === source) setConnState("open");
  };
  source.onmessage = (e) => {
    // Any received frame is definitive proof the connection is live. Browsers do
    // NOT reliably re-fire `onopen` on an automatic reconnect (and some proxies
    // flush the first frame before it), so keying "open" off `onopen` alone can
    // strand the state at "connecting" while data is flowing — surfacing a
    // phantom "Reconnecting…" pill over a perfectly healthy stream. The server
    // emits a `connected` frame at the top of EVERY (re)connection, so this
    // flips us to "open" the instant a connection succeeds, regardless of onopen.
    if (es === source) setConnState("open");
    let frame: StreamFrame;
    try {
      frame = JSON.parse(e.data) as StreamFrame;
    } catch {
      return; // keep-alive comment / non-JSON line — EventSource won't normally
      // deliver `: keepalive` comments here, but guard anyway.
    }
    for (const l of frameListeners) l(frame);
  };
  source.onerror = () => {
    if (es !== source) return; // stale handler from a superseded connection
    if (source.readyState === EventSource.CLOSED) {
      // The browser has given up and will NOT auto-reconnect — happens when a
      // proxy answers the SSE request with a non-2xx / HTML error page (e.g.
      // mid server-restart). Without self-healing this strands the pill on
      // "Reconnecting…" forever over a dead stream. Drop the instance and
      // re-open ourselves after a short backoff (while there are subscribers).
      es = null;
      setConnState("connecting");
      if (refCount > 0 && reopenTimer === null) {
        reopenTimer = setTimeout(() => {
          reopenTimer = null;
          if (refCount > 0 && !es) open();
        }, RECONNECT_MS);
      }
    } else {
      // readyState CONNECTING → the browser is auto-retrying; reflect the
      // transient drop. State returns to "open" via onopen/onmessage on reconnect.
      setConnState("connecting");
    }
  };
}

function close(): void {
  clearReopenTimer();
  es?.close();
  es = null;
  setConnState("closed");
}

/**
 * Subscribe to every frame on the shared `/api/stream` channel. Returns an
 * unsubscribe function. The single underlying EventSource is opened on the
 * first subscriber and closed when the last one unsubscribes (ref-counted),
 * so there is at most one connection regardless of how many views subscribe.
 */
export function subscribeStream(handler: FrameHandler): () => void {
  frameListeners.add(handler);
  if (++refCount === 1) open();
  return () => {
    frameListeners.delete(handler);
    if (--refCount === 0) close();
  };
}

/**
 * Subscribe to connection-state changes. This does NOT bump the ref count, so
 * it will not, by itself, hold the stream open — pair it with `subscribeStream`
 * (or rely on another subscriber) when you need the connection alive. Designed
 * for `useSyncExternalStore`, so it does not fire immediately; read the current
 * value with `getStreamState`.
 */
export function subscribeStreamState(handler: StateHandler): () => void {
  stateListeners.add(handler);
  return () => {
    stateListeners.delete(handler);
  };
}

/** Current connection state of the shared stream ("closed" when no subscribers). */
export function getStreamState(): StreamConnState {
  return connState;
}
