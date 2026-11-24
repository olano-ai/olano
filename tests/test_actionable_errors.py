"""Tests for structured, actionable error classification and the context-var
reset fix that motivated it."""

from __future__ import annotations

import asyncio

import httpx

from olano.core import actionable_errors as ae
from olano.core.actionable_errors import (
    CODE_AUTH_EXPIRED,
    CODE_INVALID_KEY,
    REMEDY_RECONNECT,
    REMEDY_SET_KEY,
)

# The real-world error that motivated this work.
_TOKEN_INVALIDATED = (
    "Error code: 401 - {'error': {'message': 'Your authentication token has been "
    "invalidated. Please try signing in again.', 'code': 'token_invalidated'}, 'status': 401}"
)


def test_codex_token_invalidated_is_reconnect_not_env_key():
    err = ae.classify_exception(Exception(_TOKEN_INVALIDATED), "openai-codex")
    assert err is not None
    assert err.code == CODE_AUTH_EXPIRED
    # The misleading ".env API key" guidance must NOT appear.
    assert "env" not in err.message.lower()
    assert ".env" not in (err.remediation.summary if err.remediation else "")
    assert err.remediation is not None
    assert err.remediation.kind == REMEDY_RECONNECT
    assert err.remediation.action == {
        "type": "reconnect_provider",
        "provider": "openai-codex",
        "auth_type": "device_code",
        "label": err.remediation.action["label"],
    }


def test_env_provider_auth_expired_is_set_key():
    err = ae.classify_exception(Exception(_TOKEN_INVALIDATED), "anthropic")
    assert err is not None
    assert err.code == CODE_AUTH_EXPIRED
    assert err.remediation is not None
    assert err.remediation.kind == REMEDY_SET_KEY
    assert err.remediation.env_var == "ANTHROPIC_API_KEY"


def test_static_invalid_key_distinct_from_expired():
    err = ae.classify_exception(Exception("invalid_api_key"), "openai")
    assert err is not None
    assert err.code == CODE_INVALID_KEY
    assert "api key" in err.message.lower()


def test_generic_unknown_returns_none():
    assert ae.classify_exception(ValueError("something went wrong")) is None


def test_billing_message_is_provider_agnostic_without_hint():
    # Mirrors the runner's contract: no provider hint → no "OpenAI billing".
    err = ae.classify_exception(Exception("Hit insufficient_quota for org"))
    assert err is not None
    assert "OpenAI billing" not in err.message


def test_to_error_frame_includes_code_and_remediation():
    frame = ae.to_error_frame(Exception(_TOKEN_INVALIDATED), "openai-codex")
    assert frame["code"] == CODE_AUTH_EXPIRED
    assert frame["remediation"]["action"]["type"] == REMEDY_RECONNECT
    assert frame["message"]


