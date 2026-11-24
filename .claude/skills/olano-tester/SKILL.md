---
name: olano-tester
category: testing
description: Verify and test Mission Control dashboard GUI features in a real browser with Playwright (rendering, routing, auth gate, SSE) — things in-process API tests can't see.
---

# Olano Tester (dashboard GUI verification)

Use this skill when a task needs the **dashboard GUI itself** exercised — not
just the REST API. The FastAPI `TestClient` tests in `tests/test_gateway_api.py`
hit endpoints in-process and never render a page, so they miss React mounting,
client-side routing, the auth gate, SSE-driven live updates, and "does this view
actually paint" regressions. This skill drives a real Chromium via Playwright.

## When to use

- Verifying a dashboard change works ("does the scheduler view still load?").
- Reproducing a UI bug that only shows in the browser.
- A `/verify`-style confidence check before pushing a frontend change.
- Capturing screenshots of dashboard state for a report or PR.

## What's here

| File | Role |
|------|------|
| `verify_dashboard.py` | Runnable Playwright harness: boots/targets the dashboard, walks a checklist (routes mount, auth gate, react), screenshots each step, prints PASS/FAIL, exits non-zero on failure. |
| `verify_mcp.py` | Worked example of testing a feature that needs a **configured agent** (MCP indicator/tab): builds a custom gateway with an MCP-configured agent + seeded live state, checks the API and the browser UI. Run `uv run python .claude/skills/olano-tester/verify_mcp.py`. |

The pytest counterpart lives in the repo at `tests/e2e/` (regression suite,
`make test-e2e`). Both share the launcher in `tests/e2e/harness.py`, so there is
one definition of "boot a dashboard".

## Architecture facts you need

- The dashboard is a **React + Vite SPA** (`olano/dashboard_2.0/`), served by
  a Node proxy `server.mjs` on **:3200** that forwards `/api/*` to the FastAPI
  runtime on **:8765**. `olano dashboard` boots both.
- **Auth is single-user by default** (`multi_user_enabled()` → False): the
  runtime treats the caller as the implicit owner, so `/api/auth/me` resolves and
  the app lands on `/overview` with no login. (Multi-user mode adds a bootstrap/
  login gate — `phase` in `src/core/auth.tsx`.)
- Stable selectors: title is `Olano · Mission Control`; React mounts into
  `#root`; the authenticated shell is `aside.sidebar[aria-label="Primary"]`.

## Workflow

