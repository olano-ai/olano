"""Coverage for the cloud catalog package (olano/catalog).

Four layers:

1. :mod:`olano.catalog.store` — the on-disk payload/state cache.
2. :mod:`olano.catalog.models_registry` — read-path ordering
   (in-process → disk → bundled), cloud refresh, and the index-backed lookups.
3. :mod:`olano.catalog.sync` — manifest diffing: unchanged catalogs are
   skipped, moved revisions re-fetch, missing manifest degrades to TTL.
3b. :mod:`olano.catalog.credits_registry` — the olano-credit rate: same
   read-path ordering, plus the USD→credits conversion the dashboard renders
   with and the settings switch that decides whether it is used.
4. The consumers: catalog-first pricing (threadcost/cost.py) and per-model
   context windows (runtime/context_report.py).

Network is always mocked (`_fetch_from_cloud` / `fetch_manifest` are
monkeypatched) so tests are deterministic and never touch api.olano.ai.
The autouse ``lionclaw_home`` fixture (conftest) isolates the disk cache.
"""

from __future__ import annotations

import time

import pytest

from olano.catalog import connectors_registry, credits_registry, models_registry, store, sync
from olano.core import mcp_registry


@pytest.fixture(autouse=True)
def _clear_catalog_caches(monkeypatch):
    """Catalog results are module-cached; clear them (and the inline-fetch
    throttles) around every test."""

    def reset() -> None:
        models_registry.invalidate_cache()
        credits_registry.invalidate_cache()
        connectors_registry.invalidate_cache()
        credits_registry._last_attempt = credits_registry.NEVER_ATTEMPTED
        # Reset to the modules' own "never attempted" sentinel, not 0.0: the
        # backoff compares against time.monotonic() (time since boot), so 0.0
        # reads as "attempted at boot" and silently throttles the first fetch
        # on a machine with low uptime — which is every fresh CI runner.
        models_registry._last_attempt = models_registry.NEVER_ATTEMPTED
        mcp_registry._cache.clear()
        mcp_registry._last_attempt = mcp_registry.NEVER_ATTEMPTED

    reset()
    # Default every test to "the cloud is unreachable" for the credit rate, so
    # a test that only exercises the model catalog can never reach the network
    # through the sync service's credits handler. Tests that care override it.
    monkeypatch.setattr(credits_registry, "_fetch_from_cloud", lambda: None)
    yield
    reset()


def _payload(revision: int = 7, price_input: float = 3.0) -> dict:
    return {
        "revision": revision,
        "providers": [{"key": "anthropic", "name": "Anthropic", "envVar": "ANTHROPIC_API_KEY"}],
        "models": [
            {
                "id": "anthropic:claude-sonnet-4-6",
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "modality": "chat",
                "vision": True,
                "reasoning": True,
                "contextWindow": 1_000_000,
                "priceInput": price_input,
                "priceOutput": 15.0,
                "priceCacheRead": 0.3,
                "tier": "flagship",
                "recommended": True,
            }
        ],
    }


# ── store ────────────────────────────────────────────────────────────────────


def test_store_roundtrip_and_state() -> None:
    store.write_payload("models", _payload(), 7)
    assert store.read_payload("models")["revision"] == 7
    entry = store.state_entry("models")
    assert entry["revision"] == 7
    assert entry["fetched_at"] > 0
    assert store.payload_age_seconds("models") < 60


def test_store_corrupt_payload_is_none() -> None:
    directory = store.catalog_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "models.json").write_text("{not json", encoding="utf-8")
    (directory / "state.json").write_text("also broken", encoding="utf-8")
    assert store.read_payload("models") is None
    assert store.read_state() == store.default_state()


# ── models_registry read path ────────────────────────────────────────────────


def test_bundled_fallback_when_no_disk_no_cloud(monkeypatch) -> None:
    monkeypatch.setattr(models_registry, "_fetch_from_cloud", lambda: None)
    catalog = models_registry.get_model_catalog()
    assert catalog["source"] == "bundled"
    assert catalog["revision"] == 0
    assert len(catalog["providers"]) >= 20
    assert any(m["id"] == "anthropic:claude-opus-4-6" for m in catalog["models"])