def test_channel_text_is_self_contained(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://lion.example.com")
    text = ae.channel_text(Exception(_TOKEN_INVALIDATED), "openai-codex")
    assert "⚠️" in text
    assert "Reconnect" in text
    # Includes a deep-link when a public URL is configured.
    assert "https://lion.example.com/providers" in text


def test_channel_text_no_link_without_config(monkeypatch):
    for var in ("LIONCLAW_PUBLIC_URL", "LIONCLAW_DASHBOARD_URL", "LIONCLAW_DASHBOARD_PORT"):
        monkeypatch.delenv(var, raising=False)
    text = ae.channel_text(Exception(_TOKEN_INVALIDATED), "openai-codex")
    assert "http" not in text  # no link, instructions only


def test_retryable_rate_limit_has_no_actionable_cta():
    err = ae.classify_exception(Exception("rate_limit exceeded (429)"))
    assert err is not None and err.retryable is True
    # A retry remedy must not render a CTA line in channel text.
    text = ae.channel_text(err)
    assert "→" not in text


def test_database_locked_is_transient_retry():
    # The exact SQLite error that the cognition fact_distill worker hit on
    # threads.db under concurrent writes — must read as transient, not auth/quota.
    import sqlite3

    err = ae.classify_exception(sqlite3.OperationalError("database is locked"))
    assert err is not None
    assert err.code == ae.CODE_DB_LOCKED
    assert err.retryable is True
    assert err.remediation is not None and err.remediation.kind == ae.REMEDY_RETRY
    # Never misclassified as a credential/billing problem.
    assert "key" not in err.message.lower()
    assert "billing" not in err.message.lower()


def test_coerce_uses_agent_error_detail():
    # An AgentError carrying a pre-classified detail should be reused verbatim.
    from olano.core.runner import _agent_error

    agent_err = _agent_error(Exception(_TOKEN_INVALIDATED), "openai-codex")
    frame = ae.to_error_frame(agent_err)
    assert frame["code"] == CODE_AUTH_EXPIRED


# ── ContextVar reset fix ─────────────────────────────────────────────────────


async def test_push_request_context_survives_cancelled_await():
    """The sync context manager must not raise when its enclosing coroutine is
    cancelled across the wrapped await (the bug: GC-finalized in a different
    asyncio Context → reset() ValueError)."""
    from olano.security.request_context import get_request_context, push_request_context

    async def worker():
        with push_request_context(source="cognition", channel="cognition"):
            assert get_request_context().source == "cognition"
            await asyncio.sleep(10)  # cancelled below

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.02)
    task.cancel()
    # Must not raise; the outer context is restored to the default.
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert get_request_context().source == "local"


def test_push_request_context_tolerates_cross_context_reset():
    """Directly exercise the tolerant-reset path: a token reset from a different
    Context raises ValueError internally, which must be swallowed."""
    import contextvars

    from olano.security import request_context as rc

    gen = rc.push_request_context(source="peer")
    gen.__enter__()
    try:
        # Closing the generator inside a *fresh* Context mimics GC finalization
        # in a different Context — reset(token) would raise ValueError without
        # the guard.
        ctx = contextvars.copy_context()
        ctx.run(gen.__exit__, None, None, None)  # must not raise
    finally:
        # The cross-context exit above cannot reset THIS context's copy of the
        # var, so the peer context would leak into every later test in the
        # process. Clear it explicitly.
        rc._REQUEST_CONTEXT.set(None)
    assert rc.get_request_context().source == "local"


# ── Unrouted managed group ───────────────────────────────────────────────────
# A managed box asks the gateway for its vision group, and the gateway serves
# no such group. The gateway answers with a 400 whose text ends "...to view
# available models for your key", close enough to the generic key/auth
# needles that the rules below it would call this an API-key problem on a box
# holding no API key at all.
_UNROUTED_VISION = (
    "Error code: 400 - {'error': {'message': '/chat/completions: Invalid model name passed in "
    "model=olano-vision. Call `/v1/models` to view available models for your key.', "
    "'type': 'None', 'param': 'None', 'code': '400'}}"
)


def test_unrouted_olano_group_is_not_an_api_key_problem():
    err = ae.classify_exception(Exception(_UNROUTED_VISION), "olano")
    assert err is not None
    assert err.code == ae.CODE_OLANO_UNROUTED
    # The whole point: never send someone hunting for a credential.
    assert "api key" not in err.message.lower() or "no API key" in err.message
    assert err.code != CODE_INVALID_KEY
    # It names the service and the group, so the reader knows what is down.
    assert "image understanding" in err.message
    assert "olano-vision" in err.message
    # Not retryable — retrying cannot publish a missing routing row.
    assert err.retryable is False


def test_unrouted_group_remediation_points_at_the_deployment_not_the_vault():
    err = ae.classify_exception(Exception(_UNROUTED_VISION), "olano")
    assert err is not None and err.remediation is not None
    assert err.remediation.kind == ae.REMEDY_CONTACT
    assert "olano models gateway --probe" in err.remediation.summary
    # A channel message must carry the remediation text (REMEDY_CONTACT is
    # deliberately not in channel_text's skip list, unlike RETRY/NONE).
    text = ae.channel_text(err, "olano")
    assert "gateway" in text.lower()
    assert err.remediation.summary.split(".")[0] in text


