"""Olano-managed model groups — the tiers (``olano:auto|turbo|pro|max``)
and the utility groups the product calls on the agent's behalf
(``olano:vision``).

These are not a provider in the usual sense. Every other entry in
:data:`olano.models.PROVIDERS` names an API key this box holds; an Olano tier
names a **service Olano runs**, reached through the Olano model gateway at
``gateway.olano.ai`` and paid for in olano credits.

Why the indirection — the thing to not "optimize away" later
-----------------------------------------------------------
It would be simpler to ship an Olano-owned provider key in ``olano.env`` and
point the tier straight at Anthropic/OpenAI. That is unshippable. This box runs
on infrastructure the customer controls, and the product deliberately hands the
agent a shell, a filesystem and a readable credential vault — so any provider
key placed here is a provider key given away, with no rate limit we could
enforce (the enforcement would run on their machine) and no revocation short of
rotating a fleet-wide secret. What the box holds instead is a **per-deployment
virtual key** (:mod:`olano.deployment.gateway_key`): budgeted to exactly this
deployment's credit balance, rate-limited, and revocable from the control
plane.

Consequences that show up in this file:

- The gateway is OpenAI-compatible, so the client is a plain ``ChatOpenAI``
  pointed at ``{gateway}/v1`` with the virtual key. No bespoke transport —
  streaming, tool calls, and every langchain/deepagents feature ride the
  standard OpenAI wire format the gateway translates per backend.
- **Wire model names are hyphenated group names** (``olano-pro``), never
  provider model ids: this process cannot name a real model on the managed
  path even by accident, and the gateway resolves the group to whatever
  backend currently serves it.
- **Tiers are routed on price/latency; utility groups are routed on a
  capability.** The tiers can be re-pointed at any backend that fits their
  price point, including a text-only model, so a feature that needs a
  *capability* must not ride on a tier. ``olano-vision``
  exists for exactly that: its membership condition is "accepts image parts",
  which makes ``image_describe``'s guarantee a property of the cloud routing
  table rather than an assumption on this box.
- The charge for a call comes back from the gateway (response cost header,
  non-streamed calls) or is computed from token usage × the tier's published
  credit rate (streamed calls: the gateway cannot know a stream's cost at
  header-time). Which real model served the request is not returned and must
  not be inferred into any user-facing surface: the customer bought
  "Olano Pro", and what backs it may change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
import weakref
from datetime import datetime
from typing import Any, cast

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

# Customer-facing tiers. `turbo` is the default — the cheap, fast, no-knobs
# tier every plan can reach; `max` is the top tier.
OLANO_TIERS: tuple[str, ...] = ("auto", "turbo", "pro", "max")
DEFAULT_TIER = "turbo"

# Tiers that still RESOLVE but are no longer OFFERED (filtered out of
# `/api/catalog/providers`, so no picker lists them).
#
# `auto` is here because it does not yet do what its name promises: the gateway
# serves the `olano-auto` group exactly as it serves `olano-turbo`, at
# byte-identical credit rates (see BUNDLED_TIER_CREDIT_RATES below), so
# "Olano picks the best model per request" was a claim nothing implemented.
# A per-request router at the gateway is what would make it real; until the
# gateway runs one, offering the tier sells routing that isn't happening.
#
# Hidden, NOT removed, and deliberately not aliased to `turbo`: agents already
# configured with `olano:auto` must keep resolving, and must keep sending the
# `olano-auto` group name on the wire — a deployment's virtual key may be
# scoped to the model groups its plan listed, so silently switching the wire
# name could 400 a working agent.
#
# ── BEFORE UN-HIDING `auto`, FIX THE BILLING ─────────────────────────────────
# Deleting this entry is enough to make the tier selectable again, and that is
# exactly the trap: `estimate_credits_micro()` below charges every *streamed*
# turn — which is every normal turn — from ONE published rate per tier. That
# arithmetic is exact today only because auto and turbo are served identically
# at the same price. The moment the router spreads Auto across backends, a turn
# it sends to the max backend still bills at auto's single rate (currently
# turbo's: 490 vs 26250 output credits per 1M, a ~50x under-charge on our
# display), and the cloud ledger silently disagrees with what the customer sees.
#
# So the router landing is a necessary but NOT sufficient condition. One of
# these has to come with it:
#   1. the gateway reports the resolved cost for streamed calls (a usage-frame
#      field, or a trailing chunk) and `estimate_credits_micro` prefers it over
#      the table — the honest fix, and the only one that stays exact;
#   2. `auto` is priced at the most expensive tier it can route to — safe for
#      the ledger, but it over-charges every cheap turn, which defeats the
#      point of routing;
#   3. the drift is accepted and the UI says so — the cloud balance is already
#      authoritative, but a visibly wrong per-turn number erodes trust in the
#      whole credits display.
# Whichever we pick, `BUNDLED_TIER_CREDIT_RATES["auto"]` stops being a copy of
# turbo's row and needs its own answer.
HIDDEN_TIERS: frozenset[str] = frozenset({"auto"})

# Tiers no per-tier entitlement flag may withhold. `turbo` because it is the
# default and a plan with no usable tier is a broken plan; `auto` because it
# was the default before turbo, so existing agents pointing at it must not
# start failing for want of an `olano_model_auto` flag no deployment sets.
UNGATED_TIERS: frozenset[str] = frozenset({"auto", "turbo"})

# Utility groups — gateway groups that are NOT tiers. The product calls these
# on the agent's behalf (vision for `image_describe`, `olano-image` for
# generation); they are never offered as a brain the customer picks, which is
# why they stay out of OLANO_TIERS / PROVIDERS default_models / every dropdown.
# Their routing guarantees a *capability* (accepts image parts / emits images)
# rather than a price-latency point, so the cloud can re-route the tiers freely
# without silently taking vision away from every managed box.
OLANO_VISION_MODEL = "olano-vision"
# `olano:vision` — the colon form callers pass to `build_model`.
OLANO_VISION_MODEL_STR = "olano:vision"

# The cloud catalog needs rows for the utility groups (that is where their
# price and routing are configured), and a catalog row lands in the dashboard's
# model pickers as soon as its modality reads `chat` — which is the default.
# This set is the instance-side guarantee that a mis-typed cloud row can't turn
# a utility group into a selectable brain. One list, every surface.
OLANO_UTILITY_MODEL_IDS: frozenset[str] = frozenset({OLANO_VISION_MODEL_STR, "olano:olano-image"})


def is_olano_utility_model(model_str: str | None) -> bool:
    """True for an Olano group the product calls on the agent's behalf."""
    return (model_str or "").strip().lower() in OLANO_UTILITY_MODEL_IDS


