"""Durable usage counters — reservations, concurrency, windows, backfill."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from olano.deployment import resolver, usage_counters
from olano.deployment.envelope import sign_license

NOW = 1_780_000_000  # 2026-05-28 UTC (a Thursday)
KEY_ID = "settings-test-2026-07"
DEP_ID = "dep_usage_test"

_PRIVATE = ed25519.Ed25519PrivateKey.generate()
_KEYS = {KEY_ID: _PRIVATE.public_key()}


def mint(settings: dict) -> str:
    payload = {
        "schema": "olano.deployment-settings",
        "schema_version": 1,
        "issuer": "cloud.olano.ai",
        "audience": "olano-deployment-settings",
        "channel": "local",
        "deployment_id": DEP_ID,
        "plan": "pro",
        "policy_revision": 1,
        "issued_at": NOW - 1000,
        "expires_at": NOW + 10 * 365 * 86400,
        "settings": settings,
    }
    return sign_license(payload, _PRIVATE, key_id=KEY_ID)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
    monkeypatch.setattr("olano.deployment.envelope.load_verify_keys", lambda: _KEYS)
    resolver.invalidate()
    yield tmp_path
    resolver.invalidate()


def manage(home: Path, settings: dict) -> None:
    (home / "installation.json").write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID})
    )
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.deployment_license_path.write_text(mint(settings) + "\n")
    resolver.invalidate()


def budget(**limits) -> dict:
    return {"usage": {k: v for k, v in limits.items()}}


class TestWindowMath:
    def test_daily(self):
        assert usage_counters.window_start_day("daily", NOW) == "2026-05-28"

    def test_weekly_iso_monday(self):
        # 2026-05-28 is a Thursday; ISO week starts Monday 2026-05-25.
        assert usage_counters.window_start_day("weekly", NOW) == "2026-05-25"

    def test_weekly_on_monday_is_same_day(self):
        monday = NOW - 3 * 86400  # 2026-05-25
        assert usage_counters.window_start_day("weekly", monday) == "2026-05-25"

    def test_monthly_first(self):
        assert usage_counters.window_start_day("monthly", NOW) == "2026-05-01"

    def test_monthly_on_month_boundary(self):
        # 2026-06-01 00:00:00 UTC
        june1 = 1_780_272_000
        start = usage_counters.window_start_day("monthly", june1)
        assert start == "2026-06-01"

    def test_weekly_across_year_boundary(self):
        # 2026-01-01 is a Thursday → ISO week starts Monday 2025-12-29.
        jan1_2026 = 1_767_225_600
        assert usage_counters.window_start_day("weekly", jan1_2026) == "2025-12-29"

    def test_lifetime_is_none(self):
        assert usage_counters.window_start_day("lifetime", NOW) is None


class TestOpenPosture:
    def test_no_reservation_no_db(self, home):
        rid, decision = usage_counters.try_reserve("a", now=NOW)
        assert rid is None
        assert decision.allowed
        usage_counters.record("a", input_tokens=5, output_tokens=5, cost_micro_usd=5, now=NOW)
        from olano.config.home import get_olano_paths

        assert not (get_olano_paths().deployment_dir / usage_counters.DB_FILE).exists()

    def test_check_budget_open_allows(self, home):
        assert usage_counters.check_budget("a", now=NOW).allowed


class TestReserveAndBudget:
    def test_under_budget_allows_and_books(self, home):
        manage(home, budget(input_tokens_total={"value": 100_000, "window": "monthly"}))
        rid, decision = usage_counters.try_reserve("a", now=NOW)
        assert decision.allowed and rid
        # The estimate is booked immediately.
        assert usage_counters.current_usage_for_limit("input_tokens_total", now=NOW) > 0

    def test_exhausted_budget_denies_with_structure(self, home):
        manage(home, budget(cost_total={"value": 1_000, "window": "monthly"}))
        usage_counters.record("a", input_tokens=0, output_tokens=0, cost_micro_usd=1_000, now=NOW)
        rid, decision = usage_counters.try_reserve("a", now=NOW)
        assert rid is None
        assert not decision.allowed
        assert decision.code == "deployment_budget_cost_total"
        assert decision.limit == 1_000
        assert decision.current == 1_000

    def test_per_agent_budget_scopes_to_agent(self, home):
        manage(home, budget(output_tokens_per_agent={"value": 100, "window": "monthly"}))
        usage_counters.record("a", input_tokens=0, output_tokens=100, cost_micro_usd=0, now=NOW)
        assert not usage_counters.try_reserve("a", now=NOW)[1].allowed
        assert usage_counters.try_reserve("b", now=NOW)[1].allowed

    def test_window_resets(self, home):
        manage(home, budget(input_tokens_total={"value": 100, "window": "daily"}))
        usage_counters.record("a", input_tokens=100, output_tokens=0, cost_micro_usd=0, now=NOW)
        assert not usage_counters.try_reserve("a", now=NOW)[1].allowed
        tomorrow = NOW + 86_400
        assert usage_counters.try_reserve("a", now=tomorrow)[1].allowed

    def test_lifetime_window_never_resets(self, home):
        manage(home, budget(input_tokens_total={"value": 100, "window": "lifetime"}))
        usage_counters.record("a", input_tokens=100, output_tokens=0, cost_micro_usd=0, now=NOW)
        much_later = NOW + 400 * 86_400
        assert not usage_counters.try_reserve("a", now=much_later)[1].allowed

    def test_check_budget_reports_exhaustion(self, home):
        manage(home, budget(output_tokens_total={"value": 10, "window": "monthly"}))
        usage_counters.record("a", input_tokens=0, output_tokens=10, cost_micro_usd=0, now=NOW)
        decision = usage_counters.check_budget("a", now=NOW)
        assert not decision.allowed
        assert decision.code == "deployment_budget_output_tokens_total"


class TestReconcile:
    def test_reconcile_replaces_estimate(self, home):
        manage(home, budget(input_tokens_total={"value": 1_000_000, "window": "monthly"}))
        rid, _ = usage_counters.try_reserve("a", now=NOW, estimate_input=500, estimate_output=100)
        usage_counters.reconcile(rid, input_tokens=42, output_tokens=7, cost_micro_usd=13)
        assert usage_counters.current_usage_for_limit("input_tokens_total", now=NOW) == 42
        assert usage_counters.current_usage_for_limit("output_tokens_total", now=NOW) == 7
        assert usage_counters.current_usage_for_limit("cost_total", now=NOW) == 13

    def test_reconcile_is_idempotent(self, home):
        manage(home, budget(input_tokens_total={"value": 1_000_000, "window": "monthly"}))
        rid, _ = usage_counters.try_reserve("a", now=NOW, estimate_input=500, estimate_output=0)
        usage_counters.reconcile(rid, input_tokens=42, output_tokens=0, cost_micro_usd=0)
        usage_counters.reconcile(rid, input_tokens=42, output_tokens=0, cost_micro_usd=0)
        assert usage_counters.current_usage_for_limit("input_tokens_total", now=NOW) == 42

    def test_unknown_reservation_noop(self, home):
        manage(home, budget())
        usage_counters.reconcile("nope", input_tokens=1, output_tokens=1, cost_micro_usd=1)

    def test_stale_reservation_released(self, home):
        manage(home, budget(input_tokens_total={"value": 1_000, "window": "monthly"}))
        usage_counters.try_reserve("a", now=NOW, estimate_input=900, estimate_output=0)
        # Un-reconciled (crashed turn). Past the stale horizon a new reserve
        # releases it, freeing the budget.
        later = NOW + usage_counters.STALE_RESERVATION_SECONDS + 10
        rid, decision = usage_counters.try_reserve("a", now=later, estimate_input=900)
        assert decision.allowed and rid
        # Only the new reservation's estimate remains booked.
        assert usage_counters.current_usage_for_limit("input_tokens_total", now=later) == 900


class TestConcurrency:
    def test_concurrent_reserves_cannot_jointly_exceed(self, home):
        """Budget fits ONE call's estimate; 6 threads race — exactly one wins."""
        manage(home, budget(input_tokens_total={"value": 1_500, "window": "monthly"}))
        # Pre-consume so remaining budget (500) < 2 × estimate (1000 each).
        usage_counters.record("a", input_tokens=1_000, output_tokens=0, cost_micro_usd=0, now=NOW)
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def attempt() -> None:
            barrier.wait()
            _rid, decision = usage_counters.try_reserve(
                "a", now=NOW, estimate_input=1_000, estimate_output=0
            )
            with lock:
                results.append(decision.allowed)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Budget not yet exhausted (1000 < 1500) ⇒ first reservation allowed;
        # it books +1000 (total 2000 ≥ 1500) ⇒ every later attempt denied.
        assert sum(results) == 1, results


