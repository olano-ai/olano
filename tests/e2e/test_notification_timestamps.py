"""E2E: a notification says how old it is.

"Background run 'memory_garden' failed — seen 3 times" is a different problem
depending on whether the last one was five minutes or five days ago, and the
list said neither: the API had carried ``created_at``/``updated_at`` since the
store was written, and nothing rendered them. The stamp is a DOM-shape fact an
in-process API test can't see — that the row carries a real ``<time>`` with the
machine-readable ``datetime`` beside the human phrase, and that the phrase
tracks the record's age rather than always reading "just now".

``updated_at`` is what a row shows, which is why the aged fixture below moves
that field: on a deduplicated row it is the last recurrence, and that is the one
an operator acts on.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

FRESH_KEY = "e2e-notification-age-fresh"  # gitleaks:allow
AGED_KEY = "e2e-notification-age-aged"
FRESH_TITLE = "E2E notification raised just now"
AGED_TITLE = "E2E notification raised hours ago"
AGED_HOURS = 3


@pytest.fixture
def aged_notifications(dashboard) -> Iterator[None]:
    """Two rows: one raised this second, one backdated three hours.

    Both are raised in-process because the runtime shares this one — the bell
    has no create endpoint, only producers inside the runtime raise rows.
    ``notify()`` hands back the stored record itself, so moving its stamp is
    how a test gets an old notification without waiting three hours for one.
    """
    from olano.runtime import notifications

    notifications.notify(
        kind="e2e_age",
        level="warn",
        title=FRESH_TITLE,
        body="Raised at the moment this test ran.",
        dedupe_key=FRESH_KEY,
    )
    aged = notifications.notify(
        kind="e2e_age",
        level="warn",
        title=AGED_TITLE,
        body="Backdated so the row has an age worth rendering.",
        dedupe_key=AGED_KEY,
    )
    aged.updated_at = (
        datetime.now(timezone.utc) - timedelta(hours=AGED_HOURS, minutes=1)
    ).isoformat()
    try:
        yield
    finally:
        # The store is a process-wide singleton shared with the session-scoped
        # dashboard, so leaving the rows behind would leak into later tests.
        notifications.resolve(FRESH_KEY)
        notifications.resolve(AGED_KEY)


def _row(page, title: str):
    """The notification card carrying `title`, in the Overview list.

    Scoped to `.notification-row` so the bell panel's copy of the same row (it
    renders the identical component) can never make the locator ambiguous under
    Playwright's strict mode.
    """
    return page.locator(".notification-row").filter(has_text=title)


def test_a_notification_shows_how_long_ago_it_was_raised(page, dashboard, aged_notifications):
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/overview", wait_until="domcontentloaded")
    wait_for_app_shell(page)

    fresh = _row(page, FRESH_TITLE).locator("time.notification-row-time")
    fresh.wait_for(state="visible", timeout=15_000)
    assert fresh.inner_text().strip() == "just now"

    aged = _row(page, AGED_TITLE).locator("time.notification-row-time")
    aged.wait_for(state="visible", timeout=15_000)
    # The point of the feature: an older row reads older, rather than every row
    # reading "just now" because the stamp was never rendered.
    assert aged.inner_text().strip() == f"{AGED_HOURS} hours ago"


def test_the_stamp_is_a_time_element_carrying_the_machine_readable_date(
    page, dashboard, aged_notifications
):
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/overview", wait_until="domcontentloaded")
    wait_for_app_shell(page)

    stamp = _row(page, FRESH_TITLE).locator("time.notification-row-time")
    stamp.wait_for(state="visible", timeout=15_000)

    # A <time datetime=…>, not a bare span: the phrase is a rounded rendering,
    # and the exact instant has to survive somewhere a machine can read it.
    assert stamp.evaluate("el => el.tagName") == "TIME"
    iso = stamp.get_attribute("datetime")
    assert iso, "the stamp must carry a datetime attribute"
    parsed = datetime.fromisoformat(iso)
    assert parsed.tzinfo is not None, f"expected a tz-aware stamp, got {iso!r}"
    # …and the tooltip carries the unrounded version for a reader who needs it.
    assert (stamp.get_attribute("title") or "").strip()