# `flash` was the pre-launch name for `turbo` and still appears in the
# deployment-settings feature placeholders (`olano_model_flash`). Accept it so a
# box that upgraded its runtime ahead of its settings keeps working.
#
# The top tier's former id (`reasoning`) is deliberately NOT here: it was
# renamed to `max` as a hard cutover, so `olano:reasoning` is an unknown tier
# and says so, rather than quietly resolving to something the string does not
# name. Do not add it back — the word `reasoning` now belongs exclusively to
# the effort-level concept (`reasoning_efforts` below, `reasoning_effort` on
# the wire), which is a different thing that happens to share the word.
_TIER_ALIASES: dict[str, str] = {"flash": "turbo"}

# Static per-tier metadata for the surfaces that need a number *before* any
# call is made: the context-window bar, and the reasoning-effort picker.
#
# Why static and conservative: the gateway deliberately never reveals which
# real model backs a tier, so the instance cannot probe. `context_window` is
# the MINIMUM across the tier's routed targets, as published by the cloud
# routing table. Underestimating only compacts context earlier; overestimating
# overflows mid-turn, so the floor is always the smallest window a tier can
# currently land on. Offline fallback: the synced catalog overrides this table.
# `reasoning_efforts` is the effort picker the dashboard offers, NOT a claim
# about whether the backend thinks. The backends behind the tiers may reason
# whether or not an effort is requested, and reasoning tokens bill at the
# tier's OUTPUT rate. The effort control is exposed where the backend takes it
# and it is worth a user's attention; turbo deliberately keeps none (it exists
# to be the fast, no-knobs tier).
OLANO_TIER_INFO: dict[str, dict[str, Any]] = {
    "auto": {"context_window": 1_000_000, "reasoning_efforts": ()},
    "turbo": {"context_window": 1_000_000, "reasoning_efforts": ()},
    "pro": {"context_window": 1_000_000, "reasoning_efforts": ("low", "medium", "high")},
    "max": {"context_window": 1_000_000, "reasoning_efforts": ("low", "medium", "high")},
}


