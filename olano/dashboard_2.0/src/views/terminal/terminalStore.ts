// App-level terminal session store (module scope), mirroring chatStore's reason
// for existing: React Router unmounts the Terminal view when you navigate away,
// which would dispose the xterm instances and close their WebSockets — killing
// the live shells. By owning each session's Terminal + WebSocket + a DETACHED
// host <div> here (outside React's tree), the shells keep running across nav;
// the view just re-attaches the active session's host when it mounts.
//
// Sessions are PER AGENT. A shell is opened with `?agent=` and lands in that
// agent's workspace (open posture) or inside that agent's sandbox container
// (managed posture), so a tab can never be re-pointed at another agent and one
// strip cannot honestly show every agent's tabs at once. The store therefore
// keeps one tab set and one active tab per agent; the view names the agent it
// is showing (`setAgent`) and sees only that agent's tabs. Other agents'
// shells stay alive, hidden, until the person switches back.

import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { LineTracker } from "./completion";
import { countsByAgent, nextActiveAfterKill, tabsFor } from "./sessionIndex";

// `unavailable` = the server refused this agent's shell because its sandbox is
// not running (close code 4503). Unlike `closed` it is not retried on a timer:
// reconnecting cannot start a container, only the person can (by starting the
// agent), so the tab waits for an explicit Reconnect.
export type TermStatus = "connecting" | "open" | "reconnecting" | "closed" | "disabled" | "unavailable";

/** What a Copy actually put on the clipboard (the UI says which). */
export type CopyResult = "selection" | "screen" | "empty" | "failed";

interface Session {
  id: string;
  title: string;
  agent: string;
  term: Terminal;
  fit: FitAddon;
  host: HTMLDivElement; // persistent, reparented into the view on attach
  opened: boolean;
  ws: WebSocket | null;
  status: TermStatus;
  attempts: number;
  timer: number | null;
  lastRows: number;
  lastCols: number;
  disposed: boolean;
  /** Local replay of the shell's current input line — see completion.ts. */
  input: LineTracker;
  /** Raw text of a paste in flight, so the line model sees it unwrapped. */
  pasteRaw: string | null;
  /** The server's close reason on a 4503 — why THIS agent has no live sandbox
   *  (a failed container start reads differently from "never booted"). */
  unavailableReason: string;
}

// Public, render-safe view of a session (no live objects).
export interface TabInfo {
  id: string;
  title: string;
  status: TermStatus;
  agent: string;
}

const MAX_RECONNECT = 5;
const WS_DISABLED_CODE = 4403;
const WS_UNAVAILABLE_CODE = 4503;

const sessions: Session[] = [];
// The agent whose tabs the view is showing, and each agent's active tab. An
// agent with tabs but no entry here has none active (all closed by hand); an
// agent with no tabs at all is a legal state — the view renders "Open
// terminal" rather than the store respawning a shell nobody asked for.
let viewAgent = "";
const activeByAgent = new Map<string, string>();
let seq = 0;
let version = 0;
const listeners = new Set<() => void>();

// Typing state (current line + suggestions) rides a SEPARATE channel from
// tabs/status. The view's attach effect keys on the `version` above and
// re-parents + refits the terminal when it fires, so bumping that on every
// keystroke would run a fit + focus per character. Two channels, one cheap.
let inputVersion = 0;
const inputListeners = new Set<() => void>();

function bump(): void {
  version += 1;
  for (const l of listeners) l();
}

function bumpInput(): void {
  inputVersion += 1;
  for (const l of inputListeners) l();
}

function activeIdFor(agent: string): string {
  return activeByAgent.get(agent) ?? "";
}

function activeFor(agent: string): Session | undefined {
  const id = activeIdFor(agent);
  return id ? sessions.find((s) => s.id === id) : undefined;
}

/** The active session of the agent on screen. */
function active(): Session | undefined {
  return activeFor(viewAgent);
}

function wsUrl(agent: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const q = agent ? `?agent=${encodeURIComponent(agent)}` : "";
  return `${proto}//${location.host}/api/terminal/ws${q}`;
}

