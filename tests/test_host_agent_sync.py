"""Host update-agent reconciler (olano/deployment/host_agent.py).

The bridge that gets update-agent fixes onto boxes provisioned before the
agent's own self-refresh existed: fetch the canonical script from the control
plane, compare hashes with the copy on the HOST (read through a helper
container over the mounted docker socket), install atomically only when they
differ. These tests pin the guard rails — never create the host file, never
install an unverified payload, and compose the helper containers exactly.
"""

from __future__ import annotations

import hashlib
import subprocess
from typing import Any

import pytest

from olano.deployment import host_agent
from olano.deployment.sources import CloudConfig

AGENT_SCRIPT = (
    "#!/usr/bin/env bash\n"
    "# fake agent for tests\n"
    "set -uo pipefail\n"
    ". /opt/olano/olano.env\n"
    '[ -n "${LIONCLAW_DEPLOYMENT_TOKEN:-}" ] || exit 0\n'
    "STATE=/opt/olano/update-applied-at\n"
)
AGENT_SHA = hashlib.sha256(AGENT_SCRIPT.encode()).hexdigest()
CFG = CloudConfig(url="https://cloud.olano.ai", token="olano_dep_x")
IMAGE = "sha256:" + "a" * 64


class FakeRun:
    """Scripted subprocess.run: routes docker invocations to canned results."""

    def __init__(self, host_sha: str | None, *, install_rc: int = 0):
        self.host_sha = host_sha
        self.install_rc = install_rc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        joined = " ".join(argv)
        if argv[:2] == ["docker", "inspect"]:
            name = argv[-1]
            if name == "olano":
                return subprocess.CompletedProcess(argv, 0, stdout=IMAGE + "\n", stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such object")
        if "sha256sum" in joined:
            if self.host_sha is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such file")
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"{self.host_sha}  /host-opt/update-agent.sh\n", stderr=""
            )
        if "cat >" in joined:
            return subprocess.CompletedProcess(
                argv, self.install_rc, stdout="", stderr="boom" if self.install_rc else ""
            )
        raise AssertionError(f"unexpected docker call: {argv}")

    def install_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if any("cat >" in a for a in c["argv"])]


def _fetch_ok(cfg: CloudConfig) -> tuple[str, str]:
    return AGENT_SCRIPT, AGENT_SHA


def test_in_sync_touches_nothing():
    run = FakeRun(host_sha=AGENT_SHA)
    result = host_agent.sync_host_agent(cfg=CFG, run_fn=run, fetch_fn=_fetch_ok)
    assert result == {"status": "in-sync", "sha": AGENT_SHA}
    assert run.install_calls() == []


def test_differing_host_agent_is_replaced_atomically():
    run = FakeRun(host_sha="b" * 64)
    result = host_agent.sync_host_agent(cfg=CFG, run_fn=run, fetch_fn=_fetch_ok)
    assert result["status"] == "installed"
    assert result["to"] == AGENT_SHA

    (install,) = run.install_calls()
    argv = install["argv"]
    # The payload rides stdin (never argv), the helper is offline, and the
    # write is staged + syntax-checked + moved — not edited in place.
    assert install["input"] == AGENT_SCRIPT
    assert "--network" in argv and "none" in argv
    assert "-v" in argv and "/opt/olano:/host-opt" in argv
    shell = argv[-1]
    assert "cat >/host-opt/update-agent.sh.new" in shell
    assert "bash -n /host-opt/update-agent.sh.new" in shell
    assert "mv /host-opt/update-agent.sh.new /host-opt/update-agent.sh" in shell


def test_never_creates_a_missing_host_agent():
    run = FakeRun(host_sha=None)  # sha256sum: No such file
    result = host_agent.sync_host_agent(cfg=CFG, run_fn=run, fetch_fn=_fetch_ok)
    assert result == {"status": "no-host-agent"}
    assert run.install_calls() == []


def test_install_failure_is_reported_not_raised():
    run = FakeRun(host_sha="b" * 64, install_rc=1)
    result = host_agent.sync_host_agent(cfg=CFG, run_fn=run, fetch_fn=_fetch_ok)
    assert result["status"] == "install-failed"


@pytest.mark.parametrize(
    ("mutate", "reason_fragment"),
    [
        (lambda s: s.replace("#!/usr/bin/env bash", "#!/bin/sh"), "shebang"),
        (lambda s: s + "# café\n", "non-ASCII"),
        (lambda s: s.replace("update-applied-at", "elsewhere"), "anchor"),
    ],
)
def test_unacceptable_payloads_are_refused(mutate, reason_fragment):
    bad = mutate(AGENT_SCRIPT)

    def fetch_bad(cfg: CloudConfig) -> tuple[str, str]:
        return bad, hashlib.sha256(bad.encode()).hexdigest()

    run = FakeRun(host_sha="b" * 64)
    result = host_agent.sync_host_agent(cfg=CFG, run_fn=run, fetch_fn=fetch_bad)
    assert result["status"] == "invalid-script"
    assert reason_fragment.lower() in result["reason"].lower()
    assert run.calls == [], "an invalid payload must not even spawn helpers"


def test_fetch_refuses_body_that_does_not_match_advertised_sha(monkeypatch):
    class FakeResponse:
        text = AGENT_SCRIPT
        headers = {"X-Agent-Sha256": "f" * 64}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(host_agent.httpx, "get", lambda *a, **k: FakeResponse())
    assert host_agent.fetch_canonical_agent(CFG) is None


def test_fetch_accepts_matching_sha(monkeypatch):
    class FakeResponse:
        text = AGENT_SCRIPT
        headers = {"X-Agent-Sha256": AGENT_SHA}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(host_agent.httpx, "get", lambda *a, **k: FakeResponse())
    assert host_agent.fetch_canonical_agent(CFG) == (AGENT_SCRIPT, AGENT_SHA)


def test_runtime_image_falls_back_to_legacy_container_name():
    calls: list[list[str]] = []

    def run_fn(argv, **kwargs):
        calls.append(list(argv))
        if argv[-1] == "olano":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such object")
        return subprocess.CompletedProcess(argv, 0, stdout=IMAGE + "\n", stderr="")

    assert host_agent._runtime_image(run_fn) == IMAGE
    assert [c[-1] for c in calls] == ["olano", "lionclaw"]


def test_sync_disabled_by_env(monkeypatch):
    monkeypatch.setenv(host_agent.DISABLE_ENV, "0")
    assert host_agent.sync_enabled() is False
