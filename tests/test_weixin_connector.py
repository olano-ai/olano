"""Weixin connector credential resolution.

The dashboard form writes ``extra.bot_id`` / ``extra.app_token`` while the
adapter historically read only ``account_id`` / ``token`` — these tests pin the
alias resolution, plus the fallback to the account persisted by the
``olano connect weixin`` QR flow when nothing is configured at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.platforms import weixin as weixin_mod
from olano.platforms.config import PlatformConfig
from olano.platforms.weixin import (
    WeixinAdapter,
    list_persisted_accounts,
    pick_persisted_account,
    save_weixin_account,
)

ENV_VARS = ("WEIXIN_ACCOUNT_ID", "WEIXIN_BOT_ID", "WEIXIN_TOKEN", "WEIXIN_APP_TOKEN")


@pytest.fixture
def weixin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated olano home + no ambient WEIXIN_* env leaking into resolution."""
    monkeypatch.setattr(weixin_mod, "get_olano_home", lambda: tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _adapter(extra: dict | None = None, token: str | None = None) -> WeixinAdapter:
    return WeixinAdapter(PlatformConfig(enabled=True, token=token, extra=extra or {}))


# ── dashboard field-name aliases ─────────────────────────────────────────────


def test_dashboard_field_names_resolve(weixin_home: Path) -> None:
    adapter = _adapter({"bot_id": "wxid_dash", "app_token": "tok_dash"})
    assert adapter._account_id == "wxid_dash"
    assert adapter._token == "tok_dash"


def test_canonical_keys_win_over_aliases(weixin_home: Path) -> None:
    adapter = _adapter(
        {
            "account_id": "wxid_canonical",
            "bot_id": "wxid_alias",
            "token": "tok_canonical",
            "app_token": "tok_alias",
        }
    )
    assert adapter._account_id == "wxid_canonical"
    assert adapter._token == "tok_canonical"


def test_env_var_aliases_resolve(weixin_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEIXIN_BOT_ID", "wxid_env")
    monkeypatch.setenv("WEIXIN_APP_TOKEN", "tok_env")
    adapter = _adapter()
    assert adapter._account_id == "wxid_env"
    assert adapter._token == "tok_env"


def test_config_token_wins_over_extra(weixin_home: Path) -> None:
    adapter = _adapter({"app_token": "tok_extra"}, token="tok_config")
    assert adapter._token == "tok_config"


# ── persisted QR-login account fallback ──────────────────────────────────────


def test_single_persisted_account_fallback(weixin_home: Path) -> None:
    save_weixin_account(
        str(weixin_home),
        account_id="wxid_saved",
        token="tok_saved",
        base_url="https://redirect.example",
    )
    adapter = _adapter()
    assert adapter._account_id == "wxid_saved"
    assert adapter._token == "tok_saved"
    assert adapter._base_url == "https://redirect.example"


def test_fallback_ignores_sidecar_files(weixin_home: Path) -> None:
    """*.sync.json poll buffers and *.context-tokens.json caches share the dir."""
    save_weixin_account(str(weixin_home), account_id="wxid_a", token="tok_a", base_url="")
    accounts = weixin_home / "weixin" / "accounts"
    (accounts / "wxid_a.sync.json").write_text('{"get_updates_buf": "x"}', encoding="utf-8")
    (accounts / "wxid_a.context-tokens.json").write_text("{}", encoding="utf-8")
    adapter = _adapter()
    assert adapter._account_id == "wxid_a"
    assert adapter._token == "tok_a"


def _stamp_saved_at(home: Path, account_id: str, saved_at: str) -> None:
    """Backdate a persisted login so ordering between saves is deterministic."""
    path = home / "weixin" / "accounts" / f"{account_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["saved_at"] = saved_at
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_empty_user_ids_are_never_ambiguous_newest_wins(weixin_home: Path) -> None:
    """iLink returns no user id on QR confirm, so a person who re-scans piles up
    logins with empty user_ids. Those are UNKNOWN, never provably-distinct —
    the newest must win instead of dead-ending the connector. This is the live
    '7 different WeChat logins … none is pinned' bug: all 7 were one person's
    re-scans with empty user ids."""
    for i in range(7):
        save_weixin_account(str(weixin_home), account_id=f"wxid_{i}", token=f"tok_{i}", base_url="")
        _stamp_saved_at(weixin_home, f"wxid_{i}", f"2026-08-0{i + 1}T00:00:00Z")
    adapter = _adapter()
    assert adapter._account_id == "wxid_6"  # newest by saved_at
    assert adapter._token == "tok_6"
    assert adapter._startup_problem is None


def test_relogin_of_same_wechat_account_supersedes(weixin_home: Path) -> None:
    """A QR re-scan mints a NEW iLink bot id for the same WeChat user (same
    ilink_user_id). The stale login must not dead-end the connector — the
    newest session wins. This is the live 'no bot token even though the QR
    login just succeeded' bug."""
    save_weixin_account(
        str(weixin_home), account_id="wxid_old", token="tok_old", base_url="", user_id="u1"
    )
    _stamp_saved_at(weixin_home, "wxid_old", "2026-01-01T00:00:00Z")
    save_weixin_account(
        str(weixin_home), account_id="wxid_new", token="tok_new", base_url="", user_id="u1"
    )
    _stamp_saved_at(weixin_home, "wxid_new", "2026-08-01T00:00:00Z")
    adapter = _adapter()
    assert adapter._account_id == "wxid_new"
    assert adapter._token == "tok_new"
    assert adapter._startup_problem is None


def test_two_different_wechat_users_stay_ambiguous(weixin_home: Path) -> None:
    save_weixin_account(
        str(weixin_home), account_id="wxid_a", token="tok_a", base_url="", user_id="u1"
    )
    _stamp_saved_at(weixin_home, "wxid_a", "2026-01-01T00:00:00Z")
    save_weixin_account(
        str(weixin_home), account_id="wxid_b", token="tok_b", base_url="", user_id="u2"
    )
    _stamp_saved_at(weixin_home, "wxid_b", "2026-08-01T00:00:00Z")
    assert pick_persisted_account(str(weixin_home)) is None


def test_explicit_account_loads_its_persisted_token(weixin_home: Path) -> None:
    save_weixin_account(str(weixin_home), account_id="wxid_a", token="tok_a", base_url="")
    save_weixin_account(str(weixin_home), account_id="wxid_b", token="tok_b", base_url="")
    adapter = _adapter({"bot_id": "wxid_b"})
    assert adapter._account_id == "wxid_b"
    assert adapter._token == "tok_b"


def test_unknown_pinned_account_reports_linked_ones(weixin_home: Path) -> None:
    """A bot_id pointing at no saved login must say which logins DO exist —
    'no bot token, scan the QR' sends the user in the wrong direction."""
    save_weixin_account(str(weixin_home), account_id="wxid_real", token="tok", base_url="")
    adapter = _adapter({"bot_id": "wxid_typo"})
    assert adapter._token == ""
    assert adapter._startup_problem is not None
    code, message = adapter._startup_problem
    assert code == "weixin_unknown_account"
    assert "wxid_typo" in message and "wxid_real" in message


async def test_connect_reports_the_startup_problem(weixin_home: Path) -> None:
    """The fatal error surfaced in diagnostics must be the specific problem,
    not the generic 'no bot token' whose QR advice mints yet another login."""
    save_weixin_account(
        str(weixin_home), account_id="wxid_a", token="tok_a", base_url="", user_id="u1"
    )
    _stamp_saved_at(weixin_home, "wxid_a", "2026-01-01T00:00:00Z")
    save_weixin_account(
        str(weixin_home), account_id="wxid_b", token="tok_b", base_url="", user_id="u2"
    )
    _stamp_saved_at(weixin_home, "wxid_b", "2026-08-01T00:00:00Z")
    adapter = _adapter()
    assert await adapter.connect() is False
    assert adapter.fatal_error_code == "weixin_ambiguous_account"
    assert adapter.fatal_error_message is not None
    assert "wxid_a" in adapter.fatal_error_message
    assert "scan the QR" not in adapter.fatal_error_message


# ── per-agent login isolation ────────────────────────────────────────────────


def _agent_adapter(agent_name: str, extra: dict | None = None) -> WeixinAdapter:
    return WeixinAdapter(PlatformConfig(enabled=True, extra=extra or {}), agent_name=agent_name)


async def test_two_agents_cannot_share_one_login(weixin_home: Path) -> None:
    """A single WeChat login must not be split across two agents — each
    getupdates poll drains the same queue, so messages would scatter between
    them. The second agent to connect refuses with a specific error."""
    save_weixin_account(str(weixin_home), account_id="wxid_shared", token="tok_shared", base_url="")
    a = _agent_adapter("agent-a", {"bot_id": "wxid_shared"})
    b = _agent_adapter("agent-b", {"bot_id": "wxid_shared"})
    try:
        assert await a.connect() is True
        assert await b.connect() is False
        assert b.fatal_error_code == "weixin_login_in_use"
        assert b.fatal_error_message is not None
        assert "agent-a" in b.fatal_error_message
    finally:
        await a.disconnect()


async def test_same_agent_reconnect_replaces_its_registration(weixin_home: Path) -> None:
    """A hot swap / reload builds a fresh adapter for the SAME agent; it must
    take over the login, not deadlock against its own prior registration."""
    save_weixin_account(str(weixin_home), account_id="wxid_x", token="tok_x", base_url="")
    first = _agent_adapter("agent-a", {"bot_id": "wxid_x"})
    second = _agent_adapter("agent-a", {"bot_id": "wxid_x"})
    try:
        assert await first.connect() is True
        # Prior registration still live, same agent name → allowed.
        assert await second.connect() is True
        assert second.fatal_error_code is None
    finally:
        await first.disconnect()
        await second.disconnect()


def test_pick_persisted_account_helper(weixin_home: Path) -> None:
    assert pick_persisted_account(str(weixin_home)) is None
    save_weixin_account(str(weixin_home), account_id="wxid_a", token="tok_a", base_url="")
    found = pick_persisted_account(str(weixin_home))
    assert found is not None
    assert found[0] == "wxid_a"
    assert found[1]["token"] == "tok_a"
    # A second login with an unprovable owner (no user_id) is NOT ambiguous —
    # the newest wins. Distinct non-empty user ids are what force a pin.
    save_weixin_account(str(weixin_home), account_id="wxid_b", token="tok_b", base_url="")
    found2 = pick_persisted_account(str(weixin_home))
    assert found2 is not None  # picks the newest, never blocks on empty user ids
    assert [aid for aid, _ in list_persisted_accounts(str(weixin_home))].count("wxid_a") == 1


# ── QR login event flow ──────────────────────────────────────────────────────


def _script_api(monkeypatch: pytest.MonkeyPatch, responses: list[dict]) -> None:
    """Feed qr_login_events a scripted sequence of iLink API responses."""

    async def fake_api_get(session, *, base_url, endpoint, timeout_ms):
        return responses.pop(0)

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(weixin_mod, "_api_get", fake_api_get)
    monkeypatch.setattr(weixin_mod.asyncio, "sleep", fast_sleep)


async def test_qr_login_events_happy_path(
    weixin_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_api(
        monkeypatch,
        [
            {"qrcode": "hex1", "qrcode_img_content": "https://scan.example/1"},
            {"status": "wait"},
            {"status": "scaned"},
            {
                "status": "confirmed",
                "ilink_bot_id": "wxid_new",
                "bot_token": "tok_new",
                "baseurl": "https://base.example",
                "ilink_user_id": "u1",
            },
        ],
    )
    events = [e async for e in weixin_mod.qr_login_events(str(weixin_home))]
    kinds = [e["event"] for e in events]
    assert kinds == ["qr", "status", "status", "confirmed"]
    assert events[0]["qr"] == "https://scan.example/1"
    assert events[2]["status"] == "scanned"
    assert events[3]["account_id"] == "wxid_new"
    # Confirmation must persist the account for the adapter fallback to find.
    found = pick_persisted_account(str(weixin_home))
    assert found is not None and found[0] == "wxid_new"
    assert found[1]["token"] == "tok_new"


async def test_qr_login_events_refresh_on_expiry(
    weixin_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_api(
        monkeypatch,
        [
            {"qrcode": "hex1", "qrcode_img_content": "https://scan.example/1"},
            {"status": "expired"},
            {"qrcode": "hex2", "qrcode_img_content": "https://scan.example/2"},
            {
                "status": "confirmed",
                "ilink_bot_id": "wxid_r",
                "bot_token": "tok_r",
                "baseurl": "",
                "ilink_user_id": "",
            },
        ],
    )
    events = [e async for e in weixin_mod.qr_login_events(str(weixin_home))]
    qr_events = [e for e in events if e["event"] == "qr"]
    assert [e["qr"] for e in qr_events] == ["https://scan.example/1", "https://scan.example/2"]
    assert qr_events[1]["refresh"] == 1
    assert events[-1]["event"] == "confirmed"


async def test_qr_login_events_timeout(weixin_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _script_api(monkeypatch, [{"qrcode": "hex1", "qrcode_img_content": ""}])
    events = [e async for e in weixin_mod.qr_login_events(str(weixin_home), timeout_seconds=0)]
    assert events[0]["event"] == "qr"
    assert events[0]["qr"] == "hex1"  # falls back to the hex token without a URL
    assert events[-1]["event"] == "error"


async def test_qr_login_wrapper_returns_credentials(
    weixin_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_api(
        monkeypatch,
        [
            {"qrcode": "hex1", "qrcode_img_content": "https://scan.example/1"},
            {
                "status": "confirmed",
                "ilink_bot_id": "wxid_cli",
                "bot_token": "tok_cli",
                "baseurl": "https://base.example",
                "ilink_user_id": "u9",
            },
        ],
    )
    creds = await weixin_mod.qr_login(str(weixin_home))
    assert creds == {
        "account_id": "wxid_cli",
        "token": "tok_cli",
        "base_url": "https://base.example",
        "user_id": "u9",
    }


# ── dashboard SSE pairing endpoint ───────────────────────────────────────────


def test_weixin_pair_stream_never_leaks_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSE stream forwards QR + status + account_id — never the bot token."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))

    async def fake_events(hermes_home: str, **kwargs):
        yield {"event": "qr", "qr": "https://scan.example/1", "refresh": 0}
        yield {"event": "status", "status": "scanned"}
        yield {
            "event": "confirmed",
            "account_id": "wxid_sse",
            "token": "SECRET_BOT_TOKEN",
            "base_url": "https://base.example",
            "user_id": "u1",
        }

    monkeypatch.setattr(weixin_mod, "qr_login_events", fake_events)

    from olano.runtime.api import create_api

    client = TestClient(create_api(MagicMock()))
    resp = client.get("/api/connectors/weixin/pair?timeout=60")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "SECRET_BOT_TOKEN" not in body
    assert '"account_id": "wxid_sse"' in body.replace("data: ", "") or "wxid_sse" in body
    assert '"type": "qr"' in body
    assert '"ok": true' in body


def test_weixin_pair_pins_account_to_the_form_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pairing from an agent's saved connector writes the minted account id into
    that agent's config, so multiple agents each keep their own login."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))

    agent_dir = home / "agents" / "alice"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.json5").write_text(
        '{"name": "alice", "channels": [{"type": "platform:weixin"}]}',
        encoding="utf-8",
    )

    async def fake_events(hermes_home: str, **kwargs):
        yield {"event": "qr", "qr": "https://scan.example/1", "refresh": 0}
        yield {
            "event": "confirmed",
            "account_id": "wxid_alice",
            "token": "SECRET",
            "base_url": "",
            "user_id": "u1",
        }

    monkeypatch.setattr(weixin_mod, "qr_login_events", fake_events)

    from olano.config.loader import load_agent_config
    from olano.runtime.api import create_api

    client = TestClient(create_api(MagicMock()))
    resp = client.get("/api/connectors/weixin/pair?timeout=60&agent=alice")
    assert resp.status_code == 200
    assert '"pinned": true' in resp.text

    cfg = load_agent_config("alice")
    weixin = next(c for c in cfg.channels if getattr(c, "type", "") == "platform:weixin")
    assert (weixin.extra or {}).get("bot_id") == "wxid_alice"


def test_weixin_pair_without_saved_connector_does_not_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No saved platform:weixin entry (form still open) → nothing to pin; the
    blank-field fallback covers that agent, so the stream reports pinned=false."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    agent_dir = home / "agents" / "bob"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.json5").write_text('{"name": "bob", "channels": []}', encoding="utf-8")

    async def fake_events(hermes_home: str, **kwargs):
        yield {
            "event": "confirmed",
            "account_id": "wxid_bob",
            "token": "SECRET",
            "base_url": "",
            "user_id": "u1",
        }

    monkeypatch.setattr(weixin_mod, "qr_login_events", fake_events)

    from olano.runtime.api import create_api

    client = TestClient(create_api(MagicMock()))
    resp = client.get("/api/connectors/weixin/pair?timeout=60&agent=bob")
    assert resp.status_code == 200
    assert '"pinned": false' in resp.text


# ── connector build_entry pins the paired login ──────────────────────────────


def test_weixin_connector_build_entry_pins_identity() -> None:
    """The channel-connect self-service flow pins the paired account id into
    extra.bot_id so each customer's agent keeps its own WeChat login."""
    from olano.platforms.channel_setup import get_connector

    connector = get_connector("wechat")
    assert connector is not None
    entry = connector.build_entry({}, identity="wxid_customer")
    assert entry["type"] == "platform:weixin"
    assert entry["extra"]["bot_id"] == "wxid_customer"
    # No identity (e.g. token connectors) → no pin.
    assert "extra" not in connector.build_entry({})


# ── CLI registration ─────────────────────────────────────────────────────────


def test_connect_weixin_registered() -> None:
    from olano.cli.connect_cmd import CONNECT_HANDLERS, supported_platforms

    assert "weixin" in supported_platforms()
    assert CONNECT_HANDLERS["weixin"].__name__ == "connect_weixin"


def test_connect_weixin_help_runs() -> None:
    from click.testing import CliRunner

    from olano.cli.main import cli

    result = CliRunner().invoke(cli, ["connect", "weixin", "--help"])
    assert result.exit_code == 0
    assert "QR" in result.output


# ── vault credential reaches the adapter ─────────────────────────────────────


def test_hydrated_credential_is_folded_into_extra() -> None:
    """`hydrate_channel_credentials` writes the credential at the config's top
    level, but `PlatformConfig.from_dict` keeps only a fixed set of top-level
    keys and the adapters read `extra` — so the boot path must fold it across.
    """
    from olano.config.channel_secrets import credential_fields
    from olano.runtime.server import _fold_credentials_into_extra

    assert credential_fields("platform:weixin") == ("app_token",)

    cfg = _fold_credentials_into_extra(
        "weixin", {"extra": {"bot_id": "wxid_x"}, "app_token": "TOKEN_FROM_VAULT"}
    )
    pc = PlatformConfig.from_dict({"enabled": True, **cfg})
    assert pc.extra["app_token"] == "TOKEN_FROM_VAULT"
    assert pc.extra["bot_id"] == "wxid_x"
    # …and the adapter's resolver reads it under the dashboard's name.
    assert weixin_mod.resolve_weixin_token(pc.extra) == "TOKEN_FROM_VAULT"


def test_explicit_connector_credential_beats_the_vault() -> None:
    """A value typed into the connector is deliberate; the vault key is only
    the default, so the fold must not overwrite it."""
    from olano.runtime.server import _fold_credentials_into_extra

    cfg = _fold_credentials_into_extra(
        "weixin", {"extra": {"app_token": "FROM_CONNECTOR"}, "app_token": "FROM_VAULT"}
    )
    assert cfg["extra"]["app_token"] == "FROM_CONNECTOR"


def test_fold_leaves_top_level_credential_fields_alone() -> None:
    """`token` / `api_key` are named PlatformConfig fields — the adapters that
    read them would break if the fold moved them into `extra`."""
    from olano.runtime.server import _fold_credentials_into_extra

    cfg = _fold_credentials_into_extra("api_server", {"api_key": "KEY", "extra": {}})
    assert PlatformConfig.from_dict({"enabled": True, **cfg}).api_key == "KEY"

    cfg = _fold_credentials_into_extra("telegram", {"token": "TOK", "extra": {}})
    assert PlatformConfig.from_dict({"enabled": True, **cfg}).token == "TOK"


# ── long-poll timeouts are not errors ────────────────────────────────────────


def test_poll_timeout_classification() -> None:
    """An idle long poll ends in a gateway timeout — that is what a quiet chat
    looks like, not a fault. Live symptom: `poll error (1/3): … HTTP 524` logged
    at ERROR every ~25s on a working connector (agent=andrew).
    """
    from olano.platforms.weixin import _is_poll_timeout

    # Cloudflare fronts ilinkai; 524 is the one actually seen in production.
    assert _is_poll_timeout(RuntimeError("iLink POST ilink/bot/getupdates HTTP 524: "))
    assert _is_poll_timeout(RuntimeError("iLink POST ilink/bot/getupdates HTTP 504: "))
    assert _is_poll_timeout(TimeoutError())

    # A real rejection must stay an error — these are the ones worth waking for.
    assert not _is_poll_timeout(RuntimeError("iLink POST ilink/bot/getupdates HTTP 401: "))
    assert not _is_poll_timeout(RuntimeError("iLink POST ilink/bot/getupdates HTTP 500: "))
    assert not _is_poll_timeout(ValueError("malformed payload"))
