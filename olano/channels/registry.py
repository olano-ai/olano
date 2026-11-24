"""Compatibility shim — re-exports from :mod:`olano.platforms.delivery`.

``ChannelRegistry`` and ``get_channel_registry`` now live in
:mod:`olano.platforms.delivery`. Existing imports from
``olano.channels.registry`` continue to resolve through this shim.
"""

from __future__ import annotations

from olano.platforms.delivery import ChannelRegistry, get_channel_registry

__all__ = ["ChannelRegistry", "get_channel_registry"]
