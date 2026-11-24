"""Magic links — browser-reachable authentication for chat-only users.

A user talking to an agent on Telegram/WhatsApp/Slack cannot complete an auth
task that needs a browser: the instance is headless (``xdg-open`` fails),
the dashboard needs a session, and pasting an API key into a chat leaks it into
the transcript, the model context, and the platform's servers.

This package mints **short-lived, PIN-protected, single-purpose links** served
from the deployment's own public origin (``https://<host>/api/connect/<token>``)
and renders one small self-contained page that drives an *existing* auth flow:

* device flows (a connector's sign-in code, OpenAI Codex) — shows the one-time code + a button
  to the provider's device page;
* authorization-code flows (the connector broker's providers, Google, MCP) —
  hands the user to the provider and reflects live status;
* forms (secret entry, LLM provider keys) — the value goes browser → HTTPS →
  vault, never through the chat.

The page **never** proxies or frames a provider's login: providers forbid
framing, it would break passkeys (origin-bound), and a credential form on our
origin is phishing-shaped. It hands off, then polls.

Layout::

    store.py     link records (hashed token + PIN, TTL, status, audit)
    flows.py     the MagicLinkFlow protocol + adapter registry
    adapters/    one thin wrapper per existing auth flow
    service.py   mint/unlock/state/act — the API every surface calls
    page.py      the self-contained HTML page
    routes.py    public /api/connect/* + admin /api/magic-links/* routes
    watch.py     completion watcher → ✅/⚠️ back to the originating chat

Minting surfaces: the ``magic_link_connect_service`` / ``magic_link_request_secret`` agent tools, the
``/olano connect`` chat command, the dashboard Connections tab, and
``olano connect link`` on the CLI. All of them go through
:mod:`olano.magic_links.service`.
"""

from __future__ import annotations

from olano.magic_links.service import (
    MagicLinkError,
    MintedLink,
    act_on_link,
    link_state,
    list_links,
    magic_links_available,
    mint_link,
    revoke_link,
    unlock_link,
)
from olano.magic_links.store import LinkRecord

__all__ = [
    "LinkRecord",
    "MagicLinkError",
    "MintedLink",
    "act_on_link",
    "link_state",
    "list_links",
    "magic_links_available",
    "mint_link",
    "revoke_link",
    "unlock_link",
]
