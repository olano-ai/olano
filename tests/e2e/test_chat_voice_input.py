"""Composer voice input E2E: the mic, the gray interim tail, the ⌘D toggle.

Real microphone capture can't be driven from a headless browser, so these tests
install a **fake `SpeechRecognition`** before the app loads and then push
recognition events at it by hand. That is the seam that matters: everything
downstream — the interim overlay, the commit-into-the-draft rule, the recording
lane, the keyboard toggle — is olano's own code, and it is what breaks.

Runs at both viewports (the shared ``page`` fixture), which is the point on the
mic button: it is a pointer-driven press-and-hold control, and mobile is where
long-press gestures normally get eaten by selection/callout handling.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

COMPOSER = ".composer textarea"
MIC = ".composer-mic"
MIRROR = ".dictation-mirror"
INTERIM = ".dictation-mirror .interim"
BAR = ".dictation-bar"

# Stands in for the browser's speech engine. Records instances so the test can
# drive onresult/onend, and mirrors the real API closely enough that the hook
# can't tell: continuous/interimResults flags, a results list with isFinal, and
# resultIndex so only new results are read.
FAKE_SPEECH = """
window.__recognizers = [];
class FakeRecognition {
  constructor() {
    this.lang = ""; this.continuous = false; this.interimResults = false;
    this.onresult = null; this.onerror = null; this.onend = null;
    this.started = false;
    window.__recognizers.push(this);
  }
  start() { this.started = true; }
  stop() { this.started = false; if (this.onend) this.onend(); }
  abort() { this.started = false; if (this.onend) this.onend(); }
  // Test helper: deliver one recognition result.
  __emit(transcript, isFinal) {
    const results = [{ 0: { transcript }, isFinal, length: 1 }];
    results.length = 1;
    if (this.onresult) this.onresult({ resultIndex: 0, results });
  }
}
window.SpeechRecognition = FakeRecognition;
window.webkitSpeechRecognition = FakeRecognition;
"""


def _open_chat(page, base_url: str):
    """Load /chat with the fake speech engine installed, return the composer.

    Returns only once the view has *settled*, not merely painted. On ``/chat``
    with no active session ChatView runs an arrival effect that fetches the
    agent's thread list (``GET /api/agents/<agent>/threads``) and then opens
    the latest conversation, or a fresh draft; that switch of the active
    conversation fires the "leaving the conversation releases the mic" effect,
    i.e. ``voice.cancel()``. A test that presses the mic inside that window
    starts a dictation the arrival then cancels, and waits for a "Listening…"
    bar that never comes. So: arm for the arrival's fetch before navigating,
    wait for its answer, for the arrival spinner to be gone, and for the mic
    itself. (Not ``networkidle`` — the dashboard holds an SSE stream open, so
    the page is never network-idle.)
    """
    page.add_init_script(FAKE_SPEECH)
    with page.expect_response(
        lambda r: r.request.method == "GET" and "/threads" in r.url, timeout=15_000
    ):
        page.goto(base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "() => { const e = document.querySelector('.topbar .muted');"
        " return !!e && e.textContent.trim().length > 0; }",
        timeout=15_000,
    )
    # The fetch has answered; its `.then` opens the conversation on the next
    # tick and the spinner it showed meanwhile goes away with it.
    page.wait_for_selector(".chat-log .spinner", state="detached", timeout=10_000)
    page.wait_for_timeout(300)
    page.locator(MIC).wait_for(state="visible", timeout=10_000)
    return composer


def _latest_recognizer(page) -> str:
    return "window.__recognizers[window.__recognizers.length - 1]"


# The composer classifies a press shorter than TAP_THRESHOLD_MS (300ms, see
# views/chat/voice.ts) as a *tap*, which switches to hands-free toggle mode
# instead of stopping. Driving the page over CDP is fast enough that a whole
# assertion sequence lands inside that window, so a test meaning to exercise
# press-and-hold has to spend real wall-clock time before releasing.
TAP_THRESHOLD_MS = 300


def _hold_past_tap_threshold(page) -> None:
    page.wait_for_timeout(TAP_THRESHOLD_MS + 150)


def test_mic_button_renders(page, dashboard):
    """A browser with speech recognition always gets the mic — no backend needed."""
    _open_chat(page, dashboard.base_url)
    mic = page.locator(MIC)
    mic.wait_for(state="visible", timeout=10_000)
    assert mic.is_enabled()


def test_hold_to_dictate_shows_interim_then_commits(page, dashboard):
    """Interim text renders dimmed and separate; a final lands in the draft."""
    composer = _open_chat(page, dashboard.base_url)
    mic = page.locator(MIC)
    mic.wait_for(state="visible", timeout=10_000)

    # Press and hold. dispatch_event, not click(): the button is driven by
    # pointerdown/pointerup, and a synthesized click fires neither.
    mic.dispatch_event("pointerdown", {"pointerId": 1, "button": 0})
    page.wait_for_selector(BAR, timeout=5_000)
    page.wait_for_selector(f"{MIC}.rec", timeout=5_000)
    page.wait_for_selector(".composer-box.recording", timeout=5_000)

    # Partial words: shown in the overlay, dimmed, NOT yet in the draft.
    page.evaluate(f"{_latest_recognizer(page)}.__emit('hello wor', false)")
    page.wait_for_selector(INTERIM, timeout=5_000)
    assert page.locator(INTERIM).inner_text().strip() == "hello wor"
    # The textarea carries the tail for sizing, but the draft itself is empty:
    # sending mid-utterance must not ship half a word.
    assert page.locator(MIRROR).count() == 1

    # The engine settles on the phrase → it becomes ordinary editable text.
    page.evaluate(f"{_latest_recognizer(page)}.__emit('hello world', true)")
    page.wait_for_function(
        "() => document.querySelector('.composer textarea').value.includes('hello world')",
        timeout=5_000,
    )
    assert composer.input_value().strip() == "hello world"
    # Interim cleared → overlay gone, textarea visible again.
    assert page.locator(INTERIM).count() == 0

    _hold_past_tap_threshold(page)
    mic.dispatch_event("pointerup", {"pointerId": 1, "button": 0})
    page.wait_for_selector(BAR, state="detached", timeout=5_000)
    assert composer.input_value().strip() == "hello world"


def test_dictation_appends_to_an_existing_draft(page, dashboard):
    """Dictation adds to what you typed instead of replacing it."""
    composer = _open_chat(page, dashboard.base_url)
    composer.fill("typed part")
    mic = page.locator(MIC)
    mic.dispatch_event("pointerdown", {"pointerId": 1, "button": 0})
    page.wait_for_selector(BAR, timeout=5_000)
    page.evaluate(f"{_latest_recognizer(page)}.__emit('spoken part', true)")
    page.wait_for_function(
        "() => document.querySelector('.composer textarea').value.includes('spoken part')",
        timeout=5_000,
    )
    assert composer.input_value().strip() == "typed part spoken part"


def test_tap_toggles_and_second_tap_stops(page, dashboard):
    """A quick tap keeps listening — the only hands-free mode phones have."""
    _open_chat(page, dashboard.base_url)
    mic = page.locator(MIC)
    mic.wait_for(state="visible", timeout=10_000)

    # Deliberately no wait between down and up: this is the *tap*, the one
    # gesture that must NOT stop on release.
    mic.dispatch_event("pointerdown", {"pointerId": 1, "button": 0})
    mic.dispatch_event("pointerup", {"pointerId": 1, "button": 0})
    # Still listening after release — that's the toggle.
    page.wait_for_selector(f"{MIC}.rec", timeout=5_000)
    assert page.locator(BAR).count() == 1

    mic.dispatch_event("pointerdown", {"pointerId": 2, "button": 0})
    mic.dispatch_event("pointerup", {"pointerId": 2, "button": 0})
    page.wait_for_selector(BAR, state="detached", timeout=5_000)


def test_trailing_interim_is_kept_when_recording_stops(page, dashboard):
    """Words the engine never finalized still make it into the draft."""
    composer = _open_chat(page, dashboard.base_url)
    mic = page.locator(MIC)
    mic.dispatch_event("pointerdown", {"pointerId": 1, "button": 0})
    page.wait_for_selector(BAR, timeout=5_000)
    page.evaluate(f"{_latest_recognizer(page)}.__emit('unfinished words', false)")
    page.wait_for_selector(INTERIM, timeout=5_000)

    _hold_past_tap_threshold(page)
    mic.dispatch_event("pointerup", {"pointerId": 1, "button": 0})
    page.wait_for_selector(BAR, state="detached", timeout=5_000)
    # The overlay is gone, so this is a real commit — not the interim tail still
    # riding along in the textarea's value, which would read the same.
    assert page.locator(INTERIM).count() == 0
    assert composer.input_value().strip() == "unfinished words"


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_meta_d_toggles_dictation(page, dashboard):
    """⌘D / Ctrl+D is a keyboard affordance — desktop only, by definition."""
    _open_chat(page, dashboard.base_url)
    page.locator(MIC).wait_for(state="visible", timeout=10_000)

    page.keyboard.press("Control+d")
    page.wait_for_selector(BAR, timeout=5_000)
    page.wait_for_selector(f"{MIC}.rec", timeout=5_000)

    page.evaluate(f"{_latest_recognizer(page)}.__emit('typed by voice', true)")
    page.keyboard.press("Control+d")
    page.wait_for_selector(BAR, state="detached", timeout=5_000)
    assert "typed by voice" in page.locator(COMPOSER).input_value()


def test_mic_is_hidden_without_browser_speech_recognition(page, dashboard):
    """No Web Speech API ⇒ no mic, even when the box can transcribe server-side.

    This is Firefox. The server *could* transcribe an uploaded recording, but
    that is a different product — no words while you speak, a wait on release,
    and a charge per use — so offering it behind the same icon reads as the mic
    being broken here. Note the absence of the fake engine: this loads the page
    with the API explicitly removed.
    """
    page.add_init_script(
        "Object.defineProperty(window,'SpeechRecognition',"
        "{value:undefined,configurable:true});"
        "Object.defineProperty(window,'webkitSpeechRecognition',"
        "{value:undefined,configurable:true});"
    )
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    page.locator(COMPOSER).wait_for(state="visible", timeout=15_000)
    # The composer is up, so a mic would have rendered by now if it were going to.
    page.locator(".composer-attach").wait_for(state="visible", timeout=10_000)
    assert page.locator(MIC).count() == 0
