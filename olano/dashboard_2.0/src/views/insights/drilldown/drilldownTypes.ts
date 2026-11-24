// Shapes returned by /api/analytics/threadcost/* (see olano/analytics/threadcost).
//
// Every `usd` below is the BYOK book: a list-price ESTIMATE of the customer's
// own provider bill, mined from the checkpoint store. An Olano-managed call
// prices to 0 there by design (analytics/threadcost/cost.py — the box is never
// told the gateway's rate), so these figures never convert into credits; the
// UI renders them with fmtUsd. The one exception is `credits` on the agent
// rows, grafted from the usage ledger, which is the gateway's own authoritative
// charge and renders verbatim.

export interface TcAgentRow {
  agent: string;
  usd: number;
  /** Olano-managed charge for this agent in the window. Absent on payloads
   *  that predate the graft; 0 means "kept the book, spent nothing". */
  credits?: number;
  total_tokens: number;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens: number;
  call_count: number;
  /** Conversations of this agent the checkpoint store can still open. This and
   *  `inspectable_usd` are the only mined figures on the row — every money
   *  figure above comes from the per-call usage ledger, same as the Overview. */
  thread_count: number;
  /** How much of `usd` those openable conversations account for. Below `usd`
   *  when compaction has dropped calls out of the stored history. */
  inspectable_usd?: number;
  pricing_complete: number;
  interactive_usd?: number;
  autonomous_usd?: number;
}

export interface TcIndexStatus {
  total: number;
  stale: number;
  refreshed: number;
  remaining: number;
}

export interface TcAgentsResp {
  agents: TcAgentRow[];
  totals: {
    usd: number;
    credits?: number;
    total_tokens: number;
    thread_count: number;
    call_count: number;
    inspectable_usd?: number;
  };
  index_status: TcIndexStatus;
}

export interface TcThreadRow {
  thread_id: string;
  agent: string;
  origin: string;
  usd: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  call_count: number;
  subagent_call_count: number;
  pricing_complete: number;
  namespaces: number;
  name: string;
  preview: string;
  updated_at: string;
}

export interface TcThreadsResp {
  threads: TcThreadRow[];
  total: number;
}

export interface TcToolCallRef {
  name: string;
  subagent_type: string | null;
}

export interface TcCall {
  seq: number;
  ns: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens: number;
  usd: number;
  pricing_known: boolean;
  tool_calls: TcToolCallRef[];
  ts: string | null;
}

export interface TcTreeNode {
  kind: "turn" | "ai_call" | "tool" | "subagent";
  label: string;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usd: number;
  seq: number | null;
  ns: string | null;
  children: TcTreeNode[];
}

export interface TcToolRow {
  tool: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usd: number;
  subagent_internal_usd: number;
}

export interface TcThreadCalls {
  thread_id: string;
  agent: string;
  origin: string;
  totals: {
    usd: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cache_read_tokens: number;
    call_count: number;
  };
  calls: TcCall[];
  turn_tree: TcTreeNode[];
  tools: TcToolRow[];
}

export interface TcToolsResp {
  tools: TcToolRow[];
  totals: { usd: number; threads_scanned: number; capped: boolean };
  /** Threads the rollup refused to read because their pending checkpoint
   * history is over the miner's cap (absent on a runtime that predates it). */
  skipped?: { thread_id: string; reason: string; bytes: number | null; cap_bytes: number }[];
}

export interface TcRawMsg {
  role: string;
  name: string;
  content: string;
  truncated: boolean;
  full_len: number;
}

export interface TcRaw {
  thread_id: string;
  ns: string;
  seq: number;
  found: boolean;
  model?: string;
  omitted_context?: number;
  request_messages?: TcRawMsg[];
  response?: { content: string; truncated: boolean; full_len: number; tool_calls: { name: string; args: string }[] };
  usage?: { input_tokens: number; output_tokens: number; cache_read_tokens: number };
  usd?: number;
}

export interface TcOriginBucket {
  usd: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  /** Mined: openable conversations. `usd`/`call_count` are ledger-sourced. */
  thread_count: number;
  call_count: number;
}

export interface TcOrigins {
  split: { origin: string; usd: number; total_tokens: number; thread_count: number; call_count: number }[];
  interactive: TcOriginBucket;
  non_interactive: TcOriginBucket;
}
