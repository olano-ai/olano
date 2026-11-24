// Unit tests for the Files tree model. Run with `npm run test:unit` (Node's
// built-in runner with type stripping — no test framework in the bundle).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  FetchQueue,
  agentRowKey,
  ancestorsOf,
  countEntries,
  findByPrefix,
  flattenTree,
  isAbortError,
  isWithin,
  listingsToHydrate,
  lruSet,
  makeClock,
  makeCollator,
  nodeKey,
  parentOf,
  pruneNodes,
  sortEntries,
  splitKey,
  workspaceRowKey,
  type AgentInfo,
  type FileEntry,
  type NodeMap,
  type Row,
} from "./tree.ts";

const collator = makeCollator();

function file(path: string, size = 10): FileEntry {
  const name = path.split("/").pop() ?? path;
  return { name, path, type: "file", size, modified: "2026-01-01T00:00:00" };
}
function dir(path: string): FileEntry {
  const name = path.split("/").pop() ?? path;
  return { name, path, type: "directory", size: 0, modified: "2026-01-01T00:00:00" };
}
function listing(entries: FileEntry[], touched = 1, extra: Partial<NodeMap[string]> = {}): NodeMap[string] {
  return { listing: { entries, truncated: false, total: entries.length }, touched, ...extra };
}
const A: AgentInfo = { name: "alpha", label: "Alpha", status: "running" };
const B: AgentInfo = { name: "beta", label: "Beta", status: "configured" };

function kinds(rows: Row[]): string[] {
  return rows.map((r) => (r.kind === "entry" ? `entry:${r.entry.path}` : r.kind === "agent" ? `agent:${r.agent}` : `${r.kind}:${r.agent}`));
}

describe("keys", () => {
  it("round-trips agent and path, root included", () => {
    assert.deepEqual(splitKey(nodeKey("alpha", "a/b.md")), { agent: "alpha", path: "a/b.md" });
    assert.deepEqual(splitKey(nodeKey("alpha", "")), { agent: "alpha", path: "" });
  });
  it("path helpers", () => {
    assert.equal(parentOf("a/b/c.md"), "a/b");
    assert.equal(parentOf("c.md"), "");
    assert.deepEqual(ancestorsOf("a/b/c.md"), ["a", "a/b"]);
    assert.deepEqual(ancestorsOf("c.md"), []);
    assert.ok(isWithin("a/b", "a"));
    assert.ok(isWithin("a", "a"));
    assert.ok(isWithin("anything", ""));
    assert.ok(!isWithin("ab", "a"));
  });
});

describe("sortEntries", () => {
  it("dirs first, then natural, case-insensitive order", () => {
    const sorted = sortEntries([file("file10"), file("File2"), dir("zeta"), file("file1"), dir("Alpha")], collator);
    assert.deepEqual(
      sorted.map((e) => e.name),
      ["Alpha", "zeta", "file1", "File2", "file10"],
    );
  });
  it("does not mutate its input", () => {
    const input = [file("b"), file("a")];
    sortEntries(input, collator);
    assert.deepEqual(input.map((e) => e.name), ["b", "a"]);
  });
});

