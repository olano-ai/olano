"""Olano-managed model tiers: routing to the gateway, and the billing split.

The property under test throughout is the one the product depends on: an
Olano-managed call is *paid for by Olano*, priced by the gateway, and charged in
credits — while a BYOK call is paid for by the customer, priced locally, and
charged in nothing. Anything that blurs those two bills the wrong party.
"""

from __future__ import annotations

import pytest

from olano.analytics.usage_ledger import UsageEvent
from olano.models import olano_gateway

# ── tier parsing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pro", "pro"),
        ("Pro", "pro"),
        ("max", "max"),
        ("  max ", "max"),
        ("Max", "max"),
        ("turbo", "turbo"),
        # `flash` is the pre-launch name of `turbo`; a settings document written
        # before the rename must not brick a box that upgraded its runtime.
        ("flash", "turbo"),
        # `reasoning` was the top tier's id before it became `max`. It was
        # retired outright, NOT aliased, so it is simply not a tier — the word
        # now belongs to the effort-level concept alone.
        ("reasoning", None),
        ("auto", "auto"),
        ("gpt-5.5", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_tier(raw, expected) -> None:
    assert olano_gateway.normalize_tier(raw) == expected


@pytest.mark.parametrize(
    "model,expected",
    [
        ("olano:pro", True),
        ("olano:auto", True),
        ("olano:flash", True),
        ("OLANO:PRO", True),
        # A plausible-looking string that is NOT a tier must not be treated as
        # managed: classifying a BYOK call as Olano-managed would hide the
        # customer's own tokens and cost from them.
        ("olano:claude-sonnet-4-6", False),
        ("anthropic:claude-sonnet-4-6", False),
        ("olano", False),
        (None, False),
    ],
)
def test_is_olano_model(model, expected) -> None:
    assert olano_gateway.is_olano_model(model) is expected


# ── building the client ──────────────────────────────────────────────────────


def test_build_refuses_without_key_or_deployment_token(monkeypatch) -> None:
    """No gateway key and no token to enroll one ⇒ hard error, never a silent
    fallback to a real provider.

    Falling back would run the customer's "Olano Pro" work on their own BYOK
    key: the wrong party pays, and the product boundary breaks silently.
    """
    from olano.deployment import gateway_key as gk
    from olano.deployment import sources

    monkeypatch.delenv("LIONCLAW_OLANO_GATEWAY_KEY", raising=False)
    monkeypatch.setattr(gk, "ensure_gateway_key", lambda: None)
    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    with pytest.raises(olano_gateway.OlanoGatewayUnavailable):
        olano_gateway.build_olano_model("pro")


def test_build_points_at_the_gateway_and_never_at_a_provider(monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_URL", "https://gw.example.test")
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")

    model = olano_gateway.build_olano_model("pro")
    # The OpenAI-compatible surface of the Olano gateway, never a provider.
    assert str(model.openai_api_base).rstrip("/") == "https://gw.example.test/v1"
    # The model name sent upstream is the tier's GROUP name at the gateway,
    # not a provider model id: this process must not be able to name a real
    # model on the managed path.
    assert model.model_name == "olano-pro"
    # Without response headers the gateway's per-call charge (non-streamed
    # calls) is invisible.
    assert model.include_response_headers is True
    # langchain only auto-enables stream usage when openai_api_base is None —
    # with our base_url it must be forced, or a STREAMED turn (every normal
    # turn) reports neither tokens nor a usage frame at all.
    assert model.stream_usage is True


def test_build_with_token_but_no_key_uses_the_unenrolled_sentinel(monkeypatch) -> None:
    """Enrollment hiccup ⇒ the build still succeeds (first call 401s into the
    self-heal path). Failing the whole agent build over a transient cloud
    outage would take the agent down entirely."""
    from olano.deployment import gateway_key as gk
    from olano.deployment import sources

    monkeypatch.delenv("LIONCLAW_OLANO_GATEWAY_KEY", raising=False)
    monkeypatch.setattr(gk, "ensure_gateway_key", lambda: None)
    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_testtoken")
    model = olano_gateway.build_olano_model("pro")
    assert model.openai_api_key.get_secret_value() == gk.UNENROLLED_SENTINEL


def test_unknown_tier_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    assert olano_gateway.build_olano_model("nonsense").model_name == "olano-turbo"


# ── retired tiers: hidden from every picker, still resolvable ────────────────
# `auto` is withdrawn until the gateway actually routes per request. Agents
# already pointing at it must keep working, and keep
# sending the `olano-auto` group name, since a deployment key may be scoped to
# the groups its plan listed.


def test_auto_is_not_offered_anywhere() -> None:
    from olano.models import get_provider_models

    assert "auto" not in olano_gateway.offered_tiers()
    assert "auto" not in get_provider_models("olano")
    assert olano_gateway.tier_offered("olano:auto") is False
    assert olano_gateway.tier_offered("olano:turbo") is True


def test_auto_still_resolves_on_its_own_wire_name(monkeypatch) -> None:
    from olano import models as models_mod
    from olano.deployment import enforcement

    # No per-tier flag is set for it — a hidden tier must not become gated.
    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: name == "olano_models")
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    assert models_mod.build_model("olano:auto").model_name == "olano-auto"
    # The new default is ungated for the same reason: a plan with no usable
    # tier is a broken plan.
    assert models_mod.build_model("olano:turbo").model_name == "olano-turbo"


def test_auto_and_turbo_still_price_identically() -> None:
    # They are the same backend today; the ledger must not drift between an
    # agent left on `auto` and one moved to `turbo`.
    assert olano_gateway.tier_credit_rates("auto") == olano_gateway.tier_credit_rates("turbo")


def test_vision_group_builds_against_the_gateway(monkeypatch) -> None:
    """`olano-vision` is a gateway group like the tiers — same virtual key,
    same base URL, same never-name-a-real-model rule — but routed on a
    guaranteed capability rather than a price/latency point."""
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    model = olano_gateway.build_olano_vision_model()
    assert model.model_name == "olano-vision"
    assert str(model.openai_api_base).startswith(olano_gateway.gateway_base_url())
    assert model.openai_api_key.get_secret_value() == "sk-olano-dep-key"


def test_build_model_vision_is_tool_only(monkeypatch) -> None:
    """`build_model("olano:vision")` resolves ONLY for the product tool
    (allow_utility=True); as a selectable brain it is hard-blocked, so a
    hand-edited config can't point an agent at the vision service."""
    from olano import models as models_mod
    from olano.deployment import enforcement

    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: True)
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    # Tool path: resolves (one model-string entry point for the product).
    assert models_mod.build_model("olano:vision", allow_utility=True).model_name == "olano-vision"
    # Brain path: refused with an actionable error naming the tiers.
    with pytest.raises(ValueError, match="not a model you can assign as an agent's brain"):
        models_mod.build_model("olano:vision")
    # The other utility group is refused as a brain too.
    with pytest.raises(ValueError, match="utility service"):
        models_mod.build_model("olano:olano-image")


