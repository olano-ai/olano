import type { ChatFrame } from "./types";
import type { WireAttachment } from "../views/chat/attachments";

// The chat stream is POST + SSE, so EventSource (GET-only) can't be used.
// We read the response body as a stream and split on the SSE frame delimiter.
export interface ChatStreamBody {
  message: string;
  agent: string;
  thread_id?: string | null;
  mode?: string;
  model?: string;
  reasoning_effort?: string;
  attachments?: WireAttachment[];
}

/** A refused stream (an HTTP error before any frame). Carries the status so a
 * caller can tell "this endpoint does not exist here" (fall back) from "this
 * request was rejected" (surface it). */
export class StreamRequestError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`stream failed (${status}): ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

/** POST + SSE for any endpoint speaking the `data: {json}` frame protocol. */
export async function streamPost<F>(
  url: string,
  body: unknown,
  onFrame: (frame: F) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const t = await res.text().catch(() => "");
    let detail = t;
    try {
      detail = JSON.parse(t).detail ?? t;
    } catch {
      // plain-text error body
    }
    throw new StreamRequestError(res.status, detail);
  }

  return readSSE<F>(res, onFrame);
}

/** Drain an SSE response body, handing each `data:` frame to `onFrame`.
 * Shared by the POST that starts a turn and the GET that re-attaches to one, so
 * a reconnecting client parses exactly what the original stream parsed. */
export async function readSSE<F>(res: Response, onFrame: (frame: F) => void): Promise<void> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          onFrame(JSON.parse(payload) as F);
        } catch {
          // keep-alive / non-JSON line — ignore
        }
      }
    }
  }
}

export async function streamChat(
  body: ChatStreamBody,
  onFrame: (frame: ChatFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPost<ChatFrame>("/api/chat/stream", body, onFrame, signal);
}

/** Rejoin a turn already running on the server.
 *
 * The recovery half of the detached run (olano/core/runs.py): the turn kept
 * working while this browser was away — a lock screen, an app switch, a network
 * handover — and `cursor` (the last `seq` this client saw) says where to pick
 * up. Frames arrive in the same schema `/api/chat/stream` emits, so the caller
 * folds them through the same reducer as a turn it started itself.
 *
 * A 404 means the run is genuinely gone (finished long enough ago to be swept,
 * or the runtime restarted); the caller falls back to refetching the transcript.
 */
export async function attachChat(
  params: { agent: string; thread_id: string; run_id?: string; cursor?: number },
  onFrame: (frame: ChatFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  const q = new URLSearchParams({
    agent: params.agent,
    thread_id: params.thread_id,
    cursor: String(params.cursor ?? 0),
  });
  if (params.run_id) q.set("run_id", params.run_id);
  const res = await fetch(`/api/chat/attach?${q.toString()}`, {
    method: "GET",
    credentials: "include",
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!res.ok) {
    const t = await res.text().catch(() => "");
    throw new StreamRequestError(res.status, t);
  }
  return readSSE<ChatFrame>(res, onFrame);
}

/** Stop the turn running on a thread.
 *
 * A detached run no longer dies when its socket does, so aborting the local
 * reader is not a stop any more — without this call Stop would silently stop
 * meaning stop. Best-effort: a run that already ended is not an error. */
export async function stopChatRun(params: {
  agent: string;
  thread_id: string;
  run_id?: string | null;
}): Promise<void> {
  try {
    await fetch("/api/chat/stop", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent: params.agent,
        thread_id: params.thread_id,
        run_id: params.run_id || undefined,
      }),
    });
  } catch {
    // The turn stops when the runtime notices the socket anyway; a failed stop
    // must never break the UI that issued it.
  }
}