describe("flattenTree", () => {
  it("collapsed agents are one row each and touch no listing", () => {
    const rows = flattenTree({ agents: [A, B], nodes: {}, expanded: new Set(), query: "", collator });
    assert.deepEqual(kinds(rows), ["agent:alpha", "agent:beta"]);
  });

  it("an expanded agent shows its workspace row; an expanded workspace shows the root listing", () => {
    const nodes: NodeMap = { [nodeKey("alpha", "")]: listing([file("z.md"), dir("docs")]) };
    const half = flattenTree({ agents: [A], nodes, expanded: new Set([agentRowKey("alpha")]), query: "", collator });
    assert.deepEqual(kinds(half), ["agent:alpha", "workspace:alpha"]);
    const full = flattenTree({
      agents: [A],
      nodes,
      expanded: new Set([agentRowKey("alpha"), workspaceRowKey("alpha")]),
      query: "",
      collator,
    });
    assert.deepEqual(kinds(full), ["agent:alpha", "workspace:alpha", "entry:docs", "entry:z.md"]);
    assert.deepEqual(full.map((r) => r.depth), [0, 1, 2, 2]);
  });

  it("a workspace expanded but not yet loaded renders a loading row; an error renders an error row", () => {
    const exp = new Set([agentRowKey("alpha"), workspaceRowKey("alpha")]);
    const loading = flattenTree({ agents: [A], nodes: { [nodeKey("alpha", "")]: { loading: true, touched: 1 } }, expanded: exp, query: "", collator });
    assert.deepEqual(kinds(loading), ["agent:alpha", "workspace:alpha", "loading:alpha"]);
    const errored = flattenTree({ agents: [A], nodes: { [nodeKey("alpha", "")]: { error: "Access denied", touched: 1 } }, expanded: exp, query: "", collator });
    assert.equal(errored[2].kind, "error");
  });

  it("nested directories open only when expanded, and only visit open subtrees", () => {
    const nodes: NodeMap = {
      [nodeKey("alpha", "")]: listing([dir("docs"), dir("src")]),
      [nodeKey("alpha", "docs")]: listing([file("docs/a.md")]),
      [nodeKey("alpha", "src")]: listing([file("src/main.py")]),
    };
    const rows = flattenTree({
      agents: [A],
      nodes,
      expanded: new Set([agentRowKey("alpha"), workspaceRowKey("alpha"), nodeKey("alpha", "docs")]),
      query: "",
      collator,
    });
    assert.deepEqual(kinds(rows), ["agent:alpha", "workspace:alpha", "entry:docs", "entry:docs/a.md", "entry:src"]);
    assert.equal((rows[2] as Extract<Row, { kind: "entry" }>).expanded, true);
    assert.equal((rows[4] as Extract<Row, { kind: "entry" }>).expanded, false);
  });

  it("two agents keep their listings apart even with identical paths", () => {
    const nodes: NodeMap = {
      [nodeKey("alpha", "")]: listing([file("AGENTS.md")]),
      [nodeKey("beta", "")]: listing([file("AGENTS.md"), file("extra.md")]),
    };
    const rows = flattenTree({
      agents: [A, B],
      nodes,
      expanded: new Set([agentRowKey("alpha"), workspaceRowKey("alpha"), agentRowKey("beta"), workspaceRowKey("beta")]),
      query: "",
      collator,
    });
    const entries = rows.filter((r) => r.kind === "entry");
    assert.deepEqual(entries.map((r) => r.agent), ["alpha", "beta", "beta"]);
    assert.deepEqual(entries.map((r) => r.key), [nodeKey("alpha", "AGENTS.md"), nodeKey("beta", "AGENTS.md"), nodeKey("beta", "extra.md")]);
  });

  it("a truncated listing ends with a truncated row carrying the counts", () => {
    const nodes: NodeMap = {
      [nodeKey("alpha", "")]: { listing: { entries: [file("a"), file("b")], truncated: true, total: 5000 }, touched: 1 },
    };
    const rows = flattenTree({ agents: [A], nodes, expanded: new Set([agentRowKey("alpha"), workspaceRowKey("alpha")]), query: "", collator });
    const last = rows[rows.length - 1];
    assert.equal(last.kind, "truncated");
    assert.deepEqual([(last as Extract<Row, { kind: "truncated" }>).shown, (last as Extract<Row, { kind: "truncated" }>).total], [2, 5000]);
  });

  it("an empty directory renders an empty row", () => {
    const nodes: NodeMap = { [nodeKey("alpha", "")]: listing([]) };
    const rows = flattenTree({ agents: [A], nodes, expanded: new Set([agentRowKey("alpha"), workspaceRowKey("alpha")]), query: "", collator });
    assert.equal(rows[2].kind, "empty");
  });

  describe("filter", () => {
    const nodes: NodeMap = {
      [nodeKey("alpha", "")]: listing([dir("docs"), file("README.md"), dir("unloaded")]),
      [nodeKey("alpha", "docs")]: listing([file("docs/plan.md"), file("docs/notes.txt")]),
      [nodeKey("beta", "")]: listing([file("other.md")]),
    };
    it("matches files by name and keeps the ancestors that lead to them, auto-opened", () => {
      const rows = flattenTree({ agents: [A, B], nodes, expanded: new Set(), query: "plan", collator });
      assert.deepEqual(kinds(rows), ["agent:alpha", "workspace:alpha", "entry:docs", "entry:docs/plan.md"]);
    });
    it("matches agent names on their own and hides agents with nothing to show", () => {
      const rows = flattenTree({ agents: [A, B], nodes, expanded: new Set(), query: "bet", collator });
      assert.deepEqual(kinds(rows), ["agent:beta"]);
    });
    it("never lists an unloaded directory to search it", () => {
      const rows = flattenTree({ agents: [A], nodes, expanded: new Set(), query: "zzz-not-here", collator });
      assert.deepEqual(kinds(rows), []);
    });
    it("a directory whose own name matches shows even when its subtree is unloaded", () => {
      const rows = flattenTree({ agents: [A], nodes, expanded: new Set(), query: "unload", collator });
      assert.deepEqual(kinds(rows), ["agent:alpha", "workspace:alpha", "entry:unloaded"]);
    });
  });
});