def test_vision_group_is_gated_by_olano_models_but_not_per_tier(monkeypatch) -> None:
    """A customer entitled to olano models is entitled to look at an image:
    the group carries no per-tier feature flag of its own. (Tool path.)"""
    from olano import models as models_mod
    from olano.deployment import enforcement

    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    monkeypatch.setattr(
        enforcement, "feature_enabled", lambda name: not name.startswith("olano_model_")
    )
    assert models_mod.build_model("olano:vision", allow_utility=True).model_name == "olano-vision"

    # olano_models still gates it — the entitlement check runs after the
    # utility guard, so even the tool path is refused when olano models are off.
    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: name != "olano_models")
    with pytest.raises(ValueError, match="disabled for this deployment"):
        models_mod.build_model("olano:vision", allow_utility=True)


# ── entitlements at model resolution ─────────────────────────────────────────


def test_build_model_respects_the_group_entitlement(monkeypatch) -> None:
    from olano import models as models_mod
    from olano.deployment import enforcement

    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: name != "olano_models")
    with pytest.raises(ValueError, match="disabled for this deployment"):
        models_mod.build_model("olano:pro")


def test_build_model_respects_a_per_tier_entitlement(monkeypatch) -> None:
    from olano import models as models_mod
    from olano.deployment import enforcement

    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: name != "olano_model_max")
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    with pytest.raises(ValueError, match="olano_model_max"):
        models_mod.build_model("olano:max")
    # A tier that IS entitled still builds — olano's `olano:pro` ref maps to
    # the gateway's `olano-pro` group name on the wire.
    assert models_mod.build_model("olano:pro").model_name == "olano-pro"


