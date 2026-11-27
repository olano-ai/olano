"""E2E: a person card with a title keeps its badges inside the card.

A person card shows a name, an optional title and a row of badges (HUMAN and
the role). The card was sized for two lines, so a title pushed the badge row
past the card's bottom edge, where ``overflow: hidden`` cut it off. A titled
card is now one line taller.

The harness is single-user, so the one person on the chart is the implicit
owner with no title; the org-chart response is amended in the page to give
them one, which is all the layout reads.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e


def test_a_titled_person_card_shows_its_badges_whole(page, dashboard) -> None:
    def org_chart(route) -> None:
        resp = route.fetch()
        data = resp.json()
        assert data.get("humans"), "the harness should put the owner on the chart"
        for h in data["humans"]:
            h["title"] = "Head of Customer Operations, Asia Pacific"
        route.fulfill(response=resp, body=json.dumps(data))

    page.route("**/api/org-chart*", org_chart)
    page.goto(dashboard.base_url.rstrip("/") + "/orgchart", wait_until="domcontentloaded")
    card = page.locator(".org-node.human").first
    card.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(300)  # let fit-to-view settle

    box = card.bounding_box()
    assert box is not None
    badges = card.locator(".org-badge")
    assert badges.count() == 2
    for i in range(badges.count()):
        b = badges.nth(i).bounding_box()
        assert b is not None
        # Inside the card, with the card's 1px border to spare (scaled with zoom).
        assert b["y"] + b["height"] <= box["y"] + box["height"], (b, box)
    title = card.locator(".org-title-line")
    assert title.count() == 1
    # The title takes one line (long ones are ellipsized, full text on hover).
    assert title.get_attribute("title") == "Head of Customer Operations, Asia Pacific"
