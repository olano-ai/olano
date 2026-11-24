// Pure A2A conversation model — no React, no fetching.
//
// The runtime emits two frames per exchange: "send" opens it, "reply" closes
// it. This module folds that stream into the two shapes the view renders:
//
//   PairThread  — one row per *conversation* (the rail)
//   Exchange    — one ask→answer pair (the transcript)
//
// Every key here mirrors `runtime/api.py` (`_a2a_thread_key`, `_a2a_join_key`)
// so a live SSE frame lands on the same conversation the server already
// assigned it to. Keep the two in step.

// ── wire types ────────────────────────────────────────────────────────────
// peer / subagent are Olano agents talking to each other. inbound is an
// OUTSIDE caller reaching an agent over MCP or A2A (`from` = "mcp:<label>" /
// "a2a:<label>", the label being the token or OAuth client it used); remote is
// an agent calling an external A2A agent (`to` = "a2a:<name>"). The rail's
// "External" segment is the last two together.
export type ThreadKind = "peer" | "subagent" | "inbound" | "remote";
export type Protocol = "mcp" | "a2a";

export interface A2AEventData {
  kind?: ThreadKind;
  protocol?: string;
  thread_id?: string;
  phase?: "send" | "reply";
  from?: string;
  to?: string;
  message?: string;
  reply?: string;
  ts?: string;
  ts_sent?: string;
  ts_reply?: string;
  tool_call_id?: string;
}

export interface A2AEvent {
  type?: string;
  data?: A2AEventData;
  ts?: string;
}

/** A conversation row, as served by GET /api/a2a/threads. */
export interface PairThread {
  key: string;
  kind: ThreadKind;
  /** "mcp" | "a2a" for an external conversation, "" otherwise. */
  protocol?: string;
  /** The agent-side transcript thread an inbound call ran on (deep link). */
  thread_id?: string;
  a: string;
  b: string;
  messages: number;
  first_ts: string;
  last_ts: string;
  last_preview: string;
  in_flight: number;
  no_reply: number;
  avg_ms: number | null;
}

/** "pending" is still working; "no_reply" aged out — see STALE_MS. */
export type ExchangeStatus = "ok" | "pending" | "no_reply";

export interface Exchange {
  id: string;
  from: string;
  to: string;
  kind: ThreadKind;
  protocol?: string;
  message: string;
  reply: string;
  ts_sent: string;
  ts_reply?: string;
  elapsedMs?: number;
  status: ExchangeStatus;
}

// A send unanswered this long stopped being "thinking": a failed
// talk_to_agent returns an error string to its caller and publishes no reply
// frame at all, so the exchange would otherwise show typing dots forever.
// Matches _A2A_STALE_SECONDS in runtime/api.py.
export const STALE_MS = 15 * 60_000;
export const PREVIEW_CHARS = 160;

// ── keys ──────────────────────────────────────────────────────────────────

/** Mirrors `analytics/a2a_log.norm_agent`. */
export function normName(name: string | undefined): string {
  return (name || "agent").trim().toLowerCase();
}

export function kindOf(d: A2AEventData): ThreadKind {
  return d.kind === "subagent" || d.kind === "inbound" || d.kind === "remote" ? d.kind : "peer";
}

export function isExternalKind(kind: string | undefined): boolean {
  return kind === "inbound" || kind === "remote";
}

/**
 * The outside party of an external exchange, split into the protocol and the
 * human label: "mcp:Claude desktop — Ana" → { protocol: "mcp", label: "Claude
 * desktop — Ana" }. Null for one of our agents.
 */
export function externalParty(name: string | undefined): { protocol: Protocol; label: string } | null {
  const m = /^(mcp|a2a):(.*)$/i.exec((name || "").trim());
  if (!m) return null;
  const protocol = m[1].toLowerCase() as Protocol;
  return { protocol, label: m[2].trim() || protocol.toUpperCase() };
}

/** New frames say which protocol; older ones are read off the party's prefix. */
export function protocolOf(d: A2AEventData): string {
  const p = (d.protocol || "").toLowerCase();
  if (p === "mcp" || p === "a2a") return p;
  return externalParty(d.from)?.protocol || externalParty(d.to)?.protocol || "";
}

/**
 * Stable conversation key. Peer exchanges fold both directions into one
 * conversation; subagent delegations stay directional because `to` is a
 * subagent *type*, not an agent (two parents delegating to `researcher` are
 * two different conversations).
 */
export function pairKey(d: A2AEventData): string {
  const kind = kindOf(d);
  const from = normName(d.from);
  const to = normName(d.to);
  // Subagent and external exchanges are directional — the other side is a
  // subagent type, a caller or a callee, never a peer that can call back.
  if (kind !== "peer") return `${kind}:${from}::${to}`;
  const [lo, hi] = from <= to ? [from, to] : [to, from];
  return `peer:${lo}::${hi}`;
}

