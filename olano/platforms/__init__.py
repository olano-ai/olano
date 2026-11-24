"""Messaging platform adapters for Olano.

Platform adapters (Telegram, Discord, Slack, Email, etc.) subclass
``BasePlatformAdapter`` and translate between a specific messaging service
and Olano's agent runner. Each adapter normalizes inbound events to
``MessageEvent`` and dispatches them via a registered message handler.

This package was originally ported from the Hermes gateway; shared
infrastructure lives in :mod:`olano.platforms.base`.
"""

from __future__ import annotations

from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from olano.platforms.config import HomeChannel, Platform, PlatformConfig
from olano.platforms.delivery import (
    BaseChannelAdapter,
    ChannelAdapter,
    ChannelRegistry,
    MessageEnvelope,
    NormalizedInbound,
    get_channel_registry,
)
from olano.platforms.registry import PlatformRegistry, get_platform_registry
from olano.platforms.session import SessionSource, build_session_key
from olano.platforms.utils import MessageDebouncer, MessageSequentializer

__all__ = [
    "BaseChannelAdapter",
    "BasePlatformAdapter",
    "ChannelAdapter",
    "ChannelRegistry",
    "HomeChannel",
    "MessageDebouncer",
    "MessageEnvelope",
    "MessageEvent",
    "MessageSequentializer",
    "MessageType",
    "NormalizedInbound",
    "Platform",
    "PlatformConfig",
    "PlatformRegistry",
    "ProcessingOutcome",
    "SendResult",
    "SessionSource",
    "build_session_key",
    "get_channel_registry",
    "get_platform_registry",
]
