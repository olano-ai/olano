"""Effective usage budgets — the signed deployment limits, plus local overrides.

The control plane issues six budgets in the signed artifact (four token
ceilings, two cost ceilings). Those numbers used to be the whole answer, and a
box had no way to see or change them: the dashboard card that showed them was
hidden on the grounds that the ceilings were "practically unlimited", so when
one did bite — an agent refused every turn against a 10^10 monthly token
ceiling — there was nothing on the box that named the limit, nothing that
warned as it filled, and nothing that could move it.

This module is the single definition of *what the budgets actually are on this
box*: :func:`effective_usage_limits` folds the ``usage_budgets:`` block of
``config.yaml`` onto the artifact, and enforcement (``usage_counters``),
display (``/api/system/deployment``, ``olano deployment status``) and the
threshold alerts all read it, so no surface can quote a different ceiling than
the one being enforced.

**Only the token ceilings are overridable.** They exist to stop a runaway
loop, not to meter anyone: BYOK tokens are the customer's own provider spend,
and managed-model spend is independently bounded by the gateway, which enforces
the deployment's credit balance as the virtual key's budget — a locally raised
token ceiling cannot let a box spend credits it does not have. ``cost_per_agent``
/ ``cost_total`` are the budgets that bound olano's own upstream bill, so they
are readable here but never writable from the box.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from olano.config.system_settings import (
    OVERRIDABLE_USAGE_LIMITS,
    load_usage_budget_settings,
)
from olano.deployment.schema import UsageLimit, UsageLimits

logger = logging.getLogger(__name__)

#: Human labels for the six budgets, in the order every surface lists them.
USAGE_LIMIT_LABELS: dict[str, str] = {
    "input_tokens_per_agent": "input tokens (this agent)",
    "input_tokens_total": "input tokens (deployment)",
    "output_tokens_per_agent": "output tokens (this agent)",
    "output_tokens_total": "output tokens (deployment)",
    "cost_per_agent": "cost (this agent)",
    "cost_total": "cost (deployment)",
}


@dataclass(frozen=True)
class EffectiveLimit:
    """One budget as this box enforces it, and where the number came from."""

    key: str
    label: str
    limit: UsageLimit
    #: ``"deployment"`` (the signed artifact) or ``"config"`` (``config.yaml``).
    source: str
    #: The artifact's own value, kept so a surface can show what was overridden.
    deployment_limit: UsageLimit

    @property
    def overridden(self) -> bool:
        return self.source == "config"

    @property
    def unit(self) -> str:
        return "micro_usd" if self.key.startswith("cost") else "tokens"

    @property
    def per_agent(self) -> bool:
        return self.key.endswith("_per_agent")

    @property
    def metric(self) -> int:
        """Index into the ``(input, output, cost)`` totals tuple."""
        if self.key.startswith("cost"):
            return 2
        return 1 if self.key.startswith("output") else 0


def effective_usage_limits(
    usage: UsageLimits,
    *,
    override: str | Path | None = None,
) -> list[EffectiveLimit]:
    """The six budgets with the local token overrides applied, in display order.

    Never raises: an unreadable config yields the artifact's own limits, which
    is the safe direction — a broken file must not be able to lift a ceiling.
    """
    settings = load_usage_budget_settings(override)
    out: list[EffectiveLimit] = []
    for key, label in USAGE_LIMIT_LABELS.items():
        deployment_limit = getattr(usage, key, None) or UsageLimit()
        limit, source = deployment_limit, "deployment"
        if key in OVERRIDABLE_USAGE_LIMITS:
            local = getattr(settings, key, None)
            if local is not None:
                limit = UsageLimit(value=local.value, window=local.window)
                source = "config"
        out.append(
            EffectiveLimit(
                key=key,
                label=label,
                limit=limit,
                source=source,
                deployment_limit=deployment_limit,
            )
        )
    return out


def alert_percents(override: str | Path | None = None) -> list[int]:
    """Thresholds (1–99) at which a filling budget raises a notification.

    100 is not in this list and is not configurable — a spent budget refuses
    turns, and that always warrants a row in the bell.
    """
    settings = load_usage_budget_settings(override)
    return list(settings.alert_percents) if settings.alerts_enabled else []


def alerts_enabled(override: str | Path | None = None) -> bool:
    return bool(load_usage_budget_settings(override).alerts_enabled)
