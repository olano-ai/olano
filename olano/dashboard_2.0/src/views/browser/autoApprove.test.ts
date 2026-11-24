import { test } from "node:test";
import assert from "node:assert/strict";
import { cleanRules, draftProblem, normalizeSite, ruleProblem, sameRules, siteIsValid } from "./autoApprove.ts";

test("a pasted address becomes the bare host name a rule takes", () => {
  assert.equal(normalizeSite("https://www.LinkedIn.com/feed/?x=1"), "linkedin.com");
  assert.equal(normalizeSite("www.linkedin.com"), "linkedin.com");
  assert.equal(normalizeSite("www.com"), "www.com");
  assert.equal(normalizeSite("mail.google.com"), "mail.google.com");
  assert.equal(normalizeSite("  linkedin.com  "), "linkedin.com");
  assert.equal(normalizeSite("example.com:8443/path"), "example.com");
  assert.equal(normalizeSite("*.example.com"), "*.example.com");
  assert.equal(normalizeSite("linkedin.com."), "linkedin.com");
});

test("a rule without a site or without an action stops the save", () => {
  assert.deepEqual(draftProblem([{ site: "", actions: ["send"], in_background: false }]), {
    key: "br.aa.needSite",
  });
  assert.deepEqual(draftProblem([{ site: "LinkedIn.com", actions: [], in_background: false }]), {
    key: "br.aa.needAction",
    site: "linkedin.com",
  });
  assert.equal(draftProblem([{ site: "linkedin.com", actions: ["send"], in_background: false }]), null);
  assert.equal(draftProblem([]), null);
});

test("cleaned rules are normalized and in a stable order, so an unchanged draft is not dirty", () => {
  const a = [{ site: "https://LinkedIn.com/", actions: ["submit", "send"] as const, in_background: false }];
  const b = [{ site: "linkedin.com", actions: ["send", "submit"] as const, in_background: false }];
  assert.deepEqual(cleanRules([...a].map((r) => ({ ...r, actions: [...r.actions] }))), [
    { site: "linkedin.com", actions: ["send", "submit"], in_background: false },
  ]);
  assert.equal(
    sameRules(
      a.map((r) => ({ ...r, actions: [...r.actions] })),
      b.map((r) => ({ ...r, actions: [...r.actions] })),
    ),
    true,
  );
});

test("a site the server would refuse is caught while typing, with the reason", () => {
  assert.equal(siteIsValid("linkedin.com"), true);
  assert.equal(siteIsValid("*.example.com"), true);
  assert.equal(siteIsValid("https://www.linkedin.com/in/jane"), true);
  assert.equal(siteIsValid("linkedin"), false);
  assert.equal(siteIsValid("my site.com"), false);
  assert.deepEqual(ruleProblem({ site: "linkedin", actions: ["send"], in_background: false }), {
    key: "br.aa.badSite",
    site: "linkedin",
  });
});

test("the same site twice is one rule too many, however it was typed", () => {
  const rules = [
    { site: "linkedin.com", actions: ["send" as const], in_background: false },
    { site: "https://www.linkedin.com/feed/", actions: ["submit" as const], in_background: false },
  ];
  assert.deepEqual(draftProblem(rules), { key: "br.aa.dupSite", site: "linkedin.com" });
});
