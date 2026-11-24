import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  SEARCH_MIN_LENGTH,
  SearchCache,
  decideSearch,
  isLatest,
  normalizeQuery,
  refineHits,
  type LastSearch,
} from "./search.ts";
import type { SearchHit } from "./tree.ts";

const hit = (agent: string, path: string): SearchHit => ({
  agent,
  path,
  name: path.split("/").pop() ?? path,
  type: "file",
});

const HITS = [hit("merli", "MEMORY.md"), hit("merli", "memories/2026-09-01.md"), hit("scout", "docs/MEMORANDUM.txt")];

describe("normalizeQuery", () => {
  it("trims and lower-cases, so case and whitespace edits are not new queries", () => {
    assert.equal(normalizeQuery("  MEMOR "), "memor");
    assert.equal(normalizeQuery("MEMOR"), normalizeQuery("memor"));
  });
});

describe("refineHits", () => {
  it("keeps hits whose name or path contains the longer query", () => {
    assert.deepEqual(
      refineHits(HITS, "memory").map((h) => h.path),
      ["MEMORY.md"],
    );
    assert.deepEqual(
      refineHits(HITS, "memor").map((h) => h.path),
      ["MEMORY.md", "memories/2026-09-01.md", "docs/MEMORANDUM.txt"],
    );
    assert.deepEqual(refineHits(HITS, "2026").map((h) => h.path), ["memories/2026-09-01.md"]);
  });
});

describe("decideSearch", () => {
  const complete = (q: string, hits: SearchHit[]): LastSearch => ({ q, hidden: false, result: { hits, truncated: false } });

  it("does nothing below the minimum length", () => {
    const cache = new SearchCache();
    assert.equal(SEARCH_MIN_LENGTH, 2);
    assert.deepEqual(decideSearch({ q: "", hidden: false, last: null, cache }), { kind: "none" });
    assert.deepEqual(decideSearch({ q: "m", hidden: false, last: null, cache }), { kind: "none" });
  });

  it("requests a query it has never seen", () => {
    assert.deepEqual(decideSearch({ q: "memor", hidden: false, last: null, cache: new SearchCache() }), { kind: "request" });
  });

  it("skips the query that is already on screen", () => {
    const d = decideSearch({ q: "memor", hidden: false, last: complete("memor", HITS), cache: new SearchCache() });
    assert.deepEqual(d, { kind: "skip" });
  });

  it("refines locally when the new query extends a complete result", () => {
    const cache = new SearchCache();
    const d = decideSearch({ q: "memory", hidden: false, last: complete("memor", HITS), cache });
    assert.equal(d.kind, "local");
    if (d.kind === "local") assert.deepEqual(d.result.hits.map((h) => h.path), ["MEMORY.md"]);
    // …and remembers the refinement, so backspacing forward again is instant.
    assert.ok(cache.get("memory", false));
  });

  it("an empty complete result makes every extension empty without a request", () => {
    const d = decideSearch({ q: "zzzz", hidden: false, last: complete("zzz", []), cache: new SearchCache() });
    assert.deepEqual(d, { kind: "local", result: { hits: [], truncated: false } });
  });

  it("does not refine a truncated result — the sample may be missing matches", () => {
    const last: LastSearch = { q: "me", hidden: false, result: { hits: HITS, truncated: true } };
    assert.deepEqual(decideSearch({ q: "mem", hidden: false, last, cache: new SearchCache() }), { kind: "request" });
  });

  it("does not refine across the hidden-files toggle", () => {
    const last: LastSearch = { q: "me", hidden: false, result: { hits: HITS, truncated: false } };
    assert.deepEqual(decideSearch({ q: "mem", hidden: true, last, cache: new SearchCache() }), { kind: "request" });
  });

  it("requests when the new query is not an extension (backspace to a shorter one)", () => {
    assert.deepEqual(decideSearch({ q: "mem", hidden: false, last: complete("memor", HITS), cache: new SearchCache() }), { kind: "request" });
  });

  it("serves a cached query without a request", () => {
    const cache = new SearchCache();
    cache.set("mem", false, { hits: HITS, truncated: false });
    const d = decideSearch({ q: "mem", hidden: false, last: complete("memor", [HITS[0]]), cache });
    assert.equal(d.kind, "local");
  });
});

describe("SearchCache", () => {
  it("is keyed by query and hidden flag, and evicts the least recently used", () => {
    const cache = new SearchCache(2);
    cache.set("a", false, { hits: [], truncated: false });
    cache.set("b", false, { hits: [], truncated: false });
    assert.ok(cache.get("a", false)); // touch a → b is now the oldest
    cache.set("c", false, { hits: [], truncated: false });
    assert.equal(cache.size, 2);
    assert.equal(cache.get("b", false), undefined);
    assert.ok(cache.get("a", false));
    assert.ok(cache.get("c", false));
    assert.equal(cache.get("a", true), undefined, "hidden is part of the key");
    cache.clear();
    assert.equal(cache.size, 0);
  });
});

describe("isLatest", () => {
  it("accepts only the latest request's answer", () => {
    assert.equal(isLatest(3, 3), true);
    assert.equal(isLatest(2, 3), false);
  });
});
