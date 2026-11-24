"""Cheap per-agent channel status, for surfaces that draw every agent at once.

The Channels tab asks ``GET /api/agents/{name}/connectors/health``, which may run
an adapter's ``diagnostic_snapshot()`` (WhatsApp's is a live bridge probe), one
agent at a time. The org chart draws the whole fleet on every visit, so it must
not pay a network probe per connector. It reads only what the runtime already
holds in memory:

* the agent's configured ``channels`` entries (what *should* be connected);
* the adapters its boot attached (``_agent_resources[name]["channels"]`` for
  classic channels, ``["platforms"]`` for ``platform:*`` adapters);
* the boot's per-connector skip reasons (``_skipped_channels``: a missing
  credential, the one "not running" with a known fix).

Four states, one rule each, evaluated in this order:

``stopped``     the agent is not running, so none of its channels are.
``connecting``  the agent is still booting (no resources recorded yet), a platform
                adapter is attached but not connected (WhatsApp waiting for its QR
                scan, a socket mid-reconnect), or a classic channel's watchdog is
                rebuilding its client.
``error``       the boot skipped the connector (reason in ``detail``), the adapter
                recorded a fatal error, or the agent runs without the adapter
                (it failed to load).
``connected``   everything else that is attached.

Pure: no runtime import, no I/O, so it is unit-testable with plain objects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

CONNECTED = "connected"
CONNECTING = "connecting"
ERROR = "error"
STOPPED = "stopped"

_NOT_LOADED = "The connector is configured but did not start. Check the agent's Channels tab."

PLATFORM_PREFIX = "platform:"


def channel_entry_type(entry: Any) -> str:
    """The wire type of one ``channels`` entry: a bare string, a ``ChannelConfig``
    or the raw dict an ``agent.json5`` read yields. ``""`` when it has none."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, Mapping):
        return str(entry.get("type") or "").strip()
    return str(getattr(entry, "type", "") or "").strip()


def _find_attached(ctype: str, channels: Iterable[Any], platforms: Iterable[Any]) -> Any | None:
    """The live adapter behind ``ctype``, resolved the way the health endpoint does:
    classic channels by ``adapter.name``, platform adapters by their enum value."""
    if ctype.startswith(PLATFORM_PREFIX):
        slug = ctype[len(PLATFORM_PREFIX) :]
        for p in platforms:
            if getattr(getattr(p, "platform", None), "value", None) == slug:
                return p
        return None
    for c in channels:
        if getattr(c, "name", None) == ctype:
            return c
    return None


def _attached_state(ctype: str, adapter: Any) -> tuple[str, str | None]:
    if ctype.startswith(PLATFORM_PREFIX):
        fatal = getattr(adapter, "fatal_error_message", None)
        if isinstance(fatal, str) and fatal:
            return ERROR, fatal
        if getattr(adapter, "is_connected", False) is True:
            return CONNECTED, None
        return CONNECTING, None
    # Classic channels expose no connection flag; attached means it started. The
    # network watchdog's rebuild is the one transient state they do expose.
    if getattr(adapter, "_rebuilding", False) is True:
        return CONNECTING, None
    return CONNECTED, None


def channel_states(
    entries: Iterable[Any] | None,
    *,
    running: bool,
    resources: Mapping[str, Any] | None,
    skipped: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """One ``{"type", "status", "detail"}`` row per configured channel, in config
    order, deduplicated by type.

    ``resources`` is the agent's ``_agent_resources`` record, ``None`` while it is
    still booting. ``skipped`` is its ``_skipped_channels`` map (type → reason).
    """
    res = resources if isinstance(resources, Mapping) else None
    attached_channels = list(res.get("channels") or []) if res is not None else []
    attached_platforms = list(res.get("platforms") or []) if res is not None else []
    skip = skipped if isinstance(skipped, Mapping) else {}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries or []:
        ctype = channel_entry_type(entry)
        if not ctype or ctype in seen:
            continue
        seen.add(ctype)

        status: str
        detail: str | None = None
        reason = skip.get(ctype)
        if not running:
            status = STOPPED
        elif isinstance(reason, str) and reason:
            status, detail = ERROR, reason
        elif res is None:
            status = CONNECTING
        else:
            adapter = _find_attached(ctype, attached_channels, attached_platforms)
            if adapter is None:
                status, detail = ERROR, _NOT_LOADED
            else:
                status, detail = _attached_state(ctype, adapter)
        rows.append({"type": ctype, "status": status, "detail": detail})
    return rows
