// The Files view's tree MODEL — pure, React-free, unit-tested on its own
// (`npm run test:unit`). Everything here is data in, data out: node keys,
// the flat row list the windowed renderer paints, sort/filter, the bounded
// listing cache and the request queue. The component owns state and effects;
// this module owns the invariants that matter at scale — twenty agents, a
// 50,000-entry directory, a session that opens a thousand files.
//
// Shape of the tree (every level lazy, one listing request per directory):
//
//   agent            ← static, from the roster; expandable
//   └─ workspace     ← static; expanding it lists the workspace root
//      ├─ dir/       ← expanding it lists that directory
//      └─ file
//
// A node is identified by (agent, workspace-relative path). The workspace
// root is path "". Agent rows have no listing of their own.

import type { Agent } from "../../core/types";

// ── identity ─────────────────────────────────────────────────────────────

/** Opaque node id: `<agent>\t<path>`. A tab can't appear in an agent handle
 *  (lowercase alnum + hyphen, minted server-side) and does not appear in any
 *  file name a person would create, so the split is unambiguous. */
export type NodeKey = string;

const KEY_SEP = "\t";

export function nodeKey(agent: string, path: string): NodeKey {
  return `${agent}${KEY_SEP}${path}`;
}

export function splitKey(key: NodeKey): { agent: string; path: string } {
  const i = key.indexOf(KEY_SEP);
  return i === -1 ? { agent: key, path: "" } : { agent: key.slice(0, i), path: key.slice(i + 1) };
}

/** Parent directory of a workspace-relative path ("" for top-level entries). */
export function parentOf(path: string): string {
  const i = path.lastIndexOf("/");
  return i === -1 ? "" : path.slice(0, i);
}

/** True when `descendant` is `ancestor` or lives somewhere below it. */
export function isWithin(descendant: string, ancestor: string): boolean {
  return ancestor === "" || descendant === ancestor || descendant.startsWith(`${ancestor}/`);
}

/** Every ancestor directory of a path, root-most first, root ("") excluded. */
export function ancestorsOf(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/"));
}

// ── server shapes ────────────────────────────────────────────────────────

export interface FileEntry {
  name: string;
  path: string;
  /** Absolute on-disk path, returned by the listing for "copy full path". */
  abs_path?: string;
  type: "file" | "directory";
  size: number;
  modified: string;
  /** Directory size from the daily disk scan (absent = not scanned / too deep). */
  scan_bytes?: number;
  scan_files?: number;
}

/** One directory's listing as the tree keeps it. `truncated`/`total` come from
 *  the `?limit=` envelope; `stale` marks a listing a live update invalidated
 *  while its node was collapsed — shown as-is, re-fetched on next expand. */
export interface Listing {
  entries: FileEntry[];
  truncated: boolean;
  total: number;
  stale?: boolean;
}

export interface NodeState {
  listing?: Listing;
  loading?: boolean;
  error?: string;
  /** Monotonic touch stamp for LRU eviction (see `pruneNodes`). */
  touched: number;
}

export type NodeMap = Readonly<Record<NodeKey, NodeState>>;

// ── sorting ──────────────────────────────────────────────────────────────

/** One collator for the whole view: natural order ("file2" before "file10"),
 *  case-insensitive, accent-insensitive — what every OS file manager does. */
export function makeCollator(): Intl.Collator {
  return new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
}

/** Directories first, then natural order. Returns a new array. */
export function sortEntries(list: readonly FileEntry[], collator: Intl.Collator): FileEntry[] {
  return [...list].sort((a, b) =>
    a.type === b.type ? collator.compare(a.name, b.name) : a.type === "directory" ? -1 : 1,
  );
}

// ── rows: the flat list the renderer paints ──────────────────────────────

/** What a tree needs to know about an agent — a projection of the roster
 *  row, so the model never depends on the full `Agent` type's churn. */
export interface AgentInfo {
  name: string;
  label: string;
  emoji?: string;
  status: string;
}