def tier_info(tier_or_model: str | None) -> dict[str, Any] | None:
    """Static metadata for a tier, accepting ``"pro"`` or ``"olano:pro"``."""
    if not tier_or_model:
        return None
    name = tier_or_model.partition(":")[2] if ":" in tier_or_model else tier_or_model
    tier = normalize_tier(name)
    return OLANO_TIER_INFO.get(tier) if tier else None


def tier_context_window(model_str: str | None) -> int | None:
    """Conservative context window for an ``olano:<tier>`` string, else None."""
    info = tier_info(model_str) if is_olano_model(model_str) else None
    return int(info["context_window"]) if info else None


# Launch credit rates, credits per 1M tokens: the SAME numbers the gateway
# prices the tiers with, so a token-count computation here lands on the
# identical charge the gateway's spend log records. Used only when the
# gateway's own cost header is absent, which is every *streamed* call (the
# gateway sends response headers before a stream's cost is knowable). The
# synced catalog's `metadata.credit_pricing` block overrides this table once
# the cloud publishes tier rows, so a reprice reaches the fleet without a
# runtime update; until a box has synced, a reprice needs a runtime update to
# show exactly. The authoritative balance always comes from cloud.olano.ai
# either way. Rates are expressed in the v1 denomination (1 credit = $0.001).
BUNDLED_TIER_CREDIT_RATES: dict[str, dict[str, float]] = {
    "auto": {"input": 245.0, "cached_input": 4.9, "output": 490.0},
    "turbo": {"input": 245.0, "cached_input": 4.9, "output": 490.0},
    "pro": {"input": 761.25, "cached_input": 6.34375, "output": 1522.5},
    "max": {"input": 5250.0, "cached_input": 525.0, "output": 26250.0},
}


class OlanoGatewayUnavailable(RuntimeError):
    """Raised when an Olano tier is selected but this box cannot reach the
    gateway — almost always an unmanaged install with no deployment token."""


def normalize_tier(name: str | None) -> str | None:
    """``"Pro"`` → ``"pro"``, ``"flash"`` → ``"turbo"``; None when not a tier."""
    if not name:
        return None
    key = name.strip().lower()
    key = _TIER_ALIASES.get(key, key)
    return key if key in OLANO_TIERS else None


def offered_tiers() -> tuple[str, ...]:
    """The tiers a picker may list — every tier except :data:`HIDDEN_TIERS`."""
    return tuple(t for t in OLANO_TIERS if t not in HIDDEN_TIERS)


def tier_offered(tier_or_model: str | None) -> bool:
    """False for a tier that still resolves but must not be offered."""
    if not tier_or_model:
        return False
    name = tier_or_model.partition(":")[2] if ":" in tier_or_model else tier_or_model
    tier = normalize_tier(name)
    return bool(tier) and tier not in HIDDEN_TIERS


def is_olano_model(model_str: str | None) -> bool:
    """True for ``olano:<tier>`` strings (the billing_source discriminator)."""
    if not model_str:
        return False
    provider, _, tier = model_str.partition(":")
    return provider.strip().lower() == "olano" and normalize_tier(tier) is not None


def gateway_base_url() -> str:
    """OpenAI-compatible base URL of the Olano model gateway (``…/v1``)."""
    from olano.deployment.gateway_key import openai_base_url

    return openai_base_url()


