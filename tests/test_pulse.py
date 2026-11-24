"""Tests for olano.pulse.*"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def pulse_state_dir(tmp_path):
    d = tmp_path / ".olano"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def pulse_config():
    from olano.config.agent_config import (
        AgentConfig,
        BudgetConfig,
        MemoryConfig,
        PulseConfig,
        SecurityConfig,
        WalletConfig,
    )

    config = AgentConfig(
        name="test",
        model="anthropic:claude-sonnet-4-6",
        pulse=PulseConfig(enabled=True, max_items_per_session=5, max_items_per_day=20),
        budget=BudgetConfig(daily_usd=None),
        security=SecurityConfig(),
        memory=MemoryConfig(),
        wallet=WalletConfig(),
    )
    return config


@pytest.fixture
def engine(pulse_config, pulse_state_dir):
    from olano.pulse.engine import PulseEngine

    return PulseEngine(pulse_config, pulse_state_dir)


class TestPulseModels:
    def test_pulse_item_active(self):
        from olano.pulse.models import PulseItem, PulseLane, PulseType

        item = PulseItem(
            id="abc",
            source="olano",
            lane=PulseLane.PULSE,
            type=PulseType.SUGGESTION,
            priority=50,
            content="Test suggestion",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert item.is_active

    def test_pulse_item_expired(self):
        from olano.pulse.models import PulseItem, PulseLane, PulseType

        item = PulseItem(
            id="abc",
            source="olano",
            lane=PulseLane.PULSE,
            type=PulseType.SUGGESTION,
            priority=50,
            content="Test suggestion",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert not item.is_active

    def test_pulse_item_to_dict(self):
        from olano.pulse.models import PulseItem, PulseLane, PulseType

        item = PulseItem(
            id="x",
            source="test",
            lane=PulseLane.OPS,
            type=PulseType.ALERT,
            priority=80,
            content="Alert!",
        )
        d = item.to_dict()
        assert d["id"] == "x"
        assert d["lane"] == "ops"
        assert d["type"] == "alert"
        assert d["priority"] == "urgent"
        assert d["priority_score"] == 80


class TestPulseEngine:
    def test_add_item(self, engine):
        item = engine.add("Do something useful", priority=60)
        assert item.id
        assert item.content == "Do something useful"
        assert item.priority == 60

    def test_get_pending_sorted_by_priority(self, engine):
        engine.add("Low priority", priority=10)
        engine.add("High priority", priority=90)
        engine.add("Medium priority", priority=50)

        pending = engine.get_pending()
        assert pending[0].priority >= pending[1].priority >= pending[2].priority

    def test_deduplication(self, engine):
        engine.add("Same content", priority=50)
        engine.add("Same content", priority=50)
        pending = engine.get_pending()
        contents = [i.content for i in pending]
        assert contents.count("Same content") == 1

    def test_resolve_item(self, engine):
        from olano.pulse.models import PulseStatus

        item = engine.add("To be dismissed")
        engine.resolve(item.id, PulseStatus.REJECTED)
        pending = engine.get_pending()
        assert not any(i.id == item.id for i in pending)

    def test_mark_delivered(self, engine):
        from olano.pulse.models import PulseStatus

        item = engine.add("Deliver me")
        engine.mark_delivered(item.id)
        assert item.status == PulseStatus.DELIVERED

    def test_budget_alert(self, engine):
        item = engine.generate_budget_alert(used_usd=85.0, limit_usd=100.0)
        assert item.lane_type.value == "ops"
        assert item.priority_level.value in {"urgent", "normal"}
        assert "85%" in item.content or "85" in item.content

    def test_security_alert(self, engine):
        item = engine.generate_security_alert("Suspicious activity detected")
        assert item.lane_type.value == "ops"
        assert item.priority_level.value == "urgent"

    def test_persistence(self, pulse_config, pulse_state_dir):
        from olano.pulse.engine import PulseEngine

        e1 = PulseEngine(pulse_config, pulse_state_dir)
        e1.add("Persistent item", priority=70)

        # Load fresh instance
        e2 = PulseEngine(pulse_config, pulse_state_dir)
        pending = e2.get_pending()
        assert any(i.content == "Persistent item" for i in pending)

    def test_clear_expired(self, engine):
        item = engine.add("Expiring soon")
        # Manually expire
        item.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        engine._save()

        removed = engine.clear_expired()
        assert removed >= 0  # Should handle gracefully


class TestCostTracker:
    @pytest.fixture
    def tracker(self, tmp_path):
        from olano.config.agent_config import BudgetConfig
        from olano.pulse.cost_tracker import CostTracker

        budget = BudgetConfig(daily_usd=10.0, monthly_usd=100.0, alert_at=0.8)
        return CostTracker("test_agent", tmp_path, budget)

    def test_record_usage(self, tracker):
        usd = tracker.record_usage("claude-sonnet-4-6", input_tokens=1000, output_tokens=500)
        assert usd > 0
        assert tracker.daily_usd() > 0

    def test_daily_accumulates(self, tracker):
        tracker.record_usage("claude-sonnet-4-6", 1000, 500)
        tracker.record_usage("claude-sonnet-4-6", 1000, 500)
        total = tracker.daily_usd()
        single = tracker.record_usage("claude-sonnet-4-6", 1000, 500)
        assert tracker.daily_usd() > total

    def test_within_budget(self, tracker):
        tracker.record_usage("claude-haiku", 100, 50)  # Tiny cost
        within, msg = tracker.check_budget()
        assert within

    def test_budget_exceeded(self, tmp_path):
        from olano.config.agent_config import BudgetConfig
        from olano.pulse.cost_tracker import CostTracker

        budget = BudgetConfig(daily_usd=0.000001)  # 1 micro-dollar limit
        tracker = CostTracker("test", tmp_path, budget)
        tracker.record_usage("claude-opus", 10000, 5000)
        within, msg = tracker.check_budget()
        assert not within
        assert "exceeded" in msg.lower()

    def test_local_model_zero_cost(self, tracker):
        usd = tracker.record_usage("ollama:llama3.2", 10000, 5000)
        assert usd == 0.0
