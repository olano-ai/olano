"""Per-agent / per-platform inbound-message rate limiting.

Covers the multi-window limiter primitive, the config resolver (precedence +
opt-in), the cross-scope "rejected message doesn't inflate the other scope"
guarantee, and the API ingress 429.
"""

from __future__ import annotations

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.system_settings import MessageWindows, RateLimitSettings
from olano.security import rate_limit as rl_mod
from olano.security.message_rate_limit import (
    check_message_rate_limit,
    rate_limit_notice,
)
from olano.security.rate_limit import RUNTIME_RATE_LIMITER, InMemoryRateLimiter


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    RUNTIME_RATE_LIMITER.clear()
    yield
    RUNTIME_RATE_LIMITER.clear()


class _FakeTime:
    """Controllable monotonic-ish clock so hour/day windows don't need real waits."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeTime:
    fake = _FakeTime()
    monkeypatch.setattr(rl_mod, "time", fake)
    return fake


def _agent(rate_limits: dict | None = None) -> AgentConfig:
    # rate_limits=None → leave `security` untouched (default), which is the
    # real-world "opt-out" case: rate_limits not in model_fields_set → unlimited.
    if rate_limits is None:
        return AgentConfig(name="a")
    return AgentConfig(name="a", security={"rate_limits": rate_limits})


# ── check_windows primitive ───────────────────────────────────────────────────


def test_check_windows_single_window(clock: _FakeTime) -> None:
    limiter = InMemoryRateLimiter()
    limits = [(3, 60)]
    assert [limiter.check_windows("k", limits).allowed for _ in range(4)] == [
        True,
        True,
        True,
        False,
    ]
    # Window slides: after 61s the oldest event ages out and one slot frees.
    clock.advance(61)
    assert limiter.check_windows("k", limits).allowed is True


def test_check_windows_hour_cap_binds_before_minute(clock: _FakeTime) -> None:
    limiter = InMemoryRateLimiter()
    limits = [(100, 60), (5, 3600)]  # generous minute, tight hour
    assert sum(limiter.check_windows("k", limits).allowed for _ in range(10)) == 5
    dec = limiter.check_windows("k", limits)
    assert dec.allowed is False
    assert 0 < dec.retry_after <= 3600
    clock.advance(3601)
    assert limiter.check_windows("k", limits).allowed is True


def test_check_windows_single_append_counts_once_across_windows(clock: _FakeTime) -> None:
    """5 calls should leave the minute AND hour buckets at 5 (one append each),
    not 5-and-10 — the windows share a single timestamp stream."""
    limiter = InMemoryRateLimiter()
    limits = [(5, 60), (10, 3600)]
    for _ in range(5):
        assert limiter.check_windows("k", limits).allowed is True
    assert limiter.check_windows("k", limits).allowed is False  # minute cap
    clock.advance(61)  # free the minute window; hour still holds the 5
    allowed_after = sum(limiter.check_windows("k", limits).allowed for _ in range(10))
    assert allowed_after == 5  # 10 hour cap - 5 already used


def test_check_windows_peek_does_not_record(clock: _FakeTime) -> None:
    limiter = InMemoryRateLimiter()
    limits = [(1, 60)]
    for _ in range(20):
        assert limiter.check_windows("k", limits, record=False).allowed is True
    # Never recorded → the single real call still succeeds.
    assert limiter.check_windows("k", limits, record=True).allowed is True
    assert limiter.check_windows("k", limits, record=True).allowed is False


def test_check_windows_empty_is_unlimited() -> None:
    limiter = InMemoryRateLimiter()
    assert all(limiter.check_windows("k", []).allowed for _ in range(1000))


def test_check_windows_zero_blocks() -> None:
    limiter = InMemoryRateLimiter()
    assert limiter.check_windows("k", [(0, 60)]).allowed is False


# ── resolver: opt-in + precedence ─────────────────────────────────────────────


def test_default_config_is_unlimited() -> None:
    cfg = _agent()  # nothing configured
    assert all(check_message_rate_limit("a", "telegram", cfg).allowed for _ in range(500))


def test_agent_messages_cap(clock: _FakeTime) -> None:
    cfg = _agent({"messages": {"per_minute": 2}})
    results = [check_message_rate_limit("a", "telegram", cfg).allowed for _ in range(4)]
    assert results == [True, True, False, False]


def test_legacy_requests_per_minute_only_when_explicit(clock: _FakeTime) -> None:
    # Explicitly set → honored as messages.per_minute.
    explicit = _agent({"requests_per_minute": 2})
    assert [check_message_rate_limit("a", "telegram", explicit).allowed for _ in range(3)] == [
        True,
        True,
        False,
    ]
    # The schema default of 60 (config untouched) must NOT activate limiting.
    RUNTIME_RATE_LIMITER.clear()
    default_cfg = _agent()
    assert default_cfg.security.rate_limits.get("requests_per_minute") == 60
    assert all(check_message_rate_limit("a", "telegram", default_cfg).allowed for _ in range(200))


def test_per_platform_overrides_agent(clock: _FakeTime) -> None:
    cfg = _agent({"messages": {"per_minute": 10}, "per_platform": {"telegram": {"per_minute": 1}}})
    assert check_message_rate_limit("a", "telegram", cfg).allowed is True
    assert check_message_rate_limit("a", "telegram", cfg).allowed is False


def test_home_wide_default_applies(clock: _FakeTime) -> None:
    home = RateLimitSettings(messages=MessageWindows(per_minute=1))
    cfg = _agent()  # no per-agent override
    assert check_message_rate_limit("a", "slack", cfg, home=home).allowed is True
    assert check_message_rate_limit("a", "slack", cfg, home=home).allowed is False


def test_agent_overrides_home_wide(clock: _FakeTime) -> None:
    home = RateLimitSettings(messages=MessageWindows(per_minute=1))
    cfg = _agent({"messages": {"per_minute": 5}})
    assert (
        sum(check_message_rate_limit("a", "slack", cfg, home=home).allowed for _ in range(6)) == 5
    )


def test_home_wide_per_platform_default(clock: _FakeTime) -> None:
    home = RateLimitSettings(per_platform={"telegram": MessageWindows(per_minute=1)})
    cfg = _agent()
    assert check_message_rate_limit("a", "telegram", cfg, home=home).allowed is True
    assert check_message_rate_limit("a", "telegram", cfg, home=home).allowed is False
    # A different platform with no cap stays unlimited.
    assert all(check_message_rate_limit("a", "slack", cfg, home=home).allowed for _ in range(50))


# ── cross-scope isolation ─────────────────────────────────────────────────────


def test_rejected_platform_message_does_not_consume_agent_budget(clock: _FakeTime) -> None:
    """Platform cap of 1 on telegram; agent cap of 5. Flooding telegram must not
    burn the shared agent budget, so another platform still has 4 left."""
    cfg = _agent({"messages": {"per_minute": 5}, "per_platform": {"telegram": {"per_minute": 1}}})
    # 1 telegram allowed, next 4 rejected by the platform scope.
    tele = [check_message_rate_limit("a", "telegram", cfg).allowed for _ in range(5)]
    assert tele == [True, False, False, False, False]
    # Agent budget consumed exactly once → slack still gets 4 before the agent cap.
    slack = [check_message_rate_limit("a", "slack", cfg).allowed for _ in range(5)]
    assert slack == [True, True, True, True, False]


def test_notice_mentions_retry_after() -> None:
    cfg = _agent({"messages": {"per_minute": 1}})
    assert check_message_rate_limit("a", "telegram", cfg).allowed is True
    decision = check_message_rate_limit("a", "telegram", cfg)
    assert decision.allowed is False
    notice = rate_limit_notice(decision)
    assert str(decision.retry_after) in notice
    assert "Rate limit" in notice


# ── API ingress (dashboard/REST) returns 429 ──────────────────────────────────


def test_api_assert_rate_limit_raises_429(clock: _FakeTime) -> None:
    from fastapi import HTTPException

    from olano.runtime.api import _assert_message_rate_limit

    class _Runner:
        config = _agent({"messages": {"per_minute": 1}})

    runner = _Runner()
    _assert_message_rate_limit("a", runner)  # first call: allowed, no raise
    with pytest.raises(HTTPException) as exc:
        _assert_message_rate_limit("a", runner)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_api_assert_rate_limit_noop_when_unconfigured() -> None:
    from olano.runtime.api import _assert_message_rate_limit

    class _Runner:
        config = _agent()

    runner = _Runner()
    for _ in range(200):
        _assert_message_rate_limit("a", runner)  # never raises
