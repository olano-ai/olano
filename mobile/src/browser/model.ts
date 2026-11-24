/**
 * The agent's browser, as the app understands it.
 *
 * With the live browser on (Config, Browser: mode live), an agent browses in
 * a real Chromium on the deployment, and a person can watch it and take over:
 * the picture comes over a WebSocket (`/api/agents/{name}/browser/live`) as
 * JPEG frames, the page's state as JSON, and a person in control sends taps,
 * scrolls and typing back as fractions of the picture. The protocol is the
 * runtime's (olano/runtime/routes/browser.py, olano/browser/live.py) and the
 * dashboard's client (views/browser/LiveScreen.tsx, liveInput.ts); this module
 * is its pure half, so `node --test` covers it.
 */

/** `GET /api/agents/{name}/browser` → `session`. */
export interface BrowserSession {
  agent: string;
  kind: "headless" | "live" | "remote" | string;
  opened_at: number;
  last_used?: number;
  controller: "agent" | "user" | string;
  /** Set when the agent asked a person to take over, with its reason. */
  attention: string | null;
  viewers: number;
  url: string;
  title?: string;
}

export interface BrowserStatus {
  session: BrowserSession | null;
  /** The live view can be watched (live mode, local browser). */
  live_view: boolean;
  availability?: { mode?: string; enabled?: boolean; reason?: string };
}

export interface LiveTab {
  id: string;
  url: string;
  title: string;
  active: boolean;
}

export interface LiveDialog {
  id: string;
  kind: "alert" | "confirm" | "prompt" | "beforeunload" | string;
  message: string;
  default?: string;
}

export interface LiveChooser {
  id: string;
  multiple: boolean;
  accept?: string;
}

/** The socket's `state` message: the page as it is now. */
export interface LiveState {
  controller: "agent" | "user" | string;
  attention: string | null;
  url: string;
  kind?: string;
  viewers?: number;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tabs?: LiveTab[];
  dialog?: LiveDialog | null;
  chooser?: LiveChooser | null;
  /** A recording of the browser running (`POST .../browser/recording`), for the red dot. */
  recording?: LiveRecording | null;
}

/** A recording in progress: since when (seconds since the epoch) and the file it will be. */
export interface LiveRecording {
  since: number;
  seconds: number;
  name: string;
}

/** A file the browser saved for the person: a recording or a picture, in the agent's workspace. */
export interface SavedBrowserFile {
  path: string;
  /** Workspace-relative, such as `recordings/browser-20261018-191502.mp4`. */
  relative: string;
  name: string;
  bytes: number;
  seconds: number;
  url: string;
  files_url: string;
}

/** "0:42" for a recording's length. */
export function recordingClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const m = Math.floor(whole / 60);
  const s = whole % 60;
  return `${m}:${s < 10 ? "0" : ""}${s}`;
}

/** What a saved file's toast says: the folder it went to and, for a video, its length. */
export function savedFileLine(file: SavedBrowserFile): string {
  const folder = file.relative.includes("/") ? file.relative.slice(0, file.relative.lastIndexOf("/")) : "";
  const size = file.bytes >= 1024 * 1024 ? `${(file.bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(file.bytes / 1024))} KB`;
  const length = file.seconds > 0 ? `${recordingClock(file.seconds)}, ` : "";
  return `Saved ${file.name} in ${folder || "the workspace"} (${length}${size}).`;
}

/** Where the agent is about to act (a screencast frame has no cursor). */
export interface LivePointer {
  x: number;
  y: number;
  kind: "move" | "click" | "type" | string;
  at: number;
}

export interface LiveSelect {
  label?: string;
  selected?: number;
  options: { label: string; disabled?: boolean; group?: string }[];
}

/** A date, time or colour field a tap landed on: the browser's own picker is never in the picture, so the phone shows its own. */
export interface LivePicker {
  kind: "date" | "time" | "datetime-local" | "month" | "week" | "color" | string;
  value: string;
  min?: string;
  max?: string;
  step?: string;
  label?: string;
}

/** What the phone's own field for a picker should be: the same kind where the phone has one, a text box for the rest. */
export function pickerInputType(kind: string): "date" | "time" | "datetime-local" | "month" | "week" | "text" {
  return kind === "date" || kind === "time" || kind === "datetime-local" || kind === "month" || kind === "week" ? kind : "text";
}