def tier_credit_rates(tier: str, at: datetime | None = None) -> dict[str, float] | None:
    """Credits-per-1M-token rates for a tier: synced catalog → bundled.

    The catalog contract: the ``olano:<tier>`` model
    row carries ``metadata.credit_pricing`` — credits per 1M tokens, the
    customer-facing price list. When the catalog hasn't synced (fresh box,
    offline) the bundled launch table answers.

    A tier whose backend has off-peak hours (DeepSeek) carries
    ``credit_pricing.off_peak``: the same three rates and the windows the
    gateway bills them in. ``at`` (default: now) inside a window returns those,
    so the estimate halves at the same moment the gateway's charge does.
    """
    resolved = normalize_tier(tier)
    if not resolved:
        return None
    try:
        from olano.catalog import models_registry

        entry = models_registry.model_entry(f"olano:{resolved}")
        block = None
        if isinstance(entry, dict):
            meta = entry.get("metadata")
            block = meta.get("credit_pricing") if isinstance(meta, dict) else None
            if block is None:
                block = entry.get("credit_pricing")
        if isinstance(block, dict):
            from olano.catalog.off_peak import active_off_peak

            off = active_off_peak(block.get("off_peak"), at)
            if off is not None and (off.get("input") or off.get("output")):
                block = off
            rates = {
                "input": float(block.get("input", 0.0) or 0.0),
                "cached_input": float(block.get("cached_input", 0.0) or 0.0),
                "output": float(block.get("output", 0.0) or 0.0),
            }
            if rates["input"] > 0 or rates["output"] > 0:
                return rates
    except Exception:  # pragma: no cover - catalog read must never break costing
        pass
    return BUNDLED_TIER_CREDIT_RATES.get(resolved)


def estimate_credits_micro(
    model_str: str | None, usage: dict[str, int], at: datetime | None = None
) -> int:
    """Micro-credits for a managed turn, from token counts × the tier rate.

    This is the streamed-call charge path: the gateway's cost header rides the
    response *headers*, which for a stream are sent before any token is
    generated, so streamed calls (= every normal turn) carry no header number.
    Rates are credits per 1M tokens, so micro-credits = tokens × rate exactly —
    the same arithmetic the gateway's spend log applies, no rounding drift.
    Returns 0 for non-olano models or when no rate resolves. ``at`` is when
    the call ran (default: now), for a tier with off-peak hours.

    **Exact only while one tier means one backend.** Every tier today routes to
    a single model, so its published rate IS the call's rate. A tier that fans
    out across backends of different prices — which is what un-hiding `auto`
    would introduce — makes this a guess against whatever the gateway actually
    charged. See the un-hiding checklist on :data:`HIDDEN_TIERS` before
    changing that.
    """
    if not is_olano_model(model_str):
        return 0
    rates = tier_credit_rates((model_str or "").partition(":")[2], at)
    if not rates:
        return 0
    input_tokens = max(0, int(usage.get("input_tokens", 0) or 0))
    cached = min(max(0, int(usage.get("cache_read", 0) or 0)), input_tokens)
    output_tokens = max(0, int(usage.get("output_tokens", 0) or 0))
    micro = (
        (input_tokens - cached) * rates["input"]
        + cached * rates["cached_input"]
        + output_tokens * rates["output"]
    )
    return max(0, round(micro))


def build_olano_model(tier: str, **kwargs: Any) -> BaseChatModel:
    """A chat model for one Olano tier, wired to the gateway.

    Raises :class:`OlanoGatewayUnavailable` when the box holds no gateway key
    and has no deployment token to enroll one with. That is a hard error rather
    than a fallback to a real provider: silently running a customer's
    "Olano Pro" work on their own BYOK key would bill the wrong party and
    quietly break the product boundary.
    """
    resolved = normalize_tier(tier) or DEFAULT_TIER
    # `model` is the tier's GROUP name at the gateway (`olano-pro`), never a
    # provider model id — this process must not be able to name a real model
    # on the managed path even by accident. Olano's own configs keep the
    # `olano:pro` colon form; only the wire name is hyphenated.
    return _build_gateway_model(f"olano-{resolved}", **kwargs)


def build_olano_vision_model(**kwargs: Any) -> BaseChatModel:
    """The managed **vision** model, wired to the gateway.

    ``olano-vision`` is a gateway GROUP, deliberately not an entry in
    :data:`OLANO_TIERS`: the tiers are the customer's brain and are routed on
    price/latency, which is a decision that can land on a text-only
    backend. Vision capability cannot ride on that — ``image_describe`` needs a
    multimodal backend *guaranteed*, so it asks for a group whose membership
    condition IS "accepts image parts", maintained in the cloud routing table
    where it can actually be enforced.

    Keeping it out of the tier list is also what keeps it out of every model
    picker (`PROVIDERS["olano"].default_models`, `/model`, the dashboard
    dropdowns all read the tiers): this is a utility model the product calls on
    the agent's behalf, never a brain the customer selects.
    """
    return _build_gateway_model(OLANO_VISION_MODEL, **kwargs)


