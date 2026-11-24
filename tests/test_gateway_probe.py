"""Tests for the gateway served-groups probe.

The gap this closes: holding a gateway key is not the same as the gateway
ROUTING what this box asks it for. A managed deployment entitled to vision,
whose gateway served no `olano-vision` group, failed every image_describe call
while every local surface still reported managed vision as available — the
failure was visible only in a chat transcript.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from olano.deployment import gateway_probe as gp


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    gp.reset_cache()
    yield
    gp.reset_cache()


def _models_response(*ids: str) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"data": [{"id": i} for i in ids]}
    resp.raise_for_status.return_value = None
    return resp


def _entitlements(**flags: bool):
    """feature_enabled stub; unnamed flags default to on (the open posture)."""

    def _enabled(name: str) -> bool:
        return flags.get(name, True)

    return _enabled


def _patched(*, reachable: bool = True, key: str | None = "sk-gw", **flags: bool):
    return (
        patch("olano.tools.image_tools.gateway_media_reachable", return_value=reachable),
        patch("olano.deployment.enforcement.feature_enabled", side_effect=_entitlements(**flags)),
        patch("olano.deployment.gateway_key.gateway_key", return_value=key),
    )


def test_expected_groups_covers_tiers_and_managed_services():
    reach, feat, _key = _patched()
    with reach, feat:
        groups = gp.expected_groups()
    # Offered tiers (auto is hidden) plus every entitled managed service.
    assert "olano-turbo" in groups
    assert "olano-pro" in groups
    assert "olano-vision" in groups
    assert "olano-image" in groups
    assert "olano-tts" in groups and "olano-stt" in groups
    # Hidden tiers are never expected — nothing asks for them.
    assert "olano-auto" not in groups


def test_a_withheld_service_is_not_expected():
    """A plan that withholds vision legitimately has no such group. Reporting it
    as missing would be a false alarm on a correctly-configured deployment."""
    reach, feat, _key = _patched(olano_vision=False, olano_model_pro=False)
    with reach, feat:
        groups = gp.expected_groups()
    assert "olano-vision" not in groups
    assert "olano-pro" not in groups
    assert "olano-turbo" in groups  # ungated: the default tier is never withheld


def test_unmanaged_box_expects_nothing():
    reach, feat, _key = _patched(reachable=False)
    with reach, feat:
        assert gp.expected_groups() == ()


def test_probe_names_the_group_the_gateway_does_not_serve():
    reach, feat, key = _patched()
    served = _models_response("olano-turbo", "olano-pro", "olano-max", "olano-image")
    with reach, feat, key, patch("httpx.get", return_value=served):
        result = gp.probe()

    assert result.ok is True
    assert "olano-vision" in result.missing
    assert "olano-turbo" not in result.missing
    assert "olano-turbo" in result.served


def test_probe_is_clean_when_everything_expected_is_served():
    reach, feat, key = _patched(olano_tts=False, olano_stt=False, olano_image=False)
    served = _models_response("olano-turbo", "olano-pro", "olano-max", "olano-vision")
    with reach, feat, key, patch("httpx.get", return_value=served):
        result = gp.probe()

    assert result.ok is True
    assert result.missing == ()


def test_probe_without_a_key_reports_why_and_makes_no_request():
    reach, feat, key = _patched(key=None)
    with reach, feat, key, patch("httpx.get") as get:
        result = gp.probe()

    assert result.ok is False
    assert "no gateway key" in (result.error or "")
    get.assert_not_called()


def test_a_failed_probe_keeps_the_last_good_reading():
    """Overwriting a good reading with an empty one on a timeout would render as
    "the gateway serves nothing" — a far bigger incident than the truth."""
    reach, feat, key = _patched()
    served = _models_response("olano-turbo", "olano-pro", "olano-max", "olano-vision")
    with reach, feat, key, patch("httpx.get", return_value=served):
        good = gp.probe()
    assert good.ok and "olano-turbo" in good.served

    reach2, feat2, key2 = _patched()
    with reach2, feat2, key2, patch("httpx.get", side_effect=Exception("connect timeout")):
        after = gp.probe(force=True)

    assert after.ok is True
    assert after.served == good.served


def test_the_cache_serves_repeat_reads_and_force_bypasses_it():
    reach, feat, key = _patched()
    served = _models_response("olano-turbo")
    with reach, feat, key, patch("httpx.get", return_value=served) as get:
        gp.probe()
        gp.probe()
        assert get.call_count == 1
        gp.probe(force=True)
        assert get.call_count == 2


def test_cached_never_touches_the_network():
    assert gp.cached() is None  # nothing read yet
    reach, feat, key = _patched()
    served = _models_response("olano-turbo")
    with reach, feat, key, patch("httpx.get", return_value=served):
        gp.probe()
    with patch("httpx.get") as get:
        assert gp.cached() is not None
        get.assert_not_called()


def test_a_malformed_payload_is_an_error_not_an_empty_gateway():
    reach, feat, key = _patched()
    resp = MagicMock()
    resp.json.return_value = {"not_data": []}
    resp.raise_for_status.return_value = None
    with reach, feat, key, patch("httpx.get", return_value=resp):
        result = gp.probe()

    assert result.ok is False
    assert result.missing == ()


def test_to_dict_is_json_safe():
    reach, feat, key = _patched()
    with reach, feat, key, patch("httpx.get", return_value=_models_response("olano-turbo")):
        payload = gp.probe().to_dict()

    import json

    assert json.loads(json.dumps(payload))["served"] == ["olano-turbo"]
    assert isinstance(payload["checked_at"], str)


# ── known_missing: the one reading that steers a tool ────────────────────────
# Everything else here is diagnostics. This is the single consumer allowed to
# skip a rung, so its guards are the interesting part: it may only ever suppress
# a call the gateway would certainly reject, and it must forget on its own.


def _probe_once(*served: str):
    """Run one real probe against a fake model list and return the reading."""
    reach, feat, key = _patched()
    with reach, feat, key, patch("httpx.get", return_value=_models_response(*served)):
        return gp.probe()


def test_known_missing_is_true_only_for_a_group_the_gateway_does_not_serve():
    result = _probe_once("olano-turbo")
    assert "olano-vision" in result.missing
    assert gp.known_missing("olano-vision") is True
    assert gp.known_missing("olano-turbo") is False


def test_known_missing_is_false_with_no_reading_at_all():
    # A box that has never probed must behave exactly as it did before this
    # existed: try the call.
    assert gp.cached() is None
    assert gp.known_missing("olano-vision") is False


def test_a_failed_probe_never_suppresses_a_rung():
    # An unreachable gateway tells you nothing about what it routes; reading its
    # empty `missing` as fact would take vision away for a network blip.
    reach, feat, key = _patched()
    with reach, feat, key, patch("httpx.get", side_effect=RuntimeError("connection refused")):
        result = gp.probe()
    assert result.ok is False
    assert gp.known_missing("olano-vision") is False


def test_a_stale_reading_is_ignored_so_a_published_group_heals_itself():
    # `cached()` deliberately ignores TTL, so without its own age check this
    # would keep a since-published group switched off until a restart.
    _probe_once("olano-turbo")
    assert gp.known_missing("olano-vision") is True
    assert gp.known_missing("olano-vision", max_age=0.0) is False


def test_known_missing_never_touches_the_network():
    # It runs on tool paths, which must not wait on I/O.
    _probe_once("olano-turbo")
    with patch("httpx.get") as get:
        assert gp.known_missing("olano-vision") is True
        get.assert_not_called()
