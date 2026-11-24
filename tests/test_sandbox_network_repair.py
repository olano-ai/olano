"""A persistent sandbox that lost its bridge is rebuilt, not re-adopted.

The production failure these pin (one managed box, 2026-09): one deployment's
agents could not `npx` anything — `EAI_AGAIN … registry.npmjs.org`, 144 s per
attempt — while an identically configured sibling deployment was fine. Both
containers had the same `/etc/resolv.conf` and the same default route; on the
broken box the container could not reach its own bridge gateway at all
(EHOSTUNREACH / timeout to 172.17.0.1), i.e. its veth was no longer plumbed
into docker0 after a daemon restart. Nothing recreated it, because its
signature still matched — which is exactly what the signature is for.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from olano.backends import lionclaw_docker as ld
from olano.backends.lionclaw_docker import OlanoDockerBackend
from olano.config.backend_config import OlanoDockerBackendConfig


def _proc(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    """Enough of the docker CLI to drive the persistent-container lifecycle."""

    def __init__(
        self,
        *,
        existing_sig: str | None = None,
        probes: list[str] | None = None,
        networks: set[str] | None = None,
        can_create_network: bool = True,
        run_fails: dict[str, str] | None = None,
    ) -> None:
        self.existing_sig = existing_sig
        self.probes = list(probes or [])
        self.networks = set(networks or set())
        self.can_create_network = can_create_network
        # {network name: stderr} — `docker run --network=<name>` fails with it.
        self.run_fails = dict(run_fails or {})
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        head = cmd[:3]
        if head[:2] == ["docker", "inspect"]:
            if self.existing_sig is None:
                return _proc(returncode=1, stderr="No such object")
            return _proc(
                json.dumps(
                    [
                        {
                            "Config": {"Labels": {"lionclaw.signature": self.existing_sig}},
                            "State": {"Status": "running"},
                        }
                    ]
                )
            )
        if head == ["docker", "network", "inspect"]:
            return _proc() if cmd[3] in self.networks else _proc(returncode=1, stderr="not found")
        if head == ["docker", "network", "create"]:
            if not self.can_create_network:
                return _proc(returncode=1, stderr="permission denied")
            self.networks.add(cmd[-1])
            return _proc(cmd[-1])
        if head[:2] == ["docker", "exec"]:
            return _proc(self.probes.pop(0) if self.probes else "ok 172.17.0.1")
        if head[:2] == ["docker", "run"]:
            for arg in cmd:
                if arg.startswith("--network="):
                    err = self.run_fails.get(arg.split("=", 1)[1])
                    if err:
                        return _proc(returncode=125, stderr=err)
            return _proc("deadbeef")
        return _proc()

    # ── helpers the assertions read ──────────────────────────────────────────
    @property
    def removed(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["docker", "rm", "-f"]]

    @property
    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["docker", "run"]]

    @property
    def probe_count(self) -> int:
        return len([c for c in self.calls if c[:2] == ["docker", "exec"]])

    def run_networks(self) -> list[str]:
        out = []
        for argv in self.runs:
            out += [a.split("=", 1)[1] for a in argv if a.startswith("--network=")]
        return out


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch):
    def _install(fake: FakeDocker) -> FakeDocker:
        monkeypatch.setattr(ld, "_run", fake)
        monkeypatch.setattr(ld.shutil, "which", lambda _name: "/usr/bin/docker")
        # `ensure_rescue_network` memoizes success for the process; a test that
        # created the network must not make the next one skip the docker calls.
        monkeypatch.setattr(ld, "_RESCUE_NETWORK_READY", False)
        return fake

    return _install


def _backend(tmp_path: Path) -> OlanoDockerBackend:
    return OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True, network="bridge"))


def _signature(backend: OlanoDockerBackend, tmp_path: Path, network: str) -> str:
    backend._workspace_root = tmp_path
    return backend._signature_for(network)


def _start(backend: OlanoDockerBackend, tmp_path: Path, agent: str = "alpha") -> None:
    """What ``start()`` does, minus ``_require_docker()``.

    ``tests/conftest.py::no_real_sandbox_containers`` stubs ``start`` out so no
    unit test can spawn a container; these drive the lifecycle it guards with a
    fake docker CLI, so they enter one level down rather than disarm the guard.
    """
    backend._agent_name = agent
    backend._workspace_root = tmp_path
    backend._extra_mounts = []
    backend._start_persistent()


# ── the probe itself ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("ok 172.17.0.1", "ok"),
        ("stranded 172.17.0.1", "stranded"),
        ("unknown no-default-route", "unknown"),
        ("", "unknown"),
        ("garbage from a login shell", "unknown"),
    ],
)
def test_probe_maps_container_output_to_a_verdict(monkeypatch, stdout, expected):
    monkeypatch.setattr(ld, "_run", lambda *a, **k: _proc(stdout))
    state, _detail = ld.probe_container_network("c")
    assert state == expected


def test_probe_never_raises_when_docker_is_unreachable(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("docker socket gone")

    monkeypatch.setattr(ld, "_run", boom)
    assert ld.probe_container_network("c")[0] == "unknown"


# ── reuse: healthy containers must be left alone ─────────────────────────────


def test_a_reachable_container_is_reused_untouched(docker, tmp_path):
    """The regression guard: a rebuild costs the agent every installed package,
    so a healthy sandbox must never be recreated by the check that exists to
    repair a broken one."""
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(existing_sig=_signature(b, tmp_path, "bridge"), probes=["ok 172.17.0.1"])
    )

    _start(b, tmp_path)

    assert fake.removed == []
    assert fake.runs == []
    assert b.network_report()["health"] == "ok"


def test_an_inconclusive_probe_is_treated_as_healthy(docker, tmp_path):
    """No `/proc`, no python3, a docker that would not answer — none of those
    are evidence the network is broken, and acting on them costs a rebuild."""
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "bridge"), probes=["unknown arp-unreadable"]
        )
    )

    _start(b, tmp_path)

    assert fake.removed == []
    assert b.network_report()["health"] == "unknown"


def test_netcheck_can_be_disabled(docker, tmp_path, monkeypatch):
    monkeypatch.setenv(ld.SANDBOX_NETCHECK_ENV, "0")
    b = _backend(tmp_path)
    fake = docker(FakeDocker(existing_sig=_signature(b, tmp_path, "bridge")))

    _start(b, tmp_path)

    assert fake.probe_count == 0
    assert fake.removed == []
    assert b.network_report()["health"] == "unchecked"


# ── reuse: a stranded container is rebuilt ───────────────────────────────────


def test_a_stranded_container_is_rebuilt_on_the_same_network(docker, tmp_path):
    """That production case. A rebuild re-plumbs the veth, which is the
    repair; the bridge itself was never the problem."""
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "bridge"),
            probes=["stranded 172.17.0.1", "ok 172.17.0.1"],
        )
    )

    _start(b, tmp_path)

    assert len(fake.removed) == 1
    assert fake.run_networks() == ["bridge"]
    assert b.network_report() == {
        "network": "bridge",
        "health": "ok",
        "rescued": False,
        "detail": "172.17.0.1",
        "isolated": False,
    }
    assert "olano-sandbox" not in fake.networks  # no escalation was needed


def test_a_broken_bridge_escalates_to_a_dedicated_network(docker, tmp_path, lionclaw_home):
    """A container built seconds ago cannot have been stranded by a daemon
    restart, so a second failure indicts the bridge — move off it."""
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "bridge"),
            probes=["stranded 172.17.0.1", "stranded 172.17.0.1", "ok 172.18.0.1"],
        )
    )

    _start(b, tmp_path)

    assert fake.run_networks() == ["bridge", "olano-sandbox"]
    assert b.network_report()["rescued"] is True
    # Sticky, or the next boot rebuilds the container all over again.
    assert ld.resolve_bridge_network() == "olano-sandbox"


def test_the_escalation_is_remembered_across_restarts(docker, tmp_path, lionclaw_home):
    """Second boot: the signature is computed on the rescue network from the
    start, so the container is reused rather than rebuilt."""
    ld._remember_sticky_network("olano-sandbox")

    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "olano-sandbox"),
            probes=["ok 172.18.0.1"],
            networks={"olano-sandbox"},
        )
    )

    _start(b, tmp_path)

    assert fake.removed == []
    assert fake.runs == []
    assert b._effective_network() == "olano-sandbox"


def test_an_unrepairable_box_reports_instead_of_looping(docker, tmp_path, lionclaw_home):
    """ip_forward off, a DROP in DOCKER-USER, NAT gone: nothing container-side
    fixes those. Two rebuilds, then stop — and say so in the status."""
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "bridge"),
            probes=["stranded x", "stranded x", "stranded x"],
        )
    )

    _start(b, tmp_path)

    assert len(fake.runs) == 2  # the same-network rebuild and the escalation
    assert b.network_report()["health"] == "stranded"
    assert ld.resolve_bridge_network() == "bridge"  # never remembered a failure


def test_no_escalation_when_the_rescue_network_cannot_be_created(docker, tmp_path, lionclaw_home):
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "bridge"),
            probes=["stranded x", "stranded x"],
            can_create_network=False,
        )
    )

    _start(b, tmp_path)

    assert fake.run_networks() == ["bridge"]
    assert b.network_report()["health"] == "stranded"


# ── docker refuses an endpoint outright (the 1024-port bridge limit) ─────────

EXFULL = (
    "docker: Error response from daemon: failed to create endpoint sandbox on network "
    "bridge: adding interface veth1a2b3c to bridge docker0 failed: exchange full"
)


def test_a_bridge_out_of_port_slots_escalates_instead_of_failing_the_agent(
    docker, tmp_path, lionclaw_home
):
    """A Linux bridge holds 1024 interfaces; past that `br_add_if` returns
    -EXFULL and docker cannot give a container an endpoint at all. That is the
    same "this lane is unusable" fact as an unreachable gateway, and it used to
    raise straight past the repair — failing every agent closed on a box the
    escalation could have rescued (one managed box, 2026-09)."""
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=None,
            probes=["ok 172.18.0.1"],
            run_fails={"bridge": EXFULL},
        )
    )

    _start(b, tmp_path)

    assert fake.run_networks() == ["bridge", "olano-sandbox"]
    assert b.network_report()["rescued"] is True
    assert ld.resolve_bridge_network() == "olano-sandbox"


def test_both_lanes_refusing_an_endpoint_raises_so_the_caller_fails_closed(
    docker, tmp_path, lionclaw_home
):
    """No container exists, so returning would register a dead sandbox whose
    every file call answers exit 127. The builder's contract is to fall back to
    a no-shell filesystem backend, which needs the raise."""
    b = _backend(tmp_path)
    docker(
        FakeDocker(
            existing_sig=None,
            run_fails={"bridge": EXFULL, "olano-sandbox": EXFULL},
        )
    )

    with pytest.raises(RuntimeError, match="docker networking needs repair"):
        _start(b, tmp_path)

    assert b.network_report()["health"] == "stranded"
    assert ld.resolve_bridge_network() == "bridge"  # never remembered a failure


def test_a_non_network_create_failure_is_never_escalated(docker, tmp_path, lionclaw_home):
    """A missing image or a bad mount is not a networking fault — retrying it on
    another bridge would paper over an unrelated defect and misreport it."""
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=None,
            run_fails={
                "bridge": "docker: Error response from daemon: No such image: olano-sandbox:latest"
            },
        )
    )

    with pytest.raises(RuntimeError, match="No such image"):
        _start(b, tmp_path)

    assert fake.run_networks() == ["bridge"]
    assert "olano-sandbox" not in fake.networks


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (EXFULL, True),
        ("failed to create endpoint x on network bridge", True),
        ("no available IPv4 addresses on this network's address pool", True),
        ("could not find an available, non-overlapping IPv4 address pool", True),
        ("network olano-sandbox not found", True),
        ("No such image: olano-sandbox:latest", False),
        ("invalid mount config for type bind: bind source path does not exist", False),
        ("", False),
    ],
)
def test_network_failure_classification(text, expected):
    assert ld.is_network_creation_failure(text) is expected


# ── the operator override ────────────────────────────────────────────────────


def test_env_override_pins_the_network_and_beats_the_sticky_file(monkeypatch, lionclaw_home):
    ld._remember_sticky_network("olano-sandbox")
    monkeypatch.setenv(ld.SANDBOX_NETWORK_ENV, "bridge")
    assert ld.resolve_bridge_network() == "bridge"

    monkeypatch.setenv(ld.SANDBOX_NETWORK_ENV, "my-net")
    assert ld.resolve_bridge_network() == "my-net"


@pytest.mark.parametrize("configured", ["none", "host"])
def test_only_bridge_resolves(docker, tmp_path, lionclaw_home, configured):
    """`none` means no network on purpose and `host` is the host namespace —
    neither is a lane anything may be moved off."""
    ld._remember_sticky_network("olano-sandbox")
    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True, network=configured))
    docker(FakeDocker(existing_sig=None, probes=["ok 10.0.0.1"]))

    _start(b, tmp_path)

    assert b._effective_network() == configured


def test_stdio_mcp_containers_follow_the_same_lane(docker, lionclaw_home, tmp_path):
    """An npx-launched MCP server left on a broken default bridge resolves
    nothing — which is the failure that started this."""
    ld._remember_sticky_network("olano-sandbox")
    docker(FakeDocker(networks={"olano-sandbox"}))
    argv = ld.mcp_stdio_run_args(agent_name="alpha", workspace_root=tmp_path, secret_names=[])
    assert "--network=olano-sandbox" in argv


# ── the marker must never strand a boot on its own ───────────────────────────


def test_a_remembered_network_that_vanished_is_recreated(docker, lionclaw_home):
    """`docker network rm olano-sandbox` after an escalation: the marker still
    names it, and a `docker run --network` on a missing network fails for
    every agent with nothing to fall back to. Bring it back instead."""
    ld._remember_sticky_network("olano-sandbox")
    fake = docker(FakeDocker())  # no networks exist

    assert ld.resolve_bridge_network() == "olano-sandbox"
    assert "olano-sandbox" in fake.networks  # re-created, not just assumed


def test_a_remembered_network_that_cannot_be_recreated_falls_back_to_bridge(docker, lionclaw_home):
    ld._remember_sticky_network("olano-sandbox")
    docker(FakeDocker(can_create_network=False))

    assert ld.resolve_bridge_network() == "bridge"


# ── the escalation goes both ways, and never against an operator's pin ───────


def test_a_broken_rescue_bridge_falls_back_to_the_default_bridge(docker, tmp_path, lionclaw_home):
    """A box that escalated once and later lost the rescue bridge instead must
    be able to come back — otherwise the marker is a one-way door."""
    ld._remember_sticky_network("olano-sandbox")
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "olano-sandbox"),
            probes=["stranded 172.18.0.1", "stranded 172.18.0.1", "ok 172.17.0.1"],
            networks={"olano-sandbox"},
        )
    )

    _start(b, tmp_path)

    assert fake.run_networks() == ["olano-sandbox", "bridge"]
    assert b.network_report()["rescued"] is True
    assert ld.resolve_bridge_network() == "bridge"


def test_an_operator_pin_is_never_escalated_away_from(docker, tmp_path, lionclaw_home, monkeypatch):
    """`LIONCLAW_SANDBOX_NETWORK` is the operator saying "this lane" — a
    stranded container is still rebuilt (that repair is lane-neutral), but
    the runtime never moves it somewhere the operator did not choose."""
    monkeypatch.setenv(ld.SANDBOX_NETWORK_ENV, "bridge")
    b = _backend(tmp_path)
    fake = docker(
        FakeDocker(
            existing_sig=_signature(b, tmp_path, "bridge"),
            probes=["stranded x", "stranded x", "ok x"],
        )
    )

    _start(b, tmp_path)

    assert fake.run_networks() == ["bridge"]  # rebuilt once, never moved
    assert "olano-sandbox" not in fake.networks
    assert b.network_report()["health"] == "stranded"