def test_disk_beats_bundled_and_no_network(monkeypatch) -> None:
    store.write_payload("models", _payload(), 7)

    def boom() -> None:
        raise AssertionError("browse must not hit the network when disk is fresh")

    monkeypatch.setattr(models_registry, "_fetch_from_cloud", boom)
    catalog = models_registry.get_model_catalog()
    assert catalog["source"] == "disk"
    assert catalog["revision"] == 7
    # Second read serves the in-process cache.
    assert models_registry.get_model_catalog()["source"] == "cache"


def test_force_refresh_fetches_and_persists(monkeypatch) -> None:
    monkeypatch.setattr(models_registry, "_fetch_from_cloud", lambda: _payload(revision=9))
    catalog = models_registry.get_model_catalog(force=True)
    assert catalog["source"] == "cloud"
    assert store.state_entry("models")["revision"] == 9


def test_force_refresh_failure_falls_back_to_disk(monkeypatch) -> None:
    store.write_payload("models", _payload(revision=7), 7)
    monkeypatch.setattr(models_registry, "_fetch_from_cloud", lambda: None)
    catalog = models_registry.get_model_catalog(force=True)
    assert catalog["source"] == "disk"
    assert catalog["revision"] == 7


def test_stale_disk_self_heals(monkeypatch) -> None:
    store.write_payload("models", _payload(revision=7), 7)
    # Age the fetch far past the self-heal threshold.
    state = store.read_state()
    state["catalogs"]["models"]["fetched_at"] = int(
        time.time() - models_registry.DISK_REFRESH_SECONDS - 60
    )
    store.write_state(state)
    monkeypatch.setattr(models_registry, "_fetch_from_cloud", lambda: _payload(revision=8))
    assert models_registry.get_model_catalog()["revision"] == 8


def test_inline_backoff_allows_the_first_attempt_on_a_freshly_booted_machine(
    monkeypatch,
) -> None:
    """The backoff must never throttle the FIRST cloud attempt.

    ``time.monotonic()`` counts from boot, so a machine with little uptime
    reports a small value. The sentinel for "never attempted" therefore cannot
    be 0.0 — ``now - 0.0 < _ATTEMPT_BACKOFF_SECONDS`` is true for the first ten
    minutes of uptime, which silently suppressed every inline self-heal on
    freshly booted boxes and made this file fail on fresh CI runners.

    Pinned to 5s of uptime so this is deterministic rather than a property of
    whatever machine happens to run it.
    """
    monkeypatch.setattr(time, "monotonic", lambda: 5.0)
    # Both registries carry their own copy of this throttle, so both are pinned
    # here — mcp_registry's copy is otherwise never exercised directly.
    for module in (models_registry, mcp_registry):
        monkeypatch.setattr(module, "_last_attempt", module.NEVER_ATTEMPTED)
        assert module._may_attempt_cloud() is True, module.__name__
        # The backoff still applies once an attempt has actually been recorded.
        assert module._may_attempt_cloud() is False, module.__name__


# ── lookups ──────────────────────────────────────────────────────────────────


def test_model_entry_exact_and_bare(monkeypatch) -> None:
    store.write_payload("models", _payload(), 7)
    assert models_registry.model_entry("anthropic:claude-sonnet-4-6")["tier"] == "flagship"
    assert models_registry.model_entry("claude-sonnet-4-6")["vision"] is True
    assert models_registry.model_entry("nope:missing") is None
    assert models_registry.model_entry(None) is None