def test_unrouted_classification_reads_the_http_response_body():
    """HTTP errors carry their meaning in the BODY: `str(exc)` is only
    "Client error '400 Bad Request' for url …", which matches no rule. Passing
    `text=` is what lets the image tools classify a gateway refusal at all."""
    opaque = Exception("Client error '400 Bad Request' for url 'https://gateway/v1/images'")
    assert ae.classify_exception(opaque, "olano") is None
    err = ae.classify_exception(opaque, "olano", text=f"400: {_UNROUTED_VISION}")
    assert err is not None and err.code == ae.CODE_OLANO_UNROUTED


def test_managed_tier_unrouted_is_labelled_as_a_tier():
    err = ae.classify_exception(Exception("Invalid model name passed in model=olano-pro."), "olano")
    assert err is not None and err.code == ae.CODE_OLANO_UNROUTED
    assert "the pro tier" in err.message


def test_olano_budget_and_auth_rules_still_win_over_the_unrouted_rule(monkeypatch):
    """The unrouted check runs first, so guard that it did not swallow the
    billing/auth cases it sits in front of. (A managed box: the test process
    is itself an open, keyless box, where a 401 means "connect a key".)"""
    monkeypatch.setattr(ae, "_unattached_open_box", lambda: False)
    budget = ae.classify_exception(Exception("ExceededBudget: max_budget reached"), "olano")
    assert budget is not None and budget.code == ae.CODE_OLANO_CREDITS
    auth = ae.classify_exception(Exception("Error code: 401 - unauthorized"), "olano")
    assert auth is not None and auth.code == CODE_AUTH_EXPIRED


# ── Relayed upstream refusal ("Insufficient Balance") ────────────────────────
# The provider account behind the turbo tier is out of balance, and the gateway
# relays the provider's 402 as-is. Without a rule for it the raw exception
# reaches the user, naming no fix and naming the provider backing the tier.
# The provider name in the fixture is a placeholder; the wire shape is what the
# classifier keys on.
_UPSTREAM_INSUFFICIENT_BALANCE = (
    "Error code: 402 - {'error': {'message': 'litellm.BadRequestError: ExampleProviderException - "
    '{"error":{"message":"Insufficient Balance","type":"unknown_error","param":null,'
    '"code":"invalid_request_error"}}. Received Model Group=olano-turbo\n'
    "Available Model Group Fallbacks=None', 'type': None, 'param': None, 'code': '402'}}"
)


def test_relayed_upstream_refusal_is_an_olano_problem_not_the_customers():
    err = ae.classify_exception(Exception(_UPSTREAM_INSUFFICIENT_BALANCE), "olano")
    assert err is not None
    assert err.code == ae.CODE_OLANO_UPSTREAM
    # It names the tier that is down; what to do about it is the remediation's
    # job, and saying it in both is how one outage read as the same sentence
    # three times over.
    assert "turbo tier" in err.message.lower()
    assert err.remediation is not None
    assert ae.SUPPORT_CONTACT in err.remediation.summary
    assert ae.SUPPORT_CONTACT in ae.channel_text(err, "olano")
    # Never "buy more credits": their balance is fine, ours is not.
    assert err.code != ae.CODE_OLANO_CREDITS
    assert "buy" not in err.message.lower()
    # Retrying immediately cannot refill an account we own.
    assert err.retryable is False


def test_upstream_message_never_leaks_which_provider_backs_the_tier():
    """`models/olano_gateway.py`: which real model served a request must not be
    inferable from a user-facing surface. The raw passthrough broke that on
    every surface at once — this is the regression that matters most."""
    err = ae.classify_exception(Exception(_UPSTREAM_INSUFFICIENT_BALANCE), "olano")
    assert err is not None
    low = err.message.lower()
    for leaked in ("exampleprovider", "litellm", "402", "apistatuserror", "insufficient balance"):
        assert leaked not in low, f"user-facing message leaks {leaked!r}"
    # The raw text still travels for the logs — it is documented as never shown.
    assert err.detail == _UPSTREAM_INSUFFICIENT_BALANCE


