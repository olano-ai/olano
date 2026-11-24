import { test } from "node:test";
import assert from "node:assert/strict";
import {
  agentDetailPath,
  agentSwitchTarget,
  canonicalTarget,
  chatPath,
  filesPath,
  parseRoute,
  shellKey,
  simpleShellFallback,
  urlAgent,
  viewPath,
  type CanonicalContext,
} from "./routeUrls.ts";

const roster = ["merli", "agentfather", "ana maria"];
const ready: CanonicalContext = {
  agent: "merli",
  known: (n) => roster.includes(n),
  fallback: "merli",
};
const loading: CanonicalContext = { agent: "", known: null, fallback: "" };

const canon = (url: string, ctx: CanonicalContext = ready) => {
  const u = new URL(url, "https://box.test");
  return canonicalTarget(u.pathname, u.search, u.hash, ctx);
};

// ── parse / build ────────────────────────────────────────────────────────────

test("per-agent views carry the agent then the section", () => {
  assert.deepEqual(parseRoute("/chat/merli"), { view: "chat", agent: "merli", section: null });
  assert.deepEqual(parseRoute("/cortex/merli/ideas"), { view: "cortex", agent: "merli", section: "ideas" });
  assert.deepEqual(parseRoute("/chat"), { view: "chat", agent: null, section: null });
});

test("agent names round-trip through encoding", () => {
  const p = viewPath("chat", { agent: "ana maria" });
  assert.equal(p, "/chat/ana%20maria");
  assert.equal(parseRoute(p).agent, "ana maria");
});

test("a malformed escape reads as no agent instead of throwing", () => {
  assert.equal(parseRoute("/chat/%E0%A4%A").agent, null);
});

test("insights puts the tab first and the agent after it", () => {
  assert.deepEqual(parseRoute("/insights/drill/merli"), { view: "insights", agent: "merli", section: "drill" });
  assert.equal(viewPath("insights", { section: "drill", agent: "merli" }), "/insights/drill/merli");
  assert.equal(viewPath("insights", { section: "overview" }), "/insights/overview");
});

test("other views keep their second segment as the section", () => {
  assert.deepEqual(parseRoute("/calendar/week"), { view: "calendar", agent: null, section: "week" });
  assert.deepEqual(parseRoute("/groups/12"), { view: "groups", agent: null, section: "12" });
  assert.deepEqual(parseRoute("/"), { view: "", agent: null, section: null });
});

test("chat links put the thread in the query, encoded", () => {
  assert.equal(chatPath("merli"), "/chat/merli");
  assert.equal(chatPath("merli", "pulse:42"), "/chat/merli?thread=pulse%3A42");
});

test("an agent's overview tab is the bare page", () => {
  assert.equal(agentDetailPath("merli"), "/agents/merli");
  assert.equal(agentDetailPath("merli", "overview"), "/agents/merli");
  assert.equal(agentDetailPath("merli", "mcp"), "/agents/merli/mcp");
});

test("file links keep the path's slashes readable", () => {
  assert.equal(filesPath("merli", "/reports/q3 plan.md"), "/files/merli?path=reports/q3%20plan.md");
  assert.equal(filesPath("merli"), "/files/merli");
  assert.equal(filesPath(), "/files");
});

// ── shell key ────────────────────────────────────────────────────────────────

test("the shell key ignores the agent and section of a view", () => {
  // Same key → no remount, no scroll reset, no drawer close.
  assert.equal(shellKey("/chat/merli"), shellKey("/chat/agentfather"));
  assert.equal(shellKey("/chat/merli"), "/chat");
  assert.equal(shellKey("/cortex/merli/ideas"), "/cortex");
  assert.equal(shellKey("/insights/drill/merli"), "/insights");
  assert.equal(shellKey("/files/merli"), "/files");
});

test("an agent page remounts per agent but never per tab, as before", () => {
  assert.equal(shellKey("/agents/merli/mcp"), "/agents/merli");
  assert.equal(shellKey("/agents/merli"), "/agents/merli");
  assert.notEqual(shellKey("/agents/merli"), shellKey("/agents/agentfather"));
  assert.equal(shellKey("/agents"), "/agents");
  assert.equal(shellKey("/groups/3"), "/groups/3");
  assert.equal(shellKey("/"), "/");
});

// ── canonical URLs ───────────────────────────────────────────────────────────

test("canonical URLs stay put", () => {
  for (const url of [
    "/chat/merli?thread=abc",
    "/agents/merli/mcp",
    "/agents",
    "/files",
    "/files/merli?path=a.md",
    "/overview",
    "/issues?issue=3",
    "/groups/2",
  ]) {
    assert.equal(canon(url), null, url);
  }
});

test("a bare per-agent view lands on the current agent, query and hash kept", () => {
  assert.deepEqual(canon("/chat?thread=abc&history=1#x"), { to: "/chat/merli?thread=abc&history=1#x" });
  assert.deepEqual(canon("/cortex"), { to: "/cortex/merli" });
});

test("a bare per-agent view waits while no agent is known", () => {
  assert.equal(canon("/chat?thread=abc", loading), null);
});

