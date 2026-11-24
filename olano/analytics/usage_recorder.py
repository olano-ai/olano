"""Shared LLM usage accounting: merge helpers + background-call recording.

``core/runner._record_usage`` is the single writer of the cross-agent usage
ledger, but only ``AgentRunner`` turns reach it. Everything else that spends
money on a model — cognition workers on the raw graph, the daily-highlights
narrative, post-turn PULSE curation, ACP invokes — was invisible to Insights
while the gateway billed it anyway (the "51 credits local vs 4,225 billed"
class of discrepancy). The merge/estimate logic those call sites need lived as
private ``AgentRunner`` staticmethods; it lives here now so background call
sites can record usage without importing (or cycling into) ``core.runner``.

Two layers:

- **Merge helpers** (``merge_usage_metadata`` / ``merge_olano_credits`` /
  ``ensure_streamed_credits`` / ``merge_result_usage``) fold LangChain message
  usage + Olano gateway charge headers into a plain int ``usage_meta`` dict.
  ``AgentRunner`` delegates to these for its own turns.
- **Recorders** (``record_background_usage`` / ``record_result_usage``) write
  one :func:`olano.analytics.usage_ledger.record_usage_event` row for a
  non-runner call, tagged with an ``origin`` label so Insights can show where
  credits went. Best-effort — accounting must never break the work it measures.

Heavy imports stay call-time, mirroring the ledger's own posture.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ledger ``origin`` labels for runner turns, mapped from the request context's
# ``root_source`` (security/request_context.make_origin). Background writers
# use their own labels: "cognition", "delegate", "pulse", "highlights",
# "reports", "prompt_gen", "acp", "vision". Empty string = a row written
# before origins existed (rendered as plain agent turns).
_ROOT_SOURCE_TO_ORIGIN = {
    "human": "interactive",
    "scheduled": "scheduled",
    "cognition": "cognition",
    "peer-unknown": "peer",
}


def turn_origin_label() -> str:
    """Ledger ``origin`` for the current runner turn, from the request context.

    Unknown/unset roots return ``""`` (the legacy bucket) rather than guessing.
    """
    try:
        from olano.security.request_context import current_origin

        root = str(current_origin().get("root_source") or "")
        return _ROOT_SOURCE_TO_ORIGIN.get(root, "")
    except Exception:  # pragma: no cover - defensive
        return ""


def merge_usage_metadata(target: dict[str, int], usage: Any) -> None:
    """Fold one ``usage_metadata`` payload into ``target``.

    One model sub-call per payload — a turn that ran three tool round-trips is
    three model calls, and that is the number the chat header and Insights both
    call "model calls". Counting turns instead would under-report a tool-heavy
    turn by an order of magnitude.
    """
    if not usage:
        return
    target["model_calls"] = target.get("model_calls", 0) + 1
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if value is not None:
            target[key] = target.get(key, 0) + int(value)
    # Cached-input tokens (Anthropic prompt cache / OpenAI cached input) so cost
    # can apply the cache-read discount — matches the drill-down's accounting.
    details = (
        usage.get("input_token_details")
        if isinstance(usage, dict)
        else getattr(usage, "input_token_details", None)
    )
    if isinstance(details, dict) and details.get("cache_read") is not None:
        target["cache_read"] = target.get("cache_read", 0) + int(details["cache_read"])


def merge_olano_credits(target: dict[str, int], msg: Any, *, managed: bool = True) -> None:
    """Accumulate the charge the Olano gateway reported for this call.

    Two header spellings are accepted:

    - ``x-olano-credits-charged`` — already denominated in credits; trusted
      from any response (only Olano infrastructure sends it).
    - ``x-litellm-response-cost``: the gateway's native cost header, in
      credit-USD (the gateway's model entries are priced in credit-USD by
      construction), so credits =
      value × ``credits_per_usd``. Honored ONLY for managed turns: a BYOK
      model pointed at some self-hosted LiteLLM must not fabricate an
      olano-credits charge out of its own cost telemetry.

    The gateway sends the cost header on non-streamed responses only (a
    stream's headers leave before its cost exists) — streamed turns get
    their credits from token counts instead (``estimate_credits_micro``,
    applied once per turn by the callers). Stored as **micro-credits** so
    ``usage_meta`` stays an int dict; the ledger divides it back out.
    """
    meta = getattr(msg, "response_metadata", None)
    if not isinstance(meta, dict):
        return
    headers = meta.get("headers")
    if not isinstance(headers, dict):
        return
    credits = credits_from_headers(headers, managed=managed)
    if credits is None:
        return
    target["credits_micro"] = target.get("credits_micro", 0) + round(credits * 1_000_000)


def credits_from_headers(headers: Any, *, managed: bool = True) -> float | None:
    """The gateway's per-call charge from a response's headers, in credits.

    Accepts any mapping-ish headers object (a plain dict, ``httpx.Headers``,
    an openai raw-response's headers) — every gateway HTTP call site can price
    its ledger row with this instead of "counted, never priced". Same header
    contract as :func:`merge_olano_credits` (which delegates here): the olano
    spelling is trusted unconditionally, the gateway's cost header only on managed
    calls. Returns ``None`` (never raises) when no positive charge is present.
    """
    try:
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    except Exception:
        return None
    credits: float | None = None
    raw = lowered.get("x-olano-credits-charged")
    if raw is not None:
        try:
            credits = float(raw)
        except (TypeError, ValueError):
            credits = None
    if credits is None and managed:
        raw = lowered.get("x-litellm-response-cost")
        if raw is not None:
            try:
                from olano.catalog.credits_registry import credits_per_usd

                credits = float(raw) * credits_per_usd()
            except (TypeError, ValueError):
                credits = None
            except Exception:
                credits = None
    if credits is None or credits <= 0:
        return None
    return credits


def ensure_streamed_credits(target: dict[str, int], model: str | None) -> None:
    """Fill in a managed turn's charge from token counts when no gateway
    header carried one, the normal case for streamed turns (the gateway's
    cost header cannot exist at stream-header time). Header-derived
    credits, when present, always win; this never overwrites them."""
    if target.get("credits_micro"):
        return
    try:
        from olano.models.olano_gateway import estimate_credits_micro

        est = estimate_credits_micro(model, target)
    except Exception:
        return
    if est > 0:
        target["credits_micro"] = est


def merge_result_usage(
    target: dict[str, int],
    result: Any,
    *,
    model: str | None = None,
    seen_ids: set[str] | None = None,
) -> None:
    """Fold a whole graph result — or one bare AIMessage — into ``target``.

    Accepts the state dict ``ainvoke`` returns (iterates ``messages``) or a
    single message object (what a bare ``build_model(...).ainvoke`` returns);
    anything else is a silent no-op.

    ``seen_ids`` is a **mutable** ledger of message ids already counted, and it
    is what makes this safe to call on a checkpointed thread: ``ainvoke``
    returns the accumulated state, not the turn, so a bare merge re-counts
    every prior turn — every time. Pass the ids present *before* the invoke
    and only what the invoke added is billed; the same set carried across a
    retry loop stops each pass from re-counting the last. Messages with no id
    are always counted (nothing to dedupe on), so a caller that can't read the
    prior state degrades to the old over-count rather than to silence.
    """
    from olano.models.olano_gateway import is_olano_model

    managed = is_olano_model(model)
    messages = result.get("messages", []) if isinstance(result, dict) else [result]
    for msg in messages:
        if msg is None:
            continue
        if seen_ids is not None:
            msg_id = getattr(msg, "id", None)
            if msg_id:
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
        merge_usage_metadata(target, getattr(msg, "usage_metadata", None))
        merge_olano_credits(target, msg, managed=managed)


def record_background_usage(
    agent: str,
    model: str | None,
    usage_meta: dict[str, int],
    *,
    origin: str,
    thread_id: str = "",
) -> None:
    """Write one ledger row for a non-runner model call chain. Never raises.

    Skips silently when ``usage_meta`` carries no calls and no tokens (a model
    that reports no usage_metadata leaves nothing worth a row).
    """
    try:
        if not usage_meta:
            return
        if not usage_meta.get("model_calls") and not usage_meta.get("input_tokens"):
            return
        ensure_streamed_credits(usage_meta, model)

        from olano.analytics.threadcost.cost import cost_for_call
        from olano.analytics.usage_ledger import record_usage_event

        input_tokens = int(usage_meta.get("input_tokens", 0) or 0)
        output_tokens = int(usage_meta.get("output_tokens", 0) or 0)
        cache_read = int(usage_meta.get("cache_read", 0) or 0)
        usd = cost_for_call(model or "unknown", input_tokens, output_tokens, cache_read)
        credits_micro = int(usage_meta.get("credits_micro", 0) or 0)
        record_usage_event(
            agent=agent,
            model=model or "unknown",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
            cache_read_tokens=cache_read,
            credits=(credits_micro / 1_000_000) if credits_micro else None,
            calls=int(usage_meta.get("model_calls", 0) or 0) or 1,
            origin=origin,
            # Most background chains have no conversation of their own; the ones
            # that do (a cognition run's cog:: thread) pass it so the drill-down
            # can rank them alongside runner turns.
            thread_id=thread_id,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("usage_recorder: background record failed", exc_info=True)


def record_result_usage(
    agent: str, model: str | None, result: Any, *, origin: str, thread_id: str = ""
) -> dict[str, int]:
    """Merge a result's usage and ledger it in one call; returns the meta dict
    so callers can log it. Never raises."""
    usage_meta: dict[str, int] = {}
    try:
        merge_result_usage(usage_meta, result, model=model)
        record_background_usage(agent, model, usage_meta, origin=origin, thread_id=thread_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug("usage_recorder: result record failed", exc_info=True)
    return usage_meta