def test_upstream_remediation_offers_support_not_a_settings_hunt():
    err = ae.classify_exception(Exception(_UPSTREAM_INSUFFICIENT_BALANCE), "olano")
    assert err is not None and err.remediation is not None
    rem = err.remediation
    assert rem.kind == ae.REMEDY_CONTACT
    assert ae.SUPPORT_CONTACT in rem.summary
    assert rem.url is not None and rem.url.startswith(f"mailto:{ae.SUPPORT_CONTACT}")
    assert rem.action == {"type": "open_url", "url": rem.url, "label": "Email support"}
    # A channel can't render a button, so the address must survive as text.
    assert ae.SUPPORT_CONTACT in ae.channel_text(err, "olano")


def test_relayed_upstream_auth_failure_is_not_the_deployments_gateway_key():
    """Branch order guard. A relayed upstream auth error carries the word
    "authentication", which the gateway-auth rule below matches — that would
    tell the reader to retry while the gateway key re-enrolls, looping forever
    against a problem no retry can fix."""
    relayed = (
        "litellm.AuthenticationError: ExampleProviderException - Authentication Fails, "
        "Your api key is invalid. Received Model Group=olano-pro\n"
        "Available Model Group Fallbacks=None"
    )
    err = ae.classify_exception(Exception(relayed), "olano")
    assert err is not None
    assert err.code == ae.CODE_OLANO_UPSTREAM
    assert err.code != CODE_AUTH_EXPIRED
    assert err.retryable is False


def test_budget_still_wins_over_the_upstream_rule():
    """The gateway refuses on budget BEFORE it routes, so a budget error can never
    carry the relay markers — but the ordering is what guarantees a genuine
    credit exhaustion is never re-labelled an Olano-side outage."""
    err = ae.classify_exception(Exception("ExceededBudget: max_budget reached"), "olano")
    assert err is not None and err.code == ae.CODE_OLANO_CREDITS


def test_payment_refusal_from_the_gateway_itself_is_the_deployments_credits():
    """No relay markers ⇒ nothing was routed ⇒ it is this deployment's balance,
    the same reading `deployment/services_client.py` gives a 402."""
    err = ae.classify_exception(
        Exception("Error code: 402 - {'error': {'message': 'Payment Required'}}"), "olano"
    )
    assert err is not None
    assert err.code == ae.CODE_OLANO_CREDITS
    assert err.remediation is not None and err.remediation.kind == ae.REMEDY_TOP_UP


def test_byok_account_out_of_balance_is_never_told_to_email_olano():
    """The identical provider text on a customer's OWN key. Their account is the
    empty one, so the fix is their top-up page — not our support inbox."""
    err = ae.classify_exception(Exception(_UPSTREAM_INSUFFICIENT_BALANCE), "deepseek")
    assert err is not None
    assert err.code == ae.CODE_BILLING
    assert ae.SUPPORT_CONTACT not in err.message
    assert err.remediation is not None
    assert err.remediation.url == "https://platform.deepseek.com/top_up"
    # Without a url there is no action dict, and the dashboard renders no button.
    assert err.remediation.action is not None


def test_support_contact_matches_the_licensing_layers_copy():
    """Two modules hold this address (core keeps a literal so it stays
    stdlib-only at import time). Pin them together so neither drifts."""
    from olano.deployment import resolver

    assert ae.SUPPORT_CONTACT == resolver.SUPPORT_CONTACT


def test_channel_text_if_classified_defers_only_when_it_knows_something():
    """Surfaces with their own generic wording (the platform adapters, the CLI)
    must not trade it for a bare exception string on an unrecognized error."""
    assert ae.channel_text_if_classified(Exception("something entirely unknown")) is None
    known = ae.channel_text_if_classified(Exception(_UPSTREAM_INSUFFICIENT_BALANCE), "olano")
    assert known is not None and ae.SUPPORT_CONTACT in known