# ── Transient-failure retry ──────────────────────────────────────────────────
# A managed tier is one hop longer than a BYOK provider: box → gateway →
# whichever backend currently serves the group. A refusal on that last leg is
# nobody's fault the customer can act on, and without a retry it costs them the
# whole turn: the runner has no retry, the openai SDK retries only its own
# transport-level set (408/409/429/5xx), and a relayed upstream 4xx (a provider
# account momentarily out of balance) is not in it.
#
# So retry here, at the client, and NOT in the graph: re-running the turn would
# re-execute tools and re-write checkpoints. Same shape as the Codex
# sibling-model retry in `olano/models/__init__.py` — including the guard that
# matters most, that a stream which has already yielded is never retried, or the
# user gets half an answer twice.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0)

# Two codes, both chosen to sit in the gap the openai SDK leaves. Its client
# already retries twice on 408/409/429/5xx and connection errors, so naming rate
# limits, overloads and timeouts here would mostly re-buy attempts a layer down
# already made.
#
# `olano_service_unavailable` — what the SDK will NOT retry is a relayed 4xx: a
# 402 from the provider account behind a managed group is a "client error" to the
# SDK and a supply outage to the customer. The classifier files every relayed
# failure under this code, so where a relayed error DOES carry a status the SDK
# retries, its attempts nest inside ours. That is bounded and deliberate: the
# budget below caps the whole thing either way.
#
# `network_transient` — the SDK's connection-error retry covers the call it
# *makes*; it does not cover a socket that dies while the response body is being
# read. A streamed turn spends almost all of its wall-clock there, so a dropped
# connection mid-stream escapes every retry in the stack and reaches the chat as
# "API error (ReadError):", an exception with no message, ending on a colon. The
# `yielded` guard below is what makes retrying it safe: once a chunk has reached
# the caller the request is not re-sent, so the only calls we replay are the
# ones that produced nothing.
#
# Deliberately NOT `olano_credits_exhausted` (a spent balance is not transient —
# retrying just delays an honest message) and NOT `olano_service_unrouted` (no
# retry can invent a routing-table entry the cloud never published). A gateway
# 401 has its own repair path, the re-enrollment self-heal below, and its
# actionable error already says "send it again".
_RETRYABLE_CODES = frozenset({"olano_service_unavailable", "network_transient"})

# The group a call was made against, stamped on the exception on the way out.
# Recovering it by regexing the gateway's error prose misses often enough that
# an outage degrades to the unhelpful "This service (Olano-managed) is
# temporarily unavailable". The call site knows the wire name exactly; there is
# no reason to guess it.
GROUP_ATTR = "olano_model_group"


def _retry_delays() -> tuple[float, ...]:
    """Backoff before each retry; empty disables retrying entirely.

    Both knobs are env-overridable so an incident can be tuned on a live box
    without a release: ``LIONCLAW_OLANO_GATEWAY_RETRIES`` is the TOTAL attempt
    count (``1`` = no retry) and ``LIONCLAW_OLANO_GATEWAY_BACKOFF`` a
    comma-separated list of seconds. A malformed value falls back to the default
    rather than failing the call.
    """
    attempts = _RETRY_ATTEMPTS
    raw_attempts = os.environ.get("LIONCLAW_OLANO_GATEWAY_RETRIES", "").strip()
    if raw_attempts:
        try:
            attempts = max(1, int(raw_attempts))
        except ValueError:
            logger.warning("Ignoring malformed LIONCLAW_OLANO_GATEWAY_RETRIES=%r", raw_attempts)

    delays = _RETRY_BACKOFF_SECONDS
    raw_backoff = os.environ.get("LIONCLAW_OLANO_GATEWAY_BACKOFF", "").strip()
    if raw_backoff:
        try:
            delays = tuple(max(0.0, float(p)) for p in raw_backoff.split(",") if p.strip())
        except ValueError:
            logger.warning("Ignoring malformed LIONCLAW_OLANO_GATEWAY_BACKOFF=%r", raw_backoff)

    if attempts <= 1 or not delays:
        return ()
    # One delay per retry: pad with the last value when fewer were configured
    # than the attempt count asks for.
    needed = attempts - 1
    return tuple(delays[i] if i < len(delays) else delays[-1] for i in range(needed))


def _jittered(delay: float) -> float:
    """±25% so a fleet retrying the same outage does not resonate."""
    return delay * random.uniform(0.75, 1.25)