export function agentInfo(a: Agent, status: string): AgentInfo {
  const label = (a.display_name || "").trim() || a.name;
  return { name: a.name, label, emoji: a.emoji, status };
}

interface RowBase {
  key: NodeKey;
  agent: string;
  depth: number;
}

/** A match the server found by name (`GET /api/files/search`) in a workspace
 *  the browser has not necessarily listed. Rendered as a `hit` row under its
 *  agent when no loaded listing already shows the same path. */
export interface SearchHit {
  agent: string;
  path: string;
  name: string;
  type: "file" | "directory";
}

export type Row =
  | (RowBase & { kind: "agent"; info: AgentInfo; expanded: boolean })
  | (RowBase & { kind: "workspace"; expanded: boolean; loading: boolean })
  | (RowBase & { kind: "entry"; entry: FileEntry; expanded: boolean; loading: boolean })
  | (RowBase & { kind: "hit"; hit: SearchHit })
  | (RowBase & { kind: "loading" })
  | (RowBase & { kind: "error"; error: string })
  | (RowBase & { kind: "truncated"; shown: number; total: number })
  | (RowBase & { kind: "empty" });

export interface FlattenInput {
  agents: readonly AgentInfo[];
  nodes: NodeMap;
  expanded: ReadonlySet<NodeKey>;
  /** Lower-cased, trimmed filter text; "" = no filter. */
  query: string;
  collator: Intl.Collator;
  /** Server-side name matches for `query` (see `SearchHit`). Only read while a
   *  query is active; each hit renders once, and never when a loaded listing
   *  already shows that path as an entry row. */
  hits?: readonly SearchHit[];
}

/** True if any LOADED descendant of `key` matches the query. The filter only
 *  ever looks at what the browser already has — it never lists directories to
 *  search them, which is what keeps typing in the box free of requests. */
function subtreeMatches(key: NodeKey, nodes: NodeMap, q: string): boolean {
  const listing = nodes[key]?.listing;
  if (!listing) return false;
  const { agent } = splitKey(key);
  return listing.entries.some(
    (e) =>
      e.name.toLowerCase().includes(q) ||
      (e.type === "directory" && subtreeMatches(nodeKey(agent, e.path), nodes, q)),
  );
}

/**
 * Flatten the tree into the rows currently visible, in paint order. O(visible
 * rows): collapsed subtrees are never visited. With a filter active, a
 * directory counts as open when it is loaded and holds a match (so matches
 * stay reachable), and agents/workspaces whose name or loaded subtree match
 * nothing drop out — except that with NO loaded subtree at all an agent row
 * still shows when its own name matches.
 *
 * This function never lists anything to search it. Matches in directories the
 * browser has not loaded arrive as `hits` (the server's name search) and are
 * appended under their agent's workspace row, after whatever the loaded tree
 * shows, each once — a hit whose path is already on screen as an entry row is
 * skipped, so a listing that lands mid-search never doubles a match.
 */
