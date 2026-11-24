"""Budget and cost tracking for agents."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from olano.config.agent_config import BudgetConfig

logger = logging.getLogger(__name__)

# Approximate cost per 1M tokens (USD) by model-name prefix. These are rough
# early-2026 public list prices for budgeting/dashboard display only — NOT exact
# billing (real cost varies with cached-input/batch discounts and region uplift).
# Matching is first-substring-wins over insertion order, so MORE-SPECIFIC
# prefixes must precede broader parents (e.g. gpt-4o-mini before gpt-4o).
_MODEL_COSTS: dict[str, tuple[float, float]] = {
    # (input_usd_per_1m, output_usd_per_1m)
    # ── Anthropic Claude 3.x ── (the "-3-"/"-3-5-" infix means these are NOT
    # caught by the claude-4.x prefixes below, so list them explicitly first)
    "claude-3-opus": (15.0, 75.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-haiku": (0.25, 1.25),
    # ── Anthropic Claude 4.x / 5.x ── (matches claude-opus-4-x / claude-sonnet-4-x …)
    # Opus 5.5 is cheaper than the rest of the Opus line, so it must precede the
    # bare "claude-opus" prefix. Its 1M window bills at these same rates.
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    # ── OpenAI GPT-5.x ── (list SPECIFIC variants before the bare prefix, since
    # matching is first-substring-wins; cached input is ~10% of input for all of
    # these, applied separately as the cache-read discount, so only standard
    # input/output rates live here). Source: OpenAI list prices (per 1M).
    # NOTE these are the standard (short-context) rates. GPT-5.4+ carry a 1M+
    # window but bill the WHOLE request at 2x input / 1.5x output once the prompt
    # crosses 272K input tokens — that tier is not modelled here, so long-context
    # turns are under-reported. The GPT-6 family has the same 272K step.
    "gpt-6-astra": (10.0, 50.0),
    # Same input/output as gpt-6-sol; only its cached input is lower ($0.10 vs
    # $0.20), which the catalog carries and this table does not.
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.6": (5.0, 30.0),  # bare alias routes to -sol
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4-pro": (30.0, 180.0),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.0),
    # gpt-5.3-codex also covers -codex-spark (same rate; spark is a preview whose
    # credit rate is not final). There is no bare gpt-5.3 chat model.
    "gpt-5.3-codex": (1.75, 14.0),
    "gpt-5.3-chat": (1.75, 14.0),
    "gpt-5.2-pro": (21.0, 168.0),
    "gpt-5.2": (1.75, 14.0),  # covers -codex / -chat (same rate)
    "gpt-5.1-codex-mini": (0.25, 2.0),
    "gpt-5.1": (1.25, 10.0),  # covers -codex / -codex-max / -chat (same rate)
    "gpt-5-pro": (15.0, 120.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    # Bare gpt-5 (2025-08) — also the last-resort catch-all for any gpt-5.x we
    # have not listed above, which is exactly the silent-mispricing hazard the
    # substring matcher can't express. Unlisted models should be flagged unknown,
    # not silently priced; see pricing_known() in analytics/threadcost/cost.py.
    "gpt-5": (1.25, 10.0),  # covers -codex / -chat / -search-api (same rate)
    # Must stay AFTER gpt-5.1-codex-mini, which it would otherwise shadow.
    "codex-mini": (1.50, 6.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-mini": (0.15, 0.60),  # must precede "gpt-4o"
    "gpt-4o": (2.5, 10.0),
    "gpt-4-turbo": (10.0, 30.0),
    "gpt-3.5": (0.5, 1.5),
    "o4-mini": (0.55, 2.20),
    "o3": (2.0, 8.0),
    # ── Google Gemini ──
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2": (1.25, 10.0),  # fallback for other gemini-2.x
    "gemini-1.5-pro": (3.5, 10.5),
    "gemini-1.5-flash": (0.35, 1.05),
    # ── Others ──
    "deepseek": (0.30, 1.20),  # V4.1 Flash, peak rate
    "mistral": (2.0, 6.0),
    "llama": (0.0, 0.0),  # local / self-hosted
}


def _catalog_cost_per_1m(model: str) -> tuple[float, float] | None:
    """(input, output) USD per 1M from the synced cloud catalog, or None.

    The catalog (olano/catalog) is the maintained source — per-model, all
    providers, updated by cloud sync without a release. The `_MODEL_COSTS`
    substring table below stays as the offline/unknown-model fallback.
    Never raises: cost accounting must survive a broken catalog.
    """
    try:
        from olano.catalog import models_registry

        pricing = models_registry.model_pricing(model)
    except Exception:
        return None
    if pricing is None:
        return None
    price_input = pricing.get("input")
    price_output = pricing.get("output")
    if price_input is None or price_output is None:
        return None
    return float(price_input), float(price_output)


def _model_cost_per_token(model: str) -> tuple[float, float]:
    """Return (input, output) cost per token (not per 1M) for the given model."""
    # Olano-managed tiers: USD cost is 0 on this box — the gateway charges
    # credits and never publishes a per-token rate. Without this branch the
    # substring-default below fabricates a near-zero-but-nonzero USD that
    # silently exempts managed turns from USD budgets while still looking
    # like a real measurement in the day ledger.
    if model.lower().startswith("olano:"):
        return 0.0, 0.0
    catalog = _catalog_cost_per_1m(model)
    if catalog is not None:
        return catalog[0] / 1_000_000, catalog[1] / 1_000_000
    model_lower = model.lower()
    for prefix, (inp, out) in _MODEL_COSTS.items():
        if prefix in model_lower:
            return inp / 1_000_000, out / 1_000_000
    # Default: use cheapest known
    return 0.0005 / 1_000_000, 0.0015 / 1_000_000


class CostTracker:
    """Tracks token usage and USD cost per agent, per day/week/month."""

    def __init__(self, agent_name: str, state_dir: Path, budget_config: BudgetConfig) -> None:
        self.agent_name = agent_name
        self.budget = budget_config
        self._path = state_dir / "cost_ledger.json"
        self._ledger: dict[str, dict] = {}
        self._load()

    def record_usage(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Record token usage and return the USD cost of this call."""
        inp_cost, out_cost = _model_cost_per_token(model)
        usd = input_tokens * inp_cost + output_tokens * out_cost

        today = date.today().isoformat()
        if today not in self._ledger:
            self._ledger[today] = {"input_tokens": 0, "output_tokens": 0, "usd": 0.0, "calls": 0}

        self._ledger[today]["input_tokens"] += input_tokens
        self._ledger[today]["output_tokens"] += output_tokens
        self._ledger[today]["usd"] += usd
        self._ledger[today]["calls"] += 1
        self._save()
        return usd

    def daily_usd(self, for_date: date | None = None) -> float:
        key = (for_date or date.today()).isoformat()
        return cast(float, self._ledger.get(key, {}).get("usd", 0.0))

    def monthly_usd(self) -> float:
        prefix = date.today().strftime("%Y-%m")
        return cast(
            float,
            sum(v["usd"] for k, v in self._ledger.items() if k.startswith(prefix)),
        )

    def check_budget(self) -> tuple[bool, str]:
        """
        Return (within_budget, message).
        within_budget=False means the agent should stop or switch model.
        """
        if self.budget.daily_usd is not None:
            daily = self.daily_usd()
            if daily >= self.budget.daily_usd:
                return False, f"Daily budget ${self.budget.daily_usd} exceeded (used ${daily:.4f})"
            if daily >= self.budget.daily_usd * self.budget.alert_at:
                return True, f"Daily budget at {int(daily / self.budget.daily_usd * 100)}%"

        if self.budget.monthly_usd is not None:
            monthly = self.monthly_usd()
            if monthly >= self.budget.monthly_usd:
                return (
                    False,
                    f"Monthly budget ${self.budget.monthly_usd} exceeded (used ${monthly:.2f})",
                )

        return True, ""

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._ledger, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("CostTracker: save failed: %s", e)

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._ledger = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._ledger = {}


def budget_gate(agent_name: str, state_dir: Path, budget: BudgetConfig) -> tuple[bool, str]:
    """Construct a CostTracker and return ``check_budget()``.

    Convenience wrapper so background callers (cognition workers, the tiered
    router) can pre-check spend without re-wiring the ledger path. The ledger
    lives at ``state_dir/cost_ledger.json`` — pass ``workspace.state_dir`` (i.e.
    ``<workspace>/.olano``) to match the per-turn tracker in
    ``core/runner.py``. Never raises: any failure is treated as "within budget"
    so a broken ledger never blocks work.
    """
    try:
        return CostTracker(agent_name, state_dir, budget).check_budget()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("budget_gate: check failed, allowing: %s", e)
        return True, ""
