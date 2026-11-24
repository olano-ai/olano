"""The first-run setup code: an unmanaged box with no accounts creates its first
admin only for someone holding the one-time code (runtime/setup_code.py).

The suite switches the rule off by default (tests/conftest.py,
``setup_code_not_enforced``); this module overrides that fixture to switch it
back on.
"""

from __future__ import annotations

import logging
import stat
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime import setup_code
from olano.runtime.api import create_api


@pytest.fixture(autouse=True)
def setup_code_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the suite-wide off switch: enforcement is the subject here."""
    monkeypatch.setattr(setup_code, "ENFORCED", True)
    monkeypatch.delenv("OLANO_SETUP_CODE", raising=False)
    monkeypatch.delenv("LIONCLAW_SETUP_CODE", raising=False)
    setup_code.reset_attempts_for_tests()


@pytest.fixture
def client() -> TestClient:
    runtime = MagicMock()
    runtime._runners = {}
    return TestClient(create_api(runtime))


def _bootstrap(client: TestClient, code: str | None = None, email: str = "admin@x.com"):
    body = {"email": email, "password": "pw-long-enough", "name": "Admin"}
    if code is not None:
        body["setup_code"] = code
    return client.post("/api/auth/bootstrap", json=body)


def test_code_shape_is_three_unambiguous_groups() -> None:
    code = setup_code.generate()
    groups = code.split("-")
    assert [len(g) for g in groups] == [4, 4, 4]
    assert set("".join(groups)) <= set(setup_code.ALPHABET)
    assert not set("01OIL") & set(setup_code.ALPHABET)


def test_setup_status_reports_the_requirement(client: TestClient) -> None:
    res = client.get("/api/auth/setup-status")
    assert res.status_code == 200
    assert res.json() == {"setup_code_required": True}


def test_missing_code_is_refused(client: TestClient) -> None:
    res = _bootstrap(client)
    assert res.status_code == 403
    assert res.json()["detail"] == "setup_code_required"


def test_wrong_code_is_refused(client: TestClient) -> None:
    setup_code.expected_code()
    res = _bootstrap(client, "AAAA-AAAA-AAAA")
    assert res.status_code == 403
    assert res.json()["detail"] == "setup_code_invalid"


def test_generated_code_is_private_and_accepted_loosely_typed(client: TestClient) -> None:
    code = setup_code.expected_code()
    path = setup_code.code_path()
    assert path.read_text().strip() == code
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Lowercase, spaces instead of dashes: still the same code.
    res = _bootstrap(client, code.lower().replace("-", " "))
    assert res.status_code == 200, res.text
    assert res.json()["user"]["is_admin"] is True


def test_code_is_consumed_by_the_first_admin(client: TestClient) -> None:
    code = setup_code.expected_code()
    assert _bootstrap(client, code).status_code == 200
    assert not setup_code.code_path().exists()
    assert setup_code.required() is False
    assert client.get("/api/auth/setup-status").json() == {"setup_code_required": False}
    # A second bootstrap is refused as before, whatever it carries.
    assert _bootstrap(client, code, email="other@x.com").status_code == 409


def test_spent_code_never_comes_back(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLANO_SETUP_CODE", "ABCD-EFGH-JKMN")
    assert setup_code.expected_code() == "ABCD-EFGH-JKMN"
    setup_code.consume()
    # The users table is still empty here (consume ran by hand), so a code is
    # still needed, but the environment's value is spent: a fresh one is minted.
    fresh = setup_code.expected_code()
    assert setup_code.normalize(fresh) != "ABCDEFGHJKMN"


def test_environment_code_wins(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLANO_SETUP_CODE", "WXYZ-2345-6789")
    assert _bootstrap(client, "AAAA-BBBB-CCCC").json()["detail"] == "setup_code_invalid"
    assert _bootstrap(client, "wxyz-2345-6789").status_code == 200
    assert not setup_code.code_path().exists()


def test_environment_code_turns_sign_in_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box an installer gave a code is reachable by others: anonymous visitors
    get the setup screen, not the implicit owner's admin powers."""
    from olano.runtime.auth import multi_user_enabled

    assert multi_user_enabled() is False
    monkeypatch.setenv("OLANO_SETUP_CODE", "WXYZ-2345-6789")
    assert multi_user_enabled() is True
    res = client.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["detail"] == "setup_required"
    assert client.get("/api/agents").json()["detail"] == "setup_required"
    # The setup screen's own probes stay reachable.
    assert client.get("/api/auth/setup-status").json() == {"setup_code_required": True}


def test_wrong_attempts_are_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_code, "MAX_WRONG_ATTEMPTS", 3)
    code = setup_code.expected_code()
    for _ in range(3):
        assert _bootstrap(client, "AAAA-AAAA-AAAA").status_code == 403
    limited = _bootstrap(client, code)
    assert limited.status_code == 429
    assert limited.json()["detail"] == "setup_code_rate_limited"
    assert int(limited.headers["Retry-After"]) > 0


def test_managed_provisioning_is_unaffected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box provisioned from LIONCLAW_ADMIN_PASSWORD is the managed path."""
    monkeypatch.setenv("LIONCLAW_ADMIN_PASSWORD", "provisioned-pw")
    assert setup_code.applies() is False
    assert setup_code.required() is False
    assert _bootstrap(client).status_code == 200
    assert not setup_code.code_path().exists()


def test_managed_posture_is_unaffected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from olano.deployment import resolver

    status = MagicMock()
    status.posture = "enforced"
    monkeypatch.setattr(resolver, "deployment_status", lambda: status)
    assert setup_code.required() is False
    assert client.get("/api/auth/setup-status").json() == {"setup_code_required": False}


def test_boot_announcement_logs_the_code(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="olano.runtime.setup_code"):
        setup_code.announce_at_boot()
    code = setup_code.code_path().read_text().strip()
    assert code in caplog.text
    assert "FIRST-RUN SETUP CODE" in caplog.text


def test_boot_announcement_names_the_environment_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OLANO_SETUP_CODE", "WXYZ-2345-6789")
    with caplog.at_level(logging.WARNING, logger="olano.runtime.setup_code"):
        setup_code.announce_at_boot()
    assert "WXYZ-2345-6789" in caplog.text
    assert not setup_code.code_path().exists()


def test_boot_announcement_is_quiet_once_an_account_exists(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    code = setup_code.expected_code(announce=False)
    assert _bootstrap(client, code).status_code == 200
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="olano.runtime.setup_code"):
        setup_code.announce_at_boot()
    assert "FIRST-RUN SETUP CODE" not in caplog.text


def test_agent_shells_never_see_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from olano.security.shell_guard import sanitized_shell_env

    env = sanitized_shell_env(
        {"OLANO_SETUP_CODE": "WXYZ-2345-6789", "LIONCLAW_SETUP_CODE": "WXYZ-2345-6789"}
    )
    assert "OLANO_SETUP_CODE" not in env
    assert "LIONCLAW_SETUP_CODE" not in env