export function flattenTree(input: FlattenInput): Row[] {
  const { agents, nodes, expanded, query, collator, hits } = input;
  const q = query;
  const rows: Row[] = [];
  // Entry keys emitted so far, for hit de-duplication. Only collected while
  // there are hits to de-duplicate against.
  const emitted = q && hits?.length ? new Set<NodeKey>() : null;
  const hitsByAgent = new Map<string, SearchHit[]>();
  if (q && hits) {
    for (const h of hits) {
      const list = hitsByAgent.get(h.agent);
      if (list) list.push(h);
      else hitsByAgent.set(h.agent, [h]);
    }
  }

  const pushDirChildren = (agent: string, dirPath: string, depth: number) => {
    const key = nodeKey(agent, dirPath);
    const node = nodes[key];
    if (!node?.listing) {
      if (node?.loading) rows.push({ kind: "loading", key, agent, depth });
      else if (node?.error) rows.push({ kind: "error", key, agent, depth, error: node.error });
      return;
    }
    const listing = node.listing;
    let entries = listing.entries;
    if (q) {
      entries = entries.filter((e) =>
        e.type === "directory"
          ? e.name.toLowerCase().includes(q) || subtreeMatches(nodeKey(agent, e.path), nodes, q)
          : e.name.toLowerCase().includes(q),
      );
    }
    entries = sortEntries(entries, collator);
    if (entries.length === 0 && !listing.truncated) {
      rows.push({ kind: "empty", key, agent, depth });
      return;
    }
    for (const entry of entries) {
      const ekey = nodeKey(agent, entry.path);
      const enode = nodes[ekey];
      const isDir = entry.type === "directory";
      const open = isDir && (q ? !!enode?.listing : expanded.has(ekey));
      emitted?.add(ekey);
      rows.push({
        kind: "entry",
        key: ekey,
        agent,
        depth,
        entry,
        expanded: open,
        loading: !!enode?.loading,
      });
      if (open) pushDirChildren(agent, entry.path, depth + 1);
    }
    if (listing.truncated && !q) {
      rows.push({
        kind: "truncated",
        key,
        agent,
        depth,
        shown: listing.entries.length,
        total: listing.total,
      });
    }
  };

  for (const info of agents) {
    const wkey = nodeKey(info.name, ""); // the workspace row's listing is the root listing
    const rootNode = nodes[wkey];
    const nameMatch = q ? info.label.toLowerCase().includes(q) || info.name.toLowerCase().includes(q) : true;
    const treeMatch = q ? subtreeMatches(wkey, nodes, q) : false;
    const agentHits = q ? hitsByAgent.get(info.name) : undefined;
    const hitMatch = !!agentHits?.length;
    if (q && !nameMatch && !treeMatch && !hitMatch) continue;

    // Agent + workspace rows are keyed apart from the root listing by kind so
    // the expanded set can hold them separately: `agent:` / `ws:` prefixes.
    const agentExpanded = (q && (treeMatch || hitMatch)) || expanded.has(agentRowKey(info.name));
    rows.push({
      kind: "agent",
      key: agentRowKey(info.name),
      agent: info.name,
      depth: 0,
      info,
      expanded: agentExpanded,
    });
    if (!agentExpanded) continue;

    const wsExpanded = (q && (treeMatch || hitMatch)) || expanded.has(workspaceRowKey(info.name));
    rows.push({
      kind: "workspace",
      key: workspaceRowKey(info.name),
      agent: info.name,
      depth: 1,
      expanded: wsExpanded,
      loading: !!rootNode?.loading,
    });
    if (wsExpanded) {
      // With hits, an unloaded root must not render its "empty"/nothing state —
      // the hits are the content. A loaded root renders as usual, then the
      // hits it does not already show follow.
      if (rootNode?.listing || !hitMatch) pushDirChildren(info.name, "", 2);
      if (agentHits?.length) {
        const fresh = agentHits.filter((h) => !emitted?.has(nodeKey(info.name, h.path)));
        fresh.sort((a, b) => collator.compare(a.path, b.path));
        for (const h of fresh) {
          rows.push({ kind: "hit", key: nodeKey(info.name, h.path), agent: info.name, depth: 2, hit: h });
        }
      }
    }
  }
  return rows;
}

/** Keys for the two synthetic rows above a workspace's root listing. They live
 *  in the same `expanded` set as directories but never collide with a listing
 *  key: listing keys are `<agent>\t<path>`, these carry a kind prefix. */
export function agentRowKey(agent: string): NodeKey {
  return `agent:${agent}`;
}
export function workspaceRowKey(agent: string): NodeKey {
  return `ws:${agent}`;
}
export function isSyntheticKey(key: NodeKey): boolean {
  return key.startsWith("agent:") || key.startsWith("ws:");
}

