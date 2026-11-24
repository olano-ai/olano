// Unit tests for a group member's live turn. Run with `npm run test:unit`.
//
// Three producers build a turn's segments — the runner (Python), the group
// route (Python) and this fold — and they must agree, or the same reply renders
// one way in the 1:1 chat and another in a group. What they have to agree on is
// the join-back invariant: `buildTurnBlocks` falls back to the old stacked
// layout when the spans do not reproduce the body, silently, so a fold that
// drifts disables interleaving product-wide instead of failing.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { MediaRef } from "../../core/types";
import { buildTurnBlocks } from "../chat/buildTurnBlocks.ts";
import { applyLiveFrame, liveTurn, type GroupStreamFrame } from "./liveTurn.ts";

function feed(frames: GroupStreamFrame[]) {
  const live = liveTurn("maria");
  const published: boolean[] = [];
  for (const f of frames) published.push(applyLiveFrame(live, f));
  return { live, published };
}

// The shape the view ends up drawing.
function shape(frames: GroupStreamFrame[]): string[] {
  const { live } = feed(frames);
  return buildTurnBlocks(live.tools, live.segments, live.body).map((b) =>
    b.kind === "text" ? `T:${b.text}` : `tools:${b.tools.map((t) => t.id).join(",")}`,
  );
}

describe("applyLiveFrame", () => {
  it("grows the reply and keeps the spans joinable", () => {
    const { live } = feed([
      { type: "text", text: "Looking" },
      { type: "text", text: " it up." },
    ]);
    assert.equal(live.body, "Looking it up.");
    assert.equal((live.segments ?? []).map((s) => s.text).join(""), live.body);
  });

  it("text → tool → text draws in the order it happened", () => {
    assert.deepEqual(
      shape([
        { type: "text", text: "Looking it up." },
        { type: "tool_call", id: "c1", name: "read_file", args: "{}" },
        { type: "tool_result", id: "c1", name: "read_file", content: "ok" },
        // The separator the runner yields when text resumes after a tool.
        { type: "text", text: "\n\n" },
        { type: "text", text: "Found it." },
      ]),
      ["T:Looking it up.", "tools:c1", "T:\n\nFound it."],
    );
  });

  it("a turn that only ran tools is a tools block and nothing else", () => {
    assert.deepEqual(
      shape([
        { type: "tool_call", id: "c1", name: "ls", args: "{}" },
        { type: "tool_result", id: "c1", name: "ls", content: "ok" },
      ]),
      ["tools:c1"],
    );
  });

  it("a turn with no tools is one block — the pre-segment layout", () => {
    const { live } = feed([{ type: "text", text: "Hi." }]);
    assert.equal(live.segments?.length, 1);
    assert.deepEqual(shape([{ type: "text", text: "Hi." }]), ["T:Hi."]);
  });

  it("tool cards never outnumber the spans that name them", () => {
    // Two calls between the same two paragraphs stay one block, in call order.
    assert.deepEqual(
      shape([
        { type: "text", text: "One." },
        { type: "tool_call", id: "c1", name: "ls", args: "{}" },
        { type: "tool_call", id: "c2", name: "read_file", args: "{}" },
        { type: "text", text: "\n\nTwo." },
      ]),
      ["T:One.", "tools:c1,c2", "T:\n\nTwo."],
    );
  });

  it("files from a tool_result land on the turn", () => {
    const { live } = feed([
      {
        type: "tool_result",
        id: "c1",
        name: "render_html_image",
        content: "ok",
        media: [{ kind: "image", name: "a.png", path: "reports/a.png", mime: "image/png", url: "/u/1" }],
      },
    ]);
    assert.deepEqual(live.media?.map((m) => m.url), ["/u/1"]);
  });

  it("the turn-end media frame merges into the same file, it does not stack it", () => {
    const ref: MediaRef = {
      kind: "image",
      name: "a.png",
      path: "reports/a.png",
      mime: "image/png",
      url: "/u/1",
    };
    const { live } = feed([
      { type: "tool_result", id: "c1", name: "render_html_image", content: "ok", media: [ref] },
      { type: "media", items: [{ ...ref, mentions: ["reports/a.png"] }] },
    ]);
    assert.equal(live.media?.length, 1);
    // The mentions arrive only on the turn-end frame; they are what turn the
    // path in the prose into a link.
    assert.deepEqual(live.media?.[0].mentions, ["reports/a.png"]);
  });

  it("subagent transcripts accumulate per path", () => {
    const { live } = feed([
      { type: "subagent_text", agents: ["researcher"], text: "one " },
      { type: "subagent_text", agents: ["researcher"], text: "two" },
    ]);
    assert.equal(live.subs.length, 1);
    assert.equal(live.subs[0].text, "one two");
  });

  it("publishes only on a frame that changed the bubble", () => {
    const { published } = feed([
      { type: "text", text: "" },
      { type: "start", addressee: "maria" },
      { type: "message" },
      { type: "done" },
      { type: "text", text: "hi" },
    ]);
    assert.deepEqual(published, [false, false, false, false, true]);
  });
});
