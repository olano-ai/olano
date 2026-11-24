"""Tests for the `olano login` read-only credential-check command."""

from __future__ import annotations

from click.testing import CliRunner

from olano.cli.main import cli
from olano.users.store import get_user_store


def _make_user(email: str = "owner@example.com", password: str = "s3cret", is_admin: bool = True):
    return get_user_store().create_user(email=email, password=password, is_admin=is_admin)


def test_login_success_with_flag() -> None:
    _make_user()
    result = CliRunner().invoke(cli, ["login", "owner@example.com", "-p", "s3cret"])
    assert result.exit_code == 0
    assert "Authenticated as owner@example.com (admin)" in result.output


def test_login_non_admin_role_label() -> None:
    _make_user(email="member@example.com", password="pw", is_admin=False)
    result = CliRunner().invoke(cli, ["login", "member@example.com", "-p", "pw"])
    assert result.exit_code == 0
    assert "(user)" in result.output


def test_login_wrong_password() -> None:
    _make_user()
    result = CliRunner().invoke(cli, ["login", "owner@example.com", "-p", "wrong"])
    assert result.exit_code == 1
    assert "Invalid email or password" in result.output


def test_login_unknown_email() -> None:
    _make_user()
    result = CliRunner().invoke(cli, ["login", "nobody@example.com", "-p", "whatever"])
    assert result.exit_code == 1
    assert "Invalid email or password" in result.output


def test_login_no_users() -> None:
    result = CliRunner().invoke(cli, ["login", "owner@example.com", "-p", "s3cret"])
    assert result.exit_code == 1
    assert "No users exist" in result.output


def test_login_prompts_for_email_and_password() -> None:
    _make_user()
    result = CliRunner().invoke(cli, ["login"], input="owner@example.com\ns3cret\n")
    assert result.exit_code == 0
    assert "Authenticated as owner@example.com" in result.output


def test_login_prompts_for_password_only() -> None:
    _make_user()
    result = CliRunner().invoke(cli, ["login", "owner@example.com"], input="s3cret\n")
    assert result.exit_code == 0
    assert "Authenticated as owner@example.com" in result.output


def test_login_inactive_account() -> None:
    # Pending invite → inactive user; authenticate fails and the message says so.
    get_user_store().invite_user(email="pending@example.com")
    result = CliRunner().invoke(cli, ["login", "pending@example.com", "-p", "anything"])
    assert result.exit_code == 1
    assert "inactive" in result.output.lower()
