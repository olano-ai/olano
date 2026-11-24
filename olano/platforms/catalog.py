"""Connector/platform catalog — the connectors the runtime can boot.

Single source of truth behind the dashboard's ``GET /api/catalog/channels``
endpoint and AgentFather's ``olano_list_platforms`` tool. The *bootable*
type keys are derived from ``runtime.server.CHANNEL_MAP`` (legacy ``BaseChannel``
adapters) and ``runtime.server.PLATFORM_MAP`` (hermes-style ``platform:<name>``
adapters) so the catalog can never drift from what actually boots; this module
owns the human-facing labels and the credential env-var hints.
"""

from __future__ import annotations

from typing import Any

# Human-facing labels, keyed by connector name. Bootable type keys themselves
# come from runtime.server.CHANNEL_MAP / PLATFORM_MAP (see build_platform_catalog).
_LEGACY_LABELS: dict[str, str] = {
    "telegram": "Telegram (classic adapter)",
    "discord": "Discord (classic adapter)",
    "slack": "Slack (classic adapter)",
    "whatsapp": "WhatsApp (classic · Baileys subprocess)",
    "email": "Email / IMAP (classic)",
    "imap": "IMAP (classic alias)",
}
# Non-credential env vars a connector still reads (endpoints, account IDs,
# phone numbers). Credentials are NOT listed here — those come from
# ``config/channel_secrets.CHANNEL_SECRET_KEYS``, the one map that decides what
# the runtime actually resolves. Keeping a second copy is what let this file
# advertise ``IMAP_PASSWORD`` and ``DEBOX_BOT_TOKEN`` long after the runtime
# moved to ``EMAIL_IMAP_PASSWORD`` and ``DEBOX_APP_KEY``.
_EXTRA_ENV_HINTS: dict[str, list[str]] = {
    "email": ["IMAP_HOST", "IMAP_USER"],
    "imap": ["IMAP_HOST", "IMAP_USER"],
    "signal": ["SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"],
    "matrix": ["MATRIX_HOMESERVER"],
    "mattermost": ["MATTERMOST_URL"],
    "homeassistant": ["HASS_URL", "HASS_TOKEN"],
    "sms": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"],
    "whatsapp_twilio": [
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_NUMBER",
    ],
    "whatsapp_meta_api": ["WHATSAPP_META_WABA_ID", "WHATSAPP_META_PHONE_NUMBER_ID"],
    "dingtalk": ["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
    "feishu": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    "wecom": ["WECOM_BOT_ID"],
    "weixin": ["WEIXIN_APP_ID"],
    "qqbot": ["QQ_APP_ID"],
    "debox": [],
}

# Every classic ``BaseChannel`` connector is duplicated by a platform adapter
# that supersedes it, so ``CHANNEL_MAP`` is now deprecated end to end:
# ``{legacy type: (replacement type, what the replacement adds)}``. One map, so
# the catalog row, the dashboard note and the boot warning can never disagree
# about which connector replaces which — ``runtime.server`` imports
# :data:`DEPRECATED_CHANNELS` (derived below) instead of keeping a second copy.
#
# Deprecated rows stay in the catalog with ``deprecated: true`` + a note because
# existing configs must remain editable; the dashboard hides them from the
# picker for NEW connectors and the runtime warns on boot.
_DEPRECATED: dict[str, tuple[str, str]] = {
    "telegram": (
        "platform:telegram",
        "media albums, DM topics, webhook mode, Stars payments, and Telegram "
        "Business (secretary) support",
    ),
    "discord": (
        "platform:discord",
        "media attachments, per-channel group-listening modes, live progress "
        "updates and the shared slash-command surface",
    ),
    "slack": (
        "platform:slack",
        "media attachments, thread replies, per-channel group-listening modes "
        "and live progress updates",
    ),
    "whatsapp": (
        "platform:whatsapp",
        "a bundled Baileys bridge with QR pairing from the dashboard, media "
        "send/receive and per-group listening modes",
    ),
    "email": (
        "platform:email",
        "attachments, HTML bodies and per-sender access control",
    ),
    "imap": (
        "platform:email",
        "attachments, HTML bodies and per-sender access control",
    ),
}

# Connectors that boot but have not been verified end to end yet. Same
# treatment as a deprecated row — kept editable for anyone already using one,
# hidden from the picker for NEW connectors — but for the opposite reason:
# nothing supersedes these, they simply have not been tested. No boot warning:
# an unverified connector is not a wrong choice, just an unproven one.
_UNVERIFIED: set[str] = {
    "bluebubbles",
    "email",
    "homeassistant",
    "matrix",
    "mattermost",
    "webhook",
}

_UNVERIFIED_NOTE = (
    "Not verified yet — this adapter boots but has not been tested end to end, "
    "so it is hidden from the connector picker for now. Existing configs keep "
    "working and stay editable."
)

# ``{legacy channel type: replacement type}`` — what ``runtime.server`` needs
# for its boot-time deprecation warning.
DEPRECATED_CHANNELS: dict[str, str] = {
    name: replacement for name, (replacement, _) in _DEPRECATED.items()
}


def _deprecation_note(channel_type: str) -> str:
    replacement, adds = _DEPRECATED[channel_type]
    return (
        f"Duplicated connector — superseded by {replacement}, which also has "
        f"{adds}. Kept only so existing configs keep booting."
    )


# One adapter, two brands. Lark is Feishu's international edition (ByteDance
# serves it from open.larksuite.com instead of open.feishu.cn); ``FeishuAdapter``
# speaks to both and reads which from ``extra.domain`` (``"feishu"`` | ``"lark"``).
# The picker used to offer Lark as its own row; it was folded into the
# "Feishu / Lark" row to shorten the list, which left Lark unreachable from the
# dashboard: the form had no domain field, so every connector saved there was
# a Feishu one. A *variant* is how a second row comes back without a second
# adapter or a second ``platform:`` type: same ``type`` and credential
# contract, a ``preset`` the form applies to its own fields on select. The
# dashboard (``connectorCatalog.ts``) expands ``variants`` into picker rows
# and matches a saved connector back to its row by the preset.
_VARIANTS: dict[str, list[dict[str, Any]]] = {
    "feishu": [
        {
            "id": "lark",
            "label": "Lark (international Feishu · platform adapter)",
            "preset": {"domain": "lark"},
        },
    ],
}

_PLATFORM_LABELS: dict[str, str] = {
    "telegram": "Telegram (platform adapter)",
    "discord": "Discord (platform adapter)",
    "slack": "Slack (platform adapter)",
    "email": "Email (platform adapter)",
    "whatsapp": "WhatsApp (platform adapter)",
    "signal": "Signal (platform adapter)",
    "matrix": "Matrix (platform adapter)",
    "mattermost": "Mattermost (platform adapter)",
    "homeassistant": "Home Assistant (platform adapter)",
    "sms": "SMS / Twilio (platform adapter)",
    "whatsapp_twilio": "WhatsApp / Twilio Business API (platform adapter)",
    "whatsapp_meta_api": "WhatsApp / Meta Cloud API (platform adapter)",
    "webhook": "Inbound webhooks (platform adapter)",
    "dingtalk": "DingTalk (platform adapter)",
    "feishu": "Feishu / Lark (platform adapter)",
    # Two products share the WeChat brand, and the labels are where a person
    # (and the integration finder, which reads them) tells them apart: WeCom is
    # the enterprise product (formerly "WeChat Work" / "Enterprise WeChat", bot
    # id + secret), WeChat is the personal messenger (Tencent's iLink Bot API,
    # QR-paired). The brand leads and the qualifier sits in the parenthetical
    # so the plain word "WeChat" appears in exactly one label.
    "wecom": "WeCom (Enterprise WeChat · platform adapter)",
    "weixin": "WeChat (Weixin / iLink Bot · platform adapter)",
    "bluebubbles": "BlueBubbles iMessage (platform adapter)",
    "qqbot": "QQ bot (platform adapter)",
    "debox": "DeBox (Web3 chat bot · platform adapter)",
    "api_server": "OpenAI-compatible API",
}


def _credential_row(channel_type: str) -> dict[str, Any]:
    """The credential contract for one connector, from the one map that owns it.

    ``config/channel_secrets.py`` decides which vault key each connector field
    resolves from, so the catalog derives its answer instead of keeping a second
    copy that can drift (it used to: email advertised ``IMAP_PASSWORD`` here
    while the runtime resolved ``EMAIL_PASSWORD``).

    ``credentials`` is the field→key map the dashboard's connector panel and
    AgentFather both read; ``required`` lists the keys a connector cannot boot
    without; ``env_vars`` stays for backwards compatibility — same keys, the
    flat shape older callers expect, plus any non-secret env hints.
    """
    from olano.config.channel_secrets import credential_keys, required_credential_fields

    keys = credential_keys(channel_type)
    required_fields = set(required_credential_fields(channel_type))
    base = base_channel_type(channel_type)
    hints = [v for v in _EXTRA_ENV_HINTS.get(base, []) if v not in keys.values()]
    return {
        "credentials": keys,
        "required": sorted({keys[f] for f in required_fields if f in keys}),
        "env_vars": list(keys.values()) + hints,
    }


def base_channel_type(channel_type: str) -> str:
    """``platform:telegram`` → ``telegram``."""
    return channel_type.split(":", 1)[1] if channel_type.startswith("platform:") else channel_type


def _qr_pairing_types() -> set[str]:
    """Base channel types that pair by QR code, derived from the connector
    registry (``channel_setup``) so the catalog can never disagree with the
    machinery that actually runs the pairing. WhatsApp and WeChat/Weixin today.
    """
    from olano.platforms.channel_setup import all_connectors

    return {
        base_channel_type(c.channel_type)
        for c in all_connectors()
        if c.mode == "qr" and c.channel_type
    }


_QR_PAIRING_HINT = (
    "QR pairing is the DEFAULT way to connect this messenger: start a pairing "
    "session, show the user the QR code, and the credentials are issued and "
    "stored automatically on scan — no token to ask for. The credential fields "
    "listed here are an optional advanced/manual fallback only."
)


def build_platform_catalog() -> list[dict[str, Any]]:
    """Return every connector the runtime can boot.

    Each row: ``type`` (the value to put in ``agent.json5 → channels``), a
    human ``label``, ``kind`` (``"legacy"`` ``BaseChannel`` vs ``"platform"``
    hermes adapter), and the credential contract from
    :func:`_credential_row`. Legacy rows use the bare type (``telegram``);
    platform rows use the ``platform:`` prefix (``platform:signal``).

    Rows for messengers that pair by QR (WhatsApp, WeChat/Weixin) additionally
    carry ``pairing: "qr"`` plus a ``pairing_hint`` saying QR is the default
    connect path and the listed credentials are the advanced/manual fallback —
    derived from the ``channel_setup`` connector registry, so an agent reading
    this catalog is steered to the pairing flow instead of asking for a token.

    A row may also carry ``deprecated`` (duplicated by a maintained adapter) or
    ``hidden`` (boots, but unverified) plus a ``note`` saying which. Both mean
    the same thing to the dashboard — keep it editable, never offer it for a
    NEW connector — so every row the picker must suppress is discoverable from
    this one payload rather than from a hard-coded list in the frontend.

    ``variants`` (Feishu's Lark row) lists the extra picker rows one adapter
    earns: ``{id, label, preset}``, where ``preset`` is the ``extra`` keys the
    form fixes for that row (``{"domain": "lark"}``). Same ``type``, same
    credentials, a saved connector is one or the other by its ``extra``.
    """
    # Lazy import: the maps live in the (heavy) runtime server module, but this
    # only reads two dict constants — nothing is started.
    from olano.runtime.server import CHANNEL_MAP, PLATFORM_MAP

    qr_types = _qr_pairing_types()
    types: list[dict[str, Any]] = []
    for ch_name in CHANNEL_MAP:
        row: dict[str, Any] = {
            "type": ch_name,
            "label": _LEGACY_LABELS.get(ch_name, ch_name),
            "kind": "legacy",
            **_credential_row(ch_name),
        }
        if ch_name in qr_types:
            row["pairing"] = "qr"
            row["pairing_hint"] = _QR_PAIRING_HINT
        if ch_name in _DEPRECATED:
            row["deprecated"] = True
            row["note"] = _deprecation_note(ch_name)
        types.append(row)
    for plat_name in sorted(PLATFORM_MAP.keys()):
        row = {
            "type": f"platform:{plat_name}",
            "label": _PLATFORM_LABELS.get(
                plat_name,
                f"{plat_name.replace('_', ' ').title()} (platform adapter)",
            ),
            "kind": "platform",
            **_credential_row(f"platform:{plat_name}"),
        }
        if plat_name in qr_types:
            row["pairing"] = "qr"
            row["pairing_hint"] = _QR_PAIRING_HINT
        if plat_name in _UNVERIFIED:
            row["hidden"] = True
            row["note"] = _UNVERIFIED_NOTE
        if plat_name in _VARIANTS:
            row["variants"] = [dict(v, preset=dict(v["preset"])) for v in _VARIANTS[plat_name]]
        types.append(row)
    return types


def connector_variants(channel_type: str) -> list[dict[str, Any]]:
    """The picker variants of one connector type (``[]`` for most)."""
    return [
        dict(v, preset=dict(v["preset"]))
        for v in _VARIANTS.get(base_channel_type(channel_type), [])
    ]


def hidden_channel_types() -> set[str]:
    """Every connector type the picker must not offer for a NEW connector.

    Deprecated *and* unverified, as the concrete ``type`` strings that appear in
    ``agent.json5`` — the dashboard's static fallback list mirrors this when the
    ``/api/catalog/channels`` fetch fails.
    """
    return set(_DEPRECATED) | {f"platform:{name}" for name in _UNVERIFIED}
