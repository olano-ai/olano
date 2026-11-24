"""Tests for olano/analytics/usage_recorder — the shared accounting chokepoint
that lets background call sites (cognition workers, highlights, PULSE, ACP)
ledger their spend without importing core.runner."""

from __future__ import annotations

from types import SimpleNamespace

from olano.analytics import usage_recorder as ur
from olano.analytics.usage_ledger import read_usage_events


def _ai(input_tokens: int = 0, output_tokens: int = 0, cache_read: int | None = None, headers=None):
    usage = None
    if input_tokens or output_tokens:
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if cache_read is not None:
            usage["input_token_details"] = {"cache_read": cache_read}
    meta = {"headers": headers} if headers is not None else {}
    return SimpleNamespace(usage_metadata=usage, response_metadata=meta)


# ── merge helpers ─────────────────────────────────────────────────────────────


def test_merge_usage_metadata_counts_calls_tokens_and_cache_reads() -> None:
    target: dict[str, int] = {}
    ur.merge_usage_metadata(target, {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    ur.merge_usage_metadata(
        target,
        {
            "input_tokens": 50,
            "output_tokens": 5,
            "total_tokens": 55,
            "input_token_details": {"cache_read": 40},
        },
    )
    assert target["model_calls"] == 2  # one sub-call per payload
    assert target["input_tokens"] == 150
    assert target["output_tokens"] == 15
    assert target["cache_read"] == 40


def test_merge_olano_credits_header_spellings_and_managed_gate(monkeypatch) -> None:
    # Olano's own header is trusted regardless of the managed flag, any casing.
    target: dict[str, int] = {}
    ur.merge_olano_credits(target, _ai(headers={"X-Olano-Credits-Charged": "1.5"}), managed=False)
    assert target["credits_micro"] == 1_500_000

    # The gateway cost header converts via credits_per_usd, managed turns only:
    # a BYOK model pointed at some self-hosted LiteLLM must not fabricate an
    # olano charge from its own cost telemetry.
    monkeypatch.setattr("olano.catalog.credits_registry.credits_per_usd", lambda: 1000.0)
    byok: dict[str, int] = {}
    ur.merge_olano_credits(byok, _ai(headers={"x-litellm-response-cost": "0.002"}), managed=False)
    assert "credits_micro" not in byok
    managed: dict[str, int] = {}
    ur.merge_olano_credits(managed, _ai(headers={"x-litellm-response-cost": "0.002"}), managed=True)
    assert managed["credits_micro"] == 2_000_000


def test_ensure_streamed_credits_estimates_only_when_absent() -> None:
    # Header-derived credits always win — never overwritten by the estimate.
    target = {"input_tokens": 1_000_000, "output_tokens": 0, "credits_micro": 7}
    ur.ensure_streamed_credits(target, "olano:pro")
    assert target["credits_micro"] == 7

    # A managed model with no header gets a token-based estimate (bundled rates).
    est: dict[str, int] = {"input_tokens": 1_000_000, "output_tokens": 1000}
    ur.ensure_streamed_credits(est, "olano:pro")
    assert est.get("credits_micro", 0) > 0

    # BYOK models never estimate olano credits.
    byok: dict[str, int] = {"input_tokens": 1_000_000, "output_tokens": 1000}
    ur.ensure_streamed_credits(byok, "anthropic:claude-sonnet-4-6")
    assert "credits_micro" not in byok


def test_merge_result_usage_seen_ids_bills_each_message_once() -> None:
    """`seen_ids` is what makes a checkpointed thread safe to merge.

    ainvoke returns the accumulated state, so the same prior messages come
    back on every turn; the ledger of already-counted ids is what stops the
    second merge from re-billing the first.
    """
    first = _ai(1000, 100)
    first.id = "ai-1"
    second = _ai(700, 70)
    second.id = "ai-2"

    seen: set[str] = set()
    target: dict[str, int] = {}
    ur.merge_result_usage(target, {"messages": [first]}, model="olano:pro", seen_ids=seen)
    ur.merge_result_usage(target, {"messages": [first, second]}, model="olano:pro", seen_ids=seen)

    assert target["input_tokens"] == 1700
    assert target["model_calls"] == 2


def test_merge_result_usage_without_seen_ids_still_counts_everything() -> None:
    """Callers with nothing to dedupe against (a bare model call, a fresh
    per-run thread) keep the plain behaviour — and so does a caller whose
    prior state was unreadable, which hands over an empty set."""
    msg = _ai(1000, 100)
    msg.id = "ai-1"

    plain: dict[str, int] = {}
    ur.merge_result_usage(plain, {"messages": [msg]}, model="olano:pro")
    empty_ledger: dict[str, int] = {}
    ur.merge_result_usage(empty_ledger, {"messages": [msg]}, model="olano:pro", seen_ids=set())

    assert plain["input_tokens"] == empty_ledger["input_tokens"] == 1000


def test_merge_result_usage_counts_messages_that_have_no_id() -> None:
    """Nothing to dedupe on → count it, rather than dropping a real cost."""
    target: dict[str, int] = {}
    ur.merge_result_usage(target, {"messages": [_ai(500, 50)]}, model="olano:pro", seen_ids=set())
    assert target["input_tokens"] == 500


# ── recorders ─────────────────────────────────────────────────────────────────


def test_record_result_usage_state_dict_writes_one_origin_tagged_row() -> None:
    result = {"messages": [_ai(1000, 100), _ai(500, 50), SimpleNamespace()]}
    meta = ur.record_result_usage("merli", "olano:pro", result, origin="cognition")
    assert meta["model_calls"] == 2

    events = read_usage_events()
    assert len(events) == 1
    ev = events[0]
    assert (ev.agent, ev.model, ev.origin) == ("merli", "olano:pro", "cognition")
    assert ev.input_tokens == 1500 and ev.output_tokens == 150 and ev.calls == 2
    # Managed model with no headers → the streamed estimate priced it.
    assert ev.credits is not None and ev.credits > 0
    assert ev.billing_source == "olano"


def test_record_result_usage_accepts_a_bare_message() -> None:
    ur.record_result_usage(
        "system", "anthropic:claude-sonnet-4-6", _ai(200, 20), origin="highlights"
    )
    events = read_usage_events()
    assert len(events) == 1
    assert events[0].origin == "highlights"
    assert events[0].credits is None  # BYOK — Olano sold nothing here
    assert events[0].usd > 0


def test_record_result_usage_skips_results_with_no_usage() -> None:
    ur.record_result_usage(
        "merli", "olano:pro", {"messages": [SimpleNamespace()]}, origin="cognition"
    )
    ur.record_result_usage("merli", "olano:pro", None, origin="cognition")
    assert read_usage_events() == []


def test_turn_origin_label_maps_root_sources() -> None:
    from olano.security.request_context import ORIGIN_KEY, make_origin, push_request_context

    with push_request_context(
        source="cognition",
        channel="cognition",
        metadata={ORIGIN_KEY: make_origin("cognition", False)},
    ):
        assert ur.turn_origin_label() == "cognition"
    with push_request_context(
        source="api", channel="dashboard", metadata={ORIGIN_KEY: make_origin("scheduled", False)}
    ):
        assert ur.turn_origin_label() == "scheduled"
    with push_request_context(
        source="api", channel="dashboard", metadata={ORIGIN_KEY: make_origin("human", True)}
    ):
        assert ur.turn_origin_label() == "interactive"
    with push_request_context(
        source="api",
        channel="dashboard",
        metadata={ORIGIN_KEY: make_origin("peer-unknown", False)},
    ):
        assert ur.turn_origin_label() == "peer"


# ── credits_from_headers: the one pricing seam every HTTP service site uses ──


def test_credits_from_headers_accepts_any_mapping_and_both_spellings(monkeypatch) -> None:
    import httpx

    # Olano's own charge header: trusted on managed AND unmanaged calls.
    assert ur.credits_from_headers({"X-Olano-Credits-Charged": "2.5"}, managed=False) == 2.5
    # httpx.Headers (what speech/image call sites actually hold) works as-is.
    assert ur.credits_from_headers(httpx.Headers({"x-olano-credits-charged": "1.25"})) == 1.25

    # The gateway's cost header converts at the published rate, managed only:
    # BYOK telemetry must never fabricate an olano charge.
    monkeypatch.setattr("olano.catalog.credits_registry.credits_per_usd", lambda: 1000.0)
    assert ur.credits_from_headers({"x-litellm-response-cost": "0.002"}, managed=True) == 2.0
    assert ur.credits_from_headers({"x-litellm-response-cost": "0.002"}, managed=False) is None


def test_credits_from_headers_returns_none_never_raises() -> None:
    assert ur.credits_from_headers({}) is None
    assert ur.credits_from_headers({"x-olano-credits-charged": "garbage"}) is None
    assert ur.credits_from_headers({"x-olano-credits-charged": "0"}) is None  # no charge
    assert ur.credits_from_headers({"x-olano-credits-charged": "-1"}) is None
    assert ur.credits_from_headers(None) is None
    assert ur.credits_from_headers(object()) is None
