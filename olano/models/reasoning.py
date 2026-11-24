"""Reasoning-effort capability detection and gating.

LangChain (July 2026) standardized ``reasoning_effort`` as a first-class chat
model parameter and ships per-model capability data ("model profiles") inside
each integration package. This module treats those profiles as the source of
truth for two questions:

- does this exact model support reasoning-effort control, and
- which effort levels does it accept?

The answers gate everything: the dashboard only offers levels the installed
client can actually send, and ``build_model`` silently drops the kwarg for
models without profile support — so switching an agent to a non-reasoning
model can never produce a rejected request.

Support requires BOTH signals: the integration class declares the
``reasoning_effort`` field AND the model's profile advertises effort levels.
A class-level field alone (e.g. ChatDeepSeek today) is not enough — without
profile data we don't know the hosted model accepts the parameter.

One fallback sits behind the profile: when the installed package says
nothing about a model's effort levels (a model the package predates), the
synced cloud catalog's ``reasoningEfforts`` / ``reasoningDefault`` for that
exact ``provider:model`` are used instead. See :func:`_probe`.
"""

from __future__ import annotations

import importlib
import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Platform default applied when an agent has no explicit setting and the model
# supports effort control. Chosen for chat responsiveness (measured 2026-07-26
# on gpt-5.6-terra: default/medium 3.3s mean vs low 2.2s mean on short turns).
DEFAULT_REASONING_EFFORT = "low"

# Providers whose models reason unconditionally, where the implicit "low"
# default REDUCES latency. Providers with optional thinking (e.g. anthropic —
# off unless requested; sending an effort would switch thinking ON) only get
# an effort when the user sets one explicitly.
_IMPLICIT_DEFAULT_PROVIDERS = ("openai", "openai-codex")

# provider key (olano PROVIDERS key) -> integration class used to resolve
# the model profile. Construction is offline (pure pydantic, dummy key) and
# cached; providers absent here simply report "no reasoning support" until a
# probe entry is added.
_PROFILE_PROBES: dict[str, tuple[str, str]] = {
    "openai": ("langchain_openai", "ChatOpenAI"),
    "openai-codex": ("langchain_openai", "ChatOpenAI"),
    "anthropic": ("langchain_anthropic", "ChatAnthropic"),
    "google_genai": ("langchain_google_genai", "ChatGoogleGenerativeAI"),
    "xai": ("langchain_xai", "ChatXAI"),
    "groq": ("langchain_groq", "ChatGroq"),
    "deepseek": ("langchain_deepseek", "ChatDeepSeek"),
    "fireworks": ("langchain_fireworks", "ChatFireworks"),
}


def _probe(provider: str, model_name: str) -> tuple[tuple[str, ...], str | None]:
    """Return (effort_levels, provider_default) for a model, offline.

    Empty levels tuple means "no reasoning-effort support": the provider has
    no probe entry or the installed package says nothing about this model
    AND the synced cloud catalog declares no ladder for it either.

    The package profile is asked first (it is authoritative for the class we
    actually build), the catalog second. The second half exists because a
    profile table is frozen at the package's release: langchain-openai 1.7.0
    carries a ``gpt-6.1-sol`` row that marks it as a reasoning model and
    lists NO effort levels, so every surface treated GPT-6.1 Sol as having no
    effort control (the chat chip hid, the agent setting was ignored, nothing
    was sent) while the provider's own page spells out five rungs. The
    catalog is synced from the cloud and can carry a ladder the package
    predates. Only the package half is memoized: the catalog index is an
    in-process dict hit, and a ``catalog_updated`` sync must be visible on
    the next read rather than after a restart.
    """
    # Olano-managed tiers can't be probed: the gateway deliberately never
    # reveals which real model backs a tier. Their capability is declared
    # statically instead (OLANO_TIER_INFO), which makes all three consumers of
    # this probe — the picker info, effort resolution, and kwarg gating —
    # agree that `olano:max` takes an effort and the other tiers don't.
    if provider == "olano":
        from olano.models.olano_gateway import tier_info

        info = tier_info(model_name)
        return (tuple(info["reasoning_efforts"]), None) if info else ((), None)
    levels, default = _profile_probe(provider, model_name)
    if levels:
        return levels, default
    return _catalog_ladder(provider, model_name)