// Fixed dark palette (terminals stay dark in both app themes for ANSI contrast).
function xtermOptions() {
  const css = getComputedStyle(document.documentElement);
  const mono = css.getPropertyValue("--font-mono").trim();
  return {
    fontFamily: mono || "Menlo, Monaco, 'Courier New', monospace",
    fontSize: 13,
    cursorBlink: true,
    scrollback: 5000,
    allowProposedApi: true,
    theme: {
      background: "#161618",
      foreground: "#dfdfd6",
      cursor: "#ff6a3d",
      cursorAccent: "#161618",
      selectionBackground: "rgba(255, 106, 61, 0.30)",
      black: "#161618",
      red: "#ff7b72",
      green: "#3ddc97",
      yellow: "#e2b341",
      blue: "#60a5fa",
      magenta: "#d2a8ff",
      cyan: "#56d4dd",
      white: "#dfdfd6",
      brightBlack: "#6a6a71",
      brightRed: "#ffa198",
      brightGreen: "#7ee787",
      brightYellow: "#ffd479",
      brightBlue: "#79c0ff",
      brightMagenta: "#d2a8ff",
      brightCyan: "#a5d6ff",
      brightWhite: "#ffffff",
    },
  } as const;
}

function setStatus(s: Session, status: TermStatus): void {
  if (s.status !== status) {
    s.status = status;
    bump();
  }
}

function sendResize(s: Session, force = false): void {
  if (!s.ws || s.ws.readyState !== WebSocket.OPEN) return;
  if (!force && s.term.rows === s.lastRows && s.term.cols === s.lastCols) return;
  s.lastRows = s.term.rows;
  s.lastCols = s.term.cols;
  s.ws.send(JSON.stringify({ type: "resize", rows: s.term.rows, cols: s.term.cols }));
}

function connect(s: Session): void {
  if (s.disposed) return;
  setStatus(s, s.attempts > 0 ? "reconnecting" : "connecting");
  const ws = new WebSocket(wsUrl(s.agent));
  ws.binaryType = "arraybuffer";
  s.ws = ws;
  ws.onopen = () => {
    s.attempts = 0;
    // A fresh PTY means a fresh prompt: whatever half-typed line we were
    // modelling died with the old shell.
    s.input.reset();
    bumpInput();
    setStatus(s, "open");
    requestAnimationFrame(() => {
      try {
        s.fit.fit();
      } catch {
        /* host not visible yet */
      }
      sendResize(s, true);
    });
  };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") s.term.write(e.data);
    else s.term.write(new Uint8Array(e.data as ArrayBuffer));
  };
  ws.onclose = (ev) => {
    if (s.disposed) return;
    if (ev.code === WS_DISABLED_CODE) {
      setStatus(s, "disabled");
      return;
    }
    if (ev.code === WS_UNAVAILABLE_CODE) {
      // The agent's sandbox is not running. No timer: nothing on this side can
      // change that, and five silent retries into the same missing container
      // is exactly the loop this code exists to end. Reconnect is manual.
      s.unavailableReason = (ev.reason || "").trim();
      setStatus(s, "unavailable");
      return;
    }
    if (ev.code !== 1000 && s.attempts < MAX_RECONNECT) {
      s.attempts += 1;
      const delay = Math.min(500 * 2 ** (s.attempts - 1), 4000);
      setStatus(s, "reconnecting");
      s.term.write(`\r\n\x1b[2m[reconnecting in ${Math.round(delay / 1000)}s…]\x1b[0m\r\n`);
      s.timer = window.setTimeout(() => connect(s), delay);
    } else {
      setStatus(s, "closed");
    }
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch {
      /* already closing */
    }
  };
}

