// Unit tests for the OS share-sheet hand-off. Run with `npm run test:unit`.
//
// The cases below are the real shapes the Android share sheet produces, which
// is the whole reason composeSharedDraft is not a join: the three fields
// overlap differently per source app, and a naive concatenation shows the same
// URL twice in the composer.

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { composeSharedDraft, stripShareParams, SHARE_PARAMS } from "./shareTarget.ts";

describe("composeSharedDraft", () => {
  it("keeps a plain text selection as-is", () => {
    assert.equal(composeSharedDraft({ text: "remember to renew the cert" }), "remember to renew the cert");
  });

  it("does not repeat a url that Chrome also put in the text", () => {
    // Chrome on Android shares a page as title + text, where text IS the URL.
    const draft = composeSharedDraft({
      title: "Olano docs",
      text: "https://olano.ai/docs",
      url: "https://olano.ai/docs",
    });
    assert.equal(draft, "Olano docs\nhttps://olano.ai/docs");
  });

  it("does not repeat a title the source app already embedded in the text", () => {
    const draft = composeSharedDraft({
      title: "Quarterly plan",
      text: "Quarterly plan — draft two",
    });
    assert.equal(draft, "Quarterly plan — draft two");
  });

  it("appends a url that appears nowhere else", () => {
    const draft = composeSharedDraft({ text: "worth reading", url: "https://example.com/a" });
    assert.equal(draft, "worth reading\nhttps://example.com/a");
  });

  it("returns an empty string when nothing usable arrived", () => {
    // The caller uses "" as the signal to leave the composer alone entirely,
    // so whitespace-only and absent fields must not produce a draft.
    assert.equal(composeSharedDraft({}), "");
    assert.equal(composeSharedDraft({ title: "  ", text: "", url: null }), "");
  });
});

describe("stripShareParams", () => {
  it("returns null when there is nothing to strip, so the URL is left untouched", () => {
    assert.equal(stripShareParams(new URLSearchParams("thread=abc")), null);
  });

  it("removes every share param and keeps the rest", () => {
    const next = stripShareParams(
      new URLSearchParams("thread=abc&share_title=T&share_text=B&share_url=U"),
    );
    assert.ok(next);
    for (const key of SHARE_PARAMS) assert.equal(next.has(key), false, key);
    assert.equal(next.get("thread"), "abc");
  });
});