def test_remediation_lines_are_the_tail_channel_text_appends():
    err = ae.classify_exception(Exception(_UPSTREAM_INSUFFICIENT_BALANCE), "olano")
    assert err is not None
    lines = ae.remediation_lines(err)
    assert lines and lines[0].startswith("→ ")
    # The mailto stays on the remediation ACTION, where the dashboard renders a
    # button — but never as a line of text. Pasted into a chat bubble it arrives
    # as a wall of percent-encoding under a message that already gives the
    # address in words.
    assert err.remediation is not None and err.remediation.url.startswith("mailto:")
    assert not any(line.startswith("mailto:") for line in lines)
    assert "%20" not in ae.channel_text(err, "olano")
    assert "\n".join(lines) in ae.channel_text(err, "olano")
    # A retryable error has nothing worth printing under it.
    rate = ae.classify_exception(Exception("429 rate limit"), "openai")
    assert rate is not None and ae.remediation_lines(rate) == []


def test_402_is_matched_as_a_status_code_not_as_three_digits():
    """A context-overflow message reading "you requested 14023 tokens" contains
    "402" as a substring; a bare needle would file it as a billing problem."""
    # The payment rule sits above the context rule, so a loose "402" needle
    # would win here and answer "top up your account" to an overflowing thread.
    overflow = "context_length exceeded: you requested 14023 tokens"
    err = ae.classify_exception(Exception(overflow), "openai")
    assert err is not None and err.code == ae.CODE_CONTEXT
    # And the real thing still matches, in both the bare and quoted shapes.
    for text in ("Error code: 402 - Payment Required", "{'code': '402'}"):
        hit = ae.classify_exception(Exception(text), "anthropic")
        assert hit is not None and hit.code == ae.CODE_BILLING, text


# ── LangGraph step budget (GraphRecursionError) ──────────────────────────────

# The exact string LangGraph raises — and the one the Cortex Think panel used
# to show verbatim, advising a `recursion_limit` config key no user can set.
_RECURSION_LIMIT = (
    "Recursion limit of 12 reached without hitting a stop condition. "
    "You can increase the limit by setting the `recursion_limit` config key."
)


def test_recursion_limit_message_is_step_budget_exhausted():
    # Cognition routes worker error STRINGS through here re-wrapped in a bare
    # Exception (classify_cognition_error_full), so the message match — not the
    # exception type — is the load-bearing path.
    err = ae.classify_exception(Exception(_RECURSION_LIMIT))
    assert err is not None
    assert err.code == ae.CODE_STEP_BUDGET
    assert err.retryable is True
    assert err.remediation is not None
    assert err.remediation.kind == ae.REMEDY_RETRY
    # The raw LangGraph advice must not leak: `recursion_limit` is not a key
    # users can set.
    assert "recursion_limit" not in err.message
    # The remediation must name a control the reader can actually reach. It used
    # to name `cognition.worker_max_iterations`, which was a dead end — the field
    # existed only in agent.json5, so acting on this advice meant SSH-ing into
    # the box. Now it points at the Cortex → Autonomy rows, and says both tiers
    # exist (this agent / all agents) because the fleet-wide one is the fix when
    # every agent is hitting the same wall.
    summary = err.remediation.summary
    assert "recursion_limit" not in summary
    assert "step budget" in summary
    assert "Cortex" in summary


def test_graph_recursion_error_type_is_step_budget_exhausted():
    # Live exception path (chat/runner) — matched by type name even if the
    # message wording changes upstream.
    exc_type = type("GraphRecursionError", (Exception,), {})
    err = ae.classify_exception(exc_type(""))
    assert err is not None
    assert err.code == ae.CODE_STEP_BUDGET