/** The listing keys (agent, dirPath) that must be loaded for the current
 *  expansion to render — the re-hydration set. Root listings for expanded
 *  workspaces plus every expanded directory whose agent+workspace are open.
 *  Directories under a collapsed workspace are NOT included: they'd render
 *  nothing, so fetching them is waste. */
export function listingsToHydrate(
  agents: readonly AgentInfo[],
  expanded: ReadonlySet<NodeKey>,
): NodeKey[] {
  const out: NodeKey[] = [];
  const openAgents = new Set<string>();
  for (const a of agents) {
    if (expanded.has(agentRowKey(a.name)) && expanded.has(workspaceRowKey(a.name))) {
      openAgents.add(a.name);
      out.push(nodeKey(a.name, ""));
    }
  }
  for (const key of expanded) {
    if (isSyntheticKey(key)) continue;
    const { agent, path } = splitKey(key);
    if (!openAgents.has(agent) || path === "") continue;
    out.push(key);
  }
  return out;
}

// ── bounded listing cache ────────────────────────────────────────────────

/**
 * Keep the node map under `cap` listings. Pinned keys (everything currently
 * expanded, plus whatever the caller names) are never evicted; the rest go
 * least-recently-touched first. Returns the SAME object when nothing is
 * evicted so a React state setter can bail out of a re-render.
 */
export function pruneNodes(nodes: NodeMap, pinned: ReadonlySet<NodeKey>, cap: number): NodeMap {
  const keys = Object.keys(nodes);
  if (keys.length <= cap) return nodes;
  const evictable = keys
    .filter((k) => !pinned.has(k) && !nodes[k].loading)
    .sort((a, b) => nodes[a].touched - nodes[b].touched);
  const excess = keys.length - cap;
  if (excess <= 0 || evictable.length === 0) return nodes;
  const drop = new Set(evictable.slice(0, excess));
  const next: Record<NodeKey, NodeState> = {};
  for (const k of keys) if (!drop.has(k)) next[k] = nodes[k];
  return next;
}

/** Monotonic stamp source for `NodeState.touched`. */
export function makeClock(): () => number {
  let n = 0;
  return () => ++n;
}

// ── bounded per-file preferences ─────────────────────────────────────────

/**
 * Insert-or-bump into an insertion-ordered Map, dropping the oldest entries
 * past `cap`. Used for the per-file bits the view remembers across tab
 * switches (draft text, Preview/Raw choice, scroll offset) — previously one
 * unbounded key per file ever opened, for the life of the SPA session.
 */
export function lruSet<K, V>(map: Map<K, V>, key: K, value: V, cap: number): void {
  if (map.has(key)) map.delete(key);
  map.set(key, value);
  while (map.size > cap) {
    const oldest = map.keys().next().value as K;
    map.delete(oldest);
  }
}

// ── request queue ────────────────────────────────────────────────────────

type Task<T> = (signal: AbortSignal) => Promise<T>;

interface Pending<T> {
  key: NodeKey;
  task: Task<T>;
  controller: AbortController;
  resolve: (value: T) => void;
  reject: (e: unknown) => void;
  promise: Promise<T>;
}

/**
 * Single-flight, concurrency-limited, cancellable FIFO for directory
 * listings. Re-hydrating a persisted expansion across many agents used to
 * fire every listing at once — against a runtime budget of 240 requests a
 * minute, a tree open a few levels deep across a fleet becomes 429s and a
 * "Connection lost" banner. Here at most `concurrency` run; the rest wait in
 * order. Enqueueing a key already queued or in flight returns that promise
 * (and its value — the caller that awaits it gets the listing directly,
 * rather than reading state React has not re-rendered yet). `cancel(key)`
 * aborts an in-flight request or drops a waiting one; the promise then
 * rejects with an AbortError-shaped error the caller ignores.
 */
export class FetchQueue<T = void> {
  private readonly concurrency: number;
  private readonly waiting: Pending<T>[] = [];
  private readonly active = new Map<NodeKey, Pending<T>>();
  private readonly queued = new Map<NodeKey, Pending<T>>();

