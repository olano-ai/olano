"""Connections view: a connector's tool chips read green once it is connected.

Same defect as the providers page had, in a second place: the card reported
connectedness in one small badge, while the tool names below it — the part that
answers "what can my agent actually do right now" — were painted the same
neutral whether the service was signed in or not.

Driven off a stubbed ``/api/auth/status``: a connected service's chips take the
success colour, an unconnected one's stay neutral. The chips are a preview of
what connecting would grant, so neutral is the correct read for the second card,
not an oversight.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e

# Card-scoped so a chip is always read against its own service's state.
MS365_CARD = ".card:has-text('Microsoft 365')"
SLACK_CARD = ".card:has-text('Slack')"

STATUS = {
    "microsoft365": {
        "connected": True,
        "scope": "system",
        "token_path": "/home/olano/.lionclaw/connectors/microsoft365_token.json",
        "shared": None,
        "methods": ["oauth"],
    },
    "slack": {"connected": False, "scope": "none", "token_path": None, "shared": None},
}


def _chip_style(page, card: str, tool: str) -> tuple[str, str]:
    chip = page.locator(f"{card} .badge", has_text=tool).first
    chip.wait_for(state="visible", timeout=15_000)
    return tuple(
        chip.evaluate(
            "el => { const s = getComputedStyle(el); return [s.color, s.backgroundColor]; }"
        )
    )


def test_tool_chips_follow_the_connection_state(page, dashboard) -> None:
    page.route(
        "**/api/auth/status*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(STATUS)
        ),
    )
    page.goto(dashboard.base_url.rstrip("/") + "/connections", wait_until="domcontentloaded")

    connected_color, connected_bg = _chip_style(page, MS365_CARD, "ms365_send_mail")
    plain_color, plain_bg = _chip_style(page, SLACK_CARD, "slack_send_message")

    assert connected_color != plain_color, (
        f"a connected service's tool chip kept the neutral text colour {connected_color}"
    )
    assert connected_bg != plain_bg, (
        f"a connected service's tool chip kept the neutral background {connected_bg}"
    )
