import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { sessionUrl, traceUrl } from "./linkUrls.ts";

const links = { available: true, host: "https://jp.cloud.langfuse.com/", project_id: "proj 1" };
const tid = "0123456789abcdef0123456789abcdef";

describe("traceUrl", () => {
  it("builds the Langfuse trace URL", () => {
    assert.equal(traceUrl(links, tid), `https://jp.cloud.langfuse.com/project/proj%201/traces/${tid}`);
  });
  it("draws nothing without a target, a project or a well-formed id", () => {
    assert.equal(traceUrl(null, tid), "");
    assert.equal(traceUrl({ available: false }, tid), "");
    assert.equal(traceUrl({ ...links, project_id: "" }, tid), "");
    assert.equal(traceUrl(links, undefined), "");
    assert.equal(traceUrl(links, "../../evil"), "");
  });
});

describe("sessionUrl", () => {
  it("encodes the scoped thread id", () => {
    assert.equal(
      sessionUrl(links, "merli::email:ann@example.com"),
      "https://jp.cloud.langfuse.com/project/proj%201/sessions/merli%3A%3Aemail%3Aann%40example.com",
    );
    assert.equal(sessionUrl({ available: false }, "x"), "");
  });
});
