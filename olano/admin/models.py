"""Which providers are actually connected, and what their models cost.

AgentFather used to create agents blind. ``AgentConfig.model`` defaults to
``anthropic:claude-sonnet-4-6`` and nine of eleven archetypes hardcode the same
string, so an install whose only connected provider is OpenAI Codex got agents
that failed on their first turn — and nothing in the admin toolkit could have
told it otherwise.

This module answers the two questions that decision needs:

* **Is this provider connected?** An API key counts when it is in the process
  env *or* the vault (secrets are no longer hydrated into ``os.environ``, so env
  alone undercounts). ``openai-codex`` authenticates by OAuth and has no env var
  at all; ``ollama`` needs a daemon, not a key.
* **What does a model cost?** From the synced cloud catalog — ``priceInput`` /
  ``priceOutput`` per million tokens, plus ``tier`` for the models it has no
  price for.

:func:`cheapest_model` turns that into a single recommendation, computed in code
so the choice is deterministic and reviewable rather than a model's guess about
names it may never have seen. That matters most for exactly the models this has
to handle: Codex model IDs come from the user's live account
(``models/codex_auth.fetch_codex_models``), so a brand-new one is unknown to the
bundled catalog and to any training data.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Providers billed by subscription or run locally: no per-token charge, so they
# outrank every metered model regardless of what the catalog says.
_NO_MARGINAL_COST_PROVIDERS = frozenset({"openai-codex", "ollama"})

# Fallback ordering for models the catalog has no price for. Lower is cheaper.
# A model with no price data must never sort as free — that is how an unlisted
# flagship would win.
_TIER_RANK: dict[str, int] = {"local": 0, "fast": 1, "standard": 2, "flagship": 3}
_UNKNOWN_TIER_RANK = 2.5

# Output tokens are the minority of a typical agent turn but the expensive half;
# this weighting ranks by realistic spend instead of by headline input price.
_INPUT_WEIGHT = 0.75
_OUTPUT_WEIGHT = 0.25


def _vault_key_names() -> set[str]:
    """Credential names in the vault. Empty (not an error) when it is locked."""
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
        if vault is None:
            return set()
        return {str(row.get("name", "")) for row in vault.list_names()}
    except Exception:
        return set()


def provider_connection(provider_key: str, vault_names: set[str] | None = None) -> dict[str, Any]:
    """Is this provider usable right now, and how does it authenticate?

    Returns ``{connected, auth, env_var, detail}``. ``auth`` is ``api_key``,
    ``oauth`` (Codex's device flow), ``deployment_token`` (Olano-managed tiers,
    connected iff the box is attached to cloud.olano.ai) or ``none`` (Ollama,
    LiteLLM proxies).
    """
    from olano.models import PROVIDERS, is_provider_available

    info = PROVIDERS.get(provider_key)
    if info is None:
        return {"connected": False, "auth": "none", "env_var": None, "detail": "unknown provider"}

    if not is_provider_available(provider_key):
        return {
            "connected": False,
            "auth": "api_key" if info.env_var else "none",
            "env_var": info.env_var,
            "detail": f"`{info.package}` is not installed",
        }

    if provider_key == "openai-codex":
        try:
            from olano.models.codex_auth import auth_status

            status = auth_status()
        except Exception as exc:
            return {"connected": False, "auth": "oauth", "env_var": None, "detail": str(exc)}
        connected = bool(status.get("logged_in"))
        return {
            "connected": connected,
            "auth": "oauth",
            "env_var": None,
            "detail": (
                "connected via ChatGPT/Codex OAuth"
                if connected
                else "not connected — run `olano auth openai-codex`"
            ),
        }

    if provider_key == "olano":
        # Olano-managed tiers authenticate with a per-deployment gateway key,
        # not a provider key. An env_var=None fallthrough saying "always
        # connected" would offer tiers that can only error at first use.
        #
        # THREE states, not two. `bool(gateway_key() or deployment_token())`
        # reported "connected" for a box that is attached to the control plane
        # but holds no gateway key — i.e. one where every olano tier 401s. That
        # is the single most misleading thing this surface can say, because it
        # is exactly the state a human comes here to diagnose (incident
        # 2026-08-14: hours of total managed-inference failure behind a green
        # provider card).
        from olano.deployment.gateway_key import enrollment_state

        state = enrollment_state()
        detail = {
            "enrolled": "billed in olano credits via the Olano model gateway",
            "unenrolled": (
                "attached, but no gateway key yet — Olano models will fail until "
                "enrollment succeeds (run: olano gateway)"
            ),
            "unattached": (
                "not connected — Olano models need a deployment attached to cloud.olano.ai"
            ),
        }[state]
        extra: dict[str, Any] = {}
        try:
            from olano.deployment import resolver as dep_resolver
            from olano.deployment.gateway_key import account_key_rejected, gateway_key_source

            # Self-hosted boxes only (see routes/catalog.py): the key family
            # and whether an account key may be connected here. A managed
            # deployment's row stays exactly as it was.
            if dep_resolver.deployment_status().posture == "open":
                source = gateway_key_source()
                extra = {"source": source, "manageable": True}
                if state == "unattached":
                    detail = (
                        "not connected: paste an Olano API key (cloud.olano.ai → API keys) "
                        "on Providers → Olano, or attach a deployment"
                    )
                elif source == "account":
                    detail = (
                        "billed in olano credits via the Olano model gateway (account API key)"
                        if not account_key_rejected()
                        else "the Olano API key was refused by the gateway; paste a valid key"
                    )
        except Exception:  # pragma: no cover - defensive
            extra = {}
        return {
            **extra,
            # `connected` answers "can this provider work on this box at all",
            # and an attached box CAN — enrollment self-heals. Narrowing it to
            # `state == "enrolled"` would drop olano out of `connected_only`,
            # out of every model picker (dashboard `providerUsable`), and out of
            # `model_provider_connected` — so a transient control-plane blip
            # would make the entire default managed configuration unselectable,
            # including for agents already running on those tiers. That is worse
            # than the bug this whole change set is fixing.
            #
            # The degraded case is reported by `state` + `detail` instead: the
            # surface no longer claims everything is fine, but it also does not
            # take the tiers away.
            "connected": state != "unattached",
            "state": state,
            "auth": "deployment_token",
            "env_var": None,
            "detail": detail,
        }

    if provider_key == "ollama":
        from olano.models import list_ollama_models

        models = list_ollama_models()
        return {
            "connected": bool(models),
            "auth": "none",
            "env_var": None,
            "detail": (
                f"{len(models)} local model(s)"
                if models
                else "no local models (is ollama running?)"
            ),
        }

    if not info.env_var:
        return {"connected": True, "auth": "none", "env_var": None, "detail": "no key required"}

    names = _vault_key_names() if vault_names is None else vault_names
    if os.environ.get(info.env_var):
        return {
            "connected": True,
            "auth": "api_key",
            "env_var": info.env_var,
            "detail": f"{info.env_var} set in the environment",
        }
    if info.env_var in names:
        return {
            "connected": True,
            "auth": "api_key",
            "env_var": info.env_var,
            "detail": f"{info.env_var} stored in the vault",
        }
    return {
        "connected": False,
        "auth": "api_key",
        "env_var": info.env_var,
        "detail": f"{info.env_var} is not set (checked the environment and the vault)",
    }


def _model_rows(provider_key: str, catalog_by_id: dict[str, dict]) -> list[dict[str, Any]]:
    """Every model this provider offers, with cost metadata attached."""
    from olano.models import get_provider_models

    rows: list[dict[str, Any]] = []
    for model in get_provider_models(provider_key):
        model_id = f"{provider_key}:{model}"
        entry = catalog_by_id.get(model_id) or {}
        price_in = entry.get("priceInput")
        price_out = entry.get("priceOutput")
        free = provider_key in _NO_MARGINAL_COST_PROVIDERS
        rows.append(
            {
                "id": model_id,
                "model": model,
                "display_name": entry.get("displayName") or model,
                "tier": entry.get("tier"),
                "context_window": entry.get("contextWindow"),
                "price_input": price_in,
                "price_output": price_out,
                "cost_class": (
                    "included" if free else "metered" if price_in is not None else "unknown"
                ),
                "recommended": bool(entry.get("recommended")),
                "default_for_provider": bool(entry.get("defaultForProvider")),
                "deprecated": bool(entry.get("deprecated")),
            }
        )
    return rows


def _sort_key(row: dict[str, Any]) -> tuple[float, float, float, int, str]:
    """Cheapest first, with every tie broken deterministically.

    1. **Cost band** — ``included`` (subscription or local, no per-token charge)
       before ``metered`` before ``unknown``. A model the catalog has no price
       for must never sort as free; that is how an unlisted flagship would win.
    2. **Tier** — the only signal present for *every* model. Codex and Ollama
       models carry no price at all, and a model too new for the catalog carries
       neither price nor tier, so it lands mid-pack instead of first.
    3. **Blended price** — output tokens are the minority of an agent turn but
       the expensive half, so rank by realistic spend, not headline input price.
    4. The provider's own default, then the id — so the answer never depends on
       dict ordering or on what a live API returned this minute.
    """
    cost_class = row["cost_class"]
    if cost_class == "included":
        band, blended = 0.0, 0.0
    elif cost_class == "metered":
        price_in = float(row["price_input"] or 0.0)
        price_out = float(row["price_output"] if row["price_output"] is not None else price_in)
        band = 1.0
        blended = price_in * _INPUT_WEIGHT + price_out * _OUTPUT_WEIGHT
    else:
        band, blended = 2.0, 0.0

    tier = float(_TIER_RANK.get(str(row.get("tier") or ""), _UNKNOWN_TIER_RANK))
    return (band, tier, blended, 0 if row["default_for_provider"] else 1, str(row["id"]))


def _rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cost-rank a model list, cheapest first, dropping deprecated models."""
    ranked = sorted((r for r in rows if not r["deprecated"]), key=_sort_key)
    for i, row in enumerate(ranked):
        row["cost_rank"] = i
    return ranked


