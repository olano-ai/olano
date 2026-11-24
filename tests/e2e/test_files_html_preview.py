"""Files-view HTML preview: *.html renders live in a sandboxed srcdoc iframe.

What only a browser can verify: the frame actually executes the page — its
sibling ``style.css`` / ``app.js``, referenced by relative path, are fetched
through the files API and inlined client-side before the document enters the
frame (from the frame's opaque origin they could never resolve: no session
cookie, no ``?agent=``) — and the ``sandbox`` attribute never grants
``allow-same-origin``, the boundary that keeps agent-authored HTML away from
the dashboard session cookie.

Seeded through the app's own files API: ``PUT /api/files/{path}`` creates the
file and any missing parent directories (see ``write_file`` in
``olano/runtime/api.py``), so one idempotent PUT per file suffices.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

DIR = "e2e-html-preview"
HTML_PATH = f"{DIR}/index.html"

FILES = {
    HTML_PATH: (
        "<!DOCTYPE html><html><head>"
        '<link rel="stylesheet" href="style.css">'
        "</head><body>"
        '<h1 id="hello">Voices of the waterfront</h1>'
        '<script src="app.js"></script>'
        "</body></html>"
    ),
    f"{DIR}/style.css": "#hello { color: rgb(1, 2, 3); }",
    f"{DIR}/app.js": "document.getElementById('hello').dataset.ready = 'yes';",
}

IFRAME = "iframe[title='index.html']"
PREVIEW_HEAD = "strong:text-is('index.html')"


def _open_preview(page, base_url: str) -> None:
    """Seed the fixture files and deep-link straight onto the HTML preview."""
    base = base_url.rstrip("/")
    for path, content in FILES.items():
        resp = page.request.put(f"{base}/api/files/{path}?agent=merli", data={"content": content})
        assert resp.ok, f"could not seed {path}: {resp.status} {resp.text()}"
    page.goto(f"{base}/files?path={HTML_PATH}&agent=merli", wait_until="domcontentloaded")
    page.locator(PREVIEW_HEAD).first.wait_for(state="visible", timeout=15_000)


def test_html_preview_renders_with_inlined_relative_assets(page, dashboard) -> None:
    """Preview is the default: the page renders, its relative script ran, and
    its relative stylesheet applied — both only possible via the inliner."""
    _open_preview(page, dashboard.base_url)

    frame = page.frame_locator(IFRAME)
    heading = frame.locator("#hello")
    heading.wait_for(state="visible", timeout=15_000)

    # app.js was inlined into the srcdoc and executed inside the sandbox.
    frame.locator("#hello[data-ready='yes']").wait_for(state="attached", timeout=10_000)

    # style.css was inlined: a live <link> could never resolve from the
    # frame's opaque origin, so the computed color proves the inlining.
    assert heading.evaluate("el => getComputedStyle(el).color") == "rgb(1, 2, 3)"

    # The security boundary itself: scripts yes, same-origin never.
    sandbox = page.locator(IFRAME).get_attribute("sandbox") or ""
    assert "allow-scripts" in sandbox
    assert "allow-same-origin" not in sandbox


def test_toggle_switches_between_preview_and_raw(page, dashboard) -> None:
    """The Preview/Raw segmented control swaps the iframe for the source view
    and back, exactly like the markdown toggle."""
    _open_preview(page, dashboard.base_url)
    page.frame_locator(IFRAME).locator("#hello").wait_for(state="visible", timeout=15_000)

    page.get_by_role("button", name="Raw", exact=True).click()
    page.locator("pre:has-text('stylesheet')").first.wait_for(state="visible", timeout=10_000)
    assert page.locator(IFRAME).count() == 0

    page.get_by_role("button", name="Preview", exact=True).click()
    page.frame_locator(IFRAME).locator("#hello").wait_for(state="visible", timeout=15_000)
