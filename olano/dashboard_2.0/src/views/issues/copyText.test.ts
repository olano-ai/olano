import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { issueMarkdown, utcStamp, type CopyEvent, type CopyIssue } from "./copyText.ts";
import { STR } from "./strings.ts";

// The English dictionary with useT's `{name}` filling, so the test reads the
// real wording rather than keys.
const t = (key: string, vars: Record<string, string> = {}) =>
  (STR.en?.[key] ?? key).replace(/\{(\w+)\}/g, (_, k: string) => vars[k] ?? "");

const ISSUE: CopyIssue = {
  id: 58,
  title: "LinkedIn pending-invitation sent timestamps change on each read",
  body: "Two reads, two times.\n\n- first\n- second\n",
  author: "ai-anna",
  assignee: "",
  state: "open",
  type: "bug",
  labels: ["linkedin", "timestamps"],
  created_at: "2026-10-16T22:55:12.345678+00:00",
};

const comment = (body: string, ts: string): CopyEvent => ({
  kind: "comment",
  actor: "ai-anna",
  ts,
  payload: { body },
});

describe("issueMarkdown", () => {
  it("writes the title, a header line, the Markdown source and every comment", () => {
    const events: CopyEvent[] = [
      comment("Worked around it.", "2026-10-17T00:01:00+00:00"),
      { kind: "assign", actor: "human-carlos", ts: "2026-10-17T01:00:00+00:00", payload: { assignee: "ai-anna" } },
      comment("```\nraw\n```", "2026-10-17T02:30:59+00:00"),
    ];
    assert.equal(
      issueMarkdown(ISSUE, events, t),
      [
        "# LinkedIn pending-invitation sent timestamps change on each read (#58)",
        "Open · bug · opened by ai-anna on 2026-10-16 22:55 UTC · Unassigned · labels: linkedin, timestamps",
        "Two reads, two times.\n\n- first\n- second",
        "---",
        "**ai-anna** commented on 2026-10-17 00:01 UTC:",
        "Worked around it.",
        "---",
        "**ai-anna** commented on 2026-10-17 02:30 UTC:",
        "```\nraw\n```",
      ].join("\n\n") + "\n",
    );
  });

  it("names the assignee and says so when there is no description", () => {
    const out = issueMarkdown({ ...ISSUE, body: "  ", assignee: "human-carlos", labels: [] }, [], t);
    assert.match(out, /· assigned to human-carlos\n/);
    assert.doesNotMatch(out, /labels:/);
    assert.match(out, /\n\n_No description provided\._\n$/);
  });
});

describe("utcStamp", () => {
  it("prints minutes in UTC, and leaves an unparseable value alone", () => {
    assert.equal(utcStamp("2026-10-16T18:07:00-06:00"), "2026-10-17 00:07 UTC");
    assert.equal(utcStamp("yesterday"), "yesterday");
  });
});
