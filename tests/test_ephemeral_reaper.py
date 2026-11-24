"""The runtime reaps the ephemeral containers its own sessions leave behind.

The production reading these pin (one managed box, 2026-09): the update agent's
host census said `docker0=1012/1024 orphans=0 veth=1062 ctrs=1503` on a box
with twelve agents. `orphans=0` means docker accounted for every one of those
ports — they were not leaked veths but ~1000 *live* containers, and the only
containers the runtime creates by the hundred are the `--rm` stdio-MCP sessions
(one per tool call) and connected jobs. A session whose teardown misses the
container (a SIGKILLed docker CLI proxies no SIGTERM) leaves it running with
nobody attached, one bridge port each, until `exchange full`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from olano.backends import lionclaw_docker as ld

NOW = 1_800_000_000.0
HOUR = 3600


def _proc(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _rfc3339(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


#: Agent names → what `docker ps --filter label=…` would see. A persistent
#: sandbox carries `lionclaw.signature`; its NAME is what tells a worker's from
#: an agent's, which is why the fake models the name filter too.
_LABELS = {
    "mcp": {"olano.mcp": "1"},
    "job": {"olano.job": "git-clone"},
    "worker": {"lionclaw.signature": "sig"},
    "sandbox": {"lionclaw.signature": "sig"},
}
_NAMES = {
    "mcp": "olano-mcp-x",
    "job": "olano-job-x",
    "worker": "lionclaw-merli__cog__pulse_agent__ab12cd-0123456789",
    "sandbox": "lionclaw-merli-0123456789",
}


class FakeDocker:
    """`docker ps -aq --filter …`, `docker inspect`, `docker rm -f` — enough to
    drive the reaper. Containers are ``{id: (family, status, created_epoch)}``;
    ``family`` picks the labels and name docker would filter on (``_LABELS`` /
    ``_NAMES``), so a container a filter must NOT match (``sandbox``: an
    agent's own) is modelled rather than assumed away."""

    def __init__(
        self,
        containers: dict[str, tuple[str, str, float]] | None = None,
        *,
        ps_fails: bool = False,
        inspect_fails: bool = False,
        rm_refuses: set[str] | None = None,
    ) -> None:
        self.containers = dict(containers or {})
        self.ps_fails = ps_fails
        self.inspect_fails = inspect_fails
        self.rm_refuses = set(rm_refuses or ())
        self.calls: list[list[str]] = []

    @staticmethod
    def _matches(family: str, filters: list[str]) -> bool:
        labels, name = _LABELS[family], _NAMES[family]
        for flt in filters:
            kind, _, value = flt.partition("=")
            if kind == "label":
                key, has_value, expected = value.partition("=")
                if key not in labels or (has_value and labels[key] != expected):
                    return False
            elif kind == "name":
                if value not in name:
                    return False
            else:  # a filter the fake does not model is a test bug
                raise AssertionError(f"unexpected docker ps filter {flt!r}")
        return True

    def __call__(self, cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[:2] == ["docker", "ps"]:
            if self.ps_fails:
                return _proc(returncode=1, stderr="Cannot connect to the Docker daemon")
            filters = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"]
            ids = [
                cid for cid, (fam, _s, _c) in self.containers.items() if self._matches(fam, filters)
            ]
            return _proc("\n".join(ids) + ("\n" if ids else ""))
        if cmd[:2] == ["docker", "inspect"]:
            if self.inspect_fails:
                return _proc(returncode=1, stderr="Error: No such object")
            rows = []
            missing = False
            for cid in cmd[4:]:
                if cid in self.containers:
                    _fam, status, created = self.containers[cid]
                    rows.append(f"{cid}\t{status}\t{_rfc3339(created)}")
                else:
                    missing = True
            return _proc("\n".join(rows) + "\n", returncode=1 if missing else 0)
        if cmd[:3] == ["docker", "rm", "-f"]:
            removed = [cid for cid in cmd[3:] if cid not in self.rm_refuses]
            for cid in removed:
                self.containers.pop(cid, None)
            refused = [cid for cid in cmd[3:] if cid in self.rm_refuses]
            return _proc(
                "\n".join(removed) + "\n",
                returncode=1 if refused else 0,
                stderr="\n".join(f"Error: cannot remove {cid}" for cid in refused),
            )
        return _proc()

    @property
    def removals(self) -> list[list[str]]:
        return [c[3:] for c in self.calls if c[:3] == ["docker", "rm", "-f"]]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch):
    def _install(fake: FakeDocker) -> FakeDocker:
        monkeypatch.setattr(ld, "_run", fake)
        monkeypatch.setattr(ld.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(ld, "_LAST_REAP", {})
        return fake

    return _install


def _cid(n: int) -> str:
    return f"{n:064x}"


def test_reaps_by_age_and_keeps_live_sessions(docker):
    fake = docker(
        FakeDocker(
            {
                _cid(1): ("mcp", "running", NOW - 30),  # a tool call in flight
                _cid(2): ("mcp", "running", NOW - 2 * HOUR),  # long, but inside the bound
                _cid(3): ("mcp", "running", NOW - 4 * HOUR),  # nobody's
                _cid(4): ("job", "running", NOW - 5 * HOUR),
                _cid(5): ("mcp", "created", NOW - 5),  # a launch between create and start
                _cid(6): (
                    "mcp",
                    "created",
                    NOW - 20 * 60,
                ),  # a `docker run` that failed at the network
                _cid(7): ("mcp", "exited", NOW - 4 * HOUR),  # `--rm` should have taken it
            }
        )
    )
    report = ld.reap_ephemeral_containers(max_age_s=3 * HOUR, now=NOW, reason="test")
    assert report["checked"] == 7
    assert report["stale"] == 4
    assert report["reaped"] == 4
    assert report["families"] == {"mcp": 3, "job": 1, "worker": 0}
    assert report["oldest_age_s"] == 5 * HOUR
    assert report["errors"] == []
    assert set(fake.removals[0]) == {_cid(3), _cid(4), _cid(6), _cid(7)}
    assert set(fake.containers) == {_cid(1), _cid(2), _cid(5)}
    # Memoized for /api/diagnostics/mcp.
    assert ld.last_reap_report()["reaped"] == 4
    assert ld.last_reap_report()["reason"] == "test"


def test_nothing_to_do_is_cheap_and_quiet(docker):
    fake = docker(FakeDocker())
    report = ld.reap_ephemeral_containers(now=NOW)
    assert report["checked"] == 0 and report["reaped"] == 0 and report["errors"] == []
    # One listing per family row (mcp, job, and one per worker marker), no
    # inspect, no rm.
    assert [c[:2] for c in fake.calls] == [["docker", "ps"]] * len(ld._EPHEMERAL_FAMILIES)
    assert report["oldest_age_s"] is None


def test_lists_exactly_the_labels_the_launchers_stamp(tmp_path: Path):
    """The filter and the label come from the same constants — pin that the
    argv `ephemeral_run_args` builds carries what the reaper looks for, so a
    renamed label cannot quietly exempt every new container from the sweep."""
    mcp = ld.mcp_stdio_run_args(agent_name="alpha", workspace_root=tmp_path, secret_names=[])
    job = ld.ephemeral_run_args(
        label="git-clone",
        agent_name="alpha",
        workspace_root=tmp_path,
        secret_names=[],
        interactive=False,
    )
    filters: dict[str, list[tuple[str, ...]]] = {}
    for family, flts in ld._EPHEMERAL_FAMILIES:
        filters.setdefault(family, []).append(flts)
    assert filters["mcp"] == [("label=olano.mcp=1",)]
    assert filters["job"] == [("label=olano.job",)]
    assert "--label=olano.mcp=1" in mcp
    assert any(a.startswith("--label=olano.job=") for a in job)
    # The worker family is the persistent-sandbox label AND a worker name
    # marker, never the label alone: the label alone is every agent's sandbox.
    assert filters["worker"] == [
        ("label=lionclaw.signature", "name=__cog__"),
        ("label=lionclaw.signature", "name=__sub__"),
    ]
    assert ld._LABEL_KEY == "lionclaw.signature"


def test_worker_sandboxes_are_reaped_and_agent_sandboxes_never_listed(docker, tmp_path: Path):
    """The managed-box reading of 2026-10: `top=br-…:959 SATURATED mcp=0
    sbx=960` on twelve agents — persistent sandboxes a cognition worker's
    unique name minted once per run. They carry the agent-sandbox label, so
    the NAME is what separates one from an agent's own container, which the
    sweep must never touch however old it is."""
    worker_name = ld._persistent_container_name("merli__cog__pulse_agent__ab12cd", tmp_path)
    assert "__cog__" in worker_name  # the slug keeps the marker
    fake = docker(
        FakeDocker(
            {
                _cid(1): ("worker", "running", NOW - 30 * 24 * HOUR),  # a run from last month
                _cid(2): ("worker", "running", NOW - 10 * 60),  # a pre-fix runtime's live run
                _cid(3): ("worker", "exited", NOW - 2 * 24 * HOUR),
                _cid(4): ("sandbox", "running", NOW - 90 * 24 * HOUR),  # an agent's own
            }
        )
    )
    report = ld.reap_ephemeral_containers(max_age_s=3 * HOUR, now=NOW, reason="test")
    assert report["checked"] == 3  # the agent's sandbox never reached the listing
    assert report["families"] == {"mcp": 0, "job": 0, "worker": 2}
    assert set(fake.removals[0]) == {_cid(1), _cid(3)}
    assert set(fake.containers) == {_cid(2), _cid(4)}
    assert report["errors"] == []


def test_docker_ps_failure_is_reported_never_raised(docker):
    fake = docker(FakeDocker({_cid(1): ("mcp", "running", NOW - 9 * HOUR)}, ps_fails=True))
    report = ld.reap_ephemeral_containers(now=NOW)
    assert report["reaped"] == 0
    assert len(report["errors"]) == len(ld._EPHEMERAL_FAMILIES)  # one per family row
    assert "Docker daemon" in report["errors"][0]
    assert fake.removals == []


def test_inspect_failure_is_reported_never_raised(docker):
    fake = docker(FakeDocker({_cid(1): ("mcp", "running", NOW - 9 * HOUR)}, inspect_fails=True))
    report = ld.reap_ephemeral_containers(now=NOW)
    assert report["checked"] == 1 and report["reaped"] == 0
    assert report["errors"] and "inspect" in report["errors"][0]
    assert fake.removals == []


def test_a_container_that_vanished_mid_sweep_does_not_hide_the_others(docker, monkeypatch):
    fake = docker(
        FakeDocker(
            {
                _cid(1): ("mcp", "running", NOW - 9 * HOUR),
                _cid(2): ("mcp", "running", NOW - 9 * HOUR),
            }
        )
    )

    # Listed, then gone before inspect: docker inspect exits 1 but still prints
    # the rows it has.
    def racy(cmd: list[str], timeout: int = 30):
        if cmd[:2] == ["docker", "inspect"]:
            fake.containers.pop(_cid(2), None)
        return fake(cmd, timeout)

    monkeypatch.setattr(ld, "_run", racy)
    report = ld.reap_ephemeral_containers(max_age_s=HOUR, now=NOW)
    assert report["reaped"] == 1
    assert fake.removals == [[_cid(1)]]


def test_removal_is_chunked_and_counts_only_what_docker_confirms(docker, monkeypatch):
    monkeypatch.setattr(ld, "_REAP_CHUNK", 3)
    stale = {_cid(n): ("mcp", "running", NOW - 9 * HOUR) for n in range(1, 8)}
    fake = docker(FakeDocker(stale, rm_refuses={_cid(4)}))
    report = ld.reap_ephemeral_containers(max_age_s=HOUR, now=NOW)
    assert [len(chunk) for chunk in fake.removals] == [3, 3, 1]
    assert report["stale"] == 7
    assert report["reaped"] == 6
    assert report["errors"] == ["docker rm: Error: cannot remove " + _cid(4)]


def test_no_docker_cli_is_a_report_not_an_exception(monkeypatch):
    monkeypatch.setattr(ld.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ld, "_LAST_REAP", {})
    calls: list[list[str]] = []
    monkeypatch.setattr(ld, "_run", lambda cmd, timeout=30: calls.append(cmd))
    report = ld.reap_ephemeral_containers(now=NOW)
    assert report["errors"] == ["docker not on PATH"]
    assert calls == []


def test_concurrent_reap_is_skipped_and_leaves_the_memo(docker):
    docker(FakeDocker())
    ld._LAST_REAP.update({"reaped": 42})
    with ld._REAP_LOCK:
        report = ld.reap_ephemeral_containers(now=NOW)
    assert report["errors"] == ["a reap is already running"]
    assert ld.last_reap_report() == {"reaped": 42}


def test_max_age_env_override_and_floor(monkeypatch):
    monkeypatch.delenv(ld.EPHEMERAL_MAX_AGE_ENV, raising=False)
    assert ld.ephemeral_max_age() == ld.EPHEMERAL_MAX_AGE_DEFAULT
    monkeypatch.setenv(ld.EPHEMERAL_MAX_AGE_ENV, "900")
    assert ld.ephemeral_max_age() == 900
    # A bound under a minute would reap a session mid-handshake.
    monkeypatch.setenv(ld.EPHEMERAL_MAX_AGE_ENV, "5")
    assert ld.ephemeral_max_age() == 60
    monkeypatch.setenv(ld.EPHEMERAL_MAX_AGE_ENV, "soon")
    assert ld.ephemeral_max_age() == ld.EPHEMERAL_MAX_AGE_DEFAULT


def test_created_timestamp_parsing():
    from datetime import datetime, timezone

    expected = datetime(2026, 9, 17, 19, 2, 16, tzinfo=timezone.utc).timestamp()
    # docker prints nanoseconds; Python's fromisoformat does not take nine digits.
    assert ld._parse_created("2026-09-17T19:02:16.123456789Z") == expected
    assert ld._parse_created("2026-09-17T19:02:16Z") == expected
    assert ld._parse_created("") is None
    assert ld._parse_created("yesterday") is None