def test_the_retired_tier_id_is_rejected_not_quietly_remapped(monkeypatch) -> None:
    """`olano:reasoning` was retired outright when the tier became `olano:max`.

    An agent.json5 still carrying it must FAIL, naming the tiers that exist —
    not resolve to something the string does not name. That is the whole point
    of a hard cutover: the config says a thing that is not true any more, and
    silently honouring it would hide that from whoever has to fix the file.
    """
    from olano import models as models_mod

    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    with pytest.raises(ValueError, match="Unknown Olano model tier"):
        models_mod.build_model("olano:reasoning")


def test_the_tier_reaches_the_max_gateway_group(monkeypatch) -> None:
    from olano import models as models_mod

    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    assert models_mod.build_model("olano:max").model_name == "olano-max"


def test_build_model_rejects_an_unknown_tier(monkeypatch) -> None:
    from olano import models as models_mod
    from olano.deployment import enforcement

    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: True)
    with pytest.raises(ValueError, match="Unknown Olano model tier"):
        models_mod.build_model("olano:claude-sonnet-4-6")


# ── the billing split in the ledger ──────────────────────────────────────────


def _event(model: str, credits: float | None) -> UsageEvent:
    return UsageEvent(
        ts="2026-08-10T00:00:00+00:00",
        agent="merli",
        model=model,
        input_tokens=1000,
        output_tokens=200,
        usd=0.0123,
        credits=credits,
    )


def test_billing_source_is_derived_not_stored() -> None:
    """Derived from the model string so events written before the split — and
    any event whose `credits` was lost — still classify correctly."""
    assert _event("olano:pro", 1.5).billing_source == "olano"
    assert _event("anthropic:claude-sonnet-4-6", None).billing_source == "customer"
    # Even an Olano event that somehow carries no charge is still Olano-managed:
    # the source is who pays the provider, not whether we captured the number.
    assert _event("olano:turbo", None).billing_source == "olano"


def test_credits_absent_is_not_credits_zero() -> None:
    """A BYOK row must omit `credits` entirely. Writing 0.0 would make "Olano
    sold nothing here" indistinguishable from "a free managed call", and the
    Insights split reads exactly this field."""
    byok = _event("anthropic:claude-sonnet-4-6", None).to_json()
    assert "credits" not in byok

    managed = _event("olano:pro", 1.5).to_json()
    assert managed["credits"] == 1.5
    assert UsageEvent.from_json(managed) == _event("olano:pro", 1.5)


def test_ledger_keeps_tokens_and_cost_for_managed_calls() -> None:
    """Nothing is thrown away. The customer-facing UI hides tokens and USD for
    Olano-managed calls, but margin monitoring needs both, so they stay on the
    row — hiding is a rendering decision, not a storage one."""
    row = _event("olano:pro", 1.5).to_json()
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert row["usd"] == 0.0123


# ── a key that arrives AFTER a model was built ───────────────────────────────


class _Client:
    def __init__(self, key: str) -> None:
        self.api_key = key


class _Model:
    def __init__(self, key: str) -> None:
        self.root_client = _Client(key)
        self.root_async_client = _Client(key)


def test_a_delivered_key_reaches_models_already_built(monkeypatch) -> None:
    """THE FAILURE THIS FIXES. Agents boot before a key exists, so their models
    hold UNENROLLED_SENTINEL. A key then arrives over the settings channel and
    lands in the vault — and nothing told the live models, so every turn kept
    401ing against a placeholder while a good key sat one lookup away. That is
    what "I updated and it still doesn't work" looked like."""
    from olano.models import olano_gateway as og

    model = _Model("olano-unenrolled")
    og._remember_model(model)
    assert og.swap_live_models("sk-delivered") >= 1
    assert model.root_client.api_key == "sk-delivered"
    assert model.root_async_client.api_key == "sk-delivered"


