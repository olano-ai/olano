"""Channel-connect: the connector registry and its magic-link flow.

Token connectors are tested with the HTTP hop mocked (the translation is what
matters: platform response → identity string / user-safe refusal). The flow is
tested store-less (LinkRecord fallback path) with the admin write path mocked.
"""

from __future__ import annotations

import pytest

from olano.magic_links import flows
from olano.magic_links.store import STATUS_DONE, LinkRecord
from olano.platforms import channel_setup


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)


class _FakeCfg:
    def __init__(self, channels=None):
        self.channels = channels or []
        self.system_prompt = ""


def _link(**params) -> LinkRecord:
    defaults = {
        "agent": "merli",
        "platforms": ["telegram", "whatsapp"],
        "active": "",
        "completed": {},
    }
    defaults.update(params)
    return LinkRecord(id="hs_conn", token_hash="x", flow="channel-connect", params=defaults)


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_covers_the_launch_platforms():
    keys = {c.platform for c in channel_setup.all_connectors()}
    assert {"telegram", "discord", "slack", "whatsapp", "wechat"} <= keys
    for connector in channel_setup.all_connectors():
        assert connector.label and connector.channel_type.startswith("platform:")
        assert connector.mode in ("token", "qr")
        if connector.mode == "token":
            assert connector.fields, f"{connector.platform} token connector needs fields"


async def test_telegram_validate_maps_the_bot_identity(monkeypatch):
    async def fake_http(method, url, **kw):
        assert "/getMe" in url
        return 200, {"ok": True, "result": {"username": "HerShopBot"}}

    monkeypatch.setattr(channel_setup, "_http_json", fake_http)
    connector = channel_setup.get_connector("telegram")
    assert await connector.validate({"token": "123:AA"}) == "@HerShopBot"


async def test_telegram_validate_rejects_bad_tokens(monkeypatch):
    connector = channel_setup.get_connector("telegram")
    with pytest.raises(ValueError, match="doesn't look like"):
        await connector.validate({"token": "no-colon"})

    async def unauthorized(method, url, **kw):
        return 401, {"ok": False}

    monkeypatch.setattr(channel_setup, "_http_json", unauthorized)
    with pytest.raises(ValueError, match="rejected"):
        await connector.validate({"token": "123:BAD"})


async def test_slack_validate_checks_both_token_shapes():
    connector = channel_setup.get_connector("slack")
    with pytest.raises(ValueError, match="xoxb"):
        await connector.validate({"bot_token": "nope", "app_token": "xapp-1"})
    with pytest.raises(ValueError, match="xapp"):
        await connector.validate({"bot_token": "xoxb-1", "app_token": "nope"})


def test_build_entry_carries_type_and_credentials():
    connector = channel_setup.get_connector("telegram")
    entry = connector.build_entry({"token": "123:AA"})
    assert entry == {"type": "platform:telegram", "token": "123:AA"}


def test_qr_svg_data_uri_encodes():
    uri = channel_setup.qr_svg_data_uri("https://wa.me/qr/abc")
    assert uri.startswith("data:image/svg+xml")
    assert channel_setup.qr_svg_data_uri("") == ""


# ── flow: mint validation ────────────────────────────────────────────────────


def test_flow_validate_normalizes_platforms(monkeypatch):
    import olano.config.loader as loader

    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeCfg())
    flow = flows.get_flow("channel-connect")
    out = flow.validate({"agent": "merli", "platforms": "WhatsApp, wechat"})
    assert out["platforms"] == ["whatsapp", "wechat"]
    out = flow.validate({"agent": "merli", "platforms": ["telegram"]})
    assert out["platforms"] == ["telegram"]


def test_flow_validate_refuses_unknown_platform_and_empty(monkeypatch):
    import olano.config.loader as loader

    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeCfg())
    flow = flows.get_flow("channel-connect")
    with pytest.raises(ValueError, match="Unknown platform"):
        flow.validate({"agent": "merli", "platforms": ["icq"]})
    with pytest.raises(ValueError, match="at least one platform"):
        flow.validate({"agent": "merli", "platforms": []})


def test_flow_validate_keeps_operator_entry_overrides(monkeypatch):
    import olano.config.loader as loader

    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeCfg())
    out = flows.get_flow("channel-connect").validate(
        {
            "agent": "merli",
            "platforms": ["telegram"],
            "entries": {"Telegram": {"allowed_users": [123], "default_chat_id": "-1001"}},
        }
    )
    assert out["entries"]["telegram"] == {"allowed_users": [123], "default_chat_id": "-1001"}


# ── flow: page state + actions ───────────────────────────────────────────────