describe("listingsToHydrate", () => {
  it("only lists what the expansion would actually render", () => {
    const expanded = new Set([
      agentRowKey("alpha"),
      workspaceRowKey("alpha"),
      nodeKey("alpha", "docs"),
      nodeKey("alpha", "docs/deep"),
      // beta: workspace expanded but agent collapsed → nothing renders
      workspaceRowKey("beta"),
      nodeKey("beta", "src"),
      // gamma is not in the roster any more
      agentRowKey("gamma"),
      workspaceRowKey("gamma"),
      nodeKey("gamma", "x"),
    ]);
    assert.deepEqual(listingsToHydrate([A, B], expanded).sort(), [nodeKey("alpha", ""), nodeKey("alpha", "docs"), nodeKey("alpha", "docs/deep")].sort());
  });
});

describe("pruneNodes", () => {
  it("returns the same object under the cap", () => {
    const nodes: NodeMap = { a: listing([], 1), b: listing([], 2) };
    assert.equal(pruneNodes(nodes, new Set(), 5), nodes);
  });
  it("evicts least-recently-touched, never pinned, never loading", () => {
    const nodes: NodeMap = {
      old: listing([], 1),
      pinned: listing([], 2),
      busy: { loading: true, touched: 3 },
      mid: listing([], 4),
      fresh: listing([], 5),
    };
    const next = pruneNodes(nodes, new Set(["pinned"]), 3);
    assert.deepEqual(Object.keys(next).sort(), ["busy", "fresh", "pinned"]);
  });
  it("leaves the map alone when everything over the cap is pinned", () => {
    const nodes: NodeMap = { a: listing([], 1), b: listing([], 2), c: listing([], 3) };
    assert.equal(pruneNodes(nodes, new Set(["a", "b", "c"]), 2), nodes);
  });
  it("clock is monotonic", () => {
    const clock = makeClock();
    assert.ok(clock() < clock());
  });
});

describe("lruSet", () => {
  it("bounds the map and bumps re-inserted keys to newest", () => {
    const m = new Map<string, number>();
    lruSet(m, "a", 1, 2);
    lruSet(m, "b", 2, 2);
    lruSet(m, "a", 3, 2); // bump a
    lruSet(m, "c", 4, 2); // evicts b (oldest now)
    assert.deepEqual([...m.entries()], [["a", 3], ["c", 4]]);
  });
});

