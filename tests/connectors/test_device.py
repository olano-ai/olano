"""Device-code sign-in — the connector broker's second method.

The provider's device endpoints are faked at the ``_post_form`` seam (no
network). What these pin, beyond the flow itself: the result is an ordinary
**broker token record** at the scope the caller asked for, so the toolkit path
(``get_credential("GITHUB_TOKEN")``) sees it with no further wiring — the
property the retired ``gh``-based connector never had — and nothing in the
module can shell out (the predecessor handed the token to the runtime's own
``gh``, a binary the agent's sandbox never runs).
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from olano.core.connectors import (
    connector_auth_status,
    disconnect_connector,
    get_provider,
    is_connector_shared,
)
from olano.core.connectors import device as dev

GITHUB = get_provider("github")
assert GITHUB is not None and GITHUB.device is not None

DEVICE_CODE_OK = {
    "device_code": "dev-code-xyz",
    "user_code": "ABCD-1234",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
    "interval": 5,
}


def _fake_provider(
    devices: list[Any],
    tokens: list[Any] | None = None,
    calls: list[tuple[str, dict]] | None = None,
):
    """Stub ``_post_form`` with a queue per endpoint.

    Each queue is consumed in order and its **last** entry repeats forever, so
    an ``authorization_pending`` poll can run as long as the test needs. An
    ``Exception`` entry is raised instead of returned.
    """
    auth = GITHUB.device
    queues = {auth.device_code_url: list(devices), auth.token_url: list(tokens or [])}

    def _post(url: str, data: dict, timeout: float) -> dict:
        if calls is not None:
            calls.append((url, data))
        queue = queues[url]
        assert queue, f"unexpected POST to {url}"
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return _post


@pytest.fixture(autouse=True)
def _clear_sessions():
    dev._SESSIONS.clear()
    yield
    dev._SESSIONS.clear()


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    """Collapse the poller's cancel-responsive sleep slices (still yields)."""
    real_sleep = time.sleep
    monkeypatch.setattr(dev.time, "sleep", lambda _s: real_sleep(0.005))


@pytest.fixture(autouse=True)
def _no_account_lookup(monkeypatch):
    monkeypatch.setattr(dev, "_lookup_account", lambda provider, token: None)


