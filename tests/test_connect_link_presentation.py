"""`_connect_link_presentation` — how a sign-in link is put in front of the user.

The dashboard chat renders any tool result carrying an `authorization_url` as a
"Connect" button card, so on that surface the model is told to point at the
button instead of pasting a six-hundred-character OAuth URL as text. Every
other surface (Telegram, WhatsApp, CLI) has no card, so there the model must
keep sending the URL as plain text. The hint must never break the result: no
request context reads as a non-dashboard surface.
"""

from __future__ import annotations

from olano.security.request_context import push_request_context
from olano.tools.olano_admin import _connect_link_presentation


def _result() -> dict:
    return {
        "session_id": "abc",
        "status": "awaiting_authorization",
        "authorization_url": "https://login.example.com/oauth2/authorize?state=x",
        "url": "https://mcp.example.com/mcp",
    }


def test_dashboard_chat_is_told_about_the_connect_button():
    with push_request_context(source="api", channel="api"):
        out = _connect_link_presentation(_result())
    assert "Connect button" in out["link_presentation"]
    assert "Do NOT paste" in out["link_presentation"]


def test_messaging_surfaces_are_told_to_send_the_url_as_text():
    with push_request_context(source="platform", channel="telegram"):
        out = _connect_link_presentation(_result())
    assert "plain text" in out["link_presentation"]
    assert "button" not in out["link_presentation"].lower()


def test_result_without_a_link_is_untouched():
    out = _connect_link_presentation({"status": "error", "error": "boom"})
    assert "link_presentation" not in out