function createSession(agent: string): Session {
  seq += 1;
  const host = document.createElement("div");
  host.className = "term-host";
  host.tabIndex = -1;
  const term = new Terminal(xtermOptions());
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon());
  const focusTerm = () => term.focus();
  // Vim often enables xterm mouse reporting. Focus before xterm handles those
  // mouse events, otherwise clicks inside Vim can leave the hidden textarea
  // unfocused and subsequent keys look "frozen".
  host.addEventListener("pointerdown", focusTerm, { capture: true });
  host.addEventListener("mousedown", focusTerm, { capture: true });

  // WORKAROUND (xterm.js 6.0.0): the built-in DECRQM "request mode" handler
  // (CSI $ p and CSI ? $ p) throws `ReferenceError: i is not defined`, which
  // kills the async parse/write loop. vim/htop/tmux issue DECRQM at startup
  // (nano doesn't), so those full-screen apps render once then freeze. We
  // register no-op handlers for both DECRQM forms; returning true consumes the
  // sequence so xterm's crashing built-in handler never runs. Apps fall back to
  // mode defaults when they get no DECRQM reply (vim handles this fine). Remove
  // once we upgrade to an xterm.js release that fixes requestMode.
  const swallowDecrqm = () => true;
  term.parser.registerCsiHandler({ intermediates: "$", final: "p" }, swallowDecrqm);
  term.parser.registerCsiHandler({ prefix: "?", intermediates: "$", final: "p" }, swallowDecrqm);

  const s: Session = {
    id: `t${seq}`,
    title: `Terminal ${seq}`,
    agent,
    term,
    fit,
    host,
    opened: false,
    ws: null,
    status: "connecting",
    attempts: 0,
    timer: null,
    lastRows: 0,
    lastCols: 0,
    disposed: false,
    unavailableReason: "",
    input: new LineTracker(),
    pasteRaw: null,
  };

  // NB: deliberately NO WebGL/canvas renderer here. Those renderers cannot
  // survive their DOM element being moved, and this store's whole design is to
  // reparent each session's host <div> in and out of the view (attachActive →
  // container.replaceChildren) on every tab switch and route change. With an
  // accelerated renderer attached, the model keeps updating but the canvas
  // stops repainting after a reparent — most visibly inside full-screen,
  // alternate-screen apps (vim, less, htop, tmux), which look frozen even
  // though keystrokes still reach the PTY. xterm's default DOM renderer is the
  // only one that supports being moved, so we rely on it.
  term.onData((d) => {
    if (s.pasteRaw !== null) {
      // A paste we initiated: `d` may be wrapped in the bracketed-paste
      // markers, so the line model is fed the raw text instead of the wrapper.
      const raw = s.pasteRaw;
      s.pasteRaw = null;
      sendData(s, d, raw);
      return;
    }
    // `→` at the end of a line accepts the suggestion instead of reaching the
    // shell, fish-style. Safe to steal: with the cursor already at the end of
    // the input (which is what a valid line model means), a right-arrow moves
    // nothing. Both encodings — normal and application-cursor-keys mode, which
    // readline turns on — because the shell decides which one xterm emits.
    const hints = sessionSuggestions(s);
    if ((d === "\x1b[C" || d === "\x1bOC") && hints.length > 0) {
      acceptSuggestion(s, hints[0]);
      return;
    }
    if (d === "\x1b" && hints.length > 0) {
      // Escape dismisses the strip. Still forwarded: it means something to the
      // shell (vi mode, an app about to start), and dismissing is a view
      // concern, not a reason to eat a key.
      s.input.dismissed = true;
      bumpInput();
    }
    sendData(s, d);
  });

  sessions.push(s);
  return s;
}

const enc = new TextEncoder();

/**
 * Suggestions for one session, or none when suggesting would be wrong.
 *
 * The alternate-buffer check is the load-bearing one and belongs HERE, not just
 * in the view: a full-screen app (vim, less, htop, tmux) owns the screen and
 * every keystroke means something to IT. Typing `git status` into a file in vim
 * would otherwise both light up the strip and make `→` — a cursor move in every
 * editor there is — insert text instead of moving.
 */
function sessionSuggestions(s: Session): string[] {
  if (s.status !== "open" || s.term.buffer.active.type === "alternate") return [];
  return s.input.suggestions();
}

/**
 * Write user input to the PTY and keep the local line model in step.
 *
 * `modelAs` replaces what the model is fed when the bytes on the wire aren't
 * what the shell will insert — i.e. a bracketed paste.
 */
function sendData(s: Session, data: string, modelAs?: string): void {
  if (modelAs === undefined) s.input.feed(data);
  else s.input.feedText(modelAs);
  bumpInput();
  if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send(enc.encode(data));
}

/** Type the rest of `text` (its shared prefix is already on the shell's line). */
function acceptSuggestion(s: Session, text: string): void {
  if (!s.input.valid || !text.startsWith(s.input.line)) return;
  const delta = text.slice(s.input.line.length);
  if (!delta) return;
  sendData(s, delta);
  s.term.focus();
}

/** The active session's visible screen, minus trailing blank lines. */
function viewportText(s: Session): string {
  const buf = s.term.buffer.active;
  const lines: string[] = [];
  for (let y = 0; y < s.term.rows; y++) {
    lines.push(buf.getLine(buf.viewportY + y)?.translateToString(true) ?? "");
  }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return lines.join("\n");
}

/** Clipboard write with a fallback for browsers/contexts without the API. */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Insecure context or a denied permission — fall back to the old
    // selection-and-copy trick, which needs no permission at all.
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