/**
 * Key pairing a "reply" frame back to its "send". Subagent frames carry a
 * tool_call_id; peer frames don't — but the peer reply re-sends `message`
 * verbatim, so (from, to, message) joins the halves. Matching on "most recent
 * open send to this recipient" instead cross-wires concurrent exchanges: with
 * marcus→ana and kevin→ana both open, ana's first reply closes the wrong one.
 */
export function joinKey(d: A2AEventData): string {
  if (d.tool_call_id) return `id:${d.tool_call_id}`;
  return `msg:${normName(d.from)}|${normName(d.to)}|${d.message || ""}`;
}

/** Display timestamp — when the exchange actually happened. */
export function eventTs(ev: A2AEvent): string {
  const d = ev.data || {};
  return d.ts_reply || d.ts || d.ts_sent || ev.ts || "";
}

function eventData(ev: A2AEvent): A2AEventData | null {
  if (ev.type && ev.type !== "agent_message") return null;
  // History and live frames are both enveloped, but tolerate a bare payload.
  return ev.data ?? (ev as A2AEventData);
}

// ── transcript ────────────────────────────────────────────────────────────

/** Fold a chronological event list into ask→answer exchanges. */
export function foldExchanges(events: A2AEvent[], nowMs: number): Exchange[] {
  const out: Exchange[] = [];
  // Indices into `out` of still-open sends, stacked per join key so repeated
  // identical messages each get closed by exactly one reply.
  const open = new Map<string, number[]>();

  events.forEach((ev, i) => {
    const d = eventData(ev);
    if (!d) return;
    const kind = kindOf(d);
    const join = joinKey(d);

    if (d.phase === "reply") {
      const stack = open.get(join);
      const idx = stack && stack.length ? stack.pop() : undefined;
      const elapsed = duration(d.ts_sent, d.ts_reply);
      if (idx !== undefined) {
        out[idx] = {
          ...out[idx],
          reply: d.reply || "",
          ts_reply: d.ts_reply,
          elapsedMs: elapsed,
          status: "ok",
        };
        return;
      }
      // The matching send predates this page — render the reply on its own
      // rather than dropping it.
      out.push({
        id: d.tool_call_id || `${join}:${d.ts_reply || i}`,
        from: d.from || "agent",
        to: d.to || "agent",
        kind,
        protocol: protocolOf(d),
        message: d.message || "",
        reply: d.reply || "",
        ts_sent: d.ts_sent || d.ts || ev.ts || "",
        ts_reply: d.ts_reply,
        elapsedMs: elapsed,
        status: "ok",
      });
      return;
    }

    const ts = d.ts || d.ts_sent || ev.ts || "";
    out.push({
      id: d.tool_call_id || `${join}:${ts}:${i}`,
      from: d.from || "agent",
      to: d.to || "agent",
      kind,
      protocol: protocolOf(d),
      message: d.message || "",
      reply: "",
      ts_sent: ts,
      status: "pending",
    });
    const stack = open.get(join);
    if (stack) stack.push(out.length - 1);
    else open.set(join, [out.length - 1]);
  });

  for (let i = 0; i < out.length; i++) {
    if (out[i].status !== "pending") continue;
    const started = Date.parse(out[i].ts_sent);
    if (Number.isFinite(started) && nowMs - started > STALE_MS) {
      out[i] = { ...out[i], status: "no_reply" };
    }
  }
  // Order by when the exchange *started*. The log is append-ordered by reply
  // time, so a slow call can land after exchanges that began later than it —
  // which would make the day separators jump back and forth.
  return out.sort((a, b) => (a.ts_sent < b.ts_sent ? -1 : a.ts_sent > b.ts_sent ? 1 : 0));
}

function duration(sent?: string, replied?: string): number | undefined {
  if (!sent || !replied) return undefined;
  const ms = Date.parse(replied) - Date.parse(sent);
  return Number.isFinite(ms) && ms >= 0 ? ms : undefined;
}

// ── rail ──────────────────────────────────────────────────────────────────

/**
 * Client-side rollup. Only used as the fallback when GET /api/a2a/threads is
 * unavailable (older runtime) — normally the server folds, over a much larger
 * scan window than the client ever holds.
 */