def test_recursion_limit_on_managed_tier_is_not_a_gateway_error():
    # provider "olano" runs the gateway rules FIRST — a local step-budget
    # failure must not be re-labelled a credits/auth/gateway problem.
    err = ae.classify_exception(Exception(_RECURSION_LIMIT), "olano")
    assert err is not None
    assert err.code == ae.CODE_STEP_BUDGET


# ── the message a user actually reads ────────────────────────────────────────


def test_group_attr_matches_the_one_the_gateway_client_stamps():
    """Two modules hold this name (core keeps a literal so it stays stdlib-only
    at import time). Pin them together, or the label silently falls back to
    regexing the gateway's error prose again."""
    from olano.models import olano_gateway

    assert ae._GROUP_ATTR == olano_gateway.GROUP_ATTR


def test_the_group_comes_from_the_call_site_not_from_provider_prose():
    """When the group name has to be recovered from the gateway's error text
    and the regex misses, an outage degrades to "This service (Olano-managed)
    is temporarily unavailable". The client knows the wire name exactly and
    stamps it on the exception."""
    # Error text with NO parseable "Model Group=" — the shape that degraded.
    exc = Exception("litellm.APIError: upstream refused the request")
    setattr(exc, ae._GROUP_ATTR, "olano-vision")
    err = ae.classify_exception(exc, "olano")
    assert err is not None and err.code == ae.CODE_OLANO_UPSTREAM
    assert err.message.startswith("Image understanding (Olano-managed)")


def test_the_prose_fallback_still_answers_for_an_untagged_error():
    """Not every relayed failure comes through the gateway client — a probe or a
    re-wrapped worker error string still has only the text to go on."""
    exc = Exception("litellm.APIError: upstream refused. Received Model Group=olano-max")
    err = ae.classify_exception(exc, "olano")
    assert err is not None and "max tier" in err.message.lower()


def test_an_unnameable_group_still_degrades_gracefully():
    exc = Exception("litellm.APIError: upstream refused the request")
    err = ae.classify_exception(exc, "olano")
    assert err is not None and err.message.startswith("This service (Olano-managed)")


def test_the_channel_message_says_it_once():
    """A channel bubble must not carry the same instruction three times (in the
    message, in the remediation summary, and spelled out again inside a raw
    percent-encoded mailto)."""
    err = ae.classify_exception(Exception(_UPSTREAM_INSUFFICIENT_BALANCE), "olano")
    text = ae.channel_text(err, "olano")
    assert text.lower().count("try again") == 1
    assert text.count(ae.SUPPORT_CONTACT) == 1
    assert "mailto:" not in text
    assert "%20" not in text
    # Still self-contained: what broke, and what to do about it.
    assert "turbo tier" in text.lower()
    assert "credits are unaffected" in text


def test_a_remediation_that_only_restates_the_message_is_dropped():
    err = ae.ActionableError(
        "x",
        "The thing is temporarily unavailable. Try again in a few minutes.",
        remediation=ae.Remediation(kind=ae.REMEDY_CONTACT, summary="Try again in a few minutes."),
    )
    assert (
        ae.channel_text(err)
        == "⚠️ The thing is temporarily unavailable. Try again in a few minutes."
    )


def test_a_remediation_that_adds_something_always_survives():
    """Exact containment, never fuzzy — a remediation with anything new in it
    must never be swallowed."""
    err = ae.ActionableError(
        "x",
        "The thing is temporarily unavailable.",
        remediation=ae.Remediation(
            kind=ae.REMEDY_CONTACT,
            summary="Try again in a few minutes.",
            url="https://cloud.olano.ai/app",
        ),
    )
    text = ae.channel_text(err)
    assert "→ Try again in a few minutes." in text
    # An http link is followable, so it still travels.
    assert "https://cloud.olano.ai/app" in text


# The Telegram report that motivated the ollama content shim: a peer agent's
# turn died with an unclassified ValueError, so the user saw only
# "[talk_to_agent error] Calling 'lucia' failed: API error (ValueError): …".
_OLLAMA_BLOCK_REFUSAL = (
    "Unsupported message content type. Must either have type 'text' or type "
    "'image_url' with a string 'image_url' field."
)


