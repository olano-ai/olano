# Dashboard E2E tests

Browser-driven tests that exercise the **Mission Control dashboard GUI** with
Playwright — React rendering, client-side routing, and the auth gate as a user
sees them. These cover what the in-process FastAPI `TestClient` tests in
`tests/test_gateway_api.py` cannot (those never render a page).

## Running

```bash
make test-e2e          # installs node_modules + chromium, then runs the suite
# or directly:
uv run pytest tests/e2e -q -m e2e
```

They are **opt-in**: the default `make test` / `uv run pytest` run excludes them
(`-m "not e2e"`) because they need Node and a browser. Each fixture degrades to a
clean `pytest.skip` when a prerequisite is missing, so the suite never hard-fails
on a machine without the tooling.

## Prerequisites

- **Node + node_modules** — `cd olano/dashboard_2.0 && npm install` (once).
- **Chromium** — `uv run playwright install chromium`. In the managed/CI image
  the browser is pre-baked and Playwright is pinned to the matching `1.56.x`
  line (`pyproject.toml`), so no download is required.

## How it works

`harness.py` boots a throwaway dashboard:

1. the FastAPI runtime in-process under uvicorn, wrapping a **mock gateway**
   (same shape as `tests/test_gateway_api.py`) — deterministic, no API keys;
2. the Vite dev server, which serves the SPA and proxies `/api` to the runtime.

`conftest.py` exposes a session-scoped `dashboard` fixture and a `page` fixture
(Playwright sync API) already navigated to the dashboard root. Tests are plain
`def` (not `async`) so the sync API runs outside any event loop.

## Adding tests

```python
import pytest
pytestmark = pytest.mark.e2e

def test_my_feature(page):
    page.goto(page.url.split("/overview")[0] + "/scheduler")
    page.locator("aside.sidebar[aria-label='Primary']").wait_for(state="visible")
    # ... interact with page.fill / page.click and assert on the DOM
```

Stable anchors: title `Olano · Mission Control`, React root `#root`, and the
authenticated shell `aside.sidebar[aria-label="Primary"]`.

For interactive, screenshot-driven verification (vs. regression), use the
`olano-tester` skill at `.claude/skills/olano-tester/` — it shares this
harness.

## Testing features that need real LLM / tool keys

The mock gateway returns canned responses, so flows that call a real model or
tool (chat completions, Tavily search, etc.) need real credentials. Provide them
as environment variables before running — e.g. `DEEPSEEK_API_KEY`,
`TAVILY_API_KEY` — or via a `.env` file at the repo root (auto-loaded). Never
commit keys. See the project secrets docs / vault for the supported names.
