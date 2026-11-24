"""Single resolver for the cloud catalog API base URL.

Historically two modules read ``LIONCLAW_CLOUD_API_URL`` with *different*
defaults (``core/mcp_registry.py`` → api.olano.ai, ``deployment/sources.py`` →
cloud.olano.ai), and the fleet tooling's ``attach-cloud.sh`` writes the latter — so which
host served the MCP directory depended on deployment mode. Both DO apps build
the same codebase, so both work, but every catalog consumer should resolve the
URL one way. This is that way:

    LIONCLAW_CATALOG_API_URL   explicit catalog override (wins)
    LIONCLAW_CLOUD_API_URL     the general cloud base URL (attach-cloud sets it)
    https://api.olano.ai       default

``deployment/sources.py`` keeps its own resolver — deployment settings are a
different, authenticated service contract.
"""

from __future__ import annotations

import os

DEFAULT_CATALOG_API_URL = "https://api.olano.ai"


def catalog_api_url() -> str:
    """Base URL of the cloud catalog API (no trailing slash)."""
    for var in ("LIONCLAW_CATALOG_API_URL", "LIONCLAW_CLOUD_API_URL"):
        value = os.environ.get(var)
        if value:
            return value.rstrip("/")
    return DEFAULT_CATALOG_API_URL
