import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_FILTERS,
  apiQuery,
  filtersActive,
  filtersKey,
  hasFilterParams,
  isDefault,
  parseFilters,
  shareUrl,
  writeFilters,
  type IssueFilters,
} from "./filters.ts";

const p = (s: string) => new URLSearchParams(s);

describe("parseFilters", () => {
  it("reads every key and trims the query", () => {
    const f = parseFilters(p("q=%20memo%20&state=closed&type=bug&assignee=ai-merli&label=x&sort=oldest"));
    assert.deepEqual(f, { q: "memo", state: "closed", type: "bug", assignee: "ai-merli", label: "x", sort: "oldest" });
  });
  it("falls back to the defaults for missing or unknown values", () => {
    assert.deepEqual(parseFilters(p("")), DEFAULT_FILTERS);
    assert.equal(parseFilters(p("state=all")).state, "open");
    assert.equal(parseFilters(p("sort=zzz")).sort, "updated");
  });
});

describe("hasFilterParams", () => {
  it("is false for a bare deep link and true for any filter key", () => {
    assert.equal(hasFilterParams(p("issue=3")), false);
    assert.equal(hasFilterParams(p("focus=speech")), false);
    assert.equal(hasFilterParams(p("issue=3&label=bug")), true);
    assert.equal(hasFilterParams(p("state=open")), true);
  });
});

describe("writeFilters", () => {
  it("writes only non-default values and preserves foreign params", () => {
    const next = writeFilters(p("issue=7&focus=x&label=stale&sort=id"), {
      ...DEFAULT_FILTERS,
      state: "closed",
      label: "bug",
    });
    assert.equal(next.get("issue"), "7");
    assert.equal(next.get("focus"), "x");
    assert.equal(next.get("state"), "closed");
    assert.equal(next.get("label"), "bug");
    assert.equal(next.has("sort"), false, "a stale filter key is cleared, not carried");
    assert.equal(next.has("q"), false);
  });
  it("writes nothing for the defaults", () => {
    assert.equal(writeFilters(p(""), DEFAULT_FILTERS).toString(), "");
  });
  it("does not mutate its input", () => {
    const input = p("label=a");
    writeFilters(input, DEFAULT_FILTERS);
    assert.equal(input.get("label"), "a");
  });
  it("round-trips through parseFilters", () => {
    const f: IssueFilters = { q: "deploy", state: "closed", type: "incident", assignee: "human-ana", label: "ops", sort: "created" };
    assert.deepEqual(parseFilters(writeFilters(p(""), f)), f);
  });
});

describe("filtersActive / isDefault / filtersKey", () => {
  it("state and sort do not count as active filters", () => {
    assert.equal(filtersActive({ ...DEFAULT_FILTERS, state: "closed", sort: "id" }), false);
    assert.equal(filtersActive({ ...DEFAULT_FILTERS, label: "x" }), true);
    assert.equal(isDefault({ ...DEFAULT_FILTERS, state: "closed" }), false);
    assert.equal(isDefault({ ...DEFAULT_FILTERS }), true);
  });
  it("gives equal filters an equal key and different ones a different key", () => {
    assert.equal(filtersKey({ ...DEFAULT_FILTERS }), filtersKey({ ...DEFAULT_FILTERS }));
    assert.notEqual(filtersKey({ ...DEFAULT_FILTERS }), filtersKey({ ...DEFAULT_FILTERS, type: "bug" }));
  });
});

describe("apiQuery", () => {
  it("ranks a search by relevance when the sort is the default, never in the URL", () => {
    const f = { ...DEFAULT_FILTERS, q: "memo" };
    const qs = new URLSearchParams(apiQuery(f));
    assert.equal(qs.get("sort"), "relevance");
    assert.equal(qs.get("q"), "memo");
    assert.equal(qs.get("limit"), "200");
    assert.equal(writeFilters(p(""), f).get("sort"), null);
  });
  it("keeps an explicit sort and omits empty filters", () => {
    const qs = new URLSearchParams(apiQuery({ ...DEFAULT_FILTERS, q: "memo", sort: "oldest", state: "closed" }));
    assert.equal(qs.get("sort"), "oldest");
    assert.equal(qs.get("state"), "closed");
    assert.equal(qs.has("type"), false);
  });
});

describe("shareUrl", () => {
  it("omits the question mark for the default list", () => {
    assert.equal(shareUrl("https://x.test", DEFAULT_FILTERS), "https://x.test/issues");
  });
  it("carries the filters and nothing else", () => {
    assert.equal(
      shareUrl("https://x.test", { ...DEFAULT_FILTERS, state: "closed", label: "ops" }),
      "https://x.test/issues?state=closed&label=ops",
    );
  });
});
