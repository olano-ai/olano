"""Chat-interface E2E: type a message, assert the streamed reply renders.

Two layers:

  * ``test_chat_streams_assistant_reply`` — runs against the shared mock
    dashboard, so it's deterministic and needs no API key or network. It proves
    the whole chat path wires up: composer → ``/api/chat/stream`` SSE → the
    assistant bubble in the DOM.

  * ``test_chat_live_<provider>`` — the *real-LLM* version. It boots a dashboard
    backed by a real runner for a project-local test agent (e.g.
    ``test-agent-deepseek``) and asserts a non-empty assistant reply renders.
    Skipped automatically unless that provider's API key is in the environment.

Shared UI steps live in ``_send_and_read_reply`` so the mock and live tests
drive the dashboard identically.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e import harness

pytestmark = pytest.mark.e2e

# Selectors (see olano/dashboard_2.0/src/views/chat/ChatView.tsx):
COMPOSER = ".composer textarea"
SEND_BUTTON = ".composer button[aria-label='Send']"
ASSISTANT_BUBBLE = ".bubble.assistant"
COPY_BUTTON = ".msg-actions .msg-copy"
TOPBAR_AGENT = ".topbar .muted"


def _send_and_read_reply(
    page, base_url: str, agent_label: str, message: str, timeout_ms: int = 30_000
) -> str:
    """Open /chat, send ``message``, return the rendered assistant bubble text.

    Waits until the agent roster has resolved and ``agent_label`` is the active
    agent before sending — otherwise the send races the async ``/api/agents``
    fetch and ``runSend`` silently drops the turn (no agent selected yet).
    """
    page.goto(base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")

    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "(label) => { const e = document.querySelector('.topbar .muted');"
        " return e && e.textContent.trim() === label; }",
        arg=agent_label,
        timeout=15_000,
    )
    composer.click()
    composer.fill(message)
    # Click Send rather than press Enter: the button is `disabled` until React
    # commits the composer state, so waiting for it to enable guarantees the
    # value is registered (pressing Enter can fire against a stale empty input).
    send = page.locator(SEND_BUTTON)
    send.wait_for(state="visible", timeout=10_000)
    page.wait_for_function("el => el && !el.disabled", arg=send.element_handle(), timeout=10_000)
    send.click()

    bubble = page.locator(ASSISTANT_BUBBLE).first
    bubble.wait_for(state="visible", timeout=timeout_ms)
    # Let streamed chunks accumulate before reading.
    page.wait_for_function(
        "el => el && el.textContent.trim().length > 0",
        arg=bubble.element_handle(),
        timeout=timeout_ms,
    )
    return (bubble.text_content() or "").strip()


# Runs at BOTH viewports: below the breakpoint the conversation takes the whole
# viewport (the thread rail becomes an off-canvas drawer — see
# test_chat_history_drawer.py), so streaming a reply is the canonical "chat
# works on mobile too" proof.
def test_chat_streams_assistant_reply(page, dashboard) -> None:
    """Deterministic: the mock runner's streamed reply lands in the bubble."""
    reply = _send_and_read_reply(page, dashboard.base_url, "merli", "ping")
    assert harness.CHAT_REPLY_SENTINEL.split(" ")[-1] in reply  # "ZXCVB"


