import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  applySteerFrame,
  canSteerChip,
  dropDeliveredSteers,
  newSteerId,
  takeBackQueued,
} from "./steering.ts";
import type { Msg, QueuedMsg, ThreadMessage } from "./sessionStore.ts";

const att = (name: string) => ({ name }) as unknown as QueuedMsg["attachments"][number];
const chip = (over: Partial<QueuedMsg>): QueuedMsg => ({
  id: 1,
  kind: "queue",
  text: "hello",
  attachments: [],
  mode: "auto",
  ...over,
});
const msg = (id: number, role: Msg["role"], body: string): Msg => ({ id, role, body, tools: [], subs: [] });

describe("newSteerId", () => {
  it("fits the backend's id rule", () => {
    const id = newSteerId();
    assert.match(id, /^[A-Za-z0-9_-]{8,64}$/);
    assert.notEqual(id, newSteerId());
  });
});

describe("canSteerChip", () => {
  it("only a queued, text-only chip while a turn is live", () => {
    assert.equal(canSteerChip(chip({}), true), true);
    assert.equal(canSteerChip(chip({}), false), false);
    assert.equal(canSteerChip(chip({ kind: "steer", steerId: "abcdefgh" }), true), false);
    assert.equal(canSteerChip(chip({ attachments: [att("a.png")] }), true), false);
  });
});

describe("applySteerFrame", () => {
  it("drops the delivered chip and splits the transcript like foldRow would", () => {
    let seq = 10;
    const messages = [msg(1, "user", "make a chart"), msg(2, "assistant", "working")];
    const queue = [chip({ id: 3, kind: "steer", steerId: "s-1", text: "use blue" }), chip({ id: 4, text: "later" })];
    const r = applySteerFrame(messages, queue, { id: "s-1", text: "use blue", sender: "Ana" }, () => ++seq, 5);
    assert.deepEqual(
      r.queue.map((q) => q.id),
      [4],
    );
    assert.equal(r.messages.length, 4);
    const aside = r.messages[2];
    assert.equal(aside.role, "user");
    assert.equal(aside.body, "use blue");
    assert.equal(aside.steer, true);
    assert.equal(aside.sender, "Ana");
    const cont = r.messages[3];
    assert.equal(cont.role, "assistant");
    assert.equal(cont.body, "");
    assert.deepEqual(cont.approvals, []);
    // The originals are untouched (the store swaps the arrays).
    assert.equal(messages.length, 2);
    assert.equal(queue.length, 2);
  });

  it("falls back to the oldest id-less steer chip, and never touches queued chips", () => {
    const queue = [chip({ id: 1, text: "plain" }), chip({ id: 2, kind: "steer", text: "a" }), chip({ id: 3, kind: "steer", text: "b" })];
    const r = applySteerFrame([], queue, { id: "server-minted", text: "a" }, () => 1, 0);
    assert.deepEqual(
      r.queue.map((q) => q.id),
      [1, 3],
    );
  });

  it("an unmatched frame still renders the aside (another device steered)", () => {
    const r = applySteerFrame([], [], { id: "x", text: "hi" }, () => 1, 0);
    assert.equal(r.messages.length, 2);
    assert.equal(r.queue.length, 0);
  });
});

describe("dropDeliveredSteers", () => {
  const rows: ThreadMessage[] = [
    { role: "user", content: "start" },
    { role: "user", content: "use blue", steer: true, steer_id: "s-1" },
    { role: "user", content: "and red", steer: true },
  ];
  it("removes steer chips the server proves were read — by id, then by text", () => {
    const queue = [
      chip({ id: 1, kind: "steer", steerId: "s-1", text: "use blue" }),
      chip({ id: 2, kind: "steer", steerId: "s-9", text: "and red" }),
      chip({ id: 3, kind: "steer", steerId: "s-5", text: "still pending" }),
      chip({ id: 4, text: "use blue" }),
    ];
    assert.deepEqual(
      dropDeliveredSteers(queue, rows).map((q) => q.id),
      [3, 4],
    );
  });
  it("is a no-op without steer rows", () => {
    const queue = [chip({ id: 1, kind: "steer", steerId: "s-1" })];
    assert.equal(dropDeliveredSteers(queue, [{ role: "user", content: "x" }]), queue);
  });
});

describe("takeBackQueued", () => {
  it("puts the chip's text ahead of the draft and re-stages its files first", () => {
    const a = att("a.png");
    const b = att("b.png");
    const queue = [chip({ id: 1, text: "first", attachments: [a] }), chip({ id: 2, text: "second" })];
    const r = takeBackQueued(queue, 1, "typed", [b]);
    assert.ok(r);
    assert.equal(r.input, "first\ntyped");
    assert.deepEqual(r.staged, [a, b]);
    assert.deepEqual(
      r.queue.map((q) => q.id),
      [2],
    );
  });
  it("an empty draft takes the chip's text as-is; a text-less chip keeps the draft", () => {
    assert.equal(takeBackQueued([chip({ id: 1, text: "only" })], 1, "   ", [])?.input, "only");
    assert.equal(takeBackQueued([chip({ id: 1, text: "", attachments: [att("x")] })], 1, "draft", [])?.input, "draft");
  });
  it("an unknown id changes nothing", () => {
    assert.equal(takeBackQueued([chip({ id: 1 })], 9, "d", []), null);
  });
});
