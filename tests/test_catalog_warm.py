"""The boot-time catalog warm thread pre-pays the reasoning probes.

`GET /api/catalog/providers` (full) instantiates a langchain chat class per
model name via ``models/reasoning.py:_probe`` — seconds on a cold process.
``RuntimeServer._warm_catalog_caches`` pays that once at boot, off the request
path, so the first Chat/Providers/agent-edit view after a restart renders its
model pickers immediately. This pins the contract: after the warm runs, every
model name the payload will probe is already in the lru cache.
"""

from __future__ import annotations

from olano.runtime.server import RuntimeServer


async def test_warm_populates_reasoning_probe_cache(monkeypatch):
    from olano.models import PROVIDERS
    from olano.models.reasoning import _profile_probe

    probed: list[str] = []

    def _spy(model_str: str):
        probed.append(model_str)
        return None

    # Intercept at the reasoning_effort_info level (what both the warm thread
    # and the payload call) so the test doesn't build real chat classes.
    from olano.models import reasoning

    monkeypatch.setattr(reasoning, "reasoning_effort_info", _spy)
    import olano.runtime.server as server_mod

    # The warm function imports reasoning_effort_info by name — patch the
    # source module attr before the warm runs (the import inside _warm
    # re-resolves it from the module each call).
    server = RuntimeServer(agent_names=[])
    await server._warm_catalog_caches()

    assert probed, "warm thread must probe at least the static provider defaults"
    expected = {f"{key}:{m}" for key, info in PROVIDERS.items() for m in info.default_models}
    missing = expected - set(probed)
    assert not missing, f"provider default models not warmed: {sorted(missing)[:5]}"
    assert server_mod is not None

    # And the real (unpatched) package-profile half of the probe stays
    # importable/cacheable (the catalog half is an uncached dict hit).
    assert hasattr(_profile_probe, "cache_info")
