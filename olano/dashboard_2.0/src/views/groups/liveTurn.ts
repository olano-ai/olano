// One member's reply as it is being written, and the frames that grow it.
//
// Pure, in its own module for the same reason `turnBlocks.ts` is: the invariant
// worth testing lives here — the spans a turn accumulates must join back to its
// body, or `buildTurnBlocks` silently drops the whole view to the old stacked
// layout with no error anywhere. `groupsStore` keeps the frames that move the
// STORE (a new speaker, a settled message, an error); those need a snapshot and
// belong there.

// Explicit `.ts`: this module is imported by `liveTurn.test.ts` under node's
// own test runner, which does no extensionless TypeScript resolution (the same
// reason threadPick.ts and fileLinks.ts spell theirs out).
import {
  appendText,
  applyToolResult,
  mergeMedia,
  subagentPath,
  upsertToolCall,
  type Sub,
  type ToolCall,
  type TurnSegment,
} from "../chat/turnText.ts";
import type { MediaRef } from "../../core/types";

/**
 * The work behind one member's reply: the tools it called and any subagent /
 * peer transcripts it produced.
 *
 * Kept per MESSAGE rather than folded into the transcript, because the
 * transcript is the server's (`GET /api/groups/{id}/messages` returns bodies
 * and attribution, never tool chrome) and this is the live stream's. It
 * survives for the life of the tab and is gone on reload — exactly like the
 * 1:1 chat, whose reloaded threads also come back as prose.
 */
export interface GroupActivity {
  author: string;
  tools: ToolCall[];
  subs: Sub[];
}

/** A reply being written right now: its author, its text so far, and its work. */
export interface GroupLiveTurn extends GroupActivity {
  /** Named `body` (not `text`) so the shared `appendText` grows it directly. */
  body: string;
  segments?: TurnSegment[];
  /** Set by a tool_call frame; the next chunk opens a span behind that call. */
  pendingAfter?: string;
  media?: MediaRef[];
}

/**
 * One frame of `POST /api/groups/{id}/messages/stream`.
 *
 * Generic in the settled row a `message` frame carries so this module stays
 * free of the transcript's types: the store reads it as `GroupStreamFrame<
 * GroupMessage>`, the fold below never looks at that field.
 */
export interface GroupStreamFrame<M = unknown> {
  type:
    | "start"
    | "speaker"
    | "text"
    | "tool_call"
    | "tool_result"
    | "subagent_text"
    | "message"
    | "media"
    | "cascade"
    | "error"
    | "done";
  /** Who this frame belongs to. Present on every frame after `start`. */
  author?: string;
  addressee?: string;
  addressees?: string[];
  text?: string;
  id?: string;
  name?: string;
  args?: string;
  content?: string;
  namespace?: string[];
  agents?: string[];
  message?: M;
  handed_to?: string;
  detail?: string;
  /** `tool_result`: files that result named, resolved for its author. */
  media?: MediaRef[];
  /** `media`: files the finished reply named. Sent just before `message`, so
      it lands on the bubble still being streamed into. */
  items?: MediaRef[];
}

/** A fresh live turn for one member. */
export function liveTurn(author: string): GroupLiveTurn {
  return { author, body: "", segments: [], tools: [], subs: [] };
}

/**
 * Fold one streamed frame into the member's live turn, in place. Returns
 * whether the bubble changed, so the caller publishes exactly once per visible
 * change (this runs per token). Frames it does not own return false.
 */
export function applyLiveFrame(live: GroupLiveTurn, frame: GroupStreamFrame): boolean {
  switch (frame.type) {
    case "text":
      if (!frame.text) return false;
      // Shared with the 1:1 chat: keeps `body` and `segments` in step, so a
      // chunk arriving after a tool opens its own span.
      appendText(live, frame.text);
      return true;
    case "tool_call":
      // The next text chunk belongs behind this call.
      live.pendingAfter = frame.id || "";
      upsertToolCall(live.tools, frame.id || "", frame.name || "", frame.args || "");
      return true;
    case "tool_result":
      applyToolResult(live.tools, frame.id || "", frame.name || "", frame.content || "");
      mergeMedia(live, frame.media);
      return true;
    case "media":
      // The finished reply's own files, sent just before `message` so they land
      // on the bubble still on screen.
      mergeMedia(live, frame.items);
      return true;
    case "subagent_text": {
      const path = subagentPath(frame.agents, frame.namespace);
      const found = live.subs.find((x) => x.path === path);
      if (found) found.text += frame.text || "";
      else live.subs.push({ path, text: frame.text || "" });
      return true;
    }
    default:
      return false;
  }
}