def test_self_heal_prefers_a_key_obtained_elsewhere_over_enrolling(monkeypatch) -> None:
    """Enrollment is the slow, failure-prone path. Calling it when a good key is
    already in hand is how a box stayed broken: the POST 504'd, the heal
    returned early, and the model kept its placeholder forever."""
    from olano.deployment import gateway_key as gk
    from olano.models import olano_gateway as og

    def must_not_enroll():  # pragma: no cover - the assertion
        raise AssertionError("must not enroll when a usable key is already held")

    monkeypatch.setattr(gk, "gateway_key", lambda: "sk-from-settings")
    monkeypatch.setattr(gk, "refresh_gateway_key", must_not_enroll)

    model = _Model("olano-unenrolled")
    og._refresh_and_swap(model)
    assert model.root_client.api_key == "sk-from-settings"


def test_self_heal_still_enrolls_when_the_held_key_is_the_dead_one(monkeypatch) -> None:
    """The genuine rotation case: what we hold IS what just 401'd, so minting is
    the only way forward."""
    from olano.deployment import gateway_key as gk
    from olano.models import olano_gateway as og

    monkeypatch.setattr(gk, "gateway_key", lambda: "sk-dead")
    monkeypatch.setattr(gk, "refresh_gateway_key", lambda: "sk-fresh")

    model = _Model("sk-dead")
    og._refresh_and_swap(model)
    assert model.root_client.api_key == "sk-fresh"


def test_swapping_is_a_no_op_when_the_model_already_has_the_key() -> None:
    from olano.models import olano_gateway as og

    model = _Model("sk-live")
    assert og._swap_key(model, "sk-live") is False


def test_the_registry_holds_models_weakly() -> None:
    """A rebuilt or discarded model must not be kept alive by the registry."""
    import gc

    from olano.models import olano_gateway as og

    before = len(list(og._live_models))
    og._remember_model(_Model("sk-x"))
    gc.collect()
    assert len(list(og._live_models)) <= before + 1


# ── transient-failure retry ──────────────────────────────────────────────────
#
# A managed tier is one hop longer than a BYOK provider, and the extra hop
# refuses sometimes. Until the client retried, one relayed upstream refusal cost
# the user the whole turn and produced a chat message telling them to try again
# while the box itself would not.


@pytest.fixture
def gateway_model(monkeypatch):
    """A gateway client with the backoff zeroed so tests do not sleep."""
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_KEY", "sk-olano-dep-key")
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_BACKOFF", "0,0")
    return olano_gateway.build_olano_model("pro")


def _relayed_upstream_error() -> Exception:
    """What the gateway relays when the backend behind a group refuses."""
    return Exception(
        "litellm.APIError: APIError: OpenAIException - upstream refused the "
        "request. Received Model Group=olano-pro"
    )