def test_unsupported_content_block_is_classified_not_a_raw_valueerror():
    err = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL), "ollama")
    assert err is not None
    assert err.code == ae.CODE_MESSAGE_FORMAT
    assert "new thread" in err.message.lower()
    # Never retryable: the offending block is in the checkpoint, so the same
    # thread fails identically forever.
    assert err.retryable is False


def test_unsupported_content_block_is_not_mistaken_for_a_context_overflow():
    """Both are "request shape", but only one is fixed by compacting."""
    err = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL), "ollama")
    assert err is not None
    assert err.code != ae.CODE_CONTEXT


def test_unsupported_content_block_reaches_a_channel_with_a_next_step():
    err = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL), "ollama")
    text = ae.channel_text(err, "ollama")
    assert "Ollama" in text
    assert "new thread" in text.lower()


def test_unsupported_content_block_carries_a_dashboard_action():
    """A remediation with no `action` renders NOTHING in the dashboard.

    `views/chat/ErrorRemediation.tsx` returns null when `action` is absent, so
    a summary alone would have made this the only classified error in the
    product whose message says "start a new thread" while the UI offers no way
    to. `kind` and `action` are independent: REMEDY_NONE keeps the summary out
    of the chat-channel tail, the action still draws the button.
    """
    err = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL), "ollama")
    assert err is not None
    assert err.remediation is not None
    assert err.remediation.kind == ae.REMEDY_NONE
    assert (err.remediation.action or {}).get("type") == "new_thread"
    assert ae.to_error_frame(err)["remediation"]["action"]["type"] == "new_thread"


def test_new_thread_action_carries_no_backend_label():
    """The label is the frontend's: it is localized, the backend is not."""
    err = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL), "ollama")
    assert err is not None and err.remediation is not None
    assert "label" not in (err.remediation.action or {})


def test_new_thread_action_never_leaks_a_providers_link_into_a_channel():
    """REMEDY_NONE short-circuits `remediation_lines`, so the deep-link helper
    (which returns /providers for ANY action) can't append a wrong link."""
    err = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL), "ollama")
    assert ae.remediation_lines(err, "ollama") == []
    assert "/providers" not in ae.channel_text(err, "ollama")


def test_unsupported_content_block_message_reads_well_without_a_provider():
    """The chat stream classifies with no provider, so the message must not
    open with `_provider_name(None)` — the lowercase "the model provider"."""
    err = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL))
    assert err is not None
    assert err.message.startswith("This agent's model could not read")
    # Still names the provider when one is known.
    named = ae.classify_exception(ValueError(_OLLAMA_BLOCK_REFUSAL), "ollama")
    assert named is not None and named.message.startswith("Ollama")


# ── Transport failures ───────────────────────────────────────────────────────
# The shape this block exists to prevent: a follow-up question on a messenger
# channel answered with
#
#   Sorry, I encountered an error (AgentError).
#   API error (ReadError):
#   Try again or use /new to start a fresh session.
#
# — an exception class name, a second exception class name, and a sentence
# ending on a bare colon, because `str(httpx.ReadError())` is the empty string.
# Resending the identical message worked first time, which is exactly what a
# dropped socket means.


def test_a_message_less_transport_error_is_still_classified():
    """The rule that matters: a transport exception carries no text, so a
    text-only classifier misses it and the raw class name reaches the user."""
    err = ae.classify_exception(httpx.ReadError(""))
    assert err is not None
    assert err.code == ae.CODE_NETWORK
    assert err.retryable is True


def test_transport_error_message_names_nothing_technical():
    """It is read by the person the agent is talking to, not by an operator."""
    err = ae.classify_exception(httpx.ReadError(""))
    assert err is not None
    text = ae.channel_text(err)
    for leak in ("ReadError", "AgentError", "Exception", "API error", "httpx", "/new"):
        assert leak not in text
    # …and it asks for the one thing that actually fixes it.
    assert "again" in text.lower()
    # The raw exception still travels for the logs.
    assert err.detail