class TestLedgerBackfill:
    def test_seed_from_ledger_counts_history(self, home, monkeypatch):
        """History predating this module still counts — tokens always, cost only
        where Olano paid. A BYOK row's provider-price estimate is the customer's
        spend on the customer's own account, and the cost budget bounds ours."""
        manage(home, budget(cost_total={"value": 10_000_000, "window": "lifetime"}))
        from olano.analytics.usage_ledger import record_usage_event
        from olano.catalog.credits_registry import credits_per_usd

        record_usage_event("olde", "anthropic:x", 1_000, 200, 1.25)
        record_usage_event("olde", "olano:pro", 500, 100, 0.0, credits=2.5)
        # First counter touch seeds from the ledger.
        assert usage_counters.current_usage_for_limit("cost_total", now=NOW) == round(
            2.5 * 1_000_000 / credits_per_usd()
        )
        assert usage_counters.current_usage_for_limit("input_tokens_total", now=NOW) == 1_500

    def test_seed_runs_once(self, home):
        manage(home, budget())
        from olano.analytics.usage_ledger import record_usage_event

        usage_counters.record("a", input_tokens=1, output_tokens=0, cost_micro_usd=0, now=NOW)
        record_usage_event("late", "anthropic:x", 999, 0, 0.0)  # after first seed
        usage_counters.record("a", input_tokens=1, output_tokens=0, cost_micro_usd=0, now=NOW)
        # The late event must not be double-imported.
        assert usage_counters.current_usage_for_limit("input_tokens_total", now=NOW) == 2

    def test_seed_skips_backfill_rows(self, home):
        """Backfill rows reconstruct spend that was already billed live —
        seeding them into the budget counters would count it twice."""
        manage(home, budget())
        from olano.analytics.usage_ledger import record_usage_event

        record_usage_event("olde", "anthropic:x", 1_000, 200, 1.25)
        record_usage_event(
            "olde",
            "olano:pro",
            9_999,
            0,
            0.0,
            credits=50.0,
            origin="cognition",
            backfill=True,
            src="cog::olde__cog__ideation__aa11bb",
        )
        assert usage_counters.current_usage_for_limit("input_tokens_total", now=NOW) == 1_000


