"""Tests for the instance→cloud credential-rotation reporter.

When the owner replaces a temporary cloud-provisioned credential (admin
password change, vault rekey), the control plane must purge its plaintext
copy. These pin the instance-side half of that contract
(deployment/credential_rotation.py): the durable local record, the
report-until-acknowledged posture, the deployment-admin matcher, and that only
booleans — never a secret — go over the wire.
"""

from __future__ import annotations

import json

import httpx

from olano.deployment import credential_rotation as cr
from olano.deployment.sources import CloudConfig

_CFG = CloudConfig(url="https://cloud.example", token="olano_dep_test")


class _Capture:
    """MockTransport handler capturing requests and serving scripted responses."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[dict] = []
        self._responses = responses or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(200, json={"status": "ok"})


def _report(capture: _Capture) -> cr.ReportResult:
    return cr.report_pending_rotations(config=_CFG, transport=httpx.MockTransport(capture))


# ── local record / pending state ─────────────────────────────────────────────


def test_record_stamps_state_and_pending_flags() -> None:
    assert cr.has_pending() is False
    cr.record_admin_password_rotation()
    state = cr.load_state()
    assert state["admin_password_rotated_at"]
    assert state["admin_password_reported_at"] is None
    flags = cr.pending_flags(state)
    assert flags == {"admin_password_rotated": True, "vault_password_rotated": False}
    assert cr.has_pending() is True


def test_state_file_lives_under_deployment_dir(lionclaw_home) -> None:
    cr.record_vault_password_rotation()
    path = lionclaw_home / "deployment" / cr.STATE_FILE
    assert path.exists()
    assert json.loads(path.read_text())["vault_password_rotated_at"]


def test_corrupt_state_file_treated_as_empty(lionclaw_home) -> None:
    path = lionclaw_home / "deployment"
    path.mkdir(parents=True, exist_ok=True)
    (path / cr.STATE_FILE).write_text("not json{")
    assert cr.load_state() == {}
    assert cr.has_pending() is False
    cr.record_admin_password_rotation()  # recovers by rewriting
    assert cr.has_pending() is True


# ── deployment-admin matcher ─────────────────────────────────────────────────


def test_matcher_uses_provisioned_admin_email(monkeypatch) -> None:
    monkeypatch.setenv(cr.ADMIN_EMAIL_ENV, "Owner@X.com")
    assert cr.is_deployment_admin_email("owner@x.com") is True  # case-insensitive
    assert cr.is_deployment_admin_email(" owner@x.com ") is True
    # Another account — even an admin — does not kill the temporary password.
    assert cr.is_deployment_admin_email("other@x.com", is_admin=True) is False
    assert cr.is_deployment_admin_email(None) is False


def test_matcher_falls_back_to_any_admin_without_env(monkeypatch) -> None:
    monkeypatch.delenv(cr.ADMIN_EMAIL_ENV, raising=False)
    assert cr.is_deployment_admin_email("anyone@x.com", is_admin=True) is True
    assert cr.is_deployment_admin_email("anyone@x.com", is_admin=False) is False


# ── reporting ────────────────────────────────────────────────────────────────


def test_nothing_pending_sends_nothing() -> None:
    capture = _Capture()
    result = _report(capture)
    assert result.reported is False and result.pending is False
    assert capture.requests == []


def test_pending_without_config_stays_pending(monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    cr.record_admin_password_rotation()
    result = cr.report_pending_rotations()  # default config resolution → None
    assert result.pending is True and result.reported is False
    assert cr.has_pending() is True


def test_reports_flags_only_and_acknowledges() -> None:
    cr.record_admin_password_rotation()
    cr.record_vault_password_rotation()
    capture = _Capture()
    result = _report(capture)

    assert result.reported is True and result.pending is False
    req = capture.requests[0]
    assert req["url"] == "https://cloud.example/api/v1/health"
    assert req["auth"] == "Bearer olano_dep_test"
    # Booleans only — the new secret never leaves the box.
    assert req["body"] == {"admin_password_rotated": True, "vault_password_rotated": True}

    assert cr.has_pending() is False
    state = cr.load_state()
    assert state["admin_password_reported_at"] and state["vault_password_reported_at"]

    # Acknowledged flags are never re-sent.
    result2 = _report(capture)
    assert result2.reported is False
    assert len(capture.requests) == 1


def test_only_pending_flag_is_sent() -> None:
    cr.record_vault_password_rotation()
    capture = _Capture()
    _report(capture)
    assert capture.requests[0]["body"] == {"vault_password_rotated": True}


def test_server_error_keeps_flags_pending() -> None:
    cr.record_admin_password_rotation()
    capture = _Capture([httpx.Response(500, json={"error": "boom"})])
    result = _report(capture)
    assert result.pending is True and result.reported is False
    assert cr.has_pending() is True
    # Next pass retries and succeeds.
    result2 = _report(capture)
    assert result2.reported is True
    assert cr.has_pending() is False


def test_revoked_token_keeps_flags_pending() -> None:
    cr.record_admin_password_rotation()
    capture = _Capture([httpx.Response(401, json={"error": "invalid_token"})])
    result = _report(capture)
    assert result.pending is True and "unauthorized" in result.error
    assert cr.has_pending() is True


def test_rotation_during_flight_stays_pending() -> None:
    """A rotation recorded while the POST is in flight must be re-reported."""
    cr.record_admin_password_rotation()

    def handler(request: httpx.Request) -> httpx.Response:
        cr.record_admin_password_rotation()  # rotate again mid-flight
        return httpx.Response(200, json={"status": "ok"})

    result = cr.report_pending_rotations(config=_CFG, transport=httpx.MockTransport(handler))
    assert result.reported is True
    # The in-flight rotation is newer than what was acknowledged → still pending.
    assert cr.has_pending() is True


def test_schedule_report_runs_inline_without_loop() -> None:
    cr.record_admin_password_rotation()
    capture = _Capture()
    # No running loop here → schedule_report attempts inline; with no config it
    # must swallow everything and leave the flag pending.
    cr.schedule_report()
    assert cr.has_pending() is True
    assert capture.requests == []


def test_reporting_enabled_gates(monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    monkeypatch.delenv(cr.DISABLE_ENV, raising=False)
    assert cr.reporting_enabled() is False
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_x")
    assert cr.reporting_enabled() is True
    monkeypatch.setenv(cr.DISABLE_ENV, "0")
    assert cr.reporting_enabled() is False


def test_vault_password_rotated_reads_the_stamp() -> None:
    """The onboarding profile step's vault half keys on this, exactly as the
    password half keys on ``admin_password_rotated``."""
    assert cr.vault_password_rotated() is False
    cr.record_admin_password_rotation()  # the other credential is not it
    assert cr.vault_password_rotated() is False
    cr.record_vault_password_rotation()
    assert cr.vault_password_rotated() is True