def test_state_lists_platforms_with_status():
    flow = flows.get_flow("channel-connect")
    state = flow.state(_link(completed={"telegram": "connected as @HerShopBot"}))
    rows = {p["platform"]: p for p in state["platforms"]}
    assert rows["telegram"]["status"] == "done"
    assert rows["whatsapp"]["status"] == "pending"
    assert state["any_completed"] is True


async def test_open_token_platform_shows_its_form():
    flow = flows.get_flow("channel-connect")
    link = _link()
    await flow.submit(link, {"_action": "open", "platform": "telegram"})
    state = flow.state(link)
    assert state["active"]["platform"] == "telegram"
    assert state["active"]["mode"] == "token"
    assert [f["name"] for f in state["active"]["fields"]] == ["token"]


async def test_open_refuses_platforms_not_on_the_link():
    flow = flows.get_flow("channel-connect")
    with pytest.raises(ValueError, match="isn't offered"):
        await flow.submit(_link(platforms=["telegram"]), {"_action": "open", "platform": "slack"})


async def test_save_validates_persists_and_marks_completed(monkeypatch):
    import olano.admin.agents as admin_agents
    import olano.config.loader as loader

    async def fake_http(method, url, **kw):
        return 200, {"ok": True, "result": {"username": "HerShopBot"}}

    monkeypatch.setattr(channel_setup, "_http_json", fake_http)
    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeCfg())
    written: dict = {}
    monkeypatch.setattr(
        admin_agents, "update_agent", lambda name, updates: written.update({name: updates})
    )

    flow = flows.get_flow("channel-connect")
    link = _link(active="telegram")
    await flow.submit(link, {"_action": "save", "platform": "telegram", "token": "123:AA"})

    channels = written["merli"]["channels"]
    assert channels == [{"type": "platform:telegram", "token": "123:AA"}]
    assert link.params["completed"]["telegram"] == "connected as @HerShopBot"
    assert link.params["active"] == ""


async def test_save_merges_operator_overrides_into_the_entry(monkeypatch):
    import olano.admin.agents as admin_agents
    import olano.config.loader as loader

    async def fake_http(method, url, **kw):
        return 200, {"ok": True, "result": {"username": "HerShopBot"}}

    monkeypatch.setattr(channel_setup, "_http_json", fake_http)
    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeCfg())
    written: dict = {}
    monkeypatch.setattr(
        admin_agents, "update_agent", lambda name, updates: written.update({name: updates})
    )

    link = _link(entries={"telegram": {"allowed_users": [123456789]}})
    await flows.get_flow("channel-connect").submit(
        link, {"_action": "save", "platform": "telegram", "token": "123:AA"}
    )
    (entry,) = written["merli"]["channels"]
    assert entry["allowed_users"] == [123456789]


async def test_save_requires_every_field():
    flow = flows.get_flow("channel-connect")
    with pytest.raises(ValueError, match="Missing"):
        await flow.submit(_link(), {"_action": "save", "platform": "telegram", "token": " "})


async def test_finish_requires_at_least_one_connection():
    flow = flows.get_flow("channel-connect")
    with pytest.raises(ValueError, match="Nothing is connected"):
        await flow.submit(_link(), {"_action": "finish"})
    done = await flow.submit(
        _link(completed={"telegram": "connected as @HerShopBot"}), {"_action": "finish"}
    )
    assert done["status"] == STATUS_DONE
    assert "@HerShopBot" in done["result"]


def test_persist_replaces_same_type_and_appends_new(monkeypatch):
    import olano.admin.agents as admin_agents
    import olano.config.loader as loader
    from olano.config.agent_config import ChannelConfig
    from olano.magic_links.adapters.channel_connect import _persist_channel_entry

    existing = [
        ChannelConfig(type="platform:telegram", token="old", allowed_users=[42]),
        ChannelConfig(type="platform:discord", token="d"),
    ]
    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeCfg(channels=existing))
    written: dict = {}
    monkeypatch.setattr(
        admin_agents, "update_agent", lambda name, updates: written.update({name: updates})
    )

    _persist_channel_entry("merli", {"type": "platform:telegram", "token": "new"})
    channels = written["merli"]["channels"]
    tele = next(c for c in channels if c["type"] == "platform:telegram")
    # Replaced token, preserved the operator's other keys (allowlist!).
    assert tele["token"] == "new"
    assert tele["allowed_users"] == [42]
    assert len(channels) == 2

    _persist_channel_entry("merli", {"type": "platform:slack", "bot_token": "xoxb-1"})
    assert len(written["merli"]["channels"]) == 3
