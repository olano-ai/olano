"""Platform configuration primitives.

Slimmed-down port of hermes-agent/gateway/config.py. Only the pieces that
``BasePlatformAdapter`` and its subclasses reach for are kept here —
``Platform`` enum, ``PlatformConfig`` dataclass, and ``HomeChannel``.

Gateway-level concepts (session-reset policy, streaming config,
sessions_dir, etc.) do not belong in the platforms package; they will live
alongside the gateway daemon when/if it is wired in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Platform(Enum):
    """Supported messaging platforms."""

    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    # Official WhatsApp Business Platform via Twilio Conversations. Distinct
    # from WHATSAPP (the bundled Baileys bridge): different credentials,
    # different transport, and an agent may run either or both.
    WHATSAPP_TWILIO = "whatsapp_twilio"
    # Official WhatsApp Business Platform through Meta's own Cloud API, with no
    # provider in between. A third, separate connector: an agent may run any
    # of the three.
    WHATSAPP_META_API = "whatsapp_meta_api"
    SLACK = "slack"
    SIGNAL = "signal"
    MATTERMOST = "mattermost"
    MATRIX = "matrix"
    HOMEASSISTANT = "homeassistant"
    EMAIL = "email"
    SMS = "sms"
    DINGTALK = "dingtalk"
    API_SERVER = "api_server"
    WEBHOOK = "webhook"
    FEISHU = "feishu"
    WECOM = "wecom"
    WECOM_CALLBACK = "wecom_callback"
    WEIXIN = "weixin"
    BLUEBUBBLES = "bluebubbles"
    QQBOT = "qqbot"
    DEBOX = "debox"


@dataclass
class HomeChannel:
    """Default delivery destination for a platform."""

    platform: Platform
    chat_id: str
    name: str = "Home"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HomeChannel:
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            name=data.get("name", "Home"),
        )


@dataclass
class PlatformConfig:
    """Configuration for a single messaging platform adapter."""

    enabled: bool = False
    token: str | None = None
    api_key: str | None = None
    home_channel: HomeChannel | None = None

    # Reply-threading mode (Telegram/Slack):
    #   "off"   — never thread replies
    #   "first" — only the first chunk threads to the user's message (default)
    #   "all"   — every chunk threads to the user's message
    reply_to_mode: str = "first"

    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": self.enabled,
            "extra": self.extra,
            "reply_to_mode": self.reply_to_mode,
        }
        if self.token:
            result["token"] = self.token
        if self.api_key:
            result["api_key"] = self.api_key
        if self.home_channel:
            result["home_channel"] = self.home_channel.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformConfig:
        home_channel = (
            HomeChannel.from_dict(data["home_channel"]) if "home_channel" in data else None
        )
        return cls(
            enabled=data.get("enabled", False),
            token=data.get("token"),
            api_key=data.get("api_key"),
            home_channel=home_channel,
            reply_to_mode=data.get("reply_to_mode", "first"),
            extra=data.get("extra", {}),
        )
