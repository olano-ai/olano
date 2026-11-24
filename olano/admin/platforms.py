"""Connector / platform catalog for AgentFather.

Wraps :func:`olano.platforms.catalog.build_platform_catalog` so AgentFather
can answer "what messaging platforms / connectors can I wire onto an agent?"
before editing an agent's ``channels`` list. Read-only.
"""

from __future__ import annotations

from typing import Any

from olano.platforms.catalog import build_platform_catalog


def list_platforms() -> dict[str, Any]:
    """Return every connector the runtime can boot (Telegram, WhatsApp, Slack…).

    Each entry carries the ``type`` to put in ``agent.json5 → channels`` (bare
    name for legacy adapters, ``platform:<name>`` for the newer platform
    adapters), a human ``label``, the adapter ``kind``, and the credential
    ``env_vars`` it expects. QR-paired messengers (WhatsApp, WeChat/Weixin)
    additionally carry ``pairing: "qr"`` + ``pairing_hint`` — QR is their
    default connect path and the env-vars are the manual fallback. These are
    messaging surfaces for humans — lock each one down with per-channel
    ``allowed_users``; they are unrelated to the ``talk_to_agent`` peer mesh.
    """
    catalog = build_platform_catalog()
    return {"platforms": catalog, "count": len(catalog)}