/** A title for the picker's sheet. */
export function pickerTitle(picker: LivePicker): string {
  if (picker.label) return picker.label;
  switch (picker.kind) {
    case "date":
      return "Choose a date";
    case "time":
      return "Choose a time";
    case "datetime-local":
      return "Choose a date and time";
    case "month":
      return "Choose a month";
    case "week":
      return "Choose a week";
    case "color":
      return "Choose a colour";
    default:
      return "Choose a value";
  }
}

/** The size the picture is drawn at: the frame fitted inside the box, never cropped. */
export function containSize(box: { w: number; h: number }, frame: { w: number; h: number }): { w: number; h: number } {
  if (frame.w <= 0 || frame.h <= 0 || box.w <= 0 || box.h <= 0) return { w: 0, h: 0 };
  const scale = Math.min(box.w / frame.w, box.h / frame.h);
  return { w: Math.round(frame.w * scale), h: Math.round(frame.h * scale) };
}

/**
 * A point on screen as fractions of the picture (what the runtime takes), or
 * null outside it. `rect` is the picture's box on screen, zoom included.
 */
export function toFraction(point: { x: number; y: number }, rect: { left: number; top: number; width: number; height: number }): { x: number; y: number } | null {
  if (rect.width <= 0 || rect.height <= 0) return null;
  const x = (point.x - rect.left) / rect.width;
  const y = (point.y - rect.top) / rect.height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x: Math.round(x * 10000) / 10000, y: Math.round(y * 10000) / 10000 };
}

/** A finger's movement on screen as page pixels to scroll (the opposite way: content follows the finger). */
export function scrollDelta(fingerPx: number, pictureWidth: number, frameWidth: number): number {
  if (pictureWidth <= 0 || frameWidth <= 0) return 0;
  return Math.round((-fingerPx * frameWidth) / pictureWidth);
}

/** How long a finger rests before a press becomes a drag, and how far it may wander before it is a scroll. */
export const HOLD_MS = 250;
export const TAP_SLOP_PX = 10;
/** Taps this close together in time and place count as one double (or triple) tap. */
export const MULTI_TAP_MS = 500;
export const MULTI_TAP_PX = 24;

export interface TapMemory {
  at: number;
  x: number;
  y: number;
  count: number;
}

/** The click count for a tap: 2 or 3 when it follows the last one closely, else 1. */
export function tapCount(last: TapMemory | null, at: number, x: number, y: number): number {
  if (!last || at - last.at > MULTI_TAP_MS || Math.hypot(x - last.x, y - last.y) > MULTI_TAP_PX) return 1;
  return Math.min(3, last.count + 1);
}

export type LiveTone = "agent" | "user" | "attention" | "approval";

/** The one line the floating window and the full view show under the picture. */
export function statusLine(controller: string | undefined, attention: string | null | undefined, approvalPending: boolean): { tone: LiveTone; text: string } {
  if (approvalPending) return { tone: "approval", text: "Waiting for your OK in the chat" };
  if (controller === "user") return { tone: "user", text: "You have control" };
  if (attention) return { tone: "attention", text: "The agent needs you" };
  return { tone: "agent", text: "The agent is browsing" };
}

/** Whether a closed socket means "nothing to watch" rather than a fault worth retrying. */
export function closedForGood(code: number, reason: string): boolean {
  // 4404: no open browser; 4403: the live view is off; 1008: no access; 1000: closed normally.
  return code === 4404 || code === 4403 || code === 1008 || code === 1000 || /no open browser|not turned on/i.test(reason);
}

/**
 * The key a dismissed floating window is remembered by: a new browser
 * session, or the agent asking for help again, brings it back.
 */
export function dismissKey(session: Pick<BrowserSession, "opened_at" | "attention"> | null | undefined): string {
  return session ? `${session.opened_at}|${session.attention ?? ""}` : "";
}

/** The site a page is on, for a title: "www.example.com" → "example.com". */
export function siteOf(url: string | undefined): string {
  if (!url) return "";
  try {
    const host = new URL(url).hostname;
    return host.replace(/^www\./, "") || url;
  } catch {
    return url;
  }
}

/** An address typed in the bar: a bare name or domain gets https://, as the runtime does. */
export function addressFor(typed: string): string {
  const value = typed.trim();
  if (!value) return "";
  return /^[a-z][a-z0-9+.-]*:/i.test(value) ? value : `https://${value}`;
}
