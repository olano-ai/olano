// Unit tests for growing a streamed reply. Run with `npm run test:unit`.
//
// The invariant these exist for: the spans must join back to the body. When
// they do not, `buildTurnBlocks` silently drops to the stacked layout with no
// error anywhere, so a normalization applied to one and not the other disables
// interleaving product-wide instead of failing.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { MediaRef } from "../../core/types";
import { appendText, mergeMedia, mergeRef, type GrowingTurn } from "./turnText.ts";

function turn(): GrowingTurn {
  return { body: "" };
}

function ref(url: string, extra: Partial<MediaRef> = {}): MediaRef {
  return { kind: "image", name: "a.png", path: "reports/a.png", mime: "image/png", url, ...extra };
}

describe("appendText", () => {
  it("keeps body and segments in step", () => {
    const m = turn();
    appendText(m, "hel");
    appendText(m, "lo");
    assert.equal(m.body, "hello");
    assert.deepEqual(m.segments, [{ text: "hello", after: null }]);
  });

  it("a chunk after a tool_call opens a span tagged with that call", () => {
    const m = turn();
    appendText(m, "looking");
    m.pendingAfter = "call-1";
    appendText(m, "\n\n");
    appendText(m, "found it");
    assert.equal(m.body, "looking\n\nfound it");
    assert.deepEqual(m.segments, [
      { text: "looking", after: null },
      { text: "\n\nfound it", after: "call-1" },
    ]);
  });

  it("the separator alone opens the span — it IS the runner's break", () => {
    // The runner yields "\n\n" as the first chunk after a tool boundary. If a
    // bare separator did not open the span, the break would land one chunk late
    // and the tool card would draw inside the paragraph that follows it.
    const m = turn();
    appendText(m, "a");
    m.pendingAfter = "call-1";
    appendText(m, "\n\n");
    assert.deepEqual(m.segments?.[1], { text: "\n\n", after: "call-1" });
  });

  it("a turn that starts behind a tool tags its first span", () => {
    const m = turn();
    m.pendingAfter = "call-1";
    appendText(m, "here");
    assert.deepEqual(m.segments, [{ text: "here", after: "call-1" }]);
  });

  it("the spans always join back to the body", () => {
    const m = turn();
    const chunks = ["a", "b", "", "c"];
    for (const c of chunks) appendText(m, c);
    m.pendingAfter = "x";
    for (const c of ["\n\n", "d", "e"]) appendText(m, c);
    assert.equal((m.segments ?? []).map((s) => s.text).join(""), m.body);
  });
});

describe("mergeRef", () => {
  it("a field the newer ref lacks keeps the old value", () => {
    const prev = ref("/u/1", { size: 12, files_url: "/files?path=a" });
    const next = ref("/u/1", { name: "renamed.png" });
    const out = mergeRef(prev, next);
    assert.equal(out.name, "renamed.png");
    assert.equal(out.size, 12);
    assert.equal(out.files_url, "/files?path=a");
  });

  it("mentions union", () => {
    const out = mergeRef(ref("/u/1", { mentions: ["a.png"] }), ref("/u/1", { mentions: ["reports/a.png", "a.png"] }));
    assert.deepEqual(out.mentions, ["a.png", "reports/a.png"]);
  });
});

describe("mergeMedia", () => {
  it("dedupes by url so a reload never stacks the same file twice", () => {
    const m = turn();
    mergeMedia(m, [ref("/u/1"), ref("/u/2")]);
    mergeMedia(m, [ref("/u/1", { mentions: ["a.png"] })]);
    assert.equal(m.media?.length, 2);
    assert.deepEqual(m.media?.[0].mentions, ["a.png"]);
  });

  it("always assigns a NEW array — renderers memoize on its identity", () => {
    const m = turn();
    mergeMedia(m, [ref("/u/1")]);
    const first = m.media;
    mergeMedia(m, [ref("/u/1", { mentions: ["a.png"] })]);
    assert.notEqual(m.media, first);
    // …and a new object for the ref that gained a field, or a memoized file
    // index keyed on the ref would keep drawing no links.
    assert.notEqual(m.media?.[0], first?.[0]);
  });

  it("nothing to merge leaves the message alone", () => {
    const m = turn();
    mergeMedia(m, undefined);
    mergeMedia(m, []);
    assert.equal(m.media, undefined);
  });

  it("a ref with no url is skipped rather than indexed under an empty key", () => {
    const m = turn();
    mergeMedia(m, [ref(""), ref("/u/1")]);
    assert.deepEqual(m.media?.map((r) => r.url), ["/u/1"]);
  });
});
