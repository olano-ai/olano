"""Unit tests for the browser tools (no real Chromium; see *_live.py for that)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.security.request_context import push_request_context


def _close_coro_and_return(expected: str):
    def _runner(coro, **kwargs):
        coro.close()
        return expected

    return _runner


@pytest.fixture
def browser_on(monkeypatch):
    """Headless mode on, on a host the hardware gate accepts."""
    from olano.browser import policy, sessions
    from olano.config.system_settings import set_browser_settings

    set_browser_settings({"mode": "headless"})
    monkeypatch.setattr(policy, "_host", lambda: (4, 8 * policy.GIB))
    monkeypatch.setattr(policy, "available_memory_bytes", lambda: None)
    sessions.reset_for_tests()
    yield
    sessions.reset_for_tests()


def test_tools_refuse_while_the_browser_is_off():
    """Default config: every tool answers with the reason, nothing launches."""
    from olano.tools.browser import (
        browser_click,
        browser_navigate,
        browser_navigate_and_screenshot,
        browser_snapshot,
    )

    for tool, args in (
        (browser_navigate, {"url": "https://example.com"}),
        (browser_snapshot, {}),
        (browser_click, {"selector": "e1"}),
        (browser_navigate_and_screenshot, {"url": "https://example.com"}),
    ):
        result = tool.invoke(args)
        assert result.startswith("Browser control is turned off"), (tool.name, result)


@pytest.mark.parametrize(
    "url",
    [
        "view-source:https://example.com/",
        "chrome://version",
        "devtools://devtools/bundled/inspector.html",
        "javascript:alert(1)",
        "about:version",
    ],
)
def test_non_web_schemes_are_refused_before_any_browser_work(url, browser_on, tmp_path):
    from olano.tools.browser import browser_navigate, browser_navigate_and_screenshot

    with (
        patch("olano.tools.browser._run_async") as run,
        push_request_context(metadata={"workspace_root": str(tmp_path)}),
    ):
        result = browser_navigate.invoke({"url": url})
        capture = browser_navigate_and_screenshot.invoke({"url": url})
    assert result.startswith("Error: ") and capture.startswith("Error: "), (result, capture)
    run.assert_not_called()


def test_browser_navigate_refuses_metadata_address(browser_on, monkeypatch):
    monkeypatch.setenv("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", "1")
    from olano.tools.browser import browser_navigate

    with patch("olano.tools.browser._run_async") as run:
        result = browser_navigate.invoke({"url": "http://169.254.169.254/latest/meta-data/"})
    assert "metadata" in result
    run.assert_not_called()


def test_browser_navigate_returns_runner_output(browser_on):
    with patch(
        "olano.tools.browser._run_async",
        side_effect=_close_coro_and_return("# Test Page\nURL: https://example.com\n\nok"),
    ):
        from olano.tools.browser import browser_navigate

        result = browser_navigate.invoke({"url": "https://example.com"})
    assert result.startswith("# Test Page")


def test_read_tools_without_a_session_say_so(browser_on):
    from olano.tools.browser import browser_extract, browser_screenshot, browser_snapshot

    assert "browser_navigate first" in browser_snapshot.invoke({})
    assert "browser_navigate first" in browser_extract.invoke({"selector": "body"})
    assert "browser_navigate first" in browser_screenshot.invoke({"filename": "x"})


def test_browser_close_when_no_browser(browser_on):
    from olano.tools.browser import browser_close

    assert browser_close.invoke({}) == "Browser closed."


def test_browser_close_maps_stringified_results_to_message(browser_on):
    from olano.tools.browser import browser_close

    for raw in ("None", "True", "False"):
        with patch("olano.tools.browser._run_async", side_effect=_close_coro_and_return(raw)):
            assert browser_close.invoke({}) == "Browser closed."


def test_browser_navigate_and_screenshot_returns_runner_output(browser_on, tmp_path):
    expected = (
        f"Screenshot saved: {tmp_path}/example-com.png (12.4 KB)\n"
        "Page: https://example.com\nTitle: Example Domain"
    )
    with patch("olano.tools.browser._run_async", side_effect=_close_coro_and_return(expected)):
        from olano.tools.browser import browser_navigate_and_screenshot

        result = browser_navigate_and_screenshot.invoke({"url": "https://example.com"})
    assert "Screenshot saved" in result


def test_page_screenshot_alias(browser_on, tmp_path):
    expected = f"Screenshot saved: {tmp_path}/shot.png (18.0 KB)\nPage: https://example.com\nTitle: Example"
    with (
        patch("olano.tools.browser._run_async", side_effect=_close_coro_and_return(expected)),
        push_request_context(metadata={"workspace_root": str(tmp_path)}),
    ):
        from olano.tools.browser import page_screenshot

        result = page_screenshot.invoke(
            {"url": "https://example.com", "save_path": str(tmp_path / "shot.png")}
        )
    assert "Screenshot saved" in result


def test_detect_browser_block_page():
    from olano.browser.blockpage import detect

    block = detect(
        "https://www.wsj.com/",
        "The Wall Street Journal",
        "Access is temporarily restricted. We detected unusual activity from your device.",
    )
    assert block is not None and block.site == "wsj.com"
    assert "restricted" in block.signal.lower() or "unusual" in block.signal.lower()


def test_screenshot_helpers_honor_request_context(tmp_path):
    """`_workspace_root` / `_screenshots_dir` resolve the *agent's* workspace from
    the request context, not the legacy/global workspace."""
    import olano.tools.browser as b

    agent_ws = tmp_path / "agents" / "sinclair" / "workspace"
    agent_ws.mkdir(parents=True)

    with push_request_context(metadata={"workspace_root": str(agent_ws)}):
        assert b._workspace_root() == agent_ws
        shots = b._screenshots_dir()

    assert shots == agent_ws / "screenshots"
    assert shots.is_dir()


def _mock_playwright(on_screenshot):
    """An async-playwright mock chain; `on_screenshot(path)` runs when the page
    screenshot is taken (use it to create the file so `.stat()` works)."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="Example")
    page.inner_text = AsyncMock(return_value="hello world")
    page.url = "https://example.com"
    page.is_closed = MagicMock(return_value=False)

    def _shot(path=None, full_page=False):
        on_screenshot(path)

    page.screenshot = AsyncMock(side_effect=_shot)

    ctx = MagicMock()
    ctx.new_page = AsyncMock(return_value=page)
    ctx.route = AsyncMock()
    ctx.route_web_socket = AsyncMock()
    ctx.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=ctx)
    browser.close = AsyncMock()
    browser.is_connected = MagicMock(return_value=True)
    started = MagicMock()
    started.chromium.launch = AsyncMock(return_value=browser)
    started.stop = AsyncMock()
    pw_cm = MagicMock()
    pw_cm.start = AsyncMock(return_value=started)
    return pw_cm, ctx


