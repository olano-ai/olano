"""Google Workspace sign-in.

Google predates the connector broker and keeps its own flow in
:mod:`olano.tools.google_tools` (the runtime API dispatches
``service == "google"`` there), so it gets its own adapter rather than riding
:mod:`connector_oauth`. The session shapes are deliberately identical, so this
file is the same twelve lines of translation.

Two Google-specific notes carried over from the underlying flow: the OAuth
client must be a **Web application** client (a Desktop client only permits
loopback redirects, which a phone can't reach), and the redirect lands on
``/api/auth/google/callback`` at this deployment's public origin — the same one
serving the magic link.
"""

from __future__ import annotations

from typing import Any

from olano.magic_links.flows import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_IN_PROGRESS,
    BaseFlow,
    register,
)
from olano.magic_links.store import LinkRecord

_STATUS_MAP = {
    "awaiting_authorization": STATUS_IN_PROGRESS,
    "exchanging": STATUS_IN_PROGRESS,
    "authorized": STATUS_DONE,
    "error": STATUS_ERROR,
}


class GoogleOAuthFlow(BaseFlow):
    id = "google"
    label = "Google"
    kind = "redirect"
    instructions = (
        "You'll pick a Google account and approve access. "
        "Gmail, Calendar and Drive tools use whichever account you choose."
    )

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"service": "google", "shared": bool(params.get("shared"))}
        agent = str(params.get("agent") or "").strip()
        if agent:
            out["agent"] = agent
        return out

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        base["service"] = {"id": "google", "name": "Google"}
        base["title"] = "Connect Google"
        return base

    def start(self, link: LinkRecord) -> dict[str, Any]:
        from olano.magic_links.service import public_base_url
        from olano.tools.google_tools import start_google_auth_flow

        origin = public_base_url()
        if not origin:
            raise ValueError("This deployment has no public origin for the OAuth callback.")
        # creds_path=None → the flow resolves the OAuth client itself
        # (vault/env GOOGLE_OAUTH_CLIENT blob → GOOGLE_CLIENT_ID/-SECRET pair
        # → legacy GOOGLE_OAUTH_CREDENTIALS path), the same lookup the
        # dashboard's Connect button performs.
        session = start_google_auth_flow(
            creds_path=None,
            agent=str(link.params.get("agent") or "") or None,
            shared=bool(link.params.get("shared")),
            redirect_uri=f"{origin}/api/auth/google/callback",
        )
        return {"session_id": session.session_id}

    def state(self, link: LinkRecord) -> dict[str, Any]:
        from olano.tools.google_tools import poll_google_auth_flow

        if not link.session_id:
            return {"status": link.status}
        session = poll_google_auth_flow(link.session_id)
        if session is None:
            return {"status": STATUS_ERROR, "error": "This sign-in attempt expired."}
        status = _STATUS_MAP.get(session.status, STATUS_IN_PROGRESS)
        out: dict[str, Any] = {
            "status": status,
            "authorization_url": session.authorization_url or "",
        }
        if status == STATUS_DONE:
            scope = f" for {link.params['agent']}" if link.params.get("agent") else ""
            out["result"] = f"Google connected{scope}."
        elif status == STATUS_ERROR:
            out["error"] = session.error or ""
        return out


register(GoogleOAuthFlow())
