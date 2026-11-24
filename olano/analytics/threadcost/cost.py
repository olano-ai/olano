"""Per-call USD cost with cache-read discount.

Pricing resolves through the synced cloud catalog first
(:func:`olano.catalog.models_registry.model_pricing` — per-model, all
providers, includes the provider's real cache-read rate), then falls back to
the legacy ``_MODEL_COSTS`` substring table in
:mod:`olano.pulse.cost_tracker` for models the catalog doesn't know.

The fallback cache multiplier is a flat approximation: Anthropic prompt-cache
reads bill at ~10% of the input rate; OpenAI cached input at ~50%. We use 10%
(conservative) and flag any cache-discounted figure as approximate in the UI.
The whole drill-down is explicitly labelled "estimated" since list prices
ignore batch/region uplift.
"""

from __future__ import annotations

from datetime import datetime

from olano.pulse.cost_tracker import _MODEL_COSTS, _model_cost_per_token

# Cached-input tokens are billed at this fraction of the normal input rate
# when the catalog has no per-model cache-read price.
_CACHE_READ_MULTIPLIER = 0.1


def _catalog_pricing(model: str, at: datetime | None = None) -> dict | None:
    """Catalog pricing dict, or None. Never raises — costing must not break."""
    try:
        from olano.catalog import models_registry

        return models_registry.model_pricing(model, at)
    except Exception:
        return None


def cost_for_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    *,
    at: datetime | None = None,
) -> float:
    """USD for one call, discounting cached input tokens.

    ``input_tokens`` is the full prompt size (cache_read is a subset of it), so the
    fresh (uncached) portion is ``input_tokens - cache_read_tokens``. ``at`` is
    when the call ran (default: now); it picks a provider's off-peak rate when
    the catalog lists one (DeepSeek bills half outside its peak hours).
    """
    # Olano-managed tiers have no USD price on this box — the authoritative
    # charge is the credits number the gateway returns per call. Any USD here
    # would be the substring-table default (~$0.0005/1M), a fabricated number
    # that then flows into budgets, the journal, and cloud-shipped metadata
    # looking real. Zero is the honest answer; pricing_known() already reports
    # False for these so consumers can label the row unpriced.
    from olano.models.olano_gateway import is_olano_model

    if is_olano_model(model):
        return 0.0

    cache_read = max(0, int(cache_read_tokens or 0))
    fresh_input = max(0, int(input_tokens or 0) - cache_read)
    output = max(0, int(output_tokens or 0))

    pricing = _catalog_pricing(model, at)
    if pricing is not None:
        inp = float(pricing["input"]) / 1_000_000
        out = float(pricing["output"]) / 1_000_000
        cache_rate = pricing.get("cache_read")
        cache_per_tok = (
            float(cache_rate) / 1_000_000
            if cache_rate is not None
            else inp * _CACHE_READ_MULTIPLIER
        )
        return fresh_input * inp + cache_read * cache_per_tok + output * out

    inp_per_tok, out_per_tok = _model_cost_per_token(model)
    return (
        fresh_input * inp_per_tok
        + cache_read * inp_per_tok * _CACHE_READ_MULTIPLIER
        + output * out_per_tok
    )


def pricing_known(model: str) -> bool:
    """True iff real pricing resolved (catalog entry or a legacy prefix match),
    vs. falling back to the default rate — so the UI can mark unknown-model
    rows as estimated."""
    if _catalog_pricing(model) is not None:
        return True
    model_lower = (model or "").lower()
    return any(prefix in model_lower for prefix in _MODEL_COSTS)