def _tag_group(exc: BaseException, wire_model: str) -> None:
    """Stamp the wire group on *exc* so the classifier can name it."""
    try:
        setattr(exc, GROUP_ATTR, wire_model)
    except Exception:  # pragma: no cover - some exceptions forbid attributes
        pass


def _is_retryable(exc: BaseException) -> bool:
    """True when the shared classifier says this failure may fix itself."""
    if not isinstance(exc, Exception):
        return False
    try:
        # Call-time import: `olano.core.__init__` pulls builder, which imports
        # `olano.models` — a module-scope import here would close that loop.
        from olano.core.actionable_errors import classify_exception

        classified = classify_exception(exc, "olano")
    except Exception:  # pragma: no cover - defensive
        return False
    return bool(classified and classified.code in _RETRYABLE_CODES)


def _log_retry(wire_model: str, attempt: int, total: int, exc: BaseException) -> None:
    # Logged per attempt, naming the group: a supply-side wobble is otherwise
    # invisible until someone reports it, and "how often did tier X bounce
    # today" needs to be greppable.
    logger.warning(
        "Olano gateway call for %s failed transiently (attempt %d/%d), retrying: %s",
        wire_model,
        attempt,
        total,
        exc,
    )


_OLANO_CHAT_CLASS: Any = None


def _note_success() -> None:
    """The gateway served a call: an account-key refusal recorded by the 401
    self-heal is history. Cheap on the hot path (one lock, no vault read) and
    a no-op when nothing was recorded; never raises."""
    try:
        from olano.deployment import gateway_key as gk

        if gk.account_key_rejection_pending():
            gk.clear_account_key_rejection()
    except Exception:  # pragma: no cover - a bookkeeping miss must not fail a turn
        pass


def _olano_chat_class() -> Any:
    """The gateway's ``ChatOpenAI`` subclass, built once per process.

    Every override is the same policy in four shapes, because langchain routes
    ``.invoke``/``.stream``/``.ainvoke``/``.astream`` through four different
    entry points and a managed call arrives on all of them (`image_describe`
    invokes, the agent turn streams).
    """
    global _OLANO_CHAT_CLASS
    if _OLANO_CHAT_CLASS is not None:
        return _OLANO_CHAT_CLASS

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - packaged in the base install
        raise ImportError(
            "Olano-managed models need langchain-openai (the gateway is "
            "OpenAI-compatible). Install with: uv add langchain-openai"
        ) from exc

    class _ChatOpenAIOlano(ChatOpenAI):  # type: ignore[misc]
        """``ChatOpenAI`` that survives a transient refusal from the gateway."""

        def _stream(self, *args: Any, **kwargs: Any) -> Any:
            delays = _retry_delays()
            for i in range(len(delays) + 1):
                yielded = False
                try:
                    for chunk in super()._stream(*args, **kwargs):
                        yielded = True
                        yield chunk
                    _note_success()
                    return
                except Exception as exc:
                    _tag_group(exc, self.model_name)
                    # A stream that already produced output can never be
                    # retried: the caller has those tokens, and a second run
                    # would append a whole second answer to them.
                    if yielded or i == len(delays) or not _is_retryable(exc):
                        raise
                    _log_retry(self.model_name, i + 1, len(delays) + 1, exc)
                    time.sleep(_jittered(delays[i]))

        async def _astream(self, *args: Any, **kwargs: Any) -> Any:
            delays = _retry_delays()
            for i in range(len(delays) + 1):
                yielded = False
                try:
                    async for chunk in super()._astream(*args, **kwargs):
                        yielded = True
                        yield chunk
                    _note_success()
                    return
                except Exception as exc:
                    _tag_group(exc, self.model_name)
                    if yielded or i == len(delays) or not _is_retryable(exc):
                        raise
                    _log_retry(self.model_name, i + 1, len(delays) + 1, exc)
                    await asyncio.sleep(_jittered(delays[i]))

        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            delays = _retry_delays()
            for i in range(len(delays) + 1):
                try:
                    result = super()._generate(*args, **kwargs)
                    _note_success()
                    return result
                except Exception as exc:
                    _tag_group(exc, self.model_name)
                    if i == len(delays) or not _is_retryable(exc):
                        raise
                    _log_retry(self.model_name, i + 1, len(delays) + 1, exc)
                    time.sleep(_jittered(delays[i]))
            raise AssertionError("unreachable")  # pragma: no cover

        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            delays = _retry_delays()
            for i in range(len(delays) + 1):
                try:
                    result = await super()._agenerate(*args, **kwargs)
                    _note_success()
                    return result
                except Exception as exc:
                    _tag_group(exc, self.model_name)
                    if i == len(delays) or not _is_retryable(exc):
                        raise
                    _log_retry(self.model_name, i + 1, len(delays) + 1, exc)
                    await asyncio.sleep(_jittered(delays[i]))
            raise AssertionError("unreachable")  # pragma: no cover

    _OLANO_CHAT_CLASS = _ChatOpenAIOlano
    return _OLANO_CHAT_CLASS


