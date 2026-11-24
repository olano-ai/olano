"""Compatibility shim — re-exports from :mod:`olano.platforms.setup`."""

from __future__ import annotations

from olano.platforms.setup import (
    ChannelSetupAdapter,
    get_setup_fn,
    setup_discord,
    setup_email,
    setup_slack,
    setup_telegram,
)

__all__ = [
    "ChannelSetupAdapter",
    "get_setup_fn",
    "setup_discord",
    "setup_email",
    "setup_slack",
    "setup_telegram",
]
