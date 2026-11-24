import { strict as assert } from "node:assert";
import { test } from "node:test";

import { applyFrame, foldTranscript, newAssistant, parseToolRow, threadTitle } from "./model.ts";
import { baseName, describeStep } from "./tools.ts";

test("a turn's tool rows and reply fold into one assistant message", () => {
  const messages = foldTranscript([
    { role: "user", content: "Find the weather" },
    { role: "tool_call", content: "", name: "web_search", args: '{"query":"weather"}', tool_call_id: "c1" },
    { role: "tool_result", content: "sunny", name: "web_search", tool_call_id: "c1" },
    { role: "assistant", content: "It is sunny." },
  ]);
  assert.equal(messages.length, 2);
  assert.equal(messages[0].role, "user");
  assert.equal(messages[1].body, "It is sunny.");
  assert.equal(messages[1].tools.length, 1);
  assert.equal(messages[1].tools[0].result, "sunny");
  assert.deepEqual(messages[1].segments, [{ text: "It is sunny.", after: "c1" }]);
});

test("write_todos becomes a checklist, not a tool step", () => {
  const messages = foldTranscript([
    { role: "user", content: "Plan it" },
    {
      role: "tool_call",
      content: 'write_todos: {"todos":[{"content":"Draft","status":"completed"},{"content":"Send","status":"pending"}]}',
    },
    { role: "tool_result", content: "write_todos: Updated todo list" },
    { role: "assistant", content: "Planned." },
  ]);
  assert.equal(messages[1].tools.length, 0);
  assert.deepEqual(
    messages[1].todos?.map((t) => t.content),
    ["Draft", "Send"],
  );
});

test("persisted tool rows recover their name from the content prefix", () => {
  assert.deepEqual(parseToolRow({ content: "read_file: notes.md" }), { name: "read_file", payload: "notes.md" });
  assert.deepEqual(parseToolRow({ content: "no prefix here" }), { name: "", payload: "no prefix here" });
});

test("live frames grow a reply with interleaved spans", () => {
  const live = newAssistant();
  live.streaming = true;
  assert.deepEqual(applyFrame(live, { type: "start", thread_id: "t1", agent: "a", model: "m", run_id: "r" }), {
    kind: "start",
    threadId: "t1",
    runId: "r",
    turnId: undefined,
  });
  applyFrame(live, { type: "text", text: "Looking." });
  applyFrame(live, { type: "tool_call", id: "c1", name: "web_search", args: "{}" });
  applyFrame(live, { type: "tool_result", id: "c1", name: "web_search", content: "ok" });
  applyFrame(live, { type: "text", text: "\n\nFound it." });
  assert.equal(live.body, "Looking.\n\nFound it.");
  assert.deepEqual(live.segments, [
    { text: "Looking.", after: null },
    { text: "\n\nFound it.", after: "c1" },
  ]);
  assert.deepEqual(applyFrame(live, { type: "done", thread_id: "t1" }), { kind: "done", threadId: "t1" });
  assert.equal(live.streaming, false);
});

test("an approval left unanswered at the end of a turn reads as declined", () => {
  const live = newAssistant();
  applyFrame(live, { type: "approval", actions: [{ name: "gmail_send", args: {}, interrupt_id: "i", risk: "high" }] } as never);
  assert.equal(live.approval?.state, "pending");
  applyFrame(live, { type: "done", thread_id: "t" });
  assert.equal(live.approval?.state, "rejected");
});

test("thread titles prefer the person's name for it", () => {
  assert.equal(threadTitle({ name: "Mine", title: "Derived", preview: "hi" }), "Mine");
  assert.equal(threadTitle({ title: "Derived", preview: "hi" }), "Derived");
  assert.equal(threadTitle({}), "New chat");
});

test("tool steps read as plain language", () => {
  assert.equal(baseName("mcp__github__create_issue"), "create_issue");
  assert.deepEqual(describeStep("web_search", '{"query":"best tacos"}', true), {
    icon: "search",
    label: "Searched the web",
    detail: "best tacos",
  });
  assert.deepEqual(describeStep("read_file", '{"file_path":"/w/notes/today.md"}', false), {
    icon: "file",
    label: "Reading",
    detail: "today.md",
  });
  assert.equal(describeStep("fetch_url", '{"url":"https://example.com/a"}', true).detail, "example.com");
  assert.deepEqual(describeStep("mcp__linear__create_issue", '{"title":"Bug"}', true), {
    icon: "tool",
    label: "Create issue",
    detail: "Bug",
  });
});

test("a reply keeps the turn it answered, from the transcript and from the start frame", () => {
  const [, reply] = foldTranscript([
    { role: "user", content: "hi" },
    { role: "assistant", content: "hello", turn_id: "turn-1" },
  ]);
  assert.equal(reply.turnId, "turn-1");
  const [, old] = foldTranscript([
    { role: "user", content: "hi" },
    { role: "assistant", content: "from before turns were stamped" },
  ]);
  assert.equal(old.turnId, undefined);

  const live = newAssistant();
  applyFrame(live, { type: "start", thread_id: "t", turn_id: "turn-2" } as Parameters<typeof applyFrame>[1]);
  assert.equal(live.turnId, "turn-2");
});