function disposeSession(s: Session): void {
  s.disposed = true;
  if (s.timer) window.clearTimeout(s.timer);
  try {
    s.ws?.close(1000);
  } catch {
    /* noop */
  }
  try {
    s.term.dispose();
  } catch {
    /* noop */
  }
  s.host.remove();
}

export const terminalStore = {
  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
  getSnapshot(): number {
    return version;
  },

  // Typing state, on its own channel (see bumpInput).
  subscribeInput(l: () => void): () => void {
    inputListeners.add(l);
    return () => inputListeners.delete(l);
  },
  getInputSnapshot(): number {
    return inputVersion;
  },

  /** Name the agent the view is showing. Its first tab becomes active when it
   *  has tabs but no active one (a tab set built while another agent was on
   *  screen). Call before ensureOne/attachActive for the same agent. */
  setAgent(agent: string): void {
    if (viewAgent === agent) return;
    viewAgent = agent;
    if (!activeFor(agent)) {
      const first = tabsFor(sessions, agent)[0];
      if (first) activeByAgent.set(agent, first.id);
      else activeByAgent.delete(agent);
    }
    bump();
  },

  /** The tabs of `agent` (default: the agent on screen), in strip order. */
  tabs(agent: string = viewAgent): TabInfo[] {
    return tabsFor(sessions, agent).map((s) => ({ id: s.id, title: s.title, status: s.status, agent: s.agent }));
  },
  activeId(agent: string = viewAgent): string {
    return activeIdFor(agent);
  },
  activeStatus(agent: string = viewAgent): TermStatus | null {
    return activeFor(agent)?.status ?? null;
  },
  /** The server's reason for the active tab's `unavailable` status ("" when
   *  it sent the generic one or the tab is in another state). */
  activeUnavailableReason(agent: string = viewAgent): string {
    const s = activeFor(agent);
    if (!s || s.status !== "unavailable") return "";
    const r = s.unavailableReason;
    return r && r !== "sandbox not running" ? r : "";
  },
  /** Open tabs per agent — every tab, whatever its status. The picker shows
   *  these so it is visible where shells are open without switching to look. */
  tabCounts(): Record<string, number> {
    return countsByAgent(sessions);
  },

  /** Command suggestions for what is currently typed in the active session. */
  suggestions(): string[] {
    const s = active();
    return s ? sessionSuggestions(s) : [];
  },

  /** Type the rest of a suggestion into the active session. */
  accept(text: string): void {
    const s = active();
    if (s) acceptSuggestion(s, text);
  },

  /**
   * Where the caret's line sits inside the terminal body, in px from its top.
   *
   * The suggestion strip is drawn from this rather than pinned to the bottom
   * of the pane: a shell prompt is usually ON the last row, so a
   * bottom-anchored strip would cover the very line the user is typing.
   */
  caretMetrics(): { lineBottom: number; rowHeight: number } | null {
    const s = active();
    if (!s || !s.opened) return null;
    const screen = s.host.querySelector<HTMLElement>(".xterm-screen");
    if (!screen || !s.term.rows) return null;
    const rowHeight = screen.clientHeight / s.term.rows;
    if (!rowHeight || !Number.isFinite(rowHeight)) return null;
    // The host is inset:0 inside the body, so its top IS the body's top; the
    // screen sits below the host's padding, which this offset picks up.
    const offset = screen.getBoundingClientRect().top - s.host.getBoundingClientRect().top;
    return {
      lineBottom: offset + (s.term.buffer.active.cursorY + 1) * rowHeight,
      rowHeight,
    };
  },

  /** Send a key/escape sequence as if the user had typed it (mobile key bar). */
  sendKey(data: string): void {
    const s = active();
    if (!s) return;
    sendData(s, data);
    s.term.focus();
  },

  /**
   * Copy the selection, or — when there is none, which is the norm on a phone
   * where selecting text in a terminal is a fight — the visible screen.
   */
  async copy(): Promise<CopyResult> {
    const s = active();
    if (!s) return "empty";
    const selection = s.term.getSelection();
    const kind: CopyResult = selection.trim() ? "selection" : "screen";
    const text = selection.trim() ? selection : viewportText(s);
    if (!text.trim()) return "empty";
    return (await writeClipboard(text)) ? kind : "failed";
  },

  /**
   * Paste into the active session. Pass `text` to supply it directly (the
   * fallback prompt) — otherwise the system clipboard is read, which needs a
   * user gesture and can be refused.
   */
  async paste(text?: string): Promise<boolean> {
    const s = active();
    if (!s) return false;
    let payload = text;
    if (payload === undefined) {
      try {
        payload = await navigator.clipboard.readText();
      } catch {
        return false; // no permission / no API — caller prompts instead
      }
    }
    if (!payload) return true; // empty clipboard: nothing to do, not a failure
    // Through xterm so bracketed-paste mode is honoured when the app asked for
    // it (that is what stops a pasted newline from auto-running in bash 5+).
    // `pasteRaw` tells the onData handler above what was really pasted; the
    // handler consumes it synchronously, and the clear below is belt-and-braces
    // in case an app has paste suppressed.
    s.pasteRaw = payload;
    s.term.paste(payload);
    s.pasteRaw = null;
    s.term.focus();
    return true;
  },

  // Make sure `agent` has at least one terminal (call from an effect, not
  // render). Per agent: arriving on the view, or switching to an agent that has
  // no shell yet, opens one — the first-visit behaviour the view always had,
  // now scoped to the agent on screen.
  ensureOne(agent: string): void {
    if (tabsFor(sessions, agent).length === 0) {
      const s = createSession(agent);
      activeByAgent.set(agent, s.id);
      bump();
    }
  },

  create(agent: string): void {
    const s = createSession(agent);
    activeByAgent.set(agent, s.id);
    bump();
  },

  setActive(id: string): void {
    const s = sessions.find((x) => x.id === id);
    if (s && activeIdFor(s.agent) !== id) {
      activeByAgent.set(s.agent, id);
      bump();
    }
  },

  rename(id: string, title: string): void {
    const s = sessions.find((x) => x.id === id);
    if (s) {
      s.title = title.trim() || s.title;
      bump();
    }
  },

  // Move tab `fromId` to the position of `toId` (drag-to-reorder). Only within
  // one agent's strip — the two are never on screen together.
  reorder(fromId: string, toId: string): void {
    if (fromId === toId) return;
    const from = sessions.findIndex((s) => s.id === fromId);
    const to = sessions.findIndex((s) => s.id === toId);
    if (from === -1 || to === -1 || sessions[from].agent !== sessions[to].agent) return;
    const [moved] = sessions.splice(from, 1);
    sessions.splice(to, 0, moved);
    bump();
  },

  // Close a tab. The neighbour that slides into its place (within the same
  // agent's strip) becomes active; when the agent has no tab left it simply
  // has none — no respawn. Respawning was the old behaviour and it made
  // "close the last tab" a hidden "restart the shell"; with per-agent sets it
  // would also open shells for agents that are not on screen, and under a
  // sandbox posture could fail straight into an error nobody asked for.
  kill(id: string): void {
    const idx = sessions.findIndex((s) => s.id === id);
    if (idx === -1) return;
    const removed = sessions[idx];
    const agent = removed.agent;
    const own = tabsFor(sessions, agent);
    const ownIdx = own.findIndex((s) => s.id === id);
    sessions.splice(idx, 1);
    disposeSession(removed);
    if (activeIdFor(agent) === id) {
      const next = nextActiveAfterKill(own.map((s) => s.id), ownIdx);
      if (next) activeByAgent.set(agent, next);
      else activeByAgent.delete(agent);
    }
    bump();
  },

  // Reparent the active session's host into the view container + fit/focus.
  // Only the active host lives in the DOM; inactive ones stay detached (alive).
  // With no active session for the agent on screen the container is emptied,
  // so a previous agent's shell never lingers under another agent's header.
  attachActive(container: HTMLElement): void {
    const s = active();
    if (!s) {
      container.replaceChildren();
      return;
    }
    if (s.host.parentElement !== container) container.replaceChildren(s.host);
    requestAnimationFrame(() => {
      if (!s.opened) {
        s.term.open(s.host);
        s.opened = true;
      }
      try {
        s.fit.fit();
      } catch {
        /* noop */
      }
      sendResize(s);
      s.host.focus({ preventScroll: true });
      s.term.focus();
      if (!s.ws) connect(s);
    });
  },

  fitActive(): void {
    const s = active();
    if (!s) return;
    try {
      s.fit.fit();
    } catch {
      /* noop */
    }
    sendResize(s);
  },

  reconnectActive(): void {
    const s = active();
    if (!s) return;
    s.attempts = 0;
    if (s.timer) window.clearTimeout(s.timer);
    try {
      s.ws?.close();
    } catch {
      /* noop */
    }
    connect(s);
  },
};
