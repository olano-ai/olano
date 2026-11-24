"""``GET /api/shell/stream`` is reachable from the dashboard and from nowhere else.

What only a real browser can verify: the provenance headers. The dashboard's
own ``EventSource`` and ``fetch`` calls arrive with the browser's genuine
``Sec-Fetch-Site: same-origin`` / ``Sec-Fetch-Mode: cors``, and a top-level
navigation to the same URL (what a link on another site would produce) arrives
with ``Sec-Fetch-Mode: navigate``. The unit tests hand-write those headers;
this test lets Chromium send them.

Every request below uses a command outside the allowlist, so a request that
passes the cross-site gate is answered "not in allowlist" and nothing is ever
executed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

REFUSAL = "cross_site_request_refused"
COMMAND = "rm -rf /"


def _first_event(page, base: str, via: str) -> str:
    """Open the stream from the page's own origin and return the first event."""
    script = """
    async ([base, command, via]) => {
      const url = `${base}/api/shell/stream?command=${encodeURIComponent(command)}`;
      if (via === "fetch") {
        const resp = await fetch(url);
        const reader = resp.body.getReader();
        const { value } = await reader.read();
        reader.cancel();
        return new TextDecoder().decode(value);
      }
      return await new Promise((resolve, reject) => {
        const es = new EventSource(url);
        es.onmessage = (e) => { es.close(); resolve(e.data); };
        es.onerror = () => { es.close(); reject(new Error("eventsource error")); };
      });
    }
    """
    return page.evaluate(script, [base, COMMAND, via])


@pytest.mark.parametrize("via", ["eventsource", "fetch"])
def test_the_dashboards_own_requests_reach_the_command_validator(page, dashboard, via) -> None:
    page.goto(f"{dashboard.base_url}/tools", wait_until="domcontentloaded")
    first = _first_event(page, dashboard.base_url.rstrip("/"), via)
    assert REFUSAL not in first
    assert "not in allowlist" in first


def test_a_navigation_to_the_stream_is_refused(page, dashboard) -> None:
    """A link click from anywhere is a navigation; the stream refuses it."""
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/api/shell/stream?command={COMMAND}", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert REFUSAL in body
    assert "not in allowlist" not in body