def test_navigate_and_screenshot_saves_in_agent_workspace_with_guards(browser_on, tmp_path):
    """The default screenshot path lands under the agent's workspace, and the
    one-shot context gets the request guard like every other context."""
    import olano.tools.browser as b

    agent_ws = tmp_path / "agents" / "sinclair" / "workspace"
    agent_ws.mkdir(parents=True)
    written: dict[str, str] = {}

    def _record(path):
        written["path"] = path
        Path(path).write_bytes(b"PNGDATA")

    pw_cm, ctx = _mock_playwright(_record)
    b._active_isolated.clear()
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_cm),
        push_request_context(metadata={"workspace_root": str(agent_ws)}),
    ):
        result = b.browser_navigate_and_screenshot.invoke({"url": "https://example.com"})

    assert "Screenshot saved" in result, result
    saved = Path(written["path"])
    assert saved.parent == agent_ws / "screenshots"
    assert saved.exists()
    ctx.route.assert_awaited()  # every request of the throwaway context is checked
    ctx.close.assert_awaited()
    assert not b._active_isolated


def test_run_async_reuses_one_persistent_loop():
    """Sequential `_run_async` calls land on the SAME live loop: Playwright
    objects are bound to the loop that created them, and the old per-call
    `asyncio.run()` left every second browser call hanging on a dead loop."""
    import asyncio

    import olano.tools.browser as b

    seen: list = []

    async def capture():
        seen.append(asyncio.get_running_loop())
        return "ok"

    assert b._run_async(capture()) == "ok"
    assert b._run_async(capture()) == "ok"
    assert seen[0] is seen[1]
    assert seen[0].is_running()