1. **Get a dashboard running.** Either point at one you started
   (`olano dashboard` → http://localhost:3200) or let the harness spin up a
   throwaway one (`--launch`, deterministic mock backend, no API keys needed).

2. **Run the checklist.**
   ```bash
   # Against a running dashboard:
   python .claude/skills/olano-tester/verify_dashboard.py --url http://localhost:3200

   # Self-contained (run from the repo root):
   python .claude/skills/olano-tester/verify_dashboard.py --launch

   # Watch it live:
   python .claude/skills/olano-tester/verify_dashboard.py --url http://localhost:3200 --headed

   # One viewport only (default is both desktop 1440×900 and mobile 390×844):
   python .claude/skills/olano-tester/verify_dashboard.py --launch --viewport desktop
   ```

3. **Read the result.** Each check prints `[PASS]`/`[FAIL]` prefixed with its
   viewport (`[desktop]` / `[mobile]`); screenshots land in
   `.olano-tester/<timestamp>/` prefixed the same way (`mobile-route-chat.png`).
   Exit code is 0 only if everything passed.

4. **Inspect screenshots** with the Read tool to confirm the UI actually looks
   right — a `[PASS]` only proves the selector appeared, not that it's correct.

## Adding a feature check

Edit `verify_dashboard.py`:
- For a "does this route mount" check, add `(suffix, selector)` to `ROUTE_CHECKS`.
- For richer interaction (fill a form, click, assert a toast), add a step inside
  `run_checks()` using the Playwright `page` API (`page.fill`, `page.click`,
  `page.locator(...).wait_for(...)`), then `report.record(ok, name, detail)`.

For permanent regression coverage, add a test to `tests/e2e/` instead and mark it
`@pytest.mark.e2e` — see `tests/e2e/test_dashboard_smoke.py` for the pattern.

## Recipe: verifying a feature that needs a *configured* agent

The default mock gateway (`harness.build_mock_gateway`) has ONE bare agent
(`merli`) with no MCP servers, no subagents, etc. To exercise UI that only
appears when an agent is configured a certain way (MCP indicator, connectors,
subagents…), build a **custom gateway** and pass it to `launch_dashboard(gateway=…)`.
`verify_mcp.py` is the reference; the moving parts:

1. **Put the agent config where the API reads it.** `agents/` resolution is
   **CWD-relative** (`config.loader._LOCAL_AGENTS_DIR = Path("agents")`;
   `api._agent_config_path` checks `./agents` first). So `os.chdir()` into a temp
   dir holding `agents/<name>/agent.json5` — that isolates your test agent from the
   repo's real `./agents` (which has leftover `test-agent-*` dirs) and `~/.olano`.
2. **Build the gateway** with `harness._gateway_for({name: harness._FakeRunner(cfg, reply)})`.
3. **Async runtime methods must be `AsyncMock`.** A bare `MagicMock` attr (e.g.
   `gw.reload_agent_mcp`) isn't awaitable — `await runtime.reload_agent_mcp(...)`
   throws. Set `gw.<method> = AsyncMock(return_value=…)` for any endpoint that
   awaits the gateway.
4. **Seed module-level live state** the endpoint reads, if any. MCP health/summary
   comes from `builder._MCP_SUMMARY[name]` (populated at real build time); seed it
   directly so `/mcp/health` and the chat indicator render without a real connect.
5. Assert the **API** directly (httpx → `runtime_url`) AND the **browser** (the
   chip/popover/tab actually paint) — they catch different bugs.

## Prerequisites & gotchas

- **Vite binds IPv4 now (was an IPv6 trap).** On macOS, Vite's default `localhost`
  resolves to IPv6 `::1`, but the harness probes / points Playwright at
  `127.0.0.1`. That mismatch used to time out `launch_dashboard` after 60s
  ("Vite dev server to serve the SPA") even though the server was up. Fixed in
  `harness._vite_server` by passing `--host 127.0.0.1`. If you ever hand-launch
  Vite, add that flag or point at `http://localhost:<port>` instead.
- **Throwaway runtime is forced single-user.** `launch_dashboard` now sets a temp
  `LIONCLAW_HOME` for its lifetime, so the runtime boots single-user (no auth
  gate) regardless of whether your real `~/.olano` has multi-user enabled.
  Without this, a multi-user home made every `/api/*` call return **401**, the SPA
  never resolved `/api/auth/me`, and no sidebar rendered (symptom: console shows
  `401 (Unauthorized)`, `app shell visible` times out). When testing a *running*
  dashboard via `--url` instead, either keep it single-user or set
  `LIONCLAW_USER`/`LIONCLAW_PASS` so the checklist signs in.
- **Node + node_modules:** `cd olano/dashboard_2.0 && npm install` once.
- **Chromium:** on a fresh machine run `uv run playwright install chromium`. In
  the managed/CI image the browser (build 1194) is pre-baked and Playwright is
  pinned to the matching 1.56.x line, so no download is needed. If you ever hit a
  Playwright/browser version mismatch, point at an existing binary with
  `LIONCLAW_PW_EXECUTABLE=/path/to/chrome` — the script passes it through and
  adds `--no-sandbox`.
- **Sync API only:** the harness uses Playwright's *sync* API, so call it from
  plain (non-async) code; the pytest tests are sync `def` for this reason.
- These checks need a live browser + Node, so they are **opt-in** and excluded
  from the default `make test` run (`-m "not e2e"`).
