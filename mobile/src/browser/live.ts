/**
 * The live view of one agent's browser: a WebSocket to
 * `/api/agents/{name}/browser/live`, shared by everything showing it (the
 * floating window in a conversation, the full-screen view), open while
 * something shows it.
 *
 * In: JPEG frames (binary, newest only matters), and JSON `state`, `pointer`,
 * `clipboard`, `select`, `picker`, `notice`, `error`, `closed`. Out: JSON `{t: ...}` messages; the
 * runtime refuses page input until the person has taken control. Going to
 * the background closes the socket (the phone would anyway); coming back
 * reopens it if anything still shows it.
 *
 * The viewer paces the stream. On open it says so (`{t: "view", ack: true}`)
 * and how big its picture is in device pixels (`w`, `h`, from
 * `setViewSize`); the deployment then sends the next frame only once this
 * side has said the last was drawn (`{t: "shown"}`, sent from `frameShown`),
 * caps frames at the picture's size, and sends smaller, rougher ones while
 * drawing is slow. A phone's web view decodes every frame on the thread the
 * taps need, so what reaches it is never more than it can draw, and the taps
 * get through on any page, however much it redraws. The FramePacer keeps the
 * picture to one frame at a time on this side too.
 */

import { getTransport, type OpenSocket } from "../net/transport.ts";
import { session } from "../state/session.ts";
import { createStore, type Store } from "../state/store.ts";
import { FramePacer } from "./framePacer.ts";
import { closedForGood, type LivePicker, type LivePointer, type LiveSelect, type LiveState } from "./model.ts";
import { viewSizeChanged, type ViewSize } from "./viewSize.ts";

export interface LiveView {
  phase: "idle" | "connecting" | "open" | "closed" | "failed";
  /** An object URL of the latest frame. */
  frame: string | null;
  state: LiveState | null;
  pointer: LivePointer | null;
  select: LiveSelect | null;
  /** A date, time or colour field a tap landed on, for the phone's own picker. */
  picker: LivePicker | null;
  /** The runtime's last complaint ("Take control first..."), shown briefly. */
  notice: string | null;
  /** Text the page put on the clipboard while the person had control. */
  clipboard: string | null;
}

const EMPTY: LiveView = { phase: "idle", frame: null, state: null, pointer: null, select: null, picker: null, notice: null, clipboard: null };
const RETRIES = 4;
const LINGER_MS = 1500;

export class LiveBrowser {
  readonly store: Store<LiveView>;
  private socket: OpenSocket | null = null;
  private users = 0;
  private frames: string[] = [];
  private retries = 0;
  private lingerTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private paused = false;
  private generation = 0;
  private pacer = new FramePacer<Uint8Array>((bytes) => this.show(bytes));
  /** Frames received and not yet answered with `shown`. */
  private unacked = 0;
  private viewSize: ViewSize | null = null;

  constructor(readonly agent: string) {
    this.store = createStore<LiveView>(EMPTY);
  }

  /** The picture has drawn the current frame (its `onLoad`): the next may come. */
  frameShown(): void {
    this.pacer.shown();
    if (this.unacked > 0) {
      this.unacked = 0;
      this.send({ t: "shown" });
    }
  }

  /** How big the picture is drawn, in device pixels: frames need be no larger. */
  setViewSize(size: ViewSize | null): void {
    if (!viewSizeChanged(this.viewSize, size)) return;
    this.viewSize = size;
    if (this.socket && this.view.phase === "open") this.announce();
  }

  /** Tells the deployment this viewer paces the stream, and its picture's size. */
  private announce(): void {
    this.send({ t: "view", ack: true, ...(this.viewSize ?? {}) });
  }

  get view(): LiveView {
    return this.store.get();
  }

  /** Something starts showing the browser. */
  acquire(): void {
    this.users += 1;
    if (this.lingerTimer) {
      clearTimeout(this.lingerTimer);
      this.lingerTimer = null;
    }
    if (!this.socket && !this.paused) this.connect();
  }

  /** It stops; the socket closes shortly after the last one (a sheet opening over the window should not reconnect). */
  release(): void {
    this.users = Math.max(0, this.users - 1);
    if (this.users > 0 || this.lingerTimer) return;
    this.lingerTimer = setTimeout(() => {
      this.lingerTimer = null;
      if (this.users === 0) this.disconnect();
    }, LINGER_MS);
  }

  /** The browser may have (re)opened: connect again if something is showing it. */
  retry(): void {
    this.retries = 0;
    if (this.users > 0 && !this.socket && !this.paused) this.connect();
  }

  send(message: Record<string, unknown>): void {
    this.socket?.send(JSON.stringify(message));
  }

  takeControl(): void {
    this.send({ t: "control", who: "user" });
  }