def _catalog_ladder(provider: str, model_name: str) -> tuple[tuple[str, ...], str | None]:
    """The synced catalog's ladder for a model, or ``((), None)``.

    Reads ``reasoningEfforts`` / ``reasoningDefault`` off the catalog entry
    (``catalog/models_registry``, cloud → disk → bundled, never a network
    wait). Never raises: a broken catalog read means "no ladder", which is
    exactly the answer this function existed to improve on, not a worse one.
    """
    try:
        from olano.catalog.models_registry import model_entry

        entry = model_entry(f"{provider}:{model_name}")
    except Exception:
        logger.debug("catalog ladder lookup failed for %s:%s", provider, model_name, exc_info=True)
        return (), None
    if not entry:
        return (), None
    raw = entry.get("reasoningEfforts") or []
    levels = tuple(
        dict.fromkeys(str(v).strip().lower() for v in raw if str(v or "").strip())
        if isinstance(raw, list | tuple)
        else ()
    )
    if not levels:
        return (), None
    default = str(entry.get("reasoningDefault") or "").strip().lower() or None
    return levels, (default if default in levels else None)


@lru_cache(maxsize=256)
def _profile_probe(provider: str, model_name: str) -> tuple[tuple[str, ...], str | None]:
    """The installed integration package's answer alone (memoized)."""
    entry = _PROFILE_PROBES.get(provider)
    if entry is None:
        return (), None
    mod_name, cls_name = entry
    try:
        cls = getattr(importlib.import_module(mod_name), cls_name)
    except Exception:
        return (), None
    if "reasoning_effort" not in getattr(cls, "model_fields", {}):
        return (), None
    profile = None
    for ctor_kwargs in ({"api_key": "profile-probe"}, {}):
        try:
            profile = getattr(cls(model=model_name, **ctor_kwargs), "profile", None)
            break
        except Exception:
            continue
    if not profile:
        return (), None
    try:
        levels = tuple(profile.get("reasoning_effort_levels") or ())
        default = profile.get("reasoning_effort_default")
    except Exception:
        return (), None
    return levels, (str(default) if default else None)


def reasoning_effort_info(model_str: str) -> dict[str, Any] | None:
    """Capability info for a ``provider:model`` string, or None if unsupported.

    Shape: ``{"levels": [...], "provider_default": str | None}``.
    """
    provider, _, model_name = model_str.partition(":")
    if not model_name:
        return None
    levels, default = _probe(provider, model_name)
    if not levels:
        return None
    return {"levels": list(levels), "provider_default": default}


def supports_reasoning_effort(model_str: str) -> bool:
    return reasoning_effort_info(model_str) is not None


def resolve_reasoning_effort(model_str: str, configured: str | None) -> str | None:
    """Effective effort for a model: configured value, else the platform default.

    Returns None when the model has no effort support (nothing must be sent),
    or when neither the configured value nor any default is valid for it. An
    invalid configured value falls back to the default rather than erroring —
    a stale setting must never break a model switch.
    """
    info = reasoning_effort_info(model_str)
    if info is None:
        return None
    levels = info["levels"]
    if configured:
        value = configured.strip().lower()
        if value in levels:
            return value
        logger.warning(
            "reasoning_effort '%s' not supported by %s (levels: %s); using default",
            configured,
            model_str,
            levels,
        )
    # Implicit default only where reasoning is always-on and "low" reduces
    # work; elsewhere unset means "leave the model's thinking behavior alone".
    provider = model_str.partition(":")[0]
    if provider not in _IMPLICIT_DEFAULT_PROVIDERS:
        return None
    if DEFAULT_REASONING_EFFORT in levels:
        return DEFAULT_REASONING_EFFORT
    provider_default = info["provider_default"]
    return str(provider_default) if provider_default else None


def gate_reasoning_kwargs(provider: str, model_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop reasoning-effort kwargs a model can't accept (in place, returned).

    Applied inside ``build_model`` so every construction path — agent build,
    per-chat override, /model switches, cognition workers — shares one guard.
    Also drops a raw Responses-API ``reasoning`` dict for non-OpenAI providers,
    covering legacy ``model_kwargs`` entries that ride along across a provider
    switch.
    """
    if "reasoning_effort" in kwargs:
        levels, _ = _probe(provider, model_name)
        value = str(kwargs["reasoning_effort"] or "").strip().lower()
        if not levels or value not in levels:
            dropped = kwargs.pop("reasoning_effort")
            logger.debug(
                "Dropping reasoning_effort=%r for %s:%s (unsupported)",
                dropped,
                provider,
                model_name,
            )
        else:
            kwargs["reasoning_effort"] = value
    if "reasoning" in kwargs and provider not in ("openai", "openai-codex", "azure_openai"):
        kwargs.pop("reasoning")
        logger.debug("Dropping raw 'reasoning' kwarg for %s:%s", provider, model_name)
    return kwargs