describe("FetchQueue", () => {
  function deferredTask() {
    let resolve!: () => void;
    let reject!: (e: unknown) => void;
    const started: AbortSignal[] = [];
    const done = new Promise<void>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    const task = (signal: AbortSignal) => {
      started.push(signal);
      signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
      return done;
    };
    return { task, started, resolve, reject };
  }

  it("runs at most `concurrency` tasks and the rest in FIFO order", async () => {
    const q = new FetchQueue(2);
    const t = [deferredTask(), deferredTask(), deferredTask(), deferredTask()];
    const promises = t.map((x, i) => q.enqueue(`k${i}`, x.task));
    assert.equal(q.inFlight, 2);
    assert.equal(q.pending, 2);
    assert.deepEqual(t.map((x) => x.started.length), [1, 1, 0, 0]);
    t[0].resolve();
    await promises[0];
    assert.deepEqual(t.map((x) => x.started.length), [1, 1, 1, 0]);
    t[1].resolve();
    t[2].resolve();
    t[3].resolve();
    await Promise.all(promises);
    assert.equal(q.inFlight, 0);
    assert.equal(q.pending, 0);
  });

  it("single-flights a key that is already queued or in flight", async () => {
    const q = new FetchQueue(1);
    const a = deferredTask();
    const b = deferredTask();
    const p1 = q.enqueue("same", a.task);
    const p2 = q.enqueue("same", b.task);
    assert.equal(p1, p2);
    assert.equal(b.started.length, 0);
    a.resolve();
    await p1;
    assert.equal(b.started.length, 0, "the duplicate never ran");
  });

  it("cancel drops a waiting task and aborts an in-flight one", async () => {
    const q = new FetchQueue(1);
    const running = deferredTask();
    const waiting = deferredTask();
    const p1 = q.enqueue("run", running.task);
    const p2 = q.enqueue("wait", waiting.task);
    q.cancel("wait");
    assert.equal(q.pending, 0);
    await assert.rejects(p2, (e: unknown) => isAbortError(e));
    assert.equal(waiting.started.length, 0);
    q.cancel("run");
    assert.ok(running.started[0].aborted);
    await assert.rejects(p1, (e: unknown) => isAbortError(e));
    assert.equal(q.inFlight, 0);
  });

  it("a failing task does not block the queue", async () => {
    const q = new FetchQueue(1);
    const bad = deferredTask();
    const good = deferredTask();
    const p1 = q.enqueue("bad", bad.task);
    const p2 = q.enqueue("good", good.task);
    bad.reject(new Error("boom"));
    await assert.rejects(p1, /boom/);
    assert.equal(good.started.length, 1);
    good.resolve();
    await p2;
  });

  it("resolves with the task's value, shared by every waiter on the key", async () => {
    const q = new FetchQueue<number>(1);
    const p1 = q.enqueue("k", async () => 42);
    const p2 = q.enqueue("k", async () => 99);
    assert.deepEqual(await Promise.all([p1, p2]), [42, 42]);
  });

  it("cancelWhere drops only the keys the predicate accepts", async () => {
    const q = new FetchQueue(1);
    const keep = deferredTask();
    const ps = [q.enqueue("alpha\tdocs", deferredTask().task), q.enqueue("beta\tdocs", keep.task), q.enqueue("alpha\tsrc", deferredTask().task)];
    q.cancelWhere((k) => k.startsWith("alpha\t"));
    await assert.rejects(ps[0], (e: unknown) => isAbortError(e));
    await assert.rejects(ps[2], (e: unknown) => isAbortError(e));
    assert.equal(keep.started.length, 1, "the surviving key was promoted");
    keep.resolve();
    await ps[1];
  });

  it("cancelAll clears everything", async () => {
    const q = new FetchQueue(1);
    const ps = [q.enqueue("a", deferredTask().task), q.enqueue("b", deferredTask().task)];
    q.cancelAll();
    await Promise.allSettled(ps);
    assert.equal(q.inFlight, 0);
    assert.equal(q.pending, 0);
  });
});

describe("small helpers", () => {
  it("countEntries", () => {
    assert.deepEqual(countEntries([dir("a"), file("b"), file("c")]), { dirs: 1, files: 2 });
  });
  it("findByPrefix wraps and is case-insensitive", () => {
    const labels = ["docs", "README.md", "src", "Roadmap.md"];
    assert.equal(findByPrefix(labels, 1, "r"), 3);
    assert.equal(findByPrefix(labels, 3, "r"), 1);
    assert.equal(findByPrefix(labels, 0, "zz"), -1);
  });
});