# Runs at BOTH viewports deliberately — the composer's typing behavior is
# *supposed* to differ by pointer type. Touch devices get a 16px font (under
# 16px, iOS Safari auto-zooms the page when the composer is focused and leaves
# it zoomed after the keyboard closes) and Return-inserts-a-newline (only the
# send button submits, like every mobile messenger); desktop keeps the base
# 14px font and Enter-to-send.
def test_chat_composer_touch_vs_desktop_typing(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "(label) => { const e = document.querySelector('.topbar .muted');"
        " return e && e.textContent.trim() === label; }",
        arg="merli",
        timeout=15_000,
    )

    # The mobile fixture's is_mobile/has_touch emulation must present as a
    # coarse pointer — that media feature is the one signal both the CSS
    # (globals.css `@media (pointer: coarse)`) and ChatView's key handler use.
    coarse = page.evaluate("() => matchMedia('(pointer: coarse)').matches")
    assert coarse == (page.viewport_size["width"] < 900)

    expected_font = "16px" if coarse else "14px"
    assert composer.evaluate("el => getComputedStyle(el).fontSize") == expected_font

    composer.click()
    composer.fill("line one")
    # Wait for React to commit the draft (the send button enables) so the
    # Enter keydown below acts on the registered value, not a stale empty one.
    send = page.locator(SEND_BUTTON)
    send.wait_for(state="visible", timeout=10_000)
    page.wait_for_function("el => el && !el.disabled", arg=send.element_handle(), timeout=10_000)
    composer.press("Enter")

    if coarse:
        # Touch: Enter inserted a newline into the draft and nothing was sent.
        page.wait_for_timeout(400)
        assert "\n" in composer.input_value()
        assert page.locator(".bubble.user").count() == 0
    else:
        # Desktop: Enter sent the turn — the draft became a user bubble.
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('.bubble.user'))"
            ".some(e => (e.textContent || '').trim() === 'line one')",
            timeout=10_000,
        )
        assert composer.input_value() == ""


# Pinned to desktop: clipboard + hover-revealed actions are mouse-centric
# interactions (the mobile layout itself is covered by the streaming test
# above and verify_dashboard's mobile pass).
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_chat_reply_has_copy_button(page, dashboard) -> None:
    """Every finished agent reply carries a Copy button that copies its text."""
    # The button writes to — and this test reads back from — the clipboard, so
    # grant the browser context that permission before driving the UI.
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])

    reply = _send_and_read_reply(page, dashboard.base_url, "merli", "ping")
    token = harness.CHAT_REPLY_SENTINEL.split(" ")[-1]  # "ZXCVB"
    assert token in reply

    copy_btn = page.locator(COPY_BUTTON).last
    copy_btn.wait_for(state="visible", timeout=10_000)
    copy_btn.click()

    # The label flips to a "Copied" confirmation only after writeText resolves,
    # so this asserts the copy path actually ran (not just that a button exists).
    # Match every locale's confirmation string (en / zh / es).
    page.wait_for_function(
        "el => /Copied|已复制|Copiado/.test(el.textContent || '')",
        arg=copy_btn.element_handle(),
        timeout=5_000,
    )

    # And the reply text really landed on the clipboard.
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert token in clip


# Pinned to desktop: inline-edit is a mouse-centric flow; the mobile layout is
# covered by the streaming test + verify_dashboard.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_chat_user_message_edit_and_resend(page, dashboard) -> None:
    """A past user message can be edited inline and resent as a new turn."""
    reply = _send_and_read_reply(page, dashboard.base_url, "merli", "ping")
    token = harness.CHAT_REPLY_SENTINEL.split(" ")[-1]  # "ZXCVB"
    assert token in reply

    # Wait for the turn to finish (the composer flips Stop → Send when idle) so
    # the resend runs immediately instead of queueing behind a live stream.
    page.locator(SEND_BUTTON).wait_for(state="visible", timeout=15_000)

    # The user's own message carries copy + edit affordances.
    user_bubble = page.locator(".bubble.user").first
    user_bubble.wait_for(state="visible", timeout=10_000)
    assert (user_bubble.text_content() or "").strip() == "ping"
    assert page.locator(".msg-actions .msg-copy").count() >= 1
    edit_btn = page.locator(".msg-edit-btn").first
    edit_btn.wait_for(state="visible", timeout=10_000)
    edit_btn.click()

    # The inline editor replaces the bubble, seeded with the original text.
    editor = page.locator(".msg-edit-input").first
    editor.wait_for(state="visible", timeout=5_000)
    assert (editor.input_value() or "").strip() == "ping"

    editor.fill("ping edited")
    page.locator(".msg-edit-actions button.primary").click()

    # The edited text lands as a NEW user message (resent turn) and a fresh
    # assistant reply streams in beneath it.
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.bubble.user'))"
        ".some(e => (e.textContent || '').trim() === 'ping edited')",
        timeout=10_000,
    )
    assert page.locator(".bubble.assistant").count() >= 2


