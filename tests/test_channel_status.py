"""runtime/channel_status.py: the org chart's per-connector status, from memory only.

Each state has one rule; these pin every rule and its precedence with plain
objects standing in for the runtime's adapters, since the helper must never
reach for the network (the org chart draws the whole fleet at once).
"""

from __future__ import annotations

from types import SimpleNamespace

from olano.config.agent_config import ChannelConfig
from olano.runtime.channel_status import (
    CONNECTED,
    CONNECTING,
    ERROR,
    STOPPED,
    channel_entry_type,
    channel_states,
)


def _platform(slug: str, *, connected: bool, fatal: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value=slug),
        is_connected=connected,
        fatal_error_message=fatal,
    )


def _classic(name: str, *, rebuilding: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=name, _rebuilding=rebuilding)


def _status(rows: list[dict], ctype: str) -> tuple[str, str | None]:
    row = next(r for r in rows if r["type"] == ctype)
    return row["status"], row["detail"]


def test_entry_type_accepts_every_config_shape():
    assert channel_entry_type("telegram") == "telegram"
    assert channel_entry_type({"type": "platform:whatsapp", "x": 1}) == "platform:whatsapp"
    assert channel_entry_type(ChannelConfig(type="slack")) == "slack"
    assert channel_entry_type({"no_type": True}) == ""


def test_stopped_agent_reports_every_channel_stopped():
    rows = channel_states(
        ["telegram", "platform:whatsapp"], running=False, resources=None, skipped=None
    )
    assert [r["status"] for r in rows] == [STOPPED, STOPPED]


def test_booting_agent_reads_connecting_until_its_resources_are_recorded():
    rows = channel_states(["platform:telegram"], running=True, resources=None, skipped={})
    assert _status(rows, "platform:telegram") == (CONNECTING, None)


def test_skipped_connector_is_an_error_with_its_reason_even_while_booting():
    rows = channel_states(
        ["telegram"],
        running=True,
        resources=None,
        skipped={"telegram": "Set TELEGRAM_BOT_TOKEN in the vault."},
    )
    assert _status(rows, "telegram") == (ERROR, "Set TELEGRAM_BOT_TOKEN in the vault.")


def test_platform_adapter_states():
    resources = {
        "channels": [],
        "platforms": [
            _platform("telegram", connected=True),
            _platform("whatsapp", connected=False),  # waiting for the QR scan
            _platform("signal", connected=False, fatal="signal-cli exited"),
        ],
    }
    rows = channel_states(
        ["platform:telegram", "platform:whatsapp", "platform:signal"],
        running=True,
        resources=resources,
        skipped={},
    )
    assert _status(rows, "platform:telegram") == (CONNECTED, None)
    assert _status(rows, "platform:whatsapp") == (CONNECTING, None)
    assert _status(rows, "platform:signal") == (ERROR, "signal-cli exited")


def test_classic_channel_is_connected_when_attached_and_connecting_while_rebuilding():
    resources = {
        "channels": [_classic("telegram"), _classic("slack", rebuilding=True)],
        "platforms": [],
    }
    rows = channel_states(["telegram", "slack"], running=True, resources=resources, skipped={})
    assert _status(rows, "telegram") == (CONNECTED, None)
    assert _status(rows, "slack") == (CONNECTING, None)


def test_running_agent_without_the_adapter_is_an_error():
    rows = channel_states(
        ["discord"], running=True, resources={"channels": [], "platforms": []}, skipped={}
    )
    status, detail = _status(rows, "discord")
    assert status == ERROR
    assert detail


def test_classic_and_platform_spellings_never_resolve_to_each_other():
    """`telegram` (classic) and `platform:telegram` are different adapters."""
    resources = {"channels": [], "platforms": [_platform("telegram", connected=True)]}
    rows = channel_states(["telegram"], running=True, resources=resources, skipped={})
    assert _status(rows, "telegram")[0] == ERROR


def test_rows_keep_config_order_and_drop_duplicates_and_blanks():
    rows = channel_states(
        ["slack", {"type": ""}, "telegram", "slack"], running=False, resources=None, skipped=None
    )
    assert [r["type"] for r in rows] == ["slack", "telegram"]


def test_non_mapping_runtime_state_is_tolerated():
    """A MagicMock runtime (tests) or a missing map must not raise."""
    rows = channel_states(["telegram"], running=True, resources="nope", skipped=42)  # type: ignore[arg-type]
    assert _status(rows, "telegram") == (CONNECTING, None)
