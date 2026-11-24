import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { cortexItemDraft, pulseArtifacts, pulseDraft } from "./handoffDraft.ts";
import { STR } from "./strings.ts";

// The real English dictionary through a minimal `fill`, so the assertions read
// the template a person sees rather than a stub of it.
const en = STR.en as Record<string, string>;
function t(key: string, vars?: Record<string, string>): string {
  let s = en[key];
  assert.ok(s !== undefined, `missing string ${key}`);
  for (const [k, v] of Object.entries(vars ?? {})) s = s.replaceAll(`{${k}}`, () => v);
  return s;
}

const idea = {
  kind: "idea",
  title: "Solar kiln",
  path: "ideas/solar-kiln",
  abs_path: "/home/u/olano/workspace/ideas/solar-kiln",
};

describe("cortexItemDraft", () => {
  it("frames the item, names the FULL workspace path, and ends waiting for the user", () => {
    const d = cortexItemDraft(t, "chat", idea);
    assert.equal(
      d,
      'The user wants to chat about this idea: "Solar kiln".\n' +
        "Artifacts (full workspace path):\n/home/u/olano/workspace/ideas/solar-kiln\n\nUser: ",
    );
  });

  it("explore more is the same framing with a different ask", () => {
    const d = cortexItemDraft(t, "explore", idea);
    assert.ok(d.startsWith('The user wants to explore more about this idea: "Solar kiln".'));
    assert.ok(d.endsWith("\n\nUser: "));
  });

  it("an older item that stored only its relative path still names it", () => {
    const d = cortexItemDraft(t, "chat", { ...idea, abs_path: undefined });
    assert.ok(d.includes("Artifacts (full workspace path):\nideas/solar-kiln"));
  });

  it("an item with no folder gets no artifacts line at all", () => {
    const d = cortexItemDraft(t, "run", { kind: "prototype", title: "P", path: "", abs_path: "" });
    assert.equal(d, 'The user wants to run/try this prototype: "P".\n\nUser: ');
  });
});

describe("pulseArtifacts", () => {
  it("resolves relative references against the card's own workspace", () => {
    assert.deepEqual(
      pulseArtifacts({ references: ["memories/notes.md", "ideas/x/idea.md"], workspace_root: "/ws/" }),
      ["/ws/memories/notes.md", "/ws/ideas/x/idea.md"],
    );
  });

  it("leaves absolute paths and URLs alone, and drops blanks", () => {
    assert.deepEqual(
      pulseArtifacts({ references: ["/abs/file.md", "https://x.y/z", "", "  "], workspace_root: "/ws" }),
      ["/abs/file.md", "https://x.y/z"],
    );
  });

  it("with no root, a relative reference passes through rather than being invented", () => {
    assert.deepEqual(pulseArtifacts({ references: ["memories/a.md"] }), ["memories/a.md"]);
  });
});

describe("pulseDraft", () => {
  it("carries the card's body and timing since a pulse has no folder", () => {
    const d = pulseDraft(t, "chat", {
      title: "Connector readiness matrix",
      content: "A v1 readiness one-pager.",
      why_now: "A worker you approved finished.",
      references: ["memories/readiness.md"],
      workspace_root: "/ws",
    });
    assert.equal(
      d,
      'The user wants to chat about this pulse: "Connector readiness matrix".\n' +
        "Artifacts (full workspace path):\n/ws/memories/readiness.md\n" +
        "Details: A v1 readiness one-pager.\n" +
        "Why now: A worker you approved finished.\n\nUser: ",
    );
  });

  it("a self-check finding asks for a fix and never repeats a body equal to its title", () => {
    const d = pulseDraft(t, "fix", { title: "Same", content: "Same", why_now: "", references: [] });
    assert.equal(
      d,
      'The user wants this self-check finding investigated and, where safe, fixed: "Same".\n\nUser: ',
    );
  });
});
