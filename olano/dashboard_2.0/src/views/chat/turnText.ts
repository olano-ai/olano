// Growing a streamed reply: text spans and the media refs that ride with it.
//
// Lifted out of `sessionStore` when the Groups view needed the same behaviour.
// Three producers now build a turn's segments — the runner (Python), the group
// route (Python) and this file — and they have to agree or the same reply
// renders one way in the 1:1 chat and another in a group. Keeping ONE copy on
// the client side is the half that can be enforced by the type system; the two
// Python copies are held together by a shared test (segments must join back to
// the body) and by comments pointing at each other.
//
// Everything here operates on a structural type, not on `Msg`, so a group's
// live turn and a chat message can both be passed in.

import type { MediaRef } from "../../core/types";

/** One span of a reply, tagged with the tool call it resumed behind. */
export type TurnSegment = { text: string; after: string | null };

/** A subagent (or peer) transcript produced inside a turn. */
export interface Sub {
  path: string;
  text: string;
}

// A tool invocation, accumulated by id so the backend's streamed partial frames
// (task / peer tools emit several as args fill in) collapse into one entry and
// pick up their result when it arrives — instead of stacking empty fragments.
export interface ToolCall {
  id: string;
  name: string;
  args: string;
  result?: string;
  hasResult: boolean;
}

/** The parts of a message these helpers touch. */
export interface GrowingTurn {
  body: string;
  segments?: TurnSegment[];
  /** Set by a `tool_call` frame; the next text chunk opens a span behind it. */
  pendingAfter?: string;
  media?: MediaRef[];
}

// Grow a live reply by one text chunk, keeping `body` and `segments` in step
// (every `body +=` on a streaming assistant message goes through here). A
// chunk arriving after a tool_call frame opens a new span — even a bare
// "\n\n", which IS the separator the runner yields when text resumes after a
// tool — anything else extends the current one.
export function appendText(m: GrowingTurn, text: string): void {
  m.body += text;
  const segs = (m.segments ||= []);
  if (m.pendingAfter !== undefined) {
    segs.push({ text, after: m.pendingAfter });
    m.pendingAfter = undefined;
  } else if (segs.length === 0) {
    segs.push({ text, after: null });
  } else {
    segs[segs.length - 1].text += text;
  }
}

// Fold two refs for the same url into one: a field the newer ref carries wins,
// one it lacks keeps the old value (a tool_result ref has no `mentions`; the
// turn-end `media` frame for the same file does), and mentions union.
export function mergeRef(prev: MediaRef, next: MediaRef): MediaRef {
  const merged: MediaRef = {
    kind: next.kind ?? prev.kind,
    name: next.name || prev.name,
    path: next.path || prev.path,
    mime: next.mime || prev.mime,
    url: prev.url,
    size: next.size ?? prev.size,
    files_url: next.files_url ?? prev.files_url,
    download_url: next.download_url ?? prev.download_url,
  };
  if (prev.mentions?.length || next.mentions?.length) {
    merged.mentions = [...new Set([...(prev.mentions ?? []), ...(next.mentions ?? [])])];
  }
  return merged;
}

// Merge media refs into a message, deduped by url so a reload (or a repeated
// frame) never stacks the same screenshot twice. Always assigns a NEW array
// (and a new object for a ref that gained fields): renderers memoize on the
// array's identity, so an in-place push would leave them showing the old
// list — or, for a ref that just learned its `mentions`, drawing no links.
export function mergeMedia(m: GrowingTurn, refs: MediaRef[] | undefined): void {
  if (!refs?.length) return;
  const list = m.media ? [...m.media] : [];
  const at = new Map<string, number>();
  list.forEach((r, i) => at.set(r.url, i));
  for (const r of refs) {
    if (!r?.url) continue;
    const i = at.get(r.url);
    if (i === undefined) {
      at.set(r.url, list.length);
      list.push(r);
    } else {
      list[i] = mergeRef(list[i], r);
    }
  }
  m.media = list;
}

export function upsertToolCall(tools: ToolCall[], id: string, name: string, args: string): void {
  let entry: ToolCall | undefined;
  if (id) entry = tools.find((c) => c.id === id);
  if (!entry && !id && name) {
    const last = tools[tools.length - 1];
    if (last && !last.hasResult && last.name === name) entry = last;
  }
  if (!entry) {
    entry = { id: id || `t${tools.length}`, name: "", args: "", hasResult: false };
    tools.push(entry);
  }
  if (name) entry.name = name;
  if (args) entry.args = args;
}

export function applyToolResult(tools: ToolCall[], id: string, name: string, content: string): void {
  let entry: ToolCall | undefined;
  if (id) entry = tools.find((c) => c.id === id);
  if (!entry) {
    const last = tools[tools.length - 1];
    if (last && !last.hasResult && (!name || last.name === name)) entry = last;
  }
  if (!entry) {
    entry = { id: id || `t${tools.length}`, name: name || "", args: "", hasResult: false };
    tools.push(entry);
  }
  entry.result = content;
  entry.hasResult = true;
  if (name && !entry.name) entry.name = name;
}

// What a subagent/peer panel is labelled with. Peer text rerouted by the runner
// carries the peer agent names, so concurrent conversations stay attributable;
// a real subagent carries its graph namespace instead. Shared with the group
// transcript, which renders the same panels.
export function subagentPath(agents?: string[], namespace?: string[]): string {
  if (agents?.length) return `peer: ${agents.join(", ")}`;
  return (namespace || []).join(" / ") || "subagent";
}
