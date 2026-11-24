"""A config edit must not rewrite the parts nobody asked to change.

Two failure modes, both silent, both reachable from an ordinary dashboard save
and (since the MCP OAuth back-fill) from an end user completing a sign-in:

* ``load_agent_config`` interpolates ``${VAR}`` before validation, so a plain
  load -> mutate -> save froze the *resolved* value into the file. The config
  stopped referencing the variable, rotating it silently stopped taking effect,
  and the live value landed in a file that is read, copied and committed to
  revision history.
* The read happened outside the deployment write lock while only the write took
  it, so a concurrent edit landing in that window was clobbered wholesale — the
  save rewrites the whole file from a model that never saw the other change.
"""

from __future__ import annotations

import json
import threading

import pytest

from olano import admin
from olano.config.loader import (
    load_agent_config,
    mutate_agent_config,
    save_agent_config,
)

# ── ${VAR} placeholders survive a round trip ─────────────────────────────────


def _raw(name: str) -> dict:
    from olano.config.home import get_olano_paths

    path = get_olano_paths().agents_dir / name / "agent.json5"
    return json.loads(path.read_text(encoding="utf-8"))


def test_env_placeholders_survive_a_load_mutate_save(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_BASE_URL", "https://live.acme.test")
    monkeypatch.setenv("ACME_TENANT", "tenant-42")

    admin.create_agent("roundtrip", description="placeholder agent")
    admin.update_agent(
        "roundtrip",
        {
            "env": {"API_BASE": "${ACME_BASE_URL}", "PLAIN": "literal"},
        },
    )
    admin.add_mcp_server(
        "roundtrip",
        "https://mcp.acme.test/mcp",
        transport="http",
        headers={"X-Tenant": "${ACME_TENANT}"},
    )

    # A completely unrelated later edit must not bake either value in.
    admin.update_agent("roundtrip", {"description": "touched"})

    raw = _raw("roundtrip")
    assert raw["env"]["API_BASE"] == "${ACME_BASE_URL}"
    assert raw["env"]["PLAIN"] == "literal"
    assert raw["mcp_servers"][0]["headers"]["X-Tenant"] == "${ACME_TENANT}"
    assert raw["description"] == "touched"

    # And the loaded view still resolves them, so behaviour is unchanged.
    cfg = load_agent_config("roundtrip", source="home")
    assert cfg.env["API_BASE"] == "https://live.acme.test"


def test_rotating_the_variable_still_takes_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the file keeps referencing the variable, so a new value
    applies on the next load instead of being frozen at whatever it was when
    some unrelated edit happened to run."""
    monkeypatch.setenv("ROTATING", "first")
    admin.create_agent("rotator", description="x")
    admin.update_agent("rotator", {"env": {"TOKEN": "${ROTATING}"}})
    admin.update_agent("rotator", {"description": "unrelated edit"})

    monkeypatch.setenv("ROTATING", "second")
    assert load_agent_config("rotator", source="home").env["TOKEN"] == "second"


def test_a_deliberate_change_to_a_placeholder_field_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoration is not "always keep the old string" — a caller that actually
    changed the value must keep its new one."""
    monkeypatch.setenv("OLD_VAR", "old-value")
    admin.create_agent("deliberate", description="x")
    admin.update_agent("deliberate", {"env": {"TOKEN": "${OLD_VAR}"}})

    admin.update_agent("deliberate", {"env": {"TOKEN": "a-new-literal"}})
    assert _raw("deliberate")["env"]["TOKEN"] == "a-new-literal"

    admin.update_agent("deliberate", {"env": {"TOKEN": "${OTHER_VAR}"}})
    assert _raw("deliberate")["env"]["TOKEN"] == "${OTHER_VAR}"


def test_an_unset_variable_round_trips_as_the_literal() -> None:
    """Interpolation leaves an unset ${VAR} alone, so the restore must be a
    no-op rather than mangling it."""
    admin.create_agent("unset", description="x")
    admin.update_agent("unset", {"env": {"TOKEN": "${NEVER_SET_ANYWHERE}"}})
    admin.update_agent("unset", {"description": "again"})
    assert _raw("unset")["env"]["TOKEN"] == "${NEVER_SET_ANYWHERE}"


def test_placeholders_survive_in_a_list_that_grew(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp_servers is a list; adding a second server must not resolve the
    first one's header. Alignment is by identity key, not position."""
    monkeypatch.setenv("KEY_A", "secret-a")
    admin.create_agent("listgrow", description="x")
    admin.add_mcp_server(
        "listgrow", "https://a.test/mcp", transport="http", headers={"K": "${KEY_A}"}
    )
    # Insert a server that sorts before the first one to also shift positions.
    admin.add_mcp_server("listgrow", "https://b.test/mcp", transport="http")

    servers = {s["url"]: s for s in _raw("listgrow")["mcp_servers"]}
    assert servers["https://a.test/mcp"]["headers"]["K"] == "${KEY_A}"


def test_vault_refs_were_already_safe_and_stay_safe() -> None:
    admin.create_agent("vaultref", description="x")
    admin.add_mcp_server(
        "vaultref",
        "https://v.test/mcp",
        transport="http",
        headers={"Authorization": "Bearer ${vault:SOME_KEY}"},
    )
    admin.update_agent("vaultref", {"description": "touched"})
    raw = _raw("vaultref")
    assert raw["mcp_servers"][0]["headers"]["Authorization"] == "Bearer ${vault:SOME_KEY}"


# ── the read-modify-write is atomic ──────────────────────────────────────────


def test_concurrent_mutations_do_not_clobber_each_other() -> None:
    """Two mutations racing on one agent: both must survive. Before the fix
    each loaded outside the lock, so the slower writer rewrote the whole file
    from a model that predated the other's change."""
    admin.create_agent("racer", description="x")

    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def add(url: str) -> None:
        try:
            start.wait(timeout=10)
            admin.add_mcp_server("racer", url, transport="http")
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=add, args=("https://one.test/mcp",)),
        threading.Thread(target=add, args=("https://two.test/mcp",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    urls = {s["url"] for s in admin.list_mcp_servers("racer")}
    assert urls == {"https://one.test/mcp", "https://two.test/mcp"}


def test_mutate_agent_config_returns_the_saved_config() -> None:
    admin.create_agent("mutator", description="before")

    def _apply(cfg) -> None:
        cfg.description = "after"

    saved = mutate_agent_config("mutator", _apply)
    assert saved.description == "after"
    assert load_agent_config("mutator", source="home").description == "after"


def test_an_exception_in_apply_writes_nothing() -> None:
    admin.create_agent("failer", description="untouched")

    def _apply(cfg) -> None:
        cfg.description = "half-written"
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        mutate_agent_config("failer", _apply)
    assert load_agent_config("failer", source="home").description == "untouched"


def test_the_deployment_lock_is_reentrant() -> None:
    """mutate_agent_config holds the lock across save_agent_config, which takes
    it again. flock is per open-file-description, so a non-reentrant nesting
    would spin until the 10s timeout instead of proceeding."""
    from olano.deployment.locks import deployment_write_lock

    with deployment_write_lock(timeout=5.0):
        with deployment_write_lock(timeout=5.0):
            pass
        # still held and usable after the inner block exits
        admin.create_agent("nested", description="x")
    assert load_agent_config("nested", source="home").description == "x"


def test_save_still_enforces_deployment_limits_under_the_nested_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring placeholders happens before the limit decision, so enforcement
    still sees exactly what will be written."""
    from olano.deployment import enforcement

    admin.create_agent("limited", description="x")
    cfg = load_agent_config("limited", source="home")

    denied = enforcement.Decision(
        allowed=False, code="limit_exceeded", message="cap reached", limit=1, current=1
    )
    monkeypatch.setattr(enforcement, "validate_config_write", lambda *a, **k: denied)
    with pytest.raises(enforcement.DeploymentLimitExceeded):
        save_agent_config(cfg)