test("the legacy ?agent= spelling moves into the path", () => {
  assert.deepEqual(canon("/chat?agent=agentfather&thread=t1"), { to: "/chat/agentfather?thread=t1" });
  assert.deepEqual(canon("/cortex?agent=merli"), { to: "/cortex/merli" });
  assert.deepEqual(canon("/connections?agent=merli"), { to: "/connections/merli" });
  // Before the roster answers, too: the link is explicit about its agent.
  assert.deepEqual(canon("/chat?agent=agentfather", loading), { to: "/chat/agentfather" });
});

test("every href the runtime stores still lands somewhere real", () => {
  // olano/runtime/server.py, scheduler/gmail_watch.py, cognition/worker.py,
  // issues/delivery.py, runtime/media_refs.py: persisted in notifications,
  // so they must resolve forever.
  assert.deepEqual(canon("/agents/merli?tab=connectors"), { to: "/agents/merli/connectors" });
  assert.deepEqual(canon("/agents/merli?tab=mcp"), { to: "/agents/merli/mcp" });
  assert.deepEqual(canon("/files?agent=merli"), { to: "/files/merli" });
  assert.deepEqual(canon("/files?path=reports/a.md&agent=merli"), { to: "/files/merli?path=reports/a.md" });
  assert.deepEqual(canon("/connections?agent=merli"), { to: "/connections/merli" });
  assert.deepEqual(canon("/cortex?agent=merli"), { to: "/cortex/merli" });
  assert.equal(canon("/issues?issue=7"), null);
  assert.equal(canon("/vault"), null);
  assert.equal(canon("/config"), null);
  assert.equal(canon("/overview"), null);
});

test("legacy agent-page spellings", () => {
  assert.deepEqual(canon("/agents?agent=merli"), { to: "/agents/merli" });
  // Both legacy keys move in one hop.
  assert.deepEqual(canon("/agents?agent=merli&tab=mcp&focus=x"), { to: "/agents/merli/mcp?focus=x" });
  assert.deepEqual(canon("/agents/merli?tab=overview"), { to: "/agents/merli" });
  assert.deepEqual(canon("/agents/merli?tab=skills&focus=x"), { to: "/agents/merli/skills?focus=x" });
});

test("an unknown agent falls back and drops the selection that belonged to it", () => {
  assert.deepEqual(canon("/chat/ghost?thread=abc"), { to: "/chat/merli", unknownAgent: "ghost" });
  assert.deepEqual(canon("/cortex/ghost/ideas?item=3"), { to: "/cortex/merli/ideas", unknownAgent: "ghost" });
  // A filter view simply widens back to "all".
  assert.deepEqual(canon("/files/ghost?path=a"), { to: "/files", unknownAgent: "ghost" });
  // Nothing to fall back to: the bare view, which then stays put.
  const empty: CanonicalContext = { agent: "", known: () => false, fallback: "" };
  assert.deepEqual(canon("/chat/ghost", empty), { to: "/chat", unknownAgent: "ghost" });
  assert.equal(canon("/chat", empty), null);
});

test("an agent is never judged unknown while the roster is loading", () => {
  assert.equal(canon("/chat/ghost?thread=abc", loading), null);
});

// ── agent switching ──────────────────────────────────────────────────────────

test("switching agent on a per-agent page keeps the page and its section", () => {
  assert.equal(agentSwitchTarget("/chat/merli", "agentfather", "dashboard"), "/chat/agentfather");
  assert.equal(agentSwitchTarget("/cortex/merli/ideas", "agentfather", "dashboard"), "/cortex/agentfather/ideas");
  assert.equal(agentSwitchTarget("/knowledge/merli/sources", "a b", "dashboard"), "/knowledge/a%20b/sources");
});

test("switching agent elsewhere does not navigate", () => {
  for (const p of ["/overview", "/files/merli", "/agents/merli/mcp", "/scheduler", "/", "/issues"]) {
    assert.equal(agentSwitchTarget(p, "agentfather", "dashboard"), null, p);
  }
});

test("the simple shell's conversation follows the agent through the query", () => {
  assert.equal(agentSwitchTarget("/", "agentfather", "simple"), "/?agent=agentfather");
  assert.equal(agentSwitchTarget("/cortex/merli", "agentfather", "simple"), "/cortex/agentfather");
  assert.equal(agentSwitchTarget("/insights", "agentfather", "simple"), null);
});

test("the agent a URL selects", () => {
  assert.equal(urlAgent("/chat/merli", "", "dashboard"), "merli");
  assert.equal(urlAgent("/chat", "?agent=agentfather", "dashboard"), "agentfather");
  assert.equal(urlAgent("/files/merli", "", "dashboard"), null);
  assert.equal(urlAgent("/agents/merli", "", "dashboard"), null);
  assert.equal(urlAgent("/", "?agent=merli&thread=x", "simple"), "merli");
  assert.equal(urlAgent("/", "?agent=merli", "dashboard"), null);
});

test("the simple shell keeps a dashboard chat link's agent and thread", () => {
  assert.equal(simpleShellFallback("/chat/merli", "?thread=t1"), "/?agent=merli&thread=t1");
  assert.equal(simpleShellFallback("/chat", "?thread=t1"), "/?thread=t1");
  assert.equal(simpleShellFallback("/agents/merli", ""), "/");
  assert.equal(simpleShellFallback("/chat/merli", "?agent=x&thread=t"), "/?agent=x&thread=t");
});