def test_run_async_timeout_closes_that_agents_session(browser_on):
    """A timed-out call can strand a half-opened session; the timeout branch
    closes the calling agent's session so its next call starts clean."""
    import olano.tools.browser as b

    closed: list[str] = []

    async def fake_close(agent, *, save=True):
        closed.append(agent)
        return True

    async def stuck():
        import asyncio as _asyncio

        await _asyncio.sleep(30)

    with patch("olano.browser.sessions.close", side_effect=fake_close):
        result = b._run_async(stuck(), timeout_s=0.2, agent="sinclair")
        import time as _time

        deadline = _time.time() + 5
        while _time.time() < deadline and not closed:
            _time.sleep(0.05)
    assert "timed out" in result
    assert "browser session reset" in result
    assert closed == ["sinclair"]


def test_run_async_callable_from_inside_running_loop():
    """`_run_async` must not deadlock when the tool is invoked on a thread that
    already runs an event loop (runtime / LangGraph path)."""
    import asyncio

    import olano.tools.browser as b

    seen: list = []

    async def capture():
        seen.append(asyncio.get_running_loop())
        return "ok"

    async def caller():
        return b._run_async(capture())

    assert asyncio.run(caller()) == "ok"
    assert seen[0].is_running()


def test_toolkit_exports_every_gated_tool():
    """policy.BROWSER_TOOL_NAMES is the gate; it must name every tool this
    module exports, or a new tool would bind while the browser is off."""
    from olano.browser.policy import BROWSER_TOOL_NAMES
    from olano.tools.browser import get_tools

    assert {t.name for t in get_tools()} == set(BROWSER_TOOL_NAMES)


# ── local files and where a tool may save ────────────────────────────────────


@pytest.fixture
def host_secret(tmp_path_factory) -> Path:
    secret = tmp_path_factory.mktemp("runtime-host") / "vault.txt"
    secret.write_text("RUNTIME-HOST-SECRET")
    return secret


def test_a_file_outside_the_workspace_is_refused_before_any_browser_work(
    browser_on, tmp_path, host_secret
):
    from olano.tools.browser import browser_navigate, browser_navigate_and_screenshot

    with (
        patch("olano.tools.browser._run_async") as run,
        push_request_context(metadata={"workspace_root": str(tmp_path)}),
    ):
        result = browser_navigate.invoke({"url": host_secret.as_uri()})
        capture = browser_navigate_and_screenshot.invoke({"url": "file:///etc/passwd"})
    assert "outside this agent's workspace" in result
    assert "outside this agent's workspace" in capture
    run.assert_not_called()


def test_local_files_are_refused_where_the_browser_is_not_on_this_host(
    browser_on, tmp_path, monkeypatch
):
    """The live sidecar and a remote provider have their own filesystems."""
    from olano.config.system_settings import set_browser_settings
    from olano.tools.browser import browser_navigate

    page = tmp_path / "report.html"
    page.write_text("<html></html>")
    set_browser_settings({"mode": "headless", "provider": "remote"})
    with (
        patch("olano.tools.browser._run_async") as run,
        push_request_context(metadata={"workspace_root": str(tmp_path)}),
    ):
        result = browser_navigate.invoke({"url": page.as_uri()})
    assert "does not run on this machine" in result
    run.assert_not_called()


def test_a_workspace_symlink_is_judged_by_its_target(tmp_path, host_secret):
    from olano.browser.files import file_access

    link = tmp_path / "report.html"
    link.symlink_to(host_secret)
    reason = file_access(tmp_path, "").refusal(link.as_uri())
    assert reason and "outside this agent's workspace" in reason