def list_models(connected_only: bool = True) -> dict[str, Any]:
    """Providers and their models, cost-ranked, with a recommendation.

    ``connected_only`` (the default) hides providers that cannot be used right
    now — that is the list to pick a model from. Pass ``False`` to see what
    connecting something else would unlock.
    """
    from olano.catalog import models_registry
    from olano.models import PROVIDERS

    try:
        catalog = models_registry.get_model_catalog()
    except Exception:  # a catalog miss must not break the tool
        logger.debug(
            "Model catalog unavailable; falling back to bare provider lists", exc_info=True
        )
        catalog = {}
    catalog_by_id = {str(e.get("id") or ""): e for e in catalog.get("models", [])}

    vault_names = _vault_key_names()
    providers: list[dict[str, Any]] = []
    for key, info in PROVIDERS.items():
        connection = provider_connection(key, vault_names)
        if connected_only and not connection["connected"]:
            continue
        row: dict[str, Any] = {"key": key, "name": info.name, **connection}
        if connection["connected"]:
            row["models"] = _rank(_model_rows(key, catalog_by_id))
        providers.append(row)

    everything = [m for p in providers if p.get("connected") for m in p.get("models", [])]
    ranked = sorted(everything, key=_sort_key)

    return {
        "providers": providers,
        "connected": [p["key"] for p in providers if p["connected"]],
        "suggested_cheap": ranked[0]["id"] if ranked else None,
        "suggested_default": _suggested_default(ranked),
        "note": (
            "Costs are per million tokens. 'included' means a subscription or local "
            "model with no per-token charge. A model with no price and no tier is "
            "ranked mid-pack, never as free."
        ),
    }


def _suggested_default(ranked: list[dict[str, Any]]) -> str | None:
    """The cheapest model the catalog also marks as generally recommended.

    Falls back to the cheapest of all — a recommendation flag is a nice-to-have,
    a working model is not.
    """
    preferred = next((r for r in ranked if r["recommended"]), None)
    if preferred is not None:
        return str(preferred["id"])
    return str(ranked[0]["id"]) if ranked else None


def cheapest_model() -> str | None:
    """The cheapest usable model on this install, or None if nothing is connected."""
    try:
        picked = list_models(connected_only=True)["suggested_cheap"]
    except Exception:
        logger.debug("Could not resolve the cheapest connected model", exc_info=True)
        return None
    return str(picked) if picked else None


def model_provider_connected(model: str) -> tuple[bool, str]:
    """``(connected, detail)`` for the provider behind a ``provider:model`` string."""
    from olano.models import parse_model_string

    provider, _ = parse_model_string(model)
    if provider is None:
        return True, "no provider prefix — resolved by langchain at build time"
    connection = provider_connection(provider)
    return bool(connection["connected"]), str(connection["detail"])