export function foldThreads(events: A2AEvent[]): PairThread[] {
  const rows = new Map<string, PairThread>();
  const open = new Map<string, string[]>();
  const durations = new Map<string, number[]>();

  for (const ev of events) {
    const d = eventData(ev);
    if (!d) continue;
    const key = pairKey(d);
    const ts = eventTs(ev);
    let row = rows.get(key);
    if (!row) {
      const kind = kindOf(d);
      let a = d.from || "agent";
      let b = d.to || "agent";
      if (kind === "peer" && normName(b) < normName(a)) [a, b] = [b, a];
      row = {
        key,
        kind,
        protocol: protocolOf(d),
        thread_id: d.thread_id || "",
        a,
        b,
        messages: 0,
        first_ts: ts,
        last_ts: ts,
        last_preview: "",
        in_flight: 0,
        no_reply: 0,
        avg_ms: null,
      };
      rows.set(key, row);
      open.set(key, []);
      durations.set(key, []);
    }
    if (ts) {
      if (!row.first_ts || ts < row.first_ts) row.first_ts = ts;
      if (ts > row.last_ts) row.last_ts = ts;
    }

    const pending = open.get(key)!;
    const join = joinKey(d);
    if (d.phase === "reply") {
      const idx = pending.lastIndexOf(join);
      if (idx >= 0) pending.splice(idx, 1);
      const ms = duration(d.ts_sent, d.ts_reply);
      if (ms !== undefined) durations.get(key)!.push(ms);
      const preview = (d.reply || d.message || "").trim();
      if (preview) row.last_preview = preview.slice(0, PREVIEW_CHARS);
    } else {
      row.messages += 1;
      pending.push(join);
      const preview = (d.message || "").trim();
      if (preview) row.last_preview = preview.slice(0, PREVIEW_CHARS);
    }
  }

  const staleBefore = new Date(Date.now() - STALE_MS).toISOString();
  for (const [key, row] of rows) {
    const pending = open.get(key)!;
    // Ages are approximate here (the fallback path has no per-send ts stack);
    // treat everything still open as in-flight only if the row itself is fresh.
    const fresh = row.last_ts >= staleBefore;
    row.in_flight = fresh ? pending.length : 0;
    row.no_reply = fresh ? 0 : pending.length;
    const samples = durations.get(key)!;
    if (samples.length) {
      row.avg_ms = Math.round(samples.reduce((a, b) => a + b, 0) / samples.length);
    }
  }
  return [...rows.values()].sort((x, y) => (x.last_ts < y.last_ts ? 1 : -1));
}

/** Patch a live frame into the rail's rollup without refetching. */
export function applyThreadFrame(threads: PairThread[], ev: A2AEvent): PairThread[] {
  const d = eventData(ev);
  if (!d) return threads;
  const key = pairKey(d);
  const ts = eventTs(ev) || new Date().toISOString();
  const next = threads.slice();
  let i = next.findIndex((t) => t.key === key);
  if (i === -1) {
    const kind = kindOf(d);
    let a = d.from || "agent";
    let b = d.to || "agent";
    if (kind === "peer" && normName(b) < normName(a)) [a, b] = [b, a];
    next.unshift({
      key,
      kind,
      protocol: protocolOf(d),
      thread_id: d.thread_id || "",
      a,
      b,
      messages: 0,
      first_ts: ts,
      last_ts: ts,
      last_preview: "",
      in_flight: 0,
      no_reply: 0,
      avg_ms: null,
    });
    i = 0;
  }
  const row = { ...next[i], last_ts: ts, thread_id: d.thread_id || next[i].thread_id };
  if (d.phase === "reply") {
    row.in_flight = Math.max(0, row.in_flight - 1);
    const preview = (d.reply || d.message || "").trim();
    if (preview) row.last_preview = preview.slice(0, PREVIEW_CHARS);
  } else {
    row.messages += 1;
    row.in_flight += 1;
    const preview = (d.message || "").trim();
    if (preview) row.last_preview = preview.slice(0, PREVIEW_CHARS);
  }
  next[i] = row;
  return next.sort((x, y) => (x.last_ts < y.last_ts ? 1 : x.last_ts > y.last_ts ? -1 : 0));
}

// ── day grouping ──────────────────────────────────────────────────────────

/**
 * Local-day bucket key. Deliberately *not* insights/helpers.fmtDayLabel: that
 * formats in UTC (correct for Insights' UTC day buckets), but the transcript
 * prints local clock times, so a 9pm message would file under tomorrow.
 */
export function localDayKey(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * Group exchanges into day buckets, preserving chronological order.
 *
 * `key` is NOT the day: a day can open more than one run (an exchange whose
 * clock is skewed, or an unsorted log), and reusing the day as a React key
 * duplicates it among siblings — which silently breaks reconciliation and
 * leaves stale cards mounted. Keyed by the first exchange in the run instead.
 */
export function groupByDay(exchanges: Exchange[]): { key: string; day: string; items: Exchange[] }[] {
  const out: { key: string; day: string; items: Exchange[] }[] = [];
  for (const ex of exchanges) {
    const day = localDayKey(ex.ts_sent);
    const last = out[out.length - 1];
    if (last && last.day === day) last.items.push(ex);
    else out.push({ key: `${day}#${ex.id}`, day, items: [ex] });
  }
  return out;
}