def _build_gateway_model(wire_model: str, **kwargs: Any) -> BaseChatModel:
    """Shared client construction for every Olano gateway group."""
    from olano.deployment.gateway_key import (
        UNENROLLED_SENTINEL,
        ensure_gateway_key,
        note_sentinel_build,
    )
    from olano.deployment.sources import deployment_token

    # Never blocks on a runtime server: the background enrollment loop owns the
    # network call, and this returns whatever is cached. A degraded control
    # plane must not put a 10s stall inside every agent build (see
    # gateway_key.set_background_enrollment).
    key = ensure_gateway_key()
    if not key:
        if not deployment_token():
            raise OlanoGatewayUnavailable(
                "Olano-managed models need a connection to cloud.olano.ai "
                "(no gateway key and no deployment token to enroll one with). "
                "Choose a provider:model instead, connect the deployment, or "
                "paste an Olano API key on Providers → Olano."
            )
        # Token present but enrollment didn't complete (cloud briefly down,
        # attempt backing off). Build anyway with a sentinel: the first call
        # 401s into the actionable-error path and the self-heal hook keeps
        # retrying enrollment — strictly better than failing the whole agent
        # build over a transient control-plane hiccup.
        #
        # But it must not be SILENT. Left unrecorded, the only symptom of a box
        # that can never enroll is a confusing 401 at first use, while every
        # dashboard surface reads "connected".
        note_sentinel_build(wire_model)
        key = UNENROLLED_SENTINEL

    model = _olano_chat_class()(
        model=wire_model,
        base_url=gateway_base_url(),
        api_key=key,  # type: ignore[arg-type]
        # The gateway reports the charge for non-streamed calls in a response
        # header (x-litellm-response-cost); without this flag langchain drops
        # response headers and those calls would ledger 0 credits.
        include_response_headers=True,
        # langchain auto-enables usage-in-streams only when openai_api_base is
        # None; with our base_url it stays off, and a streamed turn — which is
        # every normal turn — would report neither tokens nor a usage frame.
        # Force it: the gateway must send stream_options.include_usage.
        stream_usage=True,
        **kwargs,
    )
    _attach_auth_self_heal(model)
    _remember_model(model)
    return cast(BaseChatModel, model)


# ── 401 self-heal ────────────────────────────────────────────────────────────
# A gateway 401 means the virtual key was revoked or rotated (the cloud console,
# a re-enrollment elsewhere, an incident response). Re-enrolling mints a fresh
# key over the deployment token — no human needed — but the ChatOpenAI built
# this session already captured the dead key. The hook below watches responses
# on the model's underlying HTTP clients and, on a 401, re-enrolls in a
# background thread and swaps the new key into the live openai clients (their
# auth header is computed per-request from `.api_key`, so the *next* call
# succeeds). The failing call still errors — actionable_errors tells the user
# to retry — and everything here is best-effort: if any private attribute
# moved, behavior degrades to "restart picks up the new key from the vault".

_SELF_HEAL_WINDOW_SECONDS = 60.0
_self_heal_lock = threading.Lock()
_last_self_heal: float = float("-inf")


def _self_heal_due() -> bool:
    global _last_self_heal
    with _self_heal_lock:
        now = time.monotonic()
        if now - _last_self_heal < _SELF_HEAL_WINDOW_SECONDS:
            return False
        _last_self_heal = now
        return True


