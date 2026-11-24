"""The ``sandbox:`` config block and its dashboard seam.

Deployment-wide ceilings for the Docker sandbox each agent's shell runs in.
Two contracts are pinned here, and they deliberately differ:

* the **loader** coerces silently (a hand-edited config.yaml must never take a
  box offline — a bad value falls back to automatic sizing), and
* the **PUT** rejects loudly (a dashboard save must never persist something
  other than what was typed).
"""

from __future__ import annotations

import pytest

from olano.config.system_settings import (
    SandboxSettings,
    load_sandbox_settings,
    set_sandbox_settings,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    return tmp_path


# ── loader ───────────────────────────────────────────────────────────────────


def test_defaults_are_automatic(home):
    """Unset means "size me from the host", not 512m/0.5 — that pair is only
    the floor, and the historical cap when nothing can be detected."""
    settings = load_sandbox_settings()
    assert settings.memory is None
    assert settings.cpus is None


def test_missing_config_file_yields_defaults(home):
    assert not (home / "config.yaml").exists()
    assert load_sandbox_settings() == SandboxSettings()


def test_values_are_read_and_normalized(home):
    (home / "config.yaml").write_text("sandbox:\n  memory: 4G\n  cpus: 2\n")
    settings = load_sandbox_settings()
    assert settings.memory == "4g"
    assert settings.cpus == 2.0


@pytest.mark.parametrize(
    "block",
    [
        "sandbox: [not, a, mapping]\n",
        "sandbox:\n  memory: enormous\n  cpus: plenty\n",
        "sandbox:\n  memory: '5m'\n",  # below docker's 6 MiB floor
    ],
)
def test_unusable_values_fall_back_to_automatic(home, block):
    """Never raise on a hand-edit: an agent with no shell is worse than an agent
    with an automatically-sized one."""
    (home / "config.yaml").write_text(block)
    settings = load_sandbox_settings()
    assert settings.memory is None
    assert settings.cpus is None


def test_out_of_range_cpus_are_clamped(home):
    (home / "config.yaml").write_text("sandbox:\n  cpus: 9999\n")
    assert load_sandbox_settings().cpus == 64.0


# ── setter ───────────────────────────────────────────────────────────────────


def test_setter_merges_partially_and_normalizes(home):
    set_sandbox_settings({"memory": "2G"})
    set_sandbox_settings({"cpus": 3})
    settings = load_sandbox_settings()
    assert settings.memory == "2g", "a later cpus write must not drop memory"
    assert settings.cpus == 3.0


def test_setter_clears_a_field_back_to_automatic(home):
    set_sandbox_settings({"memory": "2g", "cpus": 3})
    set_sandbox_settings({"memory": None})
    settings = load_sandbox_settings()
    assert settings.memory is None
    assert settings.cpus == 3.0


def test_setter_preserves_unrelated_blocks(home):
    (home / "config.yaml").write_text("default_agent: merli\nsandbox_tool_sets: [chinese]\n")
    set_sandbox_settings({"memory": "2g"})
    text = (home / "config.yaml").read_text()
    assert "default_agent: merli" in text
    assert "chinese" in text


def test_setter_ignores_unknown_keys(home):
    set_sandbox_settings({"memory": "2g", "gpus": 4})
    assert "gpus" not in (home / "config.yaml").read_text()


# ── API seam (dashboard Config card) ─────────────────────────────────────────


@pytest.fixture
def api_client(tmp_workspace, tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.admin.memory import MemoryMonitor
    from olano.runtime import disk_monitor as dm
    from olano.runtime.api import create_api

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    rt = MagicMock()
    rt._runners = {}
    rt._channels = []
    rt._memory_monitor = MemoryMonitor()
    rt._disk_monitor = dm.DiskMonitor()
    return TestClient(create_api(rt))


def test_get_reports_stored_values_and_what_they_resolve_to(api_client):
    """The fields are usually empty (automatic), so without the computed halves
    the card would show two blanks and no way to tell what agents actually run
    with."""
    block = api_client.get("/api/system/config").json()["sandbox"]
    assert block["memory"] is None
    assert block["cpus"] is None
    assert set(block["host"]) == {"ncpu", "mem_bytes", "source"}
    assert block["effective"]["memory"]
    assert block["effective"]["cpus"] > 0
    assert block["effective"]["memory_source"] in {"env", "config", "auto", "fallback"}
    assert block["env_overrides"] == {"memory": False, "cpus": False}


def test_get_flags_an_environment_override(api_client, monkeypatch):
    """An env var silently outranking the field on screen is exactly the kind of
    thing the UX rules say must never be invisible."""
    monkeypatch.setenv("LIONCLAW_SANDBOX_MEMORY", "8g")
    block = api_client.get("/api/system/config").json()["sandbox"]
    assert block["env_overrides"]["memory"] is True
    assert block["effective"]["memory"] == "8g"
    assert block["effective"]["memory_source"] == "env"


def test_put_persists_and_merges(api_client):
    resp = api_client.put("/api/system/config", json={"sandbox": {"memory": "4G", "cpus": 2}})
    assert resp.status_code == 200
    block = resp.json()["sandbox"]
    assert block["memory"] == "4g"
    assert block["cpus"] == 2.0
    assert block["effective"] == {
        "memory": "4g",
        "cpus": 2.0,
        "memory_source": "config",
        "cpus_source": "config",
    }

    # A partial update leaves the other field alone.
    block = api_client.put("/api/system/config", json={"sandbox": {"cpus": 1}}).json()["sandbox"]
    assert block["memory"] == "4g"
    assert block["cpus"] == 1.0


def test_put_clears_a_field_back_to_automatic(api_client):
    api_client.put("/api/system/config", json={"sandbox": {"memory": "4g"}})
    block = api_client.put("/api/system/config", json={"sandbox": {"memory": ""}}).json()["sandbox"]
    assert block["memory"] is None
    assert block["effective"]["memory_source"] in {"auto", "fallback"}


@pytest.mark.parametrize(
    "payload",
    [
        {"memory": "enormous"},
        {"memory": "2gb"},
        {"memory": "5m"},  # below docker's own floor
        {"cpus": 0},
        {"cpus": 999},
        {"cpus": "many"},
    ],
)
def test_put_rejects_unusable_values(api_client, payload):
    resp = api_client.put("/api/system/config", json={"sandbox": payload})
    assert resp.status_code == 400
    # The message has to say what a good value looks like — this is the only
    # feedback an operator gets.
    detail = resp.json()["detail"]
    assert "automatic limit" in detail


def test_put_rejects_a_malformed_block(api_client):
    assert api_client.put("/api/system/config", json={"sandbox": ["nope"]}).status_code == 400


def test_a_round_tripped_get_payload_saves_cleanly(api_client):
    """The card sends back what it was given; the read-only halves must be
    dropped rather than 400ing the save."""
    block = api_client.get("/api/system/config").json()["sandbox"]
    assert api_client.put("/api/system/config", json={"sandbox": block}).status_code == 200
