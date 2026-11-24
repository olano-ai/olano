"""Compatibility shim — re-exports from :mod:`olano.platforms.delivery`.

The canonical home of ``MessageEnvelope``, ``NormalizedInbound``,
``ChannelAdapter``, ``BaseChannelAdapter``, ``MessageDebouncer``, and
``MessageSequentializer`` is now :mod:`olano.platforms`. This module
remains so existing ``from olano.channels.adapter import ...`` imports
keep working during the migration.

New code should import from :mod:`olano.platforms` instead.
"""

from __future__ import annotations

from olano.platforms.delivery import (
    BaseChannelAdapter,
    ChannelAdapter,
    MessageEnvelope,
    NormalizedInbound,
)
from olano.platforms.utils import MessageDebouncer, MessageSequentializer

__all__ = [
    "BaseChannelAdapter",
    "ChannelAdapter",
    "MessageDebouncer",
    "MessageEnvelope",
    "MessageSequentializer",
    "NormalizedInbound",
]
