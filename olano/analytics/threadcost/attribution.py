"""Attribute a thread's *billed* tokens/cost to what actually drove them, and build
a per-turn execution tree.

Why this is subtle — agentic loops re-bill context. Each LLM call's ``input_tokens``
is the WHOLE conversation so far (system prompt + every prior message + every prior
tool result). So a tool result of size R that arrives at call k is **re-billed on
every later call** it stays in context. Summing per-call cost is the real spend (you
pay for that re-billing), but a naive "credit the one-time input delta to the tool"
hugely under-credits tools whose results persist — and dumps all that re-billed
context into a misleading catch-all.

**Context-lifetime attribution (this module):** we walk each namespace tracking the
*composition* of the context (how many tokens currently belong to each source), and
bill every call's (cache-discounted) input cost across that composition. New tokens
introduced between calls are split into the prior call's **output** (its tool-call /
text message → ``(model output)``) and the **tool result** (→ the tool that produced
it). Tokens present from the start (system prompt + first user message) are the
``(system + context)`` bucket. On compaction (input shrinks) the composition is
scaled down proportionally.

Properties: the breakdown **sums exactly to the thread's net (cache-discounted)
cost**; a big result that arrives early and lingers correctly dominates; a large
system prompt shows up as ``(system + context)`` (an actionable lever). It is still
an estimate — token counts are exact (from the provider), but USD comes from a local
price table and the message-boundary split is heuristic. Subagent (``task``)
downstream cost is also surfaced as ``subagent_internal_usd``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from olano.analytics.threadcost.cost import cost_for_call
from olano.analytics.threadcost.reader import CallRecord, ThreadCallSet
from olano.catalog.off_peak import parse_timestamp

# Non-tool buckets. Parenthesized so they never collide with a real tool name.
_BASE_BUCKET = "(system + context)"  # system prompt + base conversation, re-billed
_GEN_BUCKET = "(model output)"  # generated tokens (billed at output, re-billed as input)


@dataclass
class ToolAttribution:
    tool: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    subagent_internal_usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "usd": round(self.usd, 6),
            "subagent_internal_usd": round(self.subagent_internal_usd, 6),
        }


def _attribute_namespace(
    calls: list[CallRecord],
    usd_by: dict[str, float],
    in_by: dict[str, float],
    out_by: dict[str, float],
) -> None:
    """Bill each call's input cost across the live context composition.

    ``comp`` maps bucket → tokens currently in context. Output cost is billed to
    ``(model output)``; input cost is split by ``comp`` fractions so it sums exactly
    to each call's (cache-discounted) input cost.
    """
    comp: dict[str, float] = {}
    prev_input = 0
    prev_output = 0
    prev_tools: list[str] = []

    for i, call in enumerate(calls):
        inp = call.input_tokens
        at = parse_timestamp(call.ts)
        in_cost = cost_for_call(call.model, inp, 0, call.cache_read_tokens, at=at)
        out_cost = cost_for_call(call.model, 0, call.output_tokens, at=at)

        if i == 0:
            comp = {_BASE_BUCKET: float(inp)}
        elif inp >= prev_input:
            new = inp - prev_input
            # The growth is the prior call's message (≈ its output tokens) plus the
            # tool result(s) appended after it.
            out_part = min(prev_output, new)
            tool_part = new - out_part
            if out_part:
                comp[_GEN_BUCKET] = comp.get(_GEN_BUCKET, 0.0) + out_part
            if tool_part > 0 and prev_tools:
                share = tool_part / len(prev_tools)
                for name in prev_tools:
                    comp[name] = comp.get(name, 0.0) + share
            elif tool_part > 0:
                # No tool call before this growth → a new user message / base context.
                comp[_BASE_BUCKET] = comp.get(_BASE_BUCKET, 0.0) + tool_part
        elif prev_input > 0:
            # Context shrank (compaction / pruning) → scale composition down.
            scale = inp / prev_input
            comp = {b: v * scale for b, v in comp.items()}

        total = sum(comp.values()) or 1.0
        for b, v in comp.items():
            frac = v / total
            in_by[b] = in_by.get(b, 0.0) + frac * inp
            usd_by[b] = usd_by.get(b, 0.0) + frac * in_cost

        out_by[_GEN_BUCKET] = out_by.get(_GEN_BUCKET, 0.0) + call.output_tokens
        usd_by[_GEN_BUCKET] = usd_by.get(_GEN_BUCKET, 0.0) + out_cost

        prev_input = inp
        prev_output = call.output_tokens
        prev_tools = [tc.name for tc in call.tool_calls]


def attribute_tools(callset: ThreadCallSet) -> list[ToolAttribution]:
    """Per-tool token/cost attribution across all namespaces, ranked by USD.

    Sums exactly to the thread's net (cache-discounted) cost. ``(system + context)``
    = system prompt + base conversation re-billed each call; ``(model output)`` =
    generated tokens. Subagent (``task``) downstream cost → ``subagent_internal_usd``.
    """
    usd_by: dict[str, float] = {}
    in_by: dict[str, float] = {}
    out_by: dict[str, float] = {}

    _attribute_namespace(callset.parent_calls, usd_by, in_by, out_by)
    subagent_total_usd = 0.0
    for ns_calls in callset.subagent_calls.values():
        _attribute_namespace(ns_calls, usd_by, in_by, out_by)
        subagent_total_usd += sum(c.usd for c in ns_calls)

    # Tool-invocation counts (real tool calls only; the synthetic buckets have none).
    call_counts: Counter[str] = Counter()
    for c in callset.all_calls():
        for tc in c.tool_calls:
            call_counts[tc.name] += 1

    rows: list[ToolAttribution] = []
    for name in set(usd_by) | set(in_by) | set(out_by):
        rows.append(
            ToolAttribution(
                tool=name,
                calls=call_counts.get(name, 0),
                input_tokens=int(round(in_by.get(name, 0.0))),
                output_tokens=int(round(out_by.get(name, 0.0))),
                usd=usd_by.get(name, 0.0),
            )
        )

    if subagent_total_usd:
        for r in rows:
            if r.tool == "task":
                r.subagent_internal_usd = subagent_total_usd

    return sorted(rows, key=lambda b: b.usd, reverse=True)


# ─────────────────────────────── execution tree ──────────────────────────────


@dataclass
class TurnNode:
    kind: str  # "turn" | "ai_call" | "tool" | "subagent"
    label: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    seq: int | None = None
    ns: str | None = None
    children: list[TurnNode] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "usd": round(self.usd, 6),
            "seq": self.seq,
            "ns": self.ns,
            "children": [c.as_dict() for c in self.children],
        }


def _subagent_node(ns: str, calls: list[CallRecord]) -> TurnNode:
    label = "subagent"
    for c in calls:
        for tc in c.tool_calls:
            if tc.subagent_type:
                label = tc.subagent_type
                break
    node = TurnNode(kind="subagent", label=label, ns=ns)
    for c in calls:
        node.input_tokens += c.input_tokens
        node.output_tokens += c.output_tokens
        node.usd += c.usd
        node.children.append(_ai_call_node(c))
    return node


def _ai_call_node(call: CallRecord) -> TurnNode:
    node = TurnNode(
        kind="ai_call",
        label=call.model,
        model=call.model,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        usd=call.usd,
        seq=call.seq,
        ns=call.ns,
    )
    for tc in call.tool_calls:
        node.children.append(TurnNode(kind="tool", label=tc.subagent_type or tc.name, ns=call.ns))
    return node


def build_turn_tree(callset: ThreadCallSet) -> list[TurnNode]:
    """Group parent calls into turns; attach subagent namespaces as nested nodes.

    A "turn" is a run of parent AI calls (a turn boundary is heuristic — we start a
    new turn at the first call and whenever the input shrinks sharply, which marks a
    fresh user message resetting context). Subagent namespaces are matched to their
    originating ``task`` tool_call by id when possible, else appended as siblings.
    """
    # Match subagent namespaces to task tool_call ids (best effort).
    task_call_ids: dict[str, list[CallRecord]] = {}
    for ns, calls in callset.subagent_calls.items():
        # ns looks like 'tools:<uuid>'; the uuid often matches a task tool_call id.
        ns_id = ns.split(":", 1)[1] if ":" in ns else ns
        task_call_ids[ns_id] = calls
    used_ns: set[str] = set()

    turns: list[TurnNode] = []
    current: TurnNode | None = None
    prev_input = -1
    for call in callset.parent_calls:
        if current is None or (prev_input >= 0 and call.input_tokens < prev_input * 0.5):
            current = TurnNode(kind="turn", label=f"Turn {len(turns) + 1}")
            turns.append(current)
        prev_input = call.input_tokens

        ai_node = _ai_call_node(call)
        # Attach subagent subtrees under matching task tool nodes.
        for tc, tool_node in zip(call.tool_calls, ai_node.children, strict=False):
            if tc.name == "task" and tc.id in task_call_ids and tc.id not in used_ns:
                sub = _subagent_node(f"tools:{tc.id}", task_call_ids[tc.id])
                tool_node.children.append(sub)
                used_ns.add(tc.id)
        current.input_tokens += call.input_tokens
        current.output_tokens += call.output_tokens
        current.usd += call.usd
        current.children.append(ai_node)

    # Any subagent namespace we couldn't correlate → standalone nodes.
    for ns_id, calls in task_call_ids.items():
        if ns_id in used_ns:
            continue
        turns.append(_subagent_node(f"tools:{ns_id}", calls))

    return turns
