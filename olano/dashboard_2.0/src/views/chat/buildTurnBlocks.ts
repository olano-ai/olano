// The order a reply is drawn in: tool cards and text spans interleaved as the
// turn actually ran. The store keeps the assistant's text as `segments`
// (sessionStore.Msg — each span knows the last tool call that ran before it);
// this folds those against the message's tool list into render blocks. Pure,
// so turnBlocks.test.ts runs it under node:test.

import type { ToolCall, TurnSegment } from "./turnText";

export type TurnBlock = { kind: "text"; text: string } | { kind: "tools"; tools: ToolCall[] };

function stripWs(s: string): string {
  return s.replace(/\s+/g, "");
}

// Segments are a view over `body`; if they no longer add up to it, trust the
// body — losing the interleaving is a layout nit, losing text is not.
function segmentsCover(segments: TurnSegment[], body: string): boolean {
  let joined = "";
  for (const s of segments) joined += s.text;
  return stripWs(joined) === stripWs(body);
}

/**
 * With no segments (a live message before its first chunk, a row from an older
 * runtime): every tool card first, then the whole body — today's layout. With
 * segments: walk them in order; a span whose `after` names a tool emits the
 * tools from the cursor through that tool first (an unknown id flushes every
 * tool still pending — a tool is never dropped), then the span's text.
 * Whitespace-only spans (the `\n\n` separator the runner yields when text
 * resumes after a tool) draw nothing; adjacent text spans with no tool between
 * them join into one block so a paragraph is never split for no reason.
 * Tools left after the last span close the turn as a final tools block.
 */
export function buildTurnBlocks(tools: ToolCall[], segments: TurnSegment[] | undefined, body: string): TurnBlock[] {
  const out: TurnBlock[] = [];
  if (!segments || segments.length === 0 || !segmentsCover(segments, body)) {
    if (tools.length) out.push({ kind: "tools", tools });
    if (body.trim()) out.push({ kind: "text", text: body });
    return out;
  }
  let cursor = 0;
  for (const seg of segments) {
    if (seg.after !== null && cursor < tools.length) {
      let end = tools.length;
      for (let i = cursor; i < tools.length; i++) {
        if (tools[i].id === seg.after) {
          end = i + 1;
          break;
        }
      }
      out.push({ kind: "tools", tools: tools.slice(cursor, end) });
      cursor = end;
    }
    if (!seg.text.trim()) continue;
    const last = out[out.length - 1];
    if (last && last.kind === "text") last.text += seg.text;
    else out.push({ kind: "text", text: seg.text });
  }
  if (cursor < tools.length) out.push({ kind: "tools", tools: tools.slice(cursor) });
  return out;
}