def test_a_transient_failure_is_asked_plainly_and_a_real_one_is_flagged():
    """The warning sign means "this needs you". A failure that fixes itself on
    the next message must not wear one on a customer-facing channel."""
    transient = ae.classify_exception(httpx.ReadError(""))
    assert transient is not None
    assert not ae.channel_text(transient).startswith("⚠️")
    # An expired credential still does.
    real = ae.classify_exception(Exception(_TOKEN_INVALIDATED), "openai-codex")
    assert real is not None
    assert ae.channel_text(real).startswith("⚠️")


def test_transport_names_do_not_shadow_a_credential_or_billing_failure():
    """Section ordering: every credential/billing rule has already had its say
    by the time the transport rule runs, so an auth 401 that happens to arrive
    on a connection error stays an auth 401."""
    err = ae.classify_exception(Exception("Connection error. 401 invalid_api_key"), "openai")
    assert err is not None
    assert err.code == CODE_INVALID_KEY


def test_a_bare_connection_word_does_not_become_a_model_outage():
    """The needles are deliberately specific — "connection to the database
    failed" is a local problem and must not be filed as a dropped model hop."""
    assert ae.classify_exception(Exception("connection to the database failed")) is None


def test_timeout_type_names_classify_as_timeouts_not_network():
    """``ReadTimeout`` is as message-less as ``ReadError``; it must still land
    in the timeout class rather than the connection one."""

    err = ae.classify_exception(httpx.ReadTimeout(""))
    assert err is not None
    assert err.code == ae.CODE_TIMEOUT
    assert err.retryable is True


def test_a_blocked_account_team_reads_as_out_of_credits(monkeypatch):
    """The cloud blocks an org's gateway team while its wallet is empty (the
    proxy's own budget check is strict and would serve one more call at zero),
    and the proxy then says `Team=<id> is blocked`: the same situation as a
    spent budget, so the same code and the same fix."""
    from olano.core import actionable_errors as ae

    monkeypatch.setattr(ae, "_gateway_key_source", lambda: "account")
    err = ae.classify_exception(
        Exception(
            "Error code: 401 - {'error': {'message': 'Team=org-org_1 is blocked. Reason: budget', 'code': '401'}}"
        ),
        "olano",
    )
    assert err is not None and err.code == ae.CODE_OLANO_CREDITS
    assert "account" in err.message.lower()


def test_a_401_with_no_key_on_a_self_hosted_box_says_connect_one(monkeypatch):
    """After the account key is removed the live models hold the placeholder;
    that 401 is nobody's expired session and never renews itself."""
    from olano.core import actionable_errors as ae

    monkeypatch.setattr(ae, "_gateway_key_source", lambda: None)
    monkeypatch.setattr(ae, "_unattached_open_box", lambda: True)
    err = ae.classify_exception(
        Exception("Error code: 401 - Authentication Error, Invalid proxy server token passed"),
        "olano",
    )
    assert err is not None and err.code == ae.CODE_OLANO_KEY_MISSING
    assert err.retryable is False
    assert "Providers" in err.message


def test_a_managed_box_keeps_the_self_healing_401_text(monkeypatch):
    """Byte-identical to before the account key existed: the deployment key
    re-enrolls itself, so the message promises exactly that."""
    from olano.core import actionable_errors as ae

    monkeypatch.setattr(ae, "_gateway_key_source", lambda: "deployment")
    monkeypatch.setattr(ae, "_unattached_open_box", lambda: False)
    err = ae.classify_exception(
        Exception("Error code: 401 - Authentication Error, Invalid proxy server token passed"),
        "olano",
    )
    assert err is not None and err.code == CODE_AUTH_EXPIRED
    assert err.retryable is True
    assert err.message.startswith("This deployment's gateway access needed to be refreshed.")
