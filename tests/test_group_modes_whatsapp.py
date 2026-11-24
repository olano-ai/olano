"""WhatsApp wiring for the uniform group listen/respond modes.

Mirrors the Telegram reference (``tests/test_group_modes.py``): mode
resolution (legacy ``require_mention`` mapping, new knobs winning), the
``group_respond`` gate in ``_should_process_message``, and the
``group_listen`` recording hook (``_maybe_record_observed`` → persistent
observed-messages store).

``record_observed_event`` writes inline when no event loop is running, so the
sync tests here observe the store immediately after the call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import PlatformConfig
from olano.platforms.whatsapp import WhatsAppAdapter

AGENT = "wa-modes"
GROUP_CHAT = "120363012345678901@g.us"
BOT_ID = "6511111111@s.whatsapp.net"


def _adapter(tmp_path, monkeypatch, extra: dict[str, Any] | None = None) -> WhatsAppAdapter:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("WHATSAPP_REQUIRE_MENTION", raising=False)
    monkeypatch.delenv("WHATSAPP_FREE_RESPONSE_CHATS", raising=False)
    monkeypatch.delenv("WHATSAPP_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("WHATSAPP_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("WHATSAPP_MENTION_PATTERNS", raising=False)
    return WhatsAppAdapter(PlatformConfig(enabled=True, extra=extra or {}), agent_name=AGENT)


def _group_message(body: str = "just chatting", **overrides: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "isGroup": True,
        "chatId": GROUP_CHAT,
        "chatName": "Community Group",
        "senderId": "12025550188@s.whatsapp.net",
        "senderName": "Ada Lovelace",
        "body": body,
        "botIds": [BOT_ID],
        "mentionedIds": [],
    }
    msg.update(overrides)
    return msg


def _dm_message(body: str = "hi") -> dict[str, Any]:
    return {
        "isGroup": False,
        "chatId": "12025550188@s.whatsapp.net",
        "senderId": "12025550188@s.whatsapp.net",
        "senderName": "Ada Lovelace",
        "body": body,
        "botIds": [BOT_ID],
        "mentionedIds": [],
    }


# ─── Mode resolution ─────────────────────────────────────────────────────────


class TestModeResolution:
    def test_default_modes(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch)
        assert a._whatsapp_group_respond() == "all"  # legacy default: reply to everything
        assert a._whatsapp_group_listen() == "off"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [(True, "mentions"), ("true", "mentions"), (False, "all"), ("false", "all")],
    )
    def test_legacy_require_mention_mapping(self, tmp_path, monkeypatch, configured, expected):
        a = _adapter(tmp_path, monkeypatch, {"require_mention": configured})
        assert a._whatsapp_group_respond() == expected

    def test_group_respond_wins_over_require_mention(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"require_mention": True, "group_respond": "all"})
        assert a._whatsapp_group_respond() == "all"
        a = _adapter(tmp_path, monkeypatch, {"require_mention": False, "group_respond": "none"})
        assert a._whatsapp_group_respond() == "none"

    def test_listen_knob_and_no_legacy_passive_listen(self, tmp_path, monkeypatch):
        assert (
            _adapter(tmp_path, monkeypatch, {"group_listen": "mentions"})._whatsapp_group_listen()
            == "mentions"
        )
        # WhatsApp never had passive_listen — setting it changes nothing.
        assert (
            _adapter(tmp_path, monkeypatch, {"passive_listen": True})._whatsapp_group_listen()
            == "off"
        )

    def test_listen_only_is_independent(self, tmp_path, monkeypatch):
        """listen_only (outbound suppression) never feeds the new knobs."""
        a = _adapter(tmp_path, monkeypatch, {"listen_only": True})
        assert a._whatsapp_listen_only() is True
        assert a._whatsapp_group_respond() == "all"
        assert a._whatsapp_group_listen() == "off"


# ─── Respond gating ──────────────────────────────────────────────────────────


class TestRespondGating:
    def test_respond_none_drops_group_messages(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        assert a._should_process_message(_group_message()) is False
        # Even an explicit @mention stays silent under "none".
        mentioned = _group_message(mentionedIds=[BOT_ID])
        assert a._should_process_message(mentioned) is False

    def test_respond_none_leaves_dms_alone(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        assert a._should_process_message(_dm_message()) is True

    def test_respond_none_free_response_chat_still_answers(self, tmp_path, monkeypatch):
        """Specific per-chat exemption beats the general 'none' mode."""
        a = _adapter(
            tmp_path,
            monkeypatch,
            {"group_respond": "none", "free_response_chats": [GROUP_CHAT]},
        )
        assert a._should_process_message(_group_message()) is True

    def test_respond_none_slash_command_is_the_owners_alone(self, tmp_path, monkeypatch):
        """The escape hatch belongs to a designated sender, not to the group.

        ``_is_allowed`` says yes to everyone while no allowlist is configured,
        so honouring a leading "/" unconditionally let any stranger in the
        group wake an agent set to "never reply".
        """
        open_bot = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        assert open_bot._should_process_message(_group_message("/status")) is False

        for key in ("owner_users", "allowed_users"):
            a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", key: ["+12025550188"]})
            assert a._should_process_message(_group_message("/status")) is True
            stranger = _group_message("/status", senderId="6599999999@s.whatsapp.net")
            assert a._should_process_message(stranger) is False
            # Still silent for ordinary chatter from the designated sender.
            assert a._should_process_message(_group_message("hello")) is False

    def test_group_flag_absent_falls_back_to_the_chat_id(self, tmp_path, monkeypatch):
        """A bridge that omits ``isGroup`` must not turn a group into a DM.

        ``bridge_script`` is replaceable, and a DM returns early from the
        gate — so a missing flag used to route group traffic straight past
        ``group_respond``.
        """
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        no_flag = _group_message()
        no_flag.pop("isGroup")
        assert a._is_group_chat(no_flag) is True
        assert a._should_process_message(no_flag) is False
        # A DM is unchanged: its chat id never ends in @g.us.
        dm = _dm_message()
        dm.pop("isGroup")
        assert a._is_group_chat(dm) is False
        assert a._should_process_message(dm) is True

    def test_status_updates_never_trigger_a_turn(self, tmp_path, monkeypatch):
        """A Status/story carries no group flag and is addressed to nobody."""
        a = _adapter(tmp_path, monkeypatch)  # default: reply to everything
        status = _dm_message("look at my lunch")
        status["chatId"] = "status@broadcast"
        assert a._should_process_message(status) is False

    def test_respond_mentions_matches_legacy_require_mention(self, tmp_path, monkeypatch):
        legacy = _adapter(tmp_path, monkeypatch, {"require_mention": True})
        new = _adapter(tmp_path, monkeypatch, {"group_respond": "mentions"})
        for msg in (
            _group_message(),
            _group_message(mentionedIds=[BOT_ID]),
            _group_message("/help"),
            _group_message(quotedParticipant=BOT_ID),
            _dm_message(),
        ):
            assert legacy._should_process_message(dict(msg)) == new._should_process_message(
                dict(msg)
            )
        assert new._should_process_message(_group_message()) is False
        assert new._should_process_message(_group_message(mentionedIds=[BOT_ID])) is True

    def test_respond_all_matches_legacy_default(self, tmp_path, monkeypatch):
        legacy = _adapter(tmp_path, monkeypatch, {"require_mention": False})
        new = _adapter(tmp_path, monkeypatch, {"group_respond": "all"})
        for msg in (_group_message(), _group_message("/help"), _dm_message()):
            assert legacy._should_process_message(dict(msg)) is True
            assert new._should_process_message(dict(msg)) is True

    def test_allowlist_still_beats_everything(self, tmp_path, monkeypatch):
        a = _adapter(
            tmp_path,
            monkeypatch,
            {"group_respond": "all", "allowed_users": ["+11112223333"]},
        )
        assert a._should_process_message(_group_message()) is False

    async def test_build_message_event_drops_and_records(self, tmp_path, monkeypatch):
        """The poll path routes a gated-out message through the recorder."""
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        a._maybe_record_observed = MagicMock()  # type: ignore[method-assign]
        event = await a._build_message_event(_group_message("ignored chatter"))
        assert event is None
        a._maybe_record_observed.assert_called_once()


# ─── Observed recording ──────────────────────────────────────────────────────


class TestObservedRecording:
    def test_listen_all_records_dropped_group_message(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        msg = _group_message("pizza night friday")
        assert a._should_process_message(msg) is False
        a._maybe_record_observed(msg)  # sync context → record_observed_event writes inline
        rows = get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT)
        assert [r["text"] for r in rows] == ["pizza night friday"]
        assert rows[0]["sender_name"] == "Ada Lovelace"

    def test_listen_off_records_nothing(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        a._maybe_record_observed(_group_message("vanishes"))
        assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT) == []

    def test_listen_mentions_records_only_addressed(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "mentions"})
        a._maybe_record_observed(_group_message("unaddressed"))
        a._maybe_record_observed(_group_message("hey bot", mentionedIds=[BOT_ID]))
        rows = get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT)
        assert [r["text"] for r in rows] == ["hey bot"]

    def test_records_when_the_bridge_omits_the_group_flag(self, tmp_path, monkeypatch):
        """The recorder classifies groups the same way the gate does."""
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        msg = _group_message("no flag on the wire")
        msg.pop("isGroup")
        a._maybe_record_observed(msg)
        rows = get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT)
        assert [r["text"] for r in rows] == ["no flag on the wire"]

    def test_dms_never_recorded(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_listen": "all"})
        a._maybe_record_observed(_dm_message("private"))
        assert get_chat_directory().recent_observed(AGENT) == []

    def test_empty_body_skipped(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        a._maybe_record_observed(_group_message(""))
        assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT) == []

    def test_non_allowlisted_sender_not_recorded(self, tmp_path, monkeypatch):
        a = _adapter(
            tmp_path,
            monkeypatch,
            {
                "group_respond": "none",
                "group_listen": "all",
                "allowed_users": ["+11112223333"],
            },
        )
        a._maybe_record_observed(_group_message("from a stranger"))
        assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT) == []

    def test_recording_failure_never_raises(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        monkeypatch.setattr(
            "olano.platforms.whatsapp.WhatsAppAdapter.build_source",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        a._maybe_record_observed(_group_message("still fine"))  # must not raise


# ─── Diagnostics + config plumbing ───────────────────────────────────────────


class TestDiagnosticsReportLiveModes:
    """The connector's own diagnostics answer "is it actually silent?".

    Settings are read once, at boot, so a save without a restart leaves the
    form showing one thing and the running adapter doing another — and the
    only way to tell used to be to post in the group and watch.
    """

    def _group_row(self, adapter) -> dict[str, Any]:
        rows = adapter.diagnostic_snapshot()["checks"]
        return next(r for r in rows if r["id"] == "group_modes")

    def test_silent_modes_are_reported(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "off"})
        row = self._group_row(a)
        assert row["ok"] is True  # a configured mode, not a fault
        assert "Group replies: none" in row["message"]
        assert "never answers in groups" in row["message"]
        assert "Group listening: off" in row["message"]
        assert "restarted" in row["message"]

    def test_defaults_are_reported_too(self, tmp_path, monkeypatch):
        """An unconfigured connector must not look silent."""
        row = self._group_row(_adapter(tmp_path, monkeypatch))
        assert "Group replies: all" in row["message"]
        assert "answers every group message" in row["message"]

    def test_free_response_exemptions_are_named_under_silent_mode(self, tmp_path, monkeypatch):
        a = _adapter(
            tmp_path,
            monkeypatch,
            {"group_respond": "none", "free_response_chats": [GROUP_CHAT]},
        )
        assert GROUP_CHAT in self._group_row(a)["message"]

    def test_every_mode_pair_renders(self, tmp_path, monkeypatch):
        """The detail lookups are exhaustive dicts — a new mode must not KeyError."""
        from olano.platforms.group_modes import LISTEN_MODES, RESPOND_MODES

        for respond in RESPOND_MODES:
            for listen in LISTEN_MODES:
                a = _adapter(
                    tmp_path, monkeypatch, {"group_respond": respond, "group_listen": listen}
                )
                assert self._group_row(a)["message"]


class TestConfigReachesTheAdapter:
    """The saved connector entry survives the trip to the running adapter.

    Pins the exact shape the dashboard's channel PUT writes against the
    extraction the runtime boot does, so a silent drop anywhere between them
    fails here instead of in a group chat.
    """

    def test_dashboard_entry_resolves_to_silent(self, tmp_path, monkeypatch):
        from olano.config.agent_config import ChannelConfig
        from olano.runtime.server import _fold_credentials_into_extra

        entry = ChannelConfig(
            type="platform:whatsapp",
            extra={"group_respond": "none", "group_listen": "off"},
        )
        ch_cfg = {k: v for k, v in entry.to_dict().items() if k != "type"}
        ch_cfg = _fold_credentials_into_extra("whatsapp", ch_cfg)
        config = PlatformConfig.from_dict({"enabled": True, **ch_cfg})
        assert config.extra == {"group_respond": "none", "group_listen": "off"}

        monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
        a = WhatsAppAdapter(config, agent_name=AGENT)
        assert a._whatsapp_group_respond() == "none"
        assert a._should_process_message(_group_message()) is False


class TestWhatsAppRoutesReadTheChannelEntry:
    """``/pair`` and ``/reset`` must resolve the agent's OWN bridge.

    Both walked ``AgentConfig.platforms`` — a field that does not exist — so
    ``getattr(cfg, "platforms", None) or []`` iterated nothing and each one
    resolved an EMPTY extra. Invisible while ``bridge_port``/``session_path``
    are on Auto (the same per-agent defaults come out either way) and wrong
    the moment either is pinned: Pair streamed from the default port instead
    of the configured bridge, and Reset wiped the default directory while the
    real session survived.
    """

    def _cfg(self, tmp_path, monkeypatch, extra: dict[str, Any]):
        import json

        monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
        monkeypatch.setenv("LIONCLAW_DISABLE_LOCAL_AGENTS_DIR", "1")
        agent_dir = tmp_path / "agents" / AGENT
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent.json5").write_text(
            json.dumps(
                {
                    "name": AGENT,
                    "model": "fake:instant",
                    "channels": [{"type": "platform:whatsapp", "extra": extra}],
                }
            )
        )
        from olano.config.loader import load_agent_config

        return load_agent_config(AGENT)

    def test_agent_config_has_no_platforms_attribute(self, tmp_path, monkeypatch):
        """The regression itself: the old accessor is silently always empty."""
        cfg = self._cfg(tmp_path, monkeypatch, {"bridge_port": 3999})
        assert getattr(cfg, "platforms", None) is None
        assert cfg.channels  # the connectors are here

    def test_pinned_port_and_path_are_resolved(self, tmp_path, monkeypatch):
        session = tmp_path / "pinned-session"
        cfg = self._cfg(tmp_path, monkeypatch, {"bridge_port": 3999, "session_path": str(session)})
        extra = _whatsapp_extra_from(cfg)
        port, path = WhatsAppAdapter.resolve_runtime_layout(AGENT, extra)
        assert port == 3999
        assert path == session
        # An empty extra (the old behaviour) points somewhere else entirely.
        auto_port, auto_path = WhatsAppAdapter.resolve_runtime_layout(AGENT, {})
        assert (auto_port, auto_path) != (port, path)

    def test_missing_or_bare_connector_yields_empty_extra(self, tmp_path, monkeypatch):
        assert _whatsapp_extra_from(self._cfg(tmp_path, monkeypatch, {})) == {}


def _whatsapp_extra_from(cfg: Any) -> dict[str, Any]:
    """Mirror of ``runtime/api.py:_whatsapp_channel_extra`` (nested in a route
    factory, so it cannot be imported). Pinned by the tests above; if the two
    drift, the route stops resolving a pinned bridge again."""
    for plat in getattr(cfg, "channels", None) or []:
        ptype = getattr(plat, "type", None) or (
            plat.get("type") if isinstance(plat, dict) else plat
        )
        if str(ptype) != "platform:whatsapp":
            continue
        raw_extra = plat.get("extra") if isinstance(plat, dict) else getattr(plat, "extra", None)
        return dict(raw_extra) if isinstance(raw_extra, dict) else {}
    return {}