def test_retry_delays_defaults_and_overrides(monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_OLANO_GATEWAY_RETRIES", raising=False)
    monkeypatch.delenv("LIONCLAW_OLANO_GATEWAY_BACKOFF", raising=False)
    assert olano_gateway._retry_delays() == (1.0, 4.0)

    # One attempt total means no retry at all — the kill switch.
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_RETRIES", "1")
    assert olano_gateway._retry_delays() == ()

    # More attempts than configured delays reuses the last delay.
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_RETRIES", "4")
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_BACKOFF", "2")
    assert olano_gateway._retry_delays() == (2.0, 2.0, 2.0)

    # A typo must not fail every call on the box.
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_RETRIES", "soon")
    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_BACKOFF", "later")
    assert olano_gateway._retry_delays() == (1.0, 4.0)


def test_invoke_survives_a_transient_upstream_refusal(monkeypatch, gateway_model) -> None:
    from langchain_openai import ChatOpenAI

    calls: list[int] = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise _relayed_upstream_error()
        return "the answer"

    monkeypatch.setattr(ChatOpenAI, "_generate", flaky)
    assert gateway_model._generate([]) == "the answer"
    assert len(calls) == 3


def test_a_spent_credit_balance_is_never_retried(monkeypatch, gateway_model) -> None:
    """Retrying a dry balance just delays an honest message."""
    from langchain_openai import ChatOpenAI

    calls: list[int] = []

    def broke(self, *args, **kwargs):
        calls.append(1)
        raise Exception("litellm.BadRequestError: Budget has been exceeded for this key")

    monkeypatch.setattr(ChatOpenAI, "_generate", broke)
    with pytest.raises(Exception, match="Budget has been exceeded"):
        gateway_model._generate([])
    assert len(calls) == 1


def test_the_failure_names_the_group_it_was_made_against(monkeypatch, gateway_model) -> None:
    """The classifier must not have to regex the group out of provider prose."""
    from langchain_openai import ChatOpenAI

    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_RETRIES", "1")

    def broke(self, *args, **kwargs):
        raise _relayed_upstream_error()

    monkeypatch.setattr(ChatOpenAI, "_generate", broke)
    with pytest.raises(Exception) as caught:
        gateway_model._generate([])
    assert getattr(caught.value, olano_gateway.GROUP_ATTR) == "olano-pro"


async def test_a_stream_that_already_answered_is_never_retried(monkeypatch, gateway_model) -> None:
    """The guard that matters most: a retry after partial output would append a
    whole second answer to tokens the user already has — which is precisely the
    garble this codebase has shipped before."""
    from langchain_openai import ChatOpenAI

    attempts: list[int] = []

    async def half_an_answer(self, *args, **kwargs):
        attempts.append(1)
        yield "Here is all the visible text"
        raise _relayed_upstream_error()

    monkeypatch.setattr(ChatOpenAI, "_astream", half_an_answer)

    seen = []
    with pytest.raises(Exception, match="upstream refused"):
        async for chunk in gateway_model._astream([]):
            seen.append(chunk)

    assert seen == ["Here is all the visible text"]
    assert len(attempts) == 1, "a partially-delivered stream must not be retried"


async def test_a_stream_that_failed_before_any_output_is_retried(
    monkeypatch, gateway_model
) -> None:
    from langchain_openai import ChatOpenAI

    attempts: list[int] = []

    async def flaky(self, *args, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise _relayed_upstream_error()
        yield "the answer"

    monkeypatch.setattr(ChatOpenAI, "_astream", flaky)

    seen = [chunk async for chunk in gateway_model._astream([])]
    assert seen == ["the answer"]
    assert len(attempts) == 2


async def test_a_dropped_connection_mid_stream_is_retried(monkeypatch, gateway_model) -> None:
    """The gap the openai SDK leaves. Its client retries the request it *makes*;
    a socket that dies while the response body is being read is past that point,
    and a streamed turn spends nearly all its wall-clock there. Unretried, it
    reached a customer on WhatsApp as "API error (ReadError):" — and resending
    the identical message worked first time, which is the whole diagnosis."""
    import httpx
    from langchain_openai import ChatOpenAI

    attempts: list[int] = []

    async def dropped(self, *args, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ReadError("")
        yield "the answer"

    monkeypatch.setattr(ChatOpenAI, "_astream", dropped)

    seen = [chunk async for chunk in gateway_model._astream([])]
    assert seen == ["the answer"]
    assert len(attempts) == 2


async def test_a_connection_dropped_after_output_is_still_never_retried(
    monkeypatch, gateway_model
) -> None:
    """The guard that makes retrying a transport failure safe at all: the caller
    already holds those tokens, so a second run would append a whole second
    answer to them."""
    import httpx
    from langchain_openai import ChatOpenAI

    attempts: list[int] = []

    async def dropped_late(self, *args, **kwargs):
        attempts.append(1)
        yield "half an answer"
        raise httpx.ReadError("")

    monkeypatch.setattr(ChatOpenAI, "_astream", dropped_late)

    seen: list[str] = []
    with pytest.raises(httpx.ReadError):
        async for chunk in gateway_model._astream([]):
            seen.append(chunk)

    assert seen == ["half an answer"]
    assert len(attempts) == 1