def _wait_terminal(session_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    session: dict | None = None
    while time.time() < deadline:
        session = dev.poll_device_auth(session_id)
        assert session is not None
        if session["status"] in ("approved", "error", "expired"):
            return session
        time.sleep(0.02)
    raise AssertionError(f"device session never finished: {session}")


# ── the flow ───────────────────────────────────────────────────────────────


def test_start_returns_the_code_immediately(monkeypatch):
    """A panel must never see a session without a code."""
    monkeypatch.setattr(
        dev, "_post_form", _fake_provider([DEVICE_CODE_OK], [{"error": "authorization_pending"}])
    )
    session = dev.start_device_auth(GITHUB, agent="merli")
    assert session["service"] == "github"
    assert session["status"] == "pending"
    assert session["user_code"] == "ABCD-1234"
    assert session["verification_url"] == "https://github.com/login/device"
    assert session["agent"] == "merli"
    dev.cancel_device_auth(session["session_id"])


def test_approval_saves_a_broker_record_the_toolkit_path_resolves(monkeypatch):
    from olano.config.home import get_olano_paths
    from olano.tools._credentials import agent_runtime_context, get_credential

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        dev,
        "_post_form",
        _fake_provider(
            [DEVICE_CODE_OK],
            [
                {"error": "authorization_pending"},
                {"access_token": "gho_real", "token_type": "bearer"},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(dev, "_lookup_account", lambda provider, token: "octocat")

    session = dev.start_device_auth(GITHUB, agent="merli")
    final = _wait_terminal(session["session_id"])
    assert final["status"] == "approved"
    assert final["account"] == "octocat"
    assert final["error_message"] is None
    # The device code — not the user code — is what gets polled, with the
    # public client id and the RFC 8628 grant.
    assert calls[1][1]["device_code"] == "dev-code-xyz"
    assert calls[1][1]["client_id"] == GITHUB.device.client_id
    assert calls[1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"

    path = get_olano_paths().agent_connector_token_path("merli", "github")
    record = json.loads(path.read_text())
    assert record["access_token"] == "gho_real"
    assert record["auth_mode"] == "device"
    assert record["client_source"] == "public"
    assert record["client_secret"] == ""
    assert record["expires_at"] is None  # a classic OAuth-app token never expires
    assert record["extras"]["account"] == "octocat"
    assert (path.stat().st_mode & 0o777) == 0o600

    status = connector_auth_status(GITHUB, agent="merli")
    assert status["connected"] is True
    assert status["scope"] == "agent"
    assert status["auth_mode"] == "device"
    assert status["account"] == "octocat"
    assert "device" in status["methods"]

    # The whole point: every consumer resolving the provider's exposed names
    # sees it — github_tools, an MCP env ref, the git job — with no gh anywhere.
    with agent_runtime_context("merli", None):
        assert get_credential("GITHUB_TOKEN") == "gho_real"
        assert get_credential("GITHUB_PERSONAL_ACCESS_TOKEN") == "gho_real"


def test_shared_and_system_scopes(monkeypatch):
    from olano.config.home import get_olano_paths

    monkeypatch.setattr(
        dev, "_post_form", _fake_provider([DEVICE_CODE_OK], [{"access_token": "gho_shared"}])
    )
    shared = dev.start_device_auth(GITHUB, agent="merli", shared=True)
    assert _wait_terminal(shared["session_id"])["status"] == "approved"
    assert is_connector_shared("github", "merli") is True
    assert connector_auth_status(GITHUB, agent="other")["scope"] == "shared"

    dev._SESSIONS.clear()
    monkeypatch.setattr(
        dev, "_post_form", _fake_provider([DEVICE_CODE_OK], [{"access_token": "gho_system"}])
    )
    system = dev.start_device_auth(GITHUB)
    assert system["agent"] is None and system["shared"] is False
    assert _wait_terminal(system["session_id"])["status"] == "approved"
    assert get_olano_paths().connector_token_path("github").exists()
    assert connector_auth_status(GITHUB)["scope"] == "system"


def test_a_refresh_token_is_kept_when_the_provider_grants_one(monkeypatch):
    from olano.config.home import get_olano_paths

    monkeypatch.setattr(
        dev,
        "_post_form",
        _fake_provider(
            [DEVICE_CODE_OK],
            [{"access_token": "ghu_x", "refresh_token": "ghr_y", "expires_in": 28800}],
        ),
    )
    session = dev.start_device_auth(GITHUB, agent="merli")
    assert _wait_terminal(session["session_id"])["status"] == "approved"
    record = json.loads(get_olano_paths().agent_connector_token_path("merli", "github").read_text())
    assert record["refresh_token"] == "ghr_y"
    assert record["expires_at"] is not None


@pytest.mark.parametrize(
    ("error", "status", "needle"),
    [
        ("access_denied", "error", "denied"),
        ("expired_token", "expired", "expired"),
        ("incorrect_client_credentials", "error", "client id"),
        ("something_new", "error", "something_new"),
    ],
)
def test_provider_device_errors_are_terminal(monkeypatch, error, status, needle):
    monkeypatch.setattr(dev, "_post_form", _fake_provider([DEVICE_CODE_OK], [{"error": error}]))
    final = _wait_terminal(dev.start_device_auth(GITHUB, agent="merli")["session_id"])
    assert final["status"] == status
    assert needle in final["error_message"].lower()


def test_poll_survives_a_transient_blip_and_honours_slow_down(monkeypatch):
    monkeypatch.setattr(
        dev,
        "_post_form",
        _fake_provider(
            [DEVICE_CODE_OK],
            [
                ConnectionError("connection reset"),
                {"error": "slow_down", "interval": 1},
                {"access_token": "gho_real"},
            ],
        ),
    )
    final = _wait_terminal(dev.start_device_auth(GITHUB, agent="merli")["session_id"])
    assert final["status"] == "approved"


def test_start_surfaces_a_device_code_request_failure(monkeypatch):
    monkeypatch.setattr(
        dev, "_post_form", _fake_provider([ConnectionError("name resolution failed")])
    )
    with pytest.raises(RuntimeError, match="Could not reach GitHub"):
        dev.start_device_auth(GITHUB, agent="merli")
    assert dev._SESSIONS == {}


def test_start_reports_device_flow_disabled(monkeypatch):
    monkeypatch.setattr(dev, "_post_form", _fake_provider([{"error": "device_flow_disabled"}]))
    with pytest.raises(RuntimeError, match="device flow is disabled"):
        dev.start_device_auth(GITHUB, agent="merli")


def test_a_provider_without_a_recipe_is_refused():
    ms = get_provider("microsoft365")
    assert ms is not None and ms.device is None
    with pytest.raises(ValueError, match="does not offer a sign-in code"):
        dev.start_device_auth(ms, agent="merli")


def test_env_overrides_reach_the_provider(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setenv("LIONCLAW_GH_OAUTH_CLIENT_ID", "enterprise-app")
    monkeypatch.setenv("LIONCLAW_GH_OAUTH_SCOPES", "repo,workflow")
    monkeypatch.setattr(
        dev, "_post_form", _fake_provider([DEVICE_CODE_OK], [{"access_token": "gho_e"}], calls)
    )
    final = _wait_terminal(dev.start_device_auth(GITHUB, agent="merli")["session_id"])
    assert final["status"] == "approved"
    assert calls[0][1] == {"client_id": "enterprise-app", "scope": "repo workflow"}
    assert calls[1][1]["client_id"] == "enterprise-app"


# ── single-flight / cancel / disconnect ────────────────────────────────────


def test_start_is_single_flight_per_service_and_agent(monkeypatch):
    second_code = dict(DEVICE_CODE_OK, device_code="dev-code-2", user_code="WXYZ-9999")
    monkeypatch.setattr(
        dev,
        "_post_form",
        _fake_provider([DEVICE_CODE_OK, second_code], [{"error": "authorization_pending"}]),
    )
    first = dev.start_device_auth(GITHUB, agent="merli")
    again = dev.start_device_auth(GITHUB, agent="merli")
    assert again["session_id"] == first["session_id"]
    assert again["user_code"] == first["user_code"]
    # A different scope is a different sign-in.
    other = dev.start_device_auth(GITHUB, agent="kevin")
    assert other["session_id"] != first["session_id"]
    assert other["user_code"] == "WXYZ-9999"
    for s in (first, other):
        dev.cancel_device_auth(s["session_id"])


def test_cancel_frees_the_single_flight_slot(monkeypatch):
    second_code = dict(DEVICE_CODE_OK, device_code="dev-code-2", user_code="WXYZ-9999")
    monkeypatch.setattr(
        dev,
        "_post_form",
        _fake_provider([DEVICE_CODE_OK, second_code], [{"error": "authorization_pending"}]),
    )
    session = dev.start_device_auth(GITHUB, agent="merli")
    assert dev.cancel_device_auth(session["session_id"]) is True
    cancelled = dev.poll_device_auth(session["session_id"])
    assert cancelled["status"] == "error"
    assert "cancelled" in cancelled["error_message"].lower()

    again = dev.start_device_auth(GITHUB, agent="merli")
    assert again["session_id"] != session["session_id"]
    dev.cancel_device_auth(again["session_id"])
    assert dev.cancel_device_auth(session["session_id"]) is False
    assert dev.cancel_device_auth("nope") is False


def test_disconnect_drops_in_flight_sessions(monkeypatch):
    second_code = dict(DEVICE_CODE_OK, device_code="dev-code-2", user_code="WXYZ-9999")
    monkeypatch.setattr(
        dev,
        "_post_form",
        _fake_provider([DEVICE_CODE_OK, second_code], [{"error": "authorization_pending"}]),
    )
    first = dev.start_device_auth(GITHUB, agent="merli")
    disconnect_connector(GITHUB, agent="merli")
    assert dev.poll_device_auth(first["session_id"])["status"] == "error"
    again = dev.start_device_auth(GITHUB, agent="merli")
    assert again["session_id"] != first["session_id"]
    assert again["user_code"] == "WXYZ-9999"
    dev.cancel_device_auth(again["session_id"])


def test_poller_marks_the_session_terminal_when_it_crashes(monkeypatch):
    monkeypatch.setattr(
        dev, "_post_form", _fake_provider([DEVICE_CODE_OK], [{"error": "authorization_pending"}])
    )
    monkeypatch.setattr(
        dev,
        "_poll_for_approval_inner",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    final = _wait_terminal(dev.start_device_auth(GITHUB, agent="merli")["session_id"])
    assert final["status"] == "error"
    assert "boom" in final["error_message"]


def test_the_flow_never_shells_out():
    """The predecessor handed the token to the runtime's own ``gh``. Nothing
    here may spawn a process: the record IS the connection."""
    assert not hasattr(dev, "subprocess")
    assert "gh" not in {n.split(".")[0] for n in dir(dev)}