class TestQuarantineAllowance:
    def test_quarantine_has_small_but_nonzero_budget(self, home):
        (home / "installation.json").write_text(
            json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID})
        )
        resolver.invalidate()
        assert resolver.deployment_status(now=NOW).quarantined
        rid, decision = usage_counters.try_reserve("merli", now=NOW)
        assert decision.allowed and rid  # recovery allowance
        usage_counters.record(
            "merli", input_tokens=200_000, output_tokens=0, cost_micro_usd=0, now=NOW
        )
        assert not usage_counters.try_reserve("merli", now=NOW)[1].allowed


class TestRunnerIntegration:
    def test_record_usage_raises_when_budget_exhausted(self, home):
        """An exhausted cost budget stops the agent. The spend has to be OURS:
        gateway calls report their charge in credits, which is what the budget
        counts."""
        manage(home, budget(cost_total={"value": 1, "window": "monthly"}))
        from olano.config.agent_config import AgentConfig
        from olano.core.runner import AgentError, _record_usage

        cfg = AgentConfig(name="spender", model="olano:pro")
        with pytest.raises(AgentError) as exc:
            _record_usage(cfg, {"input_tokens": 1000, "output_tokens": 1000, "credits_micro": 5000})
        assert "budget" in str(exc.value).lower()

    def test_byok_spend_does_not_consume_the_cost_budget(self, home):
        """The cost budget bounds what Olano pays upstream. A BYOK call runs on
        the customer's own provider account, so charging its provider-price
        estimate here would throttle an agent over money that was never ours."""
        manage(home, budget(cost_total={"value": 1, "window": "monthly"}))
        from olano.config.agent_config import AgentConfig
        from olano.core.runner import _record_usage

        cfg = AgentConfig(name="byok", model="anthropic:claude-sonnet-4-6")
        usd = _record_usage(cfg, {"input_tokens": 1000, "output_tokens": 1000})

        assert usd > 0.0  # the call did cost the customer something…
        # …but none of it lands on the deployment's cost counter, so a $1
        # ceiling does not stop an agent spending the customer's own key.
        assert usage_counters.current_usage_for_limit("cost_total") == 0

    def test_byok_tokens_still_count(self, home):
        """Only cost is provider-scoped; token budgets are universal."""
        manage(home, budget(input_tokens_total={"value": 10_000_000, "window": "monthly"}))
        from olano.config.agent_config import AgentConfig
        from olano.core.runner import _record_usage

        cfg = AgentConfig(name="byok2", model="anthropic:claude-sonnet-4-6")
        _record_usage(cfg, {"input_tokens": 1000, "output_tokens": 1000})

        assert usage_counters.current_usage_for_limit("input_tokens_total") >= 1000

    def test_gateway_charge_is_converted_from_credits(self, home):
        """cost_for_call() returns 0.0 for olano:* on purpose (any USD it
        invented would be fiction), so the credits the gateway reports are the
        only real charge — converted at the published rate."""
        manage(home, budget(cost_total={"value": 10_000_000, "window": "monthly"}))
        from olano.catalog.credits_registry import credits_per_usd
        from olano.config.agent_config import AgentConfig
        from olano.core.runner import _record_usage

        # Warm the one-shot ledger seed first, so the assertion below sees only
        # this call's own contribution (the seed would import the ledger row the
        # same call writes, counting it twice).
        usage_counters.record("managed", input_tokens=0, output_tokens=0, cost_micro_usd=0)

        cfg = AgentConfig(name="managed", model="olano:turbo")
        _record_usage(cfg, {"input_tokens": 1000, "output_tokens": 1000, "credits_micro": 7000})

        assert usage_counters.current_usage_for_limit("cost_total") == round(
            7000 / credits_per_usd()
        )

    def test_open_posture_record_usage_unchanged(self, home):
        from olano.config.agent_config import AgentConfig
        from olano.core.runner import _record_usage

        cfg = AgentConfig(name="free", model="anthropic:claude-sonnet-4-6")
        usd = _record_usage(cfg, {"input_tokens": 1000, "output_tokens": 1000})
        assert usd >= 0.0


