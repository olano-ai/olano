"""Time-of-day pricing: DeepSeek's half-price hours, read from the cloud catalog.

The managed gateway bills off-peak calls through LiteLLM's ``off_peak_pricing``
rule, and this box estimates the same charge with its own copy of that rule
(``olano.catalog.off_peak``). The two must agree minute for minute, which is
what the LiteLLM comparison below pins.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from olano.catalog import models_registry
from olano.catalog.off_peak import active_off_peak, is_off_peak, parse_timestamp

# The windows olano-ai/cloud migration 0104 stores: DeepSeek's peak hours are
# weekdays 01:00-04:00 and 06:00-10:00 UTC; everything else is off-peak.
WINDOWS = [
    {"weekdays": [1, 2, 3, 4, 5], "hours_utc": ["00:00-01:00", "04:00-06:00", "10:00-00:00"]},
    {"weekdays": [6, 7], "hours_utc": ["00:00-00:00"]},
]
MONDAY = datetime(2026, 10, 26, tzinfo=UTC)
PEAK = MONDAY.replace(hour=2)
OFF_PEAK = MONDAY.replace(hour=12)


def _deepseek_peak(moment: datetime) -> bool:
    hour = moment.hour + moment.minute / 60
    return moment.isoweekday() <= 5 and (1 <= hour < 4 or 6 <= hour < 10)


def test_off_peak_is_every_minute_deepseek_does_not_call_peak() -> None:
    moment = MONDAY
    while moment < MONDAY + timedelta(days=7):
        assert is_off_peak(WINDOWS, moment) is not _deepseek_peak(moment), moment
        moment += timedelta(minutes=1)


def test_agrees_with_litellm_the_gateway_bills_by() -> None:
    from litellm.litellm_core_utils.llm_cost_calc.utils import _is_off_peak

    block = {"windows": WINDOWS}
    oddities = [
        {"windows": [{"weekdays": [3], "hours_utc": "22:30-02:15"}]},
        {"windows": [{"weekdays": [1, 7], "hours_utc": ["05:00-05:00"]}]},
        {"windows": [{"weekdays": [2], "hours_utc": ["09:15-09:45", "bogus", "25:00-01:00"]}]},
    ]
    moment = MONDAY
    while moment < MONDAY + timedelta(days=7):
        for b in [block, *oddities]:
            assert is_off_peak(b["windows"], moment) == _is_off_peak(b, moment), (b, moment)
        moment += timedelta(minutes=15)


def test_time_zones_and_malformed_blocks() -> None:
    # 12:00 in UTC+8 is 04:00 UTC: off-peak. A naive time is UTC.
    assert is_off_peak(WINDOWS, datetime(2026, 10, 26, 12, tzinfo=timezone(timedelta(hours=8))))
    assert not is_off_peak(WINDOWS, datetime(2026, 10, 26, 2))
    for bad in (None, "x", [{"weekdays": "1", "hours_utc": "00:00-00:00"}], [{"weekdays": [1]}]):
        assert not is_off_peak(bad, OFF_PEAK)
    assert active_off_peak({"windows": WINDOWS, "input": 1}, OFF_PEAK) == {
        "windows": WINDOWS,
        "input": 1,
    }
    assert active_off_peak({"windows": WINDOWS, "input": 1}, PEAK) is None
    assert active_off_peak(None, OFF_PEAK) is None


def test_parse_timestamp() -> None:
    epoch = int(PEAK.timestamp())
    assert parse_timestamp(epoch) == PEAK
    assert parse_timestamp(str(epoch)) == PEAK
    assert parse_timestamp(epoch * 1000) == PEAK
    assert parse_timestamp("2026-10-26T02:00:00Z") == PEAK
    assert parse_timestamp("2026-10-26T10:00:00+08:00") == PEAK
    assert parse_timestamp(None) is None
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(True) is None


FLASH_ENTRY = {
    "id": "deepseek:deepseek-flash",
    "provider": "deepseek",
    "model": "deepseek-flash",
    "priceInput": 0.30,
    "priceOutput": 1.20,
    "priceCacheRead": 0.006,
    "metadata": {
        "litellm_key": "deepseek/deepseek-flash",
        "off_peak": {
            "price_input": 0.15,
            "price_cache_read": 0.003,
            "price_output": 0.60,
            "windows": WINDOWS,
        },
    },
}


@pytest.fixture
def flash_catalog(monkeypatch):
    monkeypatch.setattr(
        models_registry,
        "model_entry",
        lambda model_id: FLASH_ENTRY if model_id == "deepseek:deepseek-flash" else None,
    )


def test_catalog_price_follows_the_hour(flash_catalog) -> None:
    peak = models_registry.model_pricing("deepseek:deepseek-flash", PEAK)
    off = models_registry.model_pricing("deepseek:deepseek-flash", OFF_PEAK)
    assert peak is not None and off is not None
    assert (peak["input"], peak["cache_read"], peak["output"]) == (0.30, 0.006, 1.20)
    assert (off["input"], off["cache_read"], off["output"]) == (0.15, 0.003, 0.60)


def test_a_malformed_off_peak_block_keeps_the_peak_price(monkeypatch) -> None:
    broken = {**FLASH_ENTRY, "metadata": {"off_peak": {"windows": WINDOWS, "price_input": 0}}}
    monkeypatch.setattr(models_registry, "model_entry", lambda _m: broken)
    off = models_registry.model_pricing("deepseek:deepseek-flash", OFF_PEAK)
    assert off is not None and (off["input"], off["output"]) == (0.30, 1.20)


def test_byok_call_cost_halves_off_peak(flash_catalog) -> None:
    from olano.analytics.threadcost.cost import cost_for_call

    args = ("deepseek:deepseek-flash", 1_000_000, 1_000_000, 400_000)
    # 600k fresh × 0.30 + 400k cached × 0.006 + 1M out × 1.20
    assert cost_for_call(*args, at=PEAK) == pytest.approx(0.18 + 0.0024 + 1.20)
    assert cost_for_call(*args, at=OFF_PEAK) == pytest.approx((0.18 + 0.0024 + 1.20) / 2)


def test_threadcost_prices_a_recorded_call_by_its_own_time(flash_catalog) -> None:
    from olano.analytics.threadcost.attribution import _attribute_namespace
    from olano.analytics.threadcost.reader import CallRecord

    def call(ts: datetime) -> CallRecord:
        return CallRecord(
            thread_id="t",
            ns="",
            seq=0,
            message_id=None,
            model="deepseek:deepseek-flash",
            input_tokens=1_000_000,
            output_tokens=0,
            total_tokens=1_000_000,
            cache_read_tokens=0,
            usd=0.0,
            pricing_known=True,
            tool_calls=(),
            ts=ts.isoformat(),
        )

    def billed(ts: datetime) -> float:
        usd: dict[str, float] = {}
        _attribute_namespace([call(ts)], usd, {}, {})
        return sum(usd.values())

    assert billed(PEAK) == pytest.approx(0.30)
    assert billed(OFF_PEAK) == pytest.approx(0.15)


def test_managed_tier_estimate_uses_the_off_peak_credit_rate(monkeypatch) -> None:
    """The cloud copies the primary backend's off-peak rate into the tier's
    credit_pricing; a streamed turn's estimate follows the clock with it."""
    from olano.models.olano_gateway import estimate_credits_micro, tier_credit_rates

    pricing = {
        "input": 525,
        "cached_input": 10.5,
        "output": 2100,
        "off_peak": {"input": 262.5, "cached_input": 5.25, "output": 1050, "windows": WINDOWS},
    }
    monkeypatch.setattr(
        models_registry,
        "model_entry",
        lambda m: {"metadata": {"credit_pricing": pricing}} if m == "olano:turbo" else None,
    )
    assert tier_credit_rates("turbo", PEAK) == {
        "input": 525.0,
        "cached_input": 10.5,
        "output": 2100.0,
    }
    assert tier_credit_rates("turbo", OFF_PEAK) == {
        "input": 262.5,
        "cached_input": 5.25,
        "output": 1050.0,
    }
    usage = {"input_tokens": 1000, "output_tokens": 100}
    assert estimate_credits_micro("olano:turbo", usage, PEAK) == 1000 * 525 + 100 * 2100
    assert estimate_credits_micro("olano:turbo", usage, OFF_PEAK) == 1000 * 262.5 + 100 * 1050
