"""E2E: the Monetization tab's number fields can be cleared and retyped.

They were bound straight to the number, so emptying one wrote 0 back into the
box and typing next to it read "0900". The field now keeps what the person
typed while editing, and the saved value is the validated number.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_price_and_stars_can_be_cleared_and_retyped(page, dashboard) -> None:
    page.goto(
        dashboard.base_url.rstrip("/") + "/agents/merli/monetization", wait_until="domcontentloaded"
    )
    price = page.locator(".field", has_text="Price (cents)").locator("input")
    price.wait_for(state="visible", timeout=20_000)

    price.fill("")
    assert price.input_value() == ""  # stays empty while editing
    price.press_sequentially("900")
    assert price.input_value() == "900"

    stars = page.locator(".field", has_text="Stars amount").locator("input")
    stars.fill("")
    stars.press_sequentially("750")
    assert stars.input_value() == "750"

    # Leaving an emptied field shows what will be saved.
    stars.fill("")
    price.click()
    assert stars.input_value() == "0"