  constructor(concurrency: number) {
    this.concurrency = Math.max(1, concurrency);
  }

  get inFlight(): number {
    return this.active.size;
  }

  get pending(): number {
    return this.waiting.length;
  }

  has(key: NodeKey): boolean {
    return this.active.has(key) || this.queued.has(key);
  }

  enqueue(key: NodeKey, task: Task<T>): Promise<T> {
    const existing = this.active.get(key) ?? this.queued.get(key);
    if (existing) return existing.promise;
    let resolve!: (value: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    // A cancelled listing is not an error anyone needs to hear about twice:
    // the caller that awaits it handles the rejection, nobody else should see
    // an "unhandled rejection" for it.
    promise.catch(() => undefined);
    const item: Pending<T> = { key, task, controller: new AbortController(), resolve, reject, promise };
    this.waiting.push(item);
    this.queued.set(key, item);
    this.pump();
    return promise;
  }

  cancel(key: NodeKey): void {
    const queued = this.queued.get(key);
    if (queued) {
      this.queued.delete(key);
      const i = this.waiting.indexOf(queued);
      if (i !== -1) this.waiting.splice(i, 1);
      queued.controller.abort();
      queued.reject(abortError());
      return;
    }
    const active = this.active.get(key);
    if (active) active.controller.abort(); // settles through the task's own rejection
  }

  cancelAll(): void {
    this.cancelWhere(() => true);
  }

  /** Cancel every queued or in-flight key the predicate accepts — a collapsed
   *  subtree's pending listings, an agent that left the roster. */
  cancelWhere(predicate: (key: NodeKey) => boolean): void {
    for (const key of [...this.queued.keys(), ...this.active.keys()]) {
      if (predicate(key)) this.cancel(key);
    }
  }

  private pump(): void {
    while (this.active.size < this.concurrency && this.waiting.length > 0) {
      const item = this.waiting.shift()!;
      this.queued.delete(item.key);
      this.active.set(item.key, item);
      // Free the slot and start the next task BEFORE settling this one, so a
      // caller resuming from `await enqueue(...)` already sees the successor
      // in flight (a `.finally` after `.then(resolve)` runs one microtask
      // later than the awaiting caller does).
      const settle = (fn: () => void) => {
        if (this.active.get(item.key) === item) this.active.delete(item.key);
        this.pump();
        fn();
      };
      item.task(item.controller.signal).then(
        (value) => settle(() => item.resolve(value)),
        (e) => settle(() => item.reject(e)),
      );
    }
  }
}

export function abortError(): Error {
  const e = new Error("aborted");
  e.name = "AbortError";
  return e;
}

export function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
}

// ── misc pure helpers the view shares with tests ─────────────────────────

/** Byte → human string. */
export function fmtSize(bytes: number): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes < 1099511627776) return `${(bytes / 1073741824).toFixed(1)} GB`;
  return `${(bytes / 1099511627776).toFixed(1)} TB`;
}

/** Folder / file counts of a listing, for the footer. */
export function countEntries(list: readonly FileEntry[]): { dirs: number; files: number } {
  let dirs = 0;
  let files = 0;
  for (const e of list) {
    if (e.type === "directory") dirs += 1;
    else files += 1;
  }
  return { dirs, files };
}

/** True when any segment of a workspace-relative path is a dot-entry. */
export function isHiddenPath(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith("."));
}

/** Type-ahead: the next row (wrapping) whose label starts with `prefix`. */
export function findByPrefix(labels: readonly string[], from: number, prefix: string): number {
  const p = prefix.toLowerCase();
  if (!p || labels.length === 0) return -1;
  for (let step = 1; step <= labels.length; step += 1) {
    const i = (from + step) % labels.length;
    if (labels[i].toLowerCase().startsWith(p)) return i;
  }
  return -1;
}
