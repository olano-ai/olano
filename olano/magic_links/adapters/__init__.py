"""Flow adapters — one per task a magic-link page can drive.

Importing this package registers them all (each module calls
``flows.register(...)`` at import time), which is what
``flows._load_adapters()`` does on first use.

Every adapter is a *translation layer*, never a state machine: it starts an
existing session (connector broker — OAuth or device code —, MCP OAuth, the vault)
and maps that session's own status vocabulary onto the store's five statuses.
Adding a service should mean adding a file here and nothing else.
"""

from __future__ import annotations

from olano.magic_links.adapters import (  # importing a module registers its flow
    access_token,
    channel_connect,
    codex_device,
    connector_device,
    connector_oauth,
    edit_file,
    edit_prompt,
    google_oauth,
    mcp_oauth,
    owner_login,
    provider_key,
    secret_entry,
)

__all__ = [
    "access_token",
    "channel_connect",
    "codex_device",
    "connector_device",
    "connector_oauth",
    "edit_file",
    "edit_prompt",
    "google_oauth",
    "mcp_oauth",
    "owner_login",
    "provider_key",
    "secret_entry",
]