# Models built against this gateway, so a key that arrives AFTER they were built
# can be pushed into them without rebuilding the agent.
#
# Without this registry: agents boot before a key exists, so their models
# hold UNENROLLED_SENTINEL; a key then arrives over the settings channel and
# lands in the vault; and nothing ever tells the live models. Every turn 401s
# against a placeholder while a perfectly good key sits one lookup away.
#
# Weak references: a rebuilt or discarded model must not be kept alive by the
# registry, and a stale entry is harmless — swapping a key into a model nobody
# holds is a no-op.
_live_models: weakref.WeakSet[Any] = weakref.WeakSet()


def _remember_model(model: Any) -> None:
    try:
        _live_models.add(model)
    except TypeError:  # pragma: no cover - not weak-referenceable
        pass


def _swap_key(model: Any, key: str) -> bool:
    """Point a built model at `key`. True when something actually changed.

    The openai clients compute their auth header per request from `.api_key`,
    so writing it is enough — the *next* call uses the new key.
    """
    swapped = False
    for attr in ("root_client", "root_async_client"):
        client = getattr(model, attr, None)
        if client is None or not hasattr(client, "api_key"):
            continue
        try:
            if getattr(client, "api_key", None) != key:
                client.api_key = key
                swapped = True
        except Exception:  # pragma: no cover - best-effort by construction
            continue
    return swapped


def swap_live_models(key: str) -> int:
    """Push a newly obtained key into every live gateway model.

    Called when a key arrives by any route (notably the settings-channel
    delivery). Without this the key only reaches models built after it landed,
    so every agent already running keeps 401ing until the process restarts.
    """
    swapped = 0
    for model in list(_live_models):
        if _swap_key(model, key):
            swapped += 1
    if swapped:
        logger.info("olano gateway: applied the current key to %d live model(s)", swapped)
    return swapped


def _refresh_and_swap(model: Any) -> None:
    try:
        from olano.deployment.gateway_key import gateway_key, refresh_gateway_key

        # A usable key may have arrived by another route since this model was
        # built — the settings-channel delivery, or another agent's enrollment.
        # Prefer it. Enrollment is the slow, failure-prone path, and calling it
        # when a good key is already in hand leaves a box broken when the POST
        # fails: this returns None and the model keeps its placeholder with
        # the real key sitting in the vault.
        key = gateway_key()
        if key and _swap_key(model, key):
            logger.info("olano gateway: 401 self-heal applied a key obtained elsewhere")
            return

        # A pasted account key (self-hosted box) has nothing to re-enroll
        # with: the gateway refused it, and only a person can paste a new one.
        # Record the refusal so the Providers card and the error path say so
        # instead of promising a retry that cannot help.
        from olano.deployment.gateway_key import gateway_key_source, note_account_key_rejected

        if gateway_key_source() == "account":
            note_account_key_rejected()
            logger.warning(
                "olano gateway: the Olano API key was refused (401); paste a valid key "
                "on Providers → Olano (cloud.olano.ai → API keys)"
            )
            return

        # The key we hold is the one that just 401'd (or we hold none): now it
        # is worth minting a fresh one.
        minted = refresh_gateway_key()
        if minted and _swap_key(model, minted):
            logger.info("olano gateway: 401 self-heal re-enrolled the gateway key")
    except Exception:  # pragma: no cover - heal must never raise
        logger.debug("olano gateway: 401 self-heal failed", exc_info=True)


def _spawn_self_heal(model: Any) -> None:
    if not _self_heal_due():
        return
    threading.Thread(
        target=_refresh_and_swap, args=(model,), name="olano-gateway-reenroll", daemon=True
    ).start()


def _attach_auth_self_heal(model: Any) -> None:
    """Register 401 watchers on the model's httpx clients (best-effort)."""

    def _on_response(response: Any) -> None:
        if getattr(response, "status_code", None) == 401:
            _spawn_self_heal(model)

    async def _on_response_async(response: Any) -> None:
        _on_response(response)

    try:
        for attr, hook in (
            ("root_client", _on_response),
            ("root_async_client", _on_response_async),
        ):
            openai_client = getattr(model, attr, None)
            http_client = getattr(openai_client, "_client", None)
            hooks = getattr(http_client, "event_hooks", None)
            if isinstance(hooks, dict):
                hooks.setdefault("response", []).append(hook)
    except Exception:  # pragma: no cover - purely additive
        logger.debug("olano gateway: could not attach 401 self-heal hook", exc_info=True)