def write_budget_config(**block) -> None:
    """Put a ``usage_budgets:`` block in the home ``config.yaml``."""
    from olano.config.system_settings import set_usage_budget_settings

    set_usage_budget_settings(block)


class TestConfigOverrides:
    """`usage_budgets:` in config.yaml, over the signed artifact's numbers.

    The token ceilings are runaway-loop guards, not meters: BYOK tokens are the
    customer's own provider spend, and managed spend is bounded independently
    by the gateway's credit enforcement. So an operator may move them. The cost
    budgets bound olano's own upstream bill and stay where they are signed.
    """

    def test_config_raises_a_token_ceiling(self, home):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        usage_counters.record("a", input_tokens=2000, output_tokens=0, cost_micro_usd=0)
        assert not usage_counters.check_budget("a").allowed  # blocked at the signed ceiling

        write_budget_config(input_tokens_total=1_000_000)
        assert usage_counters.check_budget("a").allowed

    def test_config_can_lift_a_token_ceiling_entirely(self, home):
        manage(home, budget(input_tokens_per_agent={"value": 1000, "window": "monthly"}))
        usage_counters.record("a", input_tokens=5000, output_tokens=0, cost_micro_usd=0)
        assert not usage_counters.check_budget("a").allowed

        write_budget_config(input_tokens_per_agent="unlimited")
        assert usage_counters.check_budget("a").allowed

    def test_config_can_also_tighten(self, home):
        manage(home, budget(input_tokens_total={"value": 10_000_000, "window": "monthly"}))
        write_budget_config(input_tokens_total=1000)
        usage_counters.record("a", input_tokens=2000, output_tokens=0, cost_micro_usd=0)
        assert not usage_counters.check_budget("a").allowed

    def test_config_cannot_touch_the_cost_budget(self, home):
        """The one budget that bounds OUR bill is not settable from the box."""
        manage(home, budget(cost_total={"value": 1000, "window": "monthly"}))
        write_budget_config(cost_total=10_000_000)  # ignored, not honored
        usage_counters.record("a", input_tokens=0, output_tokens=0, cost_micro_usd=5000)

        decision = usage_counters.check_budget("a")
        assert not decision.allowed
        assert decision.code == "deployment_budget_cost_total"

        from olano.config.system_settings import load_usage_budget_settings

        assert not hasattr(load_usage_budget_settings(), "cost_total")

    def test_absent_key_inherits_the_deployment_value(self, home):
        """Only the keys an operator actually set are overridden — writing one
        budget must not silently uncap the other five."""
        manage(
            home,
            budget(
                input_tokens_total={"value": 10_000_000, "window": "monthly"},
                output_tokens_total={"value": 1000, "window": "monthly"},
            ),
        )
        write_budget_config(input_tokens_total="unlimited")
        usage_counters.record("a", input_tokens=0, output_tokens=2000, cost_micro_usd=0)

        decision = usage_counters.check_budget("a")
        assert decision.code == "deployment_budget_output_tokens_total"

    def test_clearing_an_override_restores_the_deployment_ceiling(self, home):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        write_budget_config(input_tokens_total=1_000_000)
        usage_counters.record("a", input_tokens=2000, output_tokens=0, cost_micro_usd=0)
        assert usage_counters.check_budget("a").allowed

        write_budget_config(input_tokens_total=None)  # an emptied dashboard field
        assert not usage_counters.check_budget("a").allowed

    def test_remediation_names_the_knob_for_a_token_budget(self, home):
        """The refusal an agent reads has to say what would fix it. 'Upgrade
        your plan' is wrong for a ceiling the operator can move themselves."""
        manage(home, budget(input_tokens_total={"value": 1, "window": "monthly"}))
        usage_counters.record("a", input_tokens=10, output_tokens=0, cost_micro_usd=0)

        decision = usage_counters.check_budget("a")
        assert "usage_budgets.input_tokens_total" in decision.remediation
        assert "unlimited" in decision.remediation


