"""owner-login adapter — presents the chat → dashboard sign-in page.

Thin, like every adapter: it does not run the sign-in itself (that is
:func:`olano.magic_links.owner_login.authorize`, called by the route so it can
set the session cookie). This class only tells the page **what to show** for the
current binding state — a one-tap "open the dashboard" button, a login form, or
a first-run "secure this dashboard" form — via the descriptor.

``kind = "session"`` marks it for the page: unlike the redirect/device/form
flows, its submit does not store a token or write the vault; it mints a browser
session and navigates. The route recognises the kind and routes the submit
through :func:`owner_login.authorize` instead of the generic action path.
"""

from __future__ import annotations

from typing import Any

from olano.magic_links import owner_login
from olano.magic_links.flows import (
    APPLY_NONE,
    STATUS_IN_PROGRESS,
    BaseFlow,
    FlowField,
    register,
)
from olano.magic_links.store import LinkRecord

_EMAIL = FlowField(name="email", label="Dashboard email", placeholder="you@example.com")
_PASSWORD = FlowField(name="password", label="Password", secret=True, placeholder="Your password")
_USERNAME = FlowField(
    name="username",
    label="Handle (optional)",
    required=False,
    placeholder="e.g. carlos-m",
    help=(
        "Your permanent public handle — lowercase letters, digits and dashes. "
        "It identifies you in the issue tracker and cannot be changed later. "
        "Leave empty to derive one from your email."
    ),
)
_NEW_PASSWORD = FlowField(
    name="password",
    label="Choose a password",
    secret=True,
    placeholder="At least 8 characters",
    help="This becomes your dashboard admin password.",
)


class OwnerLoginFlow(BaseFlow):
    id = "owner-login"
    label = "Dashboard sign-in"
    kind = "session"
    # A dashboard sign-in changes nothing about the agent, so there is nothing
    # to apply and nothing to offer.
    applies = APPLY_NONE

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        from olano.config.system_settings import load_magic_link_settings

        if not load_magic_link_settings().owner_login.enabled:
            raise ValueError(
                "Dashboard magic links are turned off. Enable them in the dashboard "
                "(Config → Magic links → Dashboard access) first."
            )
        out: dict[str, Any] = {"service": "owner-login"}
        section = str(params.get("section") or "").strip().lower()
        if section:
            out["section"] = section
        agent = str(params.get("agent") or "").strip()
        if agent:
            out["agent"] = agent
        return out

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        base["service"] = {"id": "owner-login", "name": "the dashboard"}
        mode, ctx = owner_login.resolve_mode(link)
        base["mode"] = mode

        if mode == owner_login.MODE_GRANT:
            email = ctx.get("email") or "your account"
            base["title"] = "Open your dashboard"
            base["instructions"] = f"You'll be signed in as {email}."
            base["cta"] = "Open the dashboard"
            base["fields"] = []
        elif mode == owner_login.MODE_LOGIN:
            base["title"] = "Sign in to your dashboard"
            base["instructions"] = (
                "Enter your dashboard email and password once to link this chat. "
                "Next time it's a single tap."
            )
            base["cta"] = "Sign in"
            base["fields"] = [_EMAIL.public(), _PASSWORD.public()]
        elif mode == owner_login.MODE_BOOTSTRAP:
            base["title"] = "Secure your dashboard"
            base["instructions"] = (
                "This dashboard has no account yet. Create the owner account now — "
                "you'll be signed in and this chat linked to it."
            )
            base["cta"] = "Create account & sign in"
            base["fields"] = [_EMAIL.public(), _USERNAME.public(), _NEW_PASSWORD.public()]
        else:  # unavailable
            base["title"] = "Dashboard sign-in unavailable"
            base["instructions"] = ctx.get("reason", "This isn't available right now.")
            base["fields"] = []
        return base

    def state(self, link: LinkRecord) -> dict[str, Any]:
        # Completion is driven by the route's authorize call (which sets the
        # cookie), not by out-of-band polling — so until then it's in progress.
        # link_state overlays the terminal status once the link is finished.
        return {"status": STATUS_IN_PROGRESS}


register(OwnerLoginFlow())