def test_model_pricing_and_context(monkeypatch) -> None:
    store.write_payload("models", _payload(), 7)
    pricing = models_registry.model_pricing("anthropic:claude-sonnet-4-6")
    assert pricing == {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": None}
    assert models_registry.model_context_window("anthropic:claude-sonnet-4-6") == 1_000_000
    assert models_registry.model_pricing("nope:missing") is None


# ── sync ─────────────────────────────────────────────────────────────────────


def _manifest(models_rev: int = 7, mcp_rev: int = 3) -> dict:
    return {
        "models": {"revision": models_rev, "updatedAt": "2026-07-27T00:00:00Z"},
        "mcp": {"revision": mcp_rev, "updatedAt": "2026-07-27T00:00:00Z"},
    }


def test_sync_fetches_only_changed_catalogs(monkeypatch) -> None:
    # Local state: models at rev 7 (fresh), mcp never fetched.
    store.write_payload("models", _payload(), 7)
    monkeypatch.setattr(sync, "fetch_manifest", lambda: _manifest(models_rev=7, mcp_rev=3))
    fetched: list[str] = []

    def fake_models():
        fetched.append("models")
        return models_registry.refresh_from_cloud()

    def fake_mcp():
        fetched.append("mcp")
        servers = [{"slug": "acme", "name": "Acme", "kind": "preset"}]
        store.write_payload("mcp", {"servers": servers}, None)
        return {"presets": servers, "examples": [], "categories": []}

    monkeypatch.setitem(sync._HANDLERS, "models", fake_models)
    monkeypatch.setitem(sync._HANDLERS, "mcp", fake_mcp)
    events: list[tuple[str, dict]] = []
    report = sync.sync_once(push_event=lambda t, d: events.append((t, d)))

    assert fetched == ["mcp"]  # models unchanged at rev 7 → skipped
    assert report["catalogs"]["models"]["status"] == "unchanged"
    assert report["catalogs"]["mcp"]["status"] == "updated"
    assert events == [("catalog_updated", {"catalog": "mcp", "revision": None})]


def test_sync_refetches_on_revision_change(monkeypatch) -> None:
    store.write_payload("models", _payload(revision=7), 7)
    monkeypatch.setattr(sync, "fetch_manifest", lambda: _manifest(models_rev=8))
    monkeypatch.setattr(models_registry, "_fetch_from_cloud", lambda: _payload(revision=8))
    monkeypatch.setitem(sync._HANDLERS, "mcp", lambda: None)  # isolate models
    report = sync.sync_once()
    assert report["catalogs"]["models"]["status"] == "updated"
    assert store.state_entry("models")["revision"] == 8


def test_sync_without_manifest_uses_ttl(monkeypatch) -> None:
    monkeypatch.setattr(sync, "fetch_manifest", lambda: None)
    store.write_payload("models", _payload(), 7)  # fresh → within fallback TTL
    fetched: list[str] = []
    monkeypatch.setitem(sync._HANDLERS, "models", lambda: fetched.append("models") or True)
    monkeypatch.setitem(sync._HANDLERS, "mcp", lambda: fetched.append("mcp") or True)
    report = sync.sync_once()
    assert report["manifest"] is False
    # models fresh → skipped; mcp never fetched → refreshed.
    assert fetched == ["mcp"]
    assert report["catalogs"]["models"]["status"] == "unchanged"


def test_sync_handler_error_does_not_break_others(monkeypatch) -> None:
    monkeypatch.setattr(sync, "fetch_manifest", lambda: _manifest(models_rev=99, mcp_rev=99))

    def broken():
        raise RuntimeError("boom")

    ok: list[str] = []
    monkeypatch.setitem(sync._HANDLERS, "models", broken)
    monkeypatch.setitem(sync._HANDLERS, "mcp", lambda: ok.append("mcp") or True)
    report = sync.sync_once()
    assert report["catalogs"]["models"]["status"] == "error"
    assert ok == ["mcp"]


# ── credits_registry (the olano-credit rate) ─────────────────────────────────


def _rate(revision: int = 4, credits_per_usd: float = 100.0) -> dict:
    return {
        "revision": revision,
        "creditsPerUsd": credits_per_usd,
        "unitName": "credit",
        "unitNamePlural": "credits",
        "decimals": 0,
        "updatedAt": "2026-08-01T00:00:00Z",
    }


def test_credit_rate_bundled_fallback_is_the_launch_rate() -> None:
    """The one test that pins the denomination itself: $1 -> 1,000 credits.

    Every other rate assertion reads BUNDLED_RATE, so a future redenomination
    is a one-line change here and nowhere else.
    """
    rate = credits_registry.get_credit_rate()
    assert rate["source"] == "bundled"
    assert rate["creditsPerUsd"] == 1000.0
    assert credits_registry.credits_for_usd(1.0) == 1000.0


def test_credit_rate_disk_beats_bundled_and_no_network(monkeypatch) -> None:
    store.write_payload("credits", _rate(revision=4, credits_per_usd=175.0), 4)

    def boom() -> None:
        raise AssertionError("a render must not hit the network when disk is fresh")

    monkeypatch.setattr(credits_registry, "_fetch_from_cloud", boom)
    rate = credits_registry.get_credit_rate()
    assert rate["source"] == "disk"
    assert rate["creditsPerUsd"] == 175.0
    # Second read serves the in-process cache and still reports the origin
    # ("disk"), not the cache tier — see the dedicated test below.
    assert credits_registry.get_credit_rate()["source"] == "disk"
    assert credits_registry.credits_for_usd(0.42) == pytest.approx(73.5)


def test_credit_rate_cache_hit_reports_its_origin_not_the_word_cache(monkeypatch) -> None:
    """A cache hit must say where the rate came from, not that it was cached:
    the Config screen keys "Synced from cloud" vs "bundled launch rate" off
    this field, and a literal "cache" made a bundled rate read as synced."""
    monkeypatch.setattr(credits_registry, "_fetch_from_cloud", lambda: None)
    assert credits_registry.get_credit_rate()["source"] == "bundled"
    # Second read is served from the in-process cache — still "bundled".
    assert credits_registry.get_credit_rate()["source"] == "bundled"

    store.write_payload("credits", _rate(revision=4), 4)
    credits_registry.invalidate_cache()
    assert credits_registry.get_credit_rate()["source"] == "disk"
    assert credits_registry.get_credit_rate()["source"] == "disk"


def test_credit_rate_rejects_nonsense_rates(monkeypatch) -> None:
    """A zero/negative/missing rate must fall through to the bundled launch
    rate, never render every cost as 0 credits."""
    for bad in ({"creditsPerUsd": 0}, {"creditsPerUsd": -5}, {}, {"creditsPerUsd": "free"}):
        assert credits_registry._normalize_payload(bad) is None
    store.write_payload("credits", {"creditsPerUsd": 0}, 1)
    assert (
        credits_registry.get_credit_rate()["creditsPerUsd"]
        == credits_registry.BUNDLED_RATE["creditsPerUsd"]
    )


def test_credit_rate_force_refresh_persists(monkeypatch) -> None:
    monkeypatch.setattr(credits_registry, "_fetch_from_cloud", lambda: _rate(revision=9))
    rate = credits_registry.get_credit_rate(force=True)
    assert rate["source"] == "cloud"
    assert store.state_entry("credits")["revision"] == 9


def test_sync_refetches_credits_on_revision_change(monkeypatch) -> None:
    store.write_payload("credits", _rate(revision=4), 4)
    monkeypatch.setattr(
        sync,
        "fetch_manifest",
        lambda: {**_manifest(), "credits": {"revision": 5, "updatedAt": "2026-08-01T00:00:00Z"}},
    )
    monkeypatch.setattr(
        credits_registry, "_fetch_from_cloud", lambda: _rate(revision=5, credits_per_usd=175.0)
    )
    monkeypatch.setitem(sync._HANDLERS, "models", lambda: None)
    monkeypatch.setitem(sync._HANDLERS, "mcp", lambda: None)
    report = sync.sync_once()
    assert report["catalogs"]["credits"]["status"] == "updated"
    assert store.state_entry("credits")["revision"] == 5


def test_cost_display_mode_defaults_to_usd_and_switches(tmp_path, monkeypatch) -> None:
    from olano.config import system_settings

    monkeypatch.delenv("LIONCLAW_COST_DISPLAY", raising=False)
    assert system_settings.cost_display_mode() == "usd"
    system_settings.set_cost_display_mode("credits")
    assert system_settings.cost_display_mode() == "credits"
    # An unknown value in the file degrades to dollars rather than erroring.
    assert system_settings.CreditsSettings.model_validate({"display": "pesos"}).display == "usd"
    with pytest.raises(ValueError):
        system_settings.set_cost_display_mode("pesos")
    # The env override wins over the stored preference.
    monkeypatch.setenv("LIONCLAW_COST_DISPLAY", "usd")
    assert system_settings.cost_display_mode() == "usd"


# ── consumers ────────────────────────────────────────────────────────────────


def test_cost_uses_catalog_pricing() -> None:
    from olano.analytics.threadcost.cost import cost_for_call, pricing_known

    store.write_payload("models", _payload(price_input=3.0), 7)
    # 1M fresh input + 1M output at catalog rates ($3 + $15), no cache reads.
    assert cost_for_call("anthropic:claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)
    # Cache reads bill at the catalog's real cache-read rate ($0.30/1M).
    assert cost_for_call(
        "anthropic:claude-sonnet-4-6", 1_000_000, 0, cache_read_tokens=1_000_000
    ) == pytest.approx(0.30)
    assert pricing_known("anthropic:claude-sonnet-4-6") is True


def test_cost_falls_back_to_legacy_table(monkeypatch) -> None:
    from olano.analytics.threadcost.cost import cost_for_call, pricing_known

    monkeypatch.setattr(models_registry, "model_pricing", lambda *_: None)
    # Legacy substring table: claude-sonnet → (3, 15) per 1M.
    assert cost_for_call("anthropic:claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)
    assert pricing_known("anthropic:claude-sonnet-4-6") is True
    assert pricing_known("totally-unknown-model-xyz") is False


def test_context_report_uses_catalog(monkeypatch) -> None:
    from olano.runtime.context_report import model_context_window

    store.write_payload("models", _payload(), 7)
    # Catalog gives the real 1M window (the old anthropic ladder said 200K).
    assert model_context_window("anthropic:claude-sonnet-4-6") == 1_000_000
    # Unknown model → provider-prefix ladder fallback.
    assert model_context_window("anthropic:claude-nonexistent") == 200_000
    assert model_context_window(None) is None


# ── connectors_registry (official-connector clients) ─────────────────────────


def _connectors_payload(revision: int = 3) -> dict:
    return {
        "revision": revision,
        "connectors": {
            "microsoft365": {"client_id": "off-ms", "enabled": True, "name": "Microsoft 365"},
            "salesforce": {"client_id": "off-sf", "enabled": True, "name": "Salesforce"},
        },
    }


def test_connectors_normalize_drops_disabled_and_bad_charset() -> None:
    payload = connectors_registry._normalize_payload(
        {
            "revision": 5,
            "connectors": {
                "good": {"client_id": "abc-123", "enabled": True, "name": "Good"},
                "disabled": {"client_id": "xyz", "enabled": False},
                "empty": {"client_id": "", "enabled": True},
                # A poisoned catalog can't push a client id that could rewrite
                # the authorize URL it gets interpolated into.
                "hostile": {"client_id": "a?b#c", "enabled": True},
            },
        }
    )
    assert payload is not None
    assert set(payload["connectors"]) == {"good"}
    assert payload["revision"] == 5
    assert connectors_registry._normalize_payload({"nope": 1}) is None


def test_connectors_read_path_disk_then_bundled_no_network(monkeypatch) -> None:
    # Nothing anywhere → bundled-empty: no official clients, never a fetch.
    monkeypatch.setattr(
        connectors_registry,
        "_fetch_from_cloud",
        lambda: (_ for _ in ()).throw(AssertionError("read path must not fetch")),
    )
    assert connectors_registry.official_client("microsoft365") is None
    assert connectors_registry.official_ids() == frozenset()

    # A synced disk copy answers (still no network involved).
    store.write_payload(connectors_registry.CATALOG_NAME, _connectors_payload(), 3)
    connectors_registry.invalidate_cache()
    assert connectors_registry.official_client("microsoft365") == "off-ms"
    assert connectors_registry.official_ids() == frozenset({"microsoft365", "salesforce"})
    assert connectors_registry.official_client("meta") is None

    status = connectors_registry.catalog_status()
    assert status["official_count"] == 2
    assert status["revision"] == 3


def test_connectors_refresh_persists_and_failure_leaves_disk_alone(monkeypatch) -> None:
    monkeypatch.setattr(connectors_registry, "_fetch_from_cloud", lambda: _connectors_payload(9))
    assert connectors_registry.refresh_from_cloud() is not None
    assert store.read_payload(connectors_registry.CATALOG_NAME)["revision"] == 9

    # A failed fetch leaves the existing copy alone (never wiped to empty).
    monkeypatch.setattr(connectors_registry, "_fetch_from_cloud", lambda: None)
    assert connectors_registry.refresh_from_cloud() is None
    assert store.read_payload(connectors_registry.CATALOG_NAME)["revision"] == 9
    connectors_registry.invalidate_cache()
    assert connectors_registry.official_client("microsoft365") == "off-ms"


def test_sync_handles_the_connectors_catalog(monkeypatch) -> None:
    """The sync service refreshes `connectors` off the manifest revision."""
    fetched: list[str] = []
    for name in ("models", "mcp", "credits", "agents"):
        monkeypatch.setitem(sync._HANDLERS, name, lambda: True)
    monkeypatch.setitem(
        sync._HANDLERS,
        "connectors",
        lambda: fetched.append("connectors")
        or connectors_registry._normalize_payload(_connectors_payload(4)),
    )
    monkeypatch.setattr(
        sync,
        "fetch_manifest",
        lambda: {name: {"revision": 4} for name in sync._HANDLERS},
    )
    report = sync.sync_once()
    assert fetched == ["connectors"]
    assert report["catalogs"]["connectors"]["status"] == "updated"


# ── connectors freshness kick ────────────────────────────────────────────────

# The real function, captured at import time: the hermetic conftest fixture
# pins the module attr to a no-op for every test, and these are the tests of
# the real thing. The function's own globals (store, refresh_from_cloud, the
# throttle) still resolve live from the module, so per-test patches apply.
_REAL_KICK = connectors_registry.kick_refresh_if_stale


@pytest.fixture(autouse=True)
def _reset_kick_throttle(monkeypatch):
    monkeypatch.setattr(connectors_registry, "_last_kick", float("-inf"))


def test_connectors_kick_spawns_once_then_throttles(monkeypatch) -> None:
    import threading

    ran = threading.Event()
    monkeypatch.setattr(connectors_registry, "refresh_from_cloud", lambda: ran.set())

    # Fresh tmp home → no disk payload → stale → one background refresh.
    assert _REAL_KICK() is True
    assert ran.wait(timeout=5.0), "kick thread never ran refresh_from_cloud"
    # Immediately after: the monotonic throttle suppresses a second fetch even
    # though the disk copy is still missing (the first one may have failed).
    assert _REAL_KICK() is False


def test_connectors_kick_noop_when_disk_is_fresh(monkeypatch) -> None:
    store.write_payload(connectors_registry.CATALOG_NAME, _connectors_payload(), 3)

    def boom() -> None:
        raise AssertionError("fresh disk must not trigger a fetch")

    monkeypatch.setattr(connectors_registry, "refresh_from_cloud", boom)
    assert _REAL_KICK() is False
    # A tiny max age flips the same disk copy to stale.
    ran = []
    monkeypatch.setattr(connectors_registry, "refresh_from_cloud", lambda: ran.append(1))
    assert _REAL_KICK(max_age_seconds=0.0) is True


def test_connectors_kick_never_raises(monkeypatch) -> None:
    # The caller is /api/auth/status on the event loop — any internal failure
    # must degrade to "no kick", never propagate.
    def boom(name: str) -> float:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(store, "payload_age_seconds", boom)
    assert _REAL_KICK() is False
