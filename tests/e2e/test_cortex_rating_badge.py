"""A rated Cortex item shows its score on the card and its reasoning in the drawer.

The Rate & Promote phase writes ``rating`` onto an item; the card badge and the
drawer's rationale / next-step lines are the only places a person sees it
without opening the folder. Both viewports: the badge sits in the card's
right-hand chip row, which wraps on a phone, and an unrated item must show no
badge at all (the fleet view mixes rated and unrated agents).
"""

from __future__ import annotations

import json
import re

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

BADGE = "[data-testid='cortex-rating']"
WORKSPACE_ROOT = "/srv/olano/workspace"


def _idea(idx: int, title: str, rating: dict | None) -> dict:
    path = f"ideas/{title.lower().replace(' ', '-')}"
    item = {
        "id": f"idea-e2e-{idx}",
        "kind": "idea",
        "slug": path.split("/")[-1],
        "title": title,
        "status": "active",
        "summary": f"{title}, in one sentence.",
        "preview": f"{title}, in one sentence.",
        "tags": [],
        "path": path,
        "abs_path": f"{WORKSPACE_ROOT}/{path}",
        "detail_file": f"{path}/idea.md",
        "source": "autonomous",
        "created_at": "2026-10-04T10:00:00Z",
        "updated_at": "2026-10-04T10:00:00Z",
        "links": {},
        "agent": "merli",
    }
    if rating is not None:
        item["rating"] = rating
    return item


RATED = _idea(
    1,
    "Solar kiln",
    {
        "score": 8,
        "rationale": "Cheap to build and the owner keeps coming back to lumber drying.",
        "dimensions": {"value": 8, "feasibility": 9},
        "recommendation": "promote",
        "next_step": "Scaffold a one-page sizing calculator.",
        "rated_at": "2026-10-04T12:00:00Z",
        "rated_by": "cortex",
    },
)
UNRATED = _idea(2, "Moss garden", None)

LIST = {
    "agent": "",
    "agents": ["merli"],
    "counts": {"idea": 2},
    "counts_by_agent": {"merli": {"idea": 2}},
    "items": [RATED, UNRATED],
}
DETAIL = {
    "agent": "merli",
    "item": RATED,
    "detail": "# Solar kiln\n\nDry lumber with a passive solar kiln.",
    "files": [f"{RATED['path']}/idea.md"],
    "progress": None,
}


def _route(page) -> None:
    page.route(
        re.compile(r"/api/cortex/items/idea-e2e-1(\?|$)"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(DETAIL)
        ),
    )
    page.route(
        re.compile(r"/api/cortex/items(\?|$)"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(LIST)
        ),
    )


def test_rated_card_shows_its_score_and_the_drawer_its_reasoning(page, dashboard) -> None:
    _route(page)
    page.goto(
        dashboard.base_url.rstrip("/") + "/cortex", wait_until="domcontentloaded", timeout=20_000
    )
    wait_for_app_shell(page)
    page.locator(".cortex-tabstrip button", has_text="Ideas").first.click()
    page.get_by_text("Solar kiln", exact=True).first.wait_for(state="visible", timeout=15_000)
    page.get_by_text("Moss garden", exact=True).first.wait_for(state="visible", timeout=15_000)

    # Exactly one badge: the rated item's, reading its score; the unrated card
    # carries none. The hover title names the recommendation and the reason.
    badges = page.locator(BADGE)
    assert badges.count() == 1
    badge = badges.first
    assert badge.inner_text().strip() == "8/10"
    assert "promote" in (badge.get_attribute("title") or "")
    assert "lumber drying" in (badge.get_attribute("title") or "")
    # Inside the viewport at this width (the chip row wraps on a phone).
    box = badge.bounding_box()
    width = page.evaluate("window.innerWidth")
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width

    page.get_by_text("Solar kiln", exact=True).first.click()
    page.get_by_text("Next step:", exact=True).first.wait_for(state="visible", timeout=15_000)
    drawer_badge = page.locator(f".drawer {BADGE}, [role='dialog'] {BADGE}").first
    drawer_badge.wait_for(state="visible", timeout=15_000)
    assert drawer_badge.inner_text().strip() == "8/10 · promote"
    assert page.get_by_text(
        "Scaffold a one-page sizing calculator.", exact=False
    ).first.is_visible()
    assert page.get_by_text("lumber drying", exact=False).first.is_visible()