class TestBudgetAlerts:
    """Threshold notifications as a budget fills, so nobody meets a ceiling
    for the first time as a refused turn."""

    @staticmethod
    def _titles(monkeypatch) -> list[dict]:
        raised: list[dict] = []
        from olano.runtime import notifications

        monkeypatch.setattr(
            notifications, "notify", lambda **kw: raised.append(kw) or kw, raising=True
        )
        return raised

    def test_crossing_a_threshold_notifies(self, home, monkeypatch):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=550, output_tokens=0, cost_micro_usd=0)

        assert len(raised) == 1
        assert "50% used" in raised[0]["title"]
        assert raised[0]["kind"] == "usage_budget"
        assert raised[0]["level"] == "info"

    def test_each_threshold_fires_once_per_window(self, home, monkeypatch):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=550, output_tokens=0, cost_micro_usd=0)
        usage_counters.record("a", input_tokens=10, output_tokens=0, cost_micro_usd=0)
        usage_counters.record("a", input_tokens=10, output_tokens=0, cost_micro_usd=0)

        assert [r["title"] for r in raised] == [raised[0]["title"]]

    def test_only_the_highest_threshold_crossed_is_announced(self, home, monkeypatch):
        """One turn can jump 40% → 96%. That is one notification, not five."""
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=960, output_tokens=0, cost_micro_usd=0)

        assert len(raised) == 1
        assert "95% used" in raised[0]["title"]
        assert raised[0]["level"] == "warn"

    def test_exhaustion_notifies_as_an_error(self, home, monkeypatch):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=1000, output_tokens=0, cost_micro_usd=0)

        assert len(raised) == 1
        assert "exhausted" in raised[0]["title"]
        assert raised[0]["level"] == "error"
        assert "refused" in raised[0]["body"]

    def test_per_agent_alerts_name_the_agent(self, home, monkeypatch):
        manage(home, budget(input_tokens_per_agent={"value": 1000, "window": "monthly"}))
        raised = self._titles(monkeypatch)

        usage_counters.record("notion", input_tokens=910, output_tokens=0, cost_micro_usd=0)

        assert raised[0]["agent"] == "notion"
        assert "notion" in raised[0]["body"]

    def test_alerts_can_be_switched_off(self, home, monkeypatch):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        write_budget_config(alerts_enabled=False)
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=1000, output_tokens=0, cost_micro_usd=0)

        assert raised == []

    def test_custom_thresholds_are_honored(self, home, monkeypatch):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        write_budget_config(alert_percents=[25])
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=260, output_tokens=0, cost_micro_usd=0)

        assert "25% used" in raised[0]["title"]

    def test_cost_alerts_render_as_dollars(self, home, monkeypatch):
        manage(home, budget(cost_total={"value": 2_000_000, "window": "monthly"}))
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=0, output_tokens=0, cost_micro_usd=1_100_000)

        assert "$1.10 of $2.00" in raised[0]["body"]

    def test_an_unlimited_budget_never_alerts(self, home, monkeypatch):
        manage(home, budget(input_tokens_total={"value": 1000, "window": "monthly"}))
        write_budget_config(input_tokens_total="unlimited")
        raised = self._titles(monkeypatch)

        usage_counters.record("a", input_tokens=999_999, output_tokens=0, cost_micro_usd=0)

        assert raised == []
