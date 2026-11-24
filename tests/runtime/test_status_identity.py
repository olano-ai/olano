"""`/api/status` must answer "what is this box running, and is its gateway up"
— from outside the login wall.

A managed box has no SSH, so after an Update the only externally checkable
facts used to be "the process answers". The 2026-08-15 gateway incident was
debugged blind for want of two fields: which image actually booted
(`runtime_version`) and whether the box holds a gateway key (`gateway`).
Both are non-secret, and both ride the public readiness probe now.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def runtime() -> MagicMock:
    rt = MagicMock()
    rt._runners = {}
    rt._channels = []
    rt._agents_booting = False
    rt._agents_expected = []
    rt.scheduler.list_jobs.return_value = []
    return rt


@pytest.fixture
def client(runtime, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    from olano.runtime.api import create_api

    return TestClient(create_api(runtime))


def test_status_reports_build_identity_and_gateway_state(client, monkeypatch):
    from olano.deployment import version as ver

    monkeypatch.setenv(ver.BUILD_SHA_ENV, "a" * 40)

    body = client.get("/api/status").json()

    assert body["runtime_version"] == f"git-{'a' * 40}"
    # No cloud attachment in a bare test home ⇒ the honest answer.
    assert body["gateway"] == "unattached"


def test_identity_failure_never_breaks_the_readiness_probe(client, monkeypatch):
    """The probe is what installers poll — identity errors degrade to null."""
    import olano.deployment.gateway_key as gk

    def boom() -> str:
        raise RuntimeError("vault on fire")

    monkeypatch.setattr(gk, "enrollment_state", boom)

    body = client.get("/api/status").json()

    assert body["status"] == "running"
    assert body["gateway"] is None