def test_the_saved_signins_can_be_neither_opened_nor_overwritten(tmp_path, monkeypatch):
    from olano.browser import sessions
    from olano.browser.files import file_access, save_check

    signins = tmp_path / "home-browser-state"
    monkeypatch.setattr(sessions, "state_dir", lambda: signins)
    profile = signins / "alice.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}")
    # Even when the sign-ins sit under a root the agent may read and write.
    opened = file_access(tmp_path, "").refusal(profile.as_uri())
    assert opened and "saved sign-ins" in opened
    saved = save_check(profile, tmp_path, "")
    assert saved and "saved sign-ins" in saved


def test_a_file_boundary_admits_the_workspace_and_nothing_else(tmp_path):
    from olano.browser.files import file_access

    files = file_access(tmp_path, "")
    assert files.refusal((tmp_path / "other.html").as_uri()) is None
    assert files.refusal("file:///etc/passwd") is not None
    assert files.refusal("file://otherhost/share/x") is not None


def test_skill_roots_and_builtin_skills_are_readable(tmp_path):
    from olano.browser.files import file_access
    from olano.skills.built_in_sync import _builtin_source_dir

    vega = _builtin_source_dir() / "charts" / "assets" / "vendor" / "vega.min.js"
    assert file_access(tmp_path, "").refusal(vega.as_uri()) is None


async def test_file_urls_are_refused_where_no_local_file_was_opened():
    from olano.browser.guard import GuardPolicy, url_refusal

    reason = await url_refusal("file:///etc/passwd", GuardPolicy(allow=["a.example"]))
    assert reason and "Local files cannot be opened" in reason


def test_save_path_outside_the_workspace_is_refused_before_any_browser_work(
    browser_on, tmp_path, tmp_path_factory
):
    from olano.tools.browser import browser_navigate_and_screenshot

    outside = tmp_path_factory.mktemp("runtime-host") / "config.yaml"
    with (
        patch("olano.tools.browser._run_async") as run,
        push_request_context(metadata={"workspace_root": str(tmp_path)}),
    ):
        result = browser_navigate_and_screenshot.invoke(
            {"url": "https://a.example/", "save_path": str(outside)}
        )
    assert result.startswith("Error: '") and "cannot be saved there" in result
    assert not outside.exists()
    run.assert_not_called()


def test_save_path_through_a_symlinked_directory_is_judged_by_its_target(
    tmp_path, tmp_path_factory
):
    from olano.tools.browser import _save_check

    outside = tmp_path_factory.mktemp("runtime-host")
    (tmp_path / "out").symlink_to(outside)
    reason = _save_check(tmp_path / "out" / "x.png", tmp_path, "alice")
    assert reason and "cannot be saved there" in reason
    assert _save_check(tmp_path / "x.png", tmp_path, "alice") is None


def test_write_access_excludes_the_read_only_roots(tmp_path):
    from olano.skills.built_in_sync import _builtin_source_dir
    from olano.tools.browser import _file_access

    writable = _file_access(tmp_path, "alice", for_write=True)
    vega = _builtin_source_dir() / "charts" / "assets" / "vendor" / "vega.min.js"
    assert writable.refusal_path(vega) is not None
    assert writable.refusal_path(tmp_path / "renders" / "x.png") is None


# ── targets: refs and selectors ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("selector", "ref", "expected"),
    [
        ("", "e12", "aria-ref=e12"),
        ("", "[ref=e12]", "aria-ref=e12"),
        ("", "ref=f1e3", "aria-ref=f1e3"),
        ("e7", "", "aria-ref=e7"),
        ("button.submit", "", "button.submit"),
    ],
)
def test_targets_resolve_refs_and_selectors(selector, ref, expected):
    from olano.tools.browser import _target

    assert _target(selector, ref) == expected


@pytest.mark.parametrize(("selector", "ref"), [("", ""), ("#a", "e1"), ("", "button")])
def test_bad_targets_are_refused_before_any_browser_work(selector, ref, browser_on):
    from olano.tools.browser import browser_click, browser_type

    with patch("olano.tools.browser._run_async") as run:
        clicked = browser_click.invoke({"selector": selector, "ref": ref})
        typed = browser_type.invoke({"text": "x", "selector": selector, "ref": ref})
    assert clicked.startswith("Error: ") and typed.startswith("Error: ")
    run.assert_not_called()