# Pinned to desktop: same mouse-centric-flow rationale as the edit test above.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_chat_user_message_edit_cancel(page, dashboard) -> None:
    """Cancelling an inline edit restores the original bubble untouched."""
    _send_and_read_reply(page, dashboard.base_url, "merli", "ping")
    page.locator(SEND_BUTTON).wait_for(state="visible", timeout=15_000)

    page.locator(".msg-edit-btn").first.click()
    editor = page.locator(".msg-edit-input").first
    editor.wait_for(state="visible", timeout=5_000)
    editor.fill("discarded edit")
    page.locator(".msg-edit-actions button.ghost").click()

    # Editor is gone, the original bubble is back, and nothing was resent.
    page.locator(".msg-edit-input").first.wait_for(state="hidden", timeout=5_000)
    assert (page.locator(".bubble.user").first.text_content() or "").strip() == "ping"
    assert page.locator(".bubble.user").count() == 1


# Pinned to desktop: hover-revealed actions need a real pointer. The @media
# (hover:none) fallback that keeps the row visible on touch devices isn't
# exercised here.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_chat_message_time_and_hover_actions(page, dashboard) -> None:
    """Each turn shows a 'time ago' label; its actions stay hidden until hover."""
    reply = _send_and_read_reply(page, dashboard.base_url, "merli", "ping")
    assert harness.CHAT_REPLY_SENTINEL.split(" ")[-1] in reply

    # Let the turn settle (composer flips Stop → Send when idle) so both the
    # user message and the finished reply have rendered their action rows.
    page.locator(SEND_BUTTON).wait_for(state="visible", timeout=15_000)

    # A just-sent turn carries a fresh, localised "just now" label on both the
    # user message and the assistant reply.
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.msg-time'))"
        ".filter(e => /just now|刚刚|ahora mismo/.test(e.textContent || '')).length >= 2",
        timeout=10_000,
    )

    # The actions row (copy/edit + time) is hidden until its message is hovered.
    actions = page.locator(".msg-actions").last
    actions.wait_for(state="attached", timeout=10_000)
    assert actions.evaluate("el => getComputedStyle(el).opacity") == "0"

    # Hovering the reply fades its actions in.
    page.locator(ASSISTANT_BUBBLE).first.hover()
    page.wait_for_function(
        "el => getComputedStyle(el).opacity === '1'",
        arg=actions.element_handle(),
        timeout=5_000,
    )


# ── Live provider tests ───────────────────────────────────────────────────────
# Each entry: (test id, project-local agent name, required env var).
_LIVE_PROVIDERS = [
    ("deepseek", "test-agent-deepseek", "DEEPSEEK_API_KEY"),
]


@pytest.mark.parametrize("provider, agent_name, env_var", _LIVE_PROVIDERS)
def test_chat_live(provider: str, agent_name: str, env_var: str) -> None:
    """Real LLM: send a message to a live agent and assert a reply renders."""
    if not os.environ.get(env_var):
        pytest.skip(f"{env_var} not set — skipping live {provider} chat test")
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    gateway, build_runners = harness.build_real_gateway([agent_name])

    with (
        harness.launch_dashboard(gateway, build_runners) as running,
        sync_playwright() as pw,
    ):
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable ({exc})")
        page = browser.new_context().new_page()
        page.goto(running.base_url, wait_until="domcontentloaded")
        try:
            reply = _send_and_read_reply(
                page,
                running.base_url,
                agent_name,
                "Reply with exactly one short word.",
                timeout_ms=90_000,
            )
            assert reply, f"{provider} returned an empty assistant bubble"
            # The model host may be blocked by the environment's network egress
            # policy (e.g. "Host not in allowlist: api.deepseek.com"). That's an
            # environment limitation, not a chat regression, so skip rather than
            # fail — but only for this live test.
            if "not in allowlist" in reply or "PermissionDeniedError" in reply:
                pytest.skip(f"{provider} host blocked by network egress policy: {reply[:200]}")
            # Guard against a false positive: the runner renders failures as an
            # "**Error:** API error (...)" frame in the same bubble. A real reply
            # must not carry that signature.
            assert "API error" not in reply and not reply.lstrip().startswith("Error:"), (
                f"{provider} chat rendered an error frame: {reply[:200]!r}"
            )
        finally:
            browser.close()
