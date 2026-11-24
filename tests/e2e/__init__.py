"""End-to-end (browser) tests for the Mission Control dashboard.

These tests drive a real Chromium instance (via Playwright) against a live
dashboard so they cover what in-process FastAPI ``TestClient`` tests cannot:
React rendering, routing, SSE streams and the auth gate as a user sees them.

They are opt-in — see ``tests/e2e/README.md``. The default ``make test`` run
skips them (``-m "not e2e"``) because they require Node + a Chromium download.
"""