  handBack(): void {
    this.send({ t: "control", who: "agent" });
  }

  clearNotice(): void {
    this.store.set((s) => ({ ...s, notice: null }));
  }

  clearSelect(): void {
    this.store.set((s) => ({ ...s, select: null }));
  }

  clearPicker(): void {
    this.store.set((s) => ({ ...s, picker: null }));
  }

  clearClipboard(): void {
    this.store.set((s) => ({ ...s, clipboard: null }));
  }

  /** The app went to the background. */
  pause(): void {
    this.paused = true;
    this.disconnect(false);
  }

  resume(): void {
    this.paused = false;
    this.retries = 0;
    if (this.users > 0) this.connect();
  }

  dispose(): void {
    this.users = 0;
    this.disconnect();
  }

  private connect(): void {
    const api = session.get().api;
    if (!api || this.socket) return;
    const generation = ++this.generation;
    this.store.set((s) => ({ ...s, phase: "connecting" }));
    this.socket = getTransport().socket(api.url(`/api/agents/${encodeURIComponent(this.agent)}/browser/live`), {
      onOpen: () => {
        if (generation !== this.generation) return;
        this.retries = 0;
        this.unacked = 0;
        this.store.set((s) => ({ ...s, phase: "open" }));
        this.announce();
      },
      onBinary: (bytes) => {
        if (generation === this.generation) this.frame(bytes);
      },
      onText: (text) => {
        if (generation === this.generation) this.message(text);
      },
      onClose: (end) => {
        if (generation !== this.generation) return;
        this.socket = null;
        if (closedForGood(end.code, end.reason) || this.paused || this.users === 0) {
          this.store.set((s) => ({ ...s, phase: this.users === 0 || this.paused ? "idle" : "closed" }));
          return;
        }
        if (this.retries >= RETRIES) {
          this.store.set((s) => ({ ...s, phase: "failed" }));
          return;
        }
        this.retries += 1;
        this.store.set((s) => ({ ...s, phase: "connecting" }));
        this.retryTimer = setTimeout(() => {
          this.retryTimer = null;
          if (this.users > 0 && !this.paused) this.connect();
        }, 600 * 2 ** this.retries);
      },
    });
  }

  private disconnect(clear = true): void {
    this.generation += 1;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.socket?.close();
    this.socket = null;
    this.pacer.reset();
    this.unacked = 0;
    if (clear) {
      for (const url of this.frames) URL.revokeObjectURL(url);
      this.frames = [];
      this.store.set(EMPTY);
    } else {
      this.store.set((s) => ({ ...s, phase: "idle" }));
    }
  }

  private frame(bytes: Uint8Array): void {
    this.unacked += 1;
    this.pacer.offer(bytes);
  }

  private show(bytes: Uint8Array): void {
    const url = URL.createObjectURL(new Blob([bytes as Uint8Array<ArrayBuffer>], { type: "image/jpeg" }));
    this.frames.push(url);
    // The picture on screen may still be decoding the previous frame: keep it one more round.
    while (this.frames.length > 3) URL.revokeObjectURL(this.frames.shift()!);
    this.store.set((s) => ({ ...s, frame: url }));
  }

  private message(text: string): void {
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(text) as Record<string, unknown>;
    } catch {
      return;
    }
    switch (data.type) {
      case "state":
        this.store.set((s) => ({ ...s, state: (data.session as LiveState) ?? s.state }));
        break;
      case "pointer":
        this.store.set((s) => ({ ...s, pointer: data as unknown as LivePointer }));
        break;
      case "clipboard":
        this.store.set((s) => ({ ...s, clipboard: typeof data.text === "string" ? data.text : null }));
        break;
      case "select":
        this.store.set((s) => ({ ...s, select: data as unknown as LiveSelect }));
        break;
      case "picker":
        this.store.set((s) => ({ ...s, picker: data as unknown as LivePicker }));
        break;
      case "notice":
      case "error":
        this.store.set((s) => ({ ...s, notice: typeof data.message === "string" ? data.message : "The browser did not take that." }));
        break;
      case "closed":
        this.store.set((s) => ({ ...s, phase: "closed" }));
        break;
      default:
        break;
    }
  }
}

const browsers = new Map<string, LiveBrowser>();

export function liveBrowserFor(agent: string): LiveBrowser {
  let browser = browsers.get(agent);
  if (!browser) {
    browser = new LiveBrowser(agent);
    browsers.set(agent, browser);
  }
  return browser;
}

export function pauseLiveBrowsers(): void {
  for (const browser of browsers.values()) browser.pause();
}

export function resumeLiveBrowsers(): void {
  for (const browser of browsers.values()) browser.resume();
}

export function closeAllLiveBrowsers(): void {
  for (const browser of browsers.values()) browser.dispose();
  browsers.clear();
}
