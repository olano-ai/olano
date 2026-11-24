"""Sandboxes go on a network where containers cannot open connections to each other.

A server an agent runs in its sandbox and binds to every interface (Express's
``app.listen(PORT)``, Python's ``http.server``) used to be reachable from every
other agent's sandbox on the shared default bridge, around the page password.
The default lane is now a docker network created with inter-container traffic
off, so docker itself drops those connections, including on boxes where the
runtime is a container and cannot touch the host firewall. These tests pin the
lane decisions against a fake docker CLI; the live proof is
``tests/integration/test_sandbox_isolation_live.py``.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from olano.backends import lionclaw_docker as ld

ISOLATED = ld._ISOLATED_NETWORK
RESCUE = ld._RESCUE_NETWORK
ICC_OFF = f"{ld._ICC_OPTION}=false"


def _proc(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeNetworks:
    """`docker network inspect|create` over an in-memory set of networks."""

    def __init__(self, existing: dict[str, dict[str, str]] | None = None, can_create: bool = True):
        self.networks: dict[str, dict[str, str]] = dict(existing or {})
        self.can_create = can_create
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[:3] == ["docker", "network", "inspect"]:
            name = cmd[-1]
            if name not in self.networks:
                return _proc(returncode=1, stderr="not found")
            if "-f" in cmd:
                return _proc(json.dumps(self.networks[name]))
            return _proc("[{}]")
        if cmd[:3] == ["docker", "network", "create"]:
            if not self.can_create:
                return _proc(returncode=1, stderr="could not find an available address pool")
            options: dict[str, str] = {}
            for i, arg in enumerate(cmd):
                if arg == "--opt":
                    key, _, value = cmd[i + 1].partition("=")
                    options[key] = value
            self.networks[cmd[-1]] = options
            return _proc(cmd[-1])
        raise AssertionError(f"unexpected docker call: {cmd}")

    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["docker", "network", "create"]]


@pytest.fixture
def docker(monkeypatch):
    monkeypatch.setenv(ld.SANDBOX_ISOLATION_ENV, "1")
    monkeypatch.delenv(ld.SANDBOX_NETWORK_ENV, raising=False)
    monkeypatch.setattr(ld, "_RESCUE_NETWORK_READY", False)

    def _install(fake: FakeNetworks) -> FakeNetworks:
        monkeypatch.setattr(ld, "_run", fake)
        return fake

    return _install


def test_default_lane_is_an_isolated_network_created_on_demand(docker, lionclaw_home):
    fake = docker(FakeNetworks())
    assert ld.resolve_bridge_network() == ISOLATED
    (create,) = fake.creates()
    assert ICC_OFF in create and create[-1] == ISOLATED
    assert "olano.network=sandboxes" in create
    assert ld.network_isolated(ISOLATED) is True


def test_an_existing_isolated_network_is_reused(docker, lionclaw_home):
    fake = docker(FakeNetworks({ISOLATED: {ld._ICC_OPTION: "false"}}))
    assert ld.resolve_bridge_network() == ISOLATED
    assert fake.creates() == []


def test_a_same_named_network_that_does_not_isolate_is_refused(docker, lionclaw_home, caplog):
    docker(FakeNetworks({ISOLATED: {}}))
    with caplog.at_level("WARNING"):
        assert ld.resolve_bridge_network() == "bridge"
        assert ld.resolve_bridge_network() == "bridge"
    assert any("does not isolate containers" in r.message for r in caplog.records)
    unavailable = [r for r in caplog.records if "isolation is unavailable" in r.message]
    assert len(unavailable) == 1, "the fallback is announced once, not per sandbox"


def test_a_box_that_cannot_create_networks_falls_back_and_does_not_retry_at_once(
    docker, lionclaw_home
):
    fake = docker(FakeNetworks(can_create=False))
    assert ld.resolve_bridge_network() == "bridge"
    assert ld.resolve_bridge_network() == "bridge"
    assert len(fake.creates()) == 1


def test_isolation_off_keeps_the_shared_bridge_without_asking_docker(
    docker, lionclaw_home, monkeypatch
):
    fake = docker(FakeNetworks())
    monkeypatch.setenv(ld.SANDBOX_ISOLATION_ENV, "0")
    assert ld.resolve_bridge_network() == "bridge"
    assert fake.calls == []


def test_an_operator_pin_beats_isolation(docker, lionclaw_home, monkeypatch):
    fake = docker(FakeNetworks())
    monkeypatch.setenv(ld.SANDBOX_NETWORK_ENV, "bridge")
    assert ld.resolve_bridge_network() == "bridge"
    assert ld._fallback_network_for("bridge") is None
    assert fake.calls == []


@pytest.mark.parametrize(
    ("sticky", "isolation", "expected"),
    [
        ("bridge", "1", ISOLATED),  # an old "back to bridge" marker moves to the isolated lane
        (ISOLATED, "1", ISOLATED),
        (ISOLATED, "0", "bridge"),  # turning isolation off is honoured
        (RESCUE, "1", RESCUE),  # an escalated box keeps its rescue lane
    ],
)
def test_remembered_lanes(docker, lionclaw_home, monkeypatch, sticky, isolation, expected):
    docker(FakeNetworks({RESCUE: {}}))
    monkeypatch.setenv(ld.SANDBOX_ISOLATION_ENV, isolation)
    ld._remember_sticky_network(sticky)
    assert ld.resolve_bridge_network() == expected


def test_escalation_moves_between_isolated_lanes(docker, lionclaw_home):
    fake = docker(FakeNetworks())
    assert ld._fallback_network_for(ISOLATED) == RESCUE
    rescue_create = next(c for c in fake.creates() if c[-1] == RESCUE)
    assert ICC_OFF in rescue_create, "a repair must not cost the isolation"
    assert ld._fallback_network_for(RESCUE) == ISOLATED
    assert ld._fallback_network_for("bridge") == RESCUE


def test_rescue_network_is_shared_when_isolation_is_off(docker, lionclaw_home, monkeypatch):
    fake = docker(FakeNetworks())
    monkeypatch.setenv(ld.SANDBOX_ISOLATION_ENV, "0")
    assert ld._fallback_network_for("bridge") == RESCUE
    assert ICC_OFF not in fake.creates()[0]
    assert ld._fallback_network_for(RESCUE) == "bridge"


def test_network_isolated_reads_the_bridge_option(docker):
    docker(FakeNetworks({"a": {ld._ICC_OPTION: "false"}, "b": {ld._ICC_OPTION: "true"}, "c": {}}))
    assert ld.network_isolated("a") is True
    assert ld.network_isolated("b") is False
    assert ld.network_isolated("c") is False
    assert ld.network_isolated("missing") is False
    for name in ("bridge", "host", "none"):
        assert ld.network_isolated(name) is False


def test_backend_reports_isolation(docker, lionclaw_home):
    from olano.backends.lionclaw_docker import OlanoDockerBackend
    from olano.config.backend_config import OlanoDockerBackendConfig

    docker(FakeNetworks())
    backend = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True, network="bridge"))
    assert backend._effective_network() == ISOLATED
    assert backend.isolated is True
    assert backend.network_report()["isolated"] is True


def test_status_reason_says_when_a_sandbox_is_not_isolated():
    from olano.core.builder import _sandbox_started_reason

    assert "other containers can reach" in _sandbox_started_reason(
        {"network": "bridge", "health": "ok", "isolated": False}
    )
    assert _sandbox_started_reason({"network": ISOLATED, "health": "ok", "isolated": True}) == (
        "sandbox container is live"
    )


def test_page_warning_only_when_the_port_is_reachable():
    from olano.pages.services import _scope_detail

    assert _scope_detail("all", "sandbox", isolated=True) == ""
    assert "other agents' sandboxes" in _scope_detail("all", "sandbox", isolated=False)
    assert "this machine" in _scope_detail("all", "host", isolated=False)
    assert _scope_detail("loopback", "sandbox", isolated=False) == ""
