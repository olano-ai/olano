"""A plan denial reaches the browser as a plan denial.

Endpoints that guard up front call ``dep_guard.reject`` and answer 403 with the
structured detail. The write guards deeper in the stack raise
``DeploymentLimitExceeded`` instead, and those used to surface as HTTP 500
carrying only ``str(exc)`` — the archetype install in the onboarding wizard lost
the limit, the count and the "upgrade your plan" remediation, and logged a stack
trace per click.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.deployment import enforcement as dep_enforcement
from olano.runtime.api import create_api


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {}
    return create_api(runtime)


@pytest.fixture
def client(app):
    return TestClient(app)


def _limit_denial() -> dep_enforcement.Decision:
    return dep_enforcement.Decision(
        allowed=False,
        code="deployment_limit_max_agents",
        message="This deployment is limited to 3 agent(s); 7 already exist.",
        remediation=dep_enforcement.REMEDIATION,
        limit=3,
        current=7,
    )


def test_archetype_install_over_limit_returns_structured_403(client, monkeypatch):
    def _raise(**kwargs):
        raise dep_enforcement.DeploymentLimitExceeded(_limit_denial())

    monkeypatch.setattr("olano.archetypes.installer.install_archetype", _raise)

    r = client.post("/api/archetypes/install", json={"archetype": "agentfather"})

    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "deployment_limit_max_agents"
    assert "limited to 3 agent(s)" in detail["message"]
    # The half the wizard was missing: what the user can actually do about it.
    assert detail["remediation"]
    assert detail["limit"] == 3
    assert detail["current"] == 7


def test_any_endpoint_raising_the_limit_answers_403(app):
    """The app-level backstop, exercised through the app's own registration.

    Every write path that reaches ``save_agent_config`` /
    ``_guarded_agent_config_write`` can raise this from inside a handler that
    never guarded up front; the endpoint under test stands in for all of them.
    """

    @app.get("/api/_test/limit-raiser")
    async def _raiser() -> dict:
        raise dep_enforcement.DeploymentLimitExceeded(_limit_denial())

    r = TestClient(app).get("/api/_test/limit-raiser")

    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "deployment_limit_max_agents"
    assert detail["remediation"]


def test_policy_unavailable_maps_to_503(client, monkeypatch):
    def _raise(**kwargs):
        raise dep_enforcement.DeploymentLimitExceeded(
            dep_enforcement.Decision(
                allowed=False,
                code="policy_unavailable",
                message="Could not evaluate deployment limits; the operation was rejected.",
                remediation="Retry shortly.",
            )
        )

    monkeypatch.setattr("olano.archetypes.installer.install_archetype", _raise)

    r = client.post("/api/archetypes/install", json={"archetype": "agentfather"})

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "policy_unavailable"
