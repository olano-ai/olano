"""The live view paced by its viewer (olano/runtime/routes/browser.py).

A viewer that cannot draw frames as fast as they come is the one that
cannot take taps either (a phone's web view decodes each on the thread the
taps need), so the stream is paced by the viewer: the next frame waits for
``shown``, a slow viewer is stepped down QUALITY_LADDER to smaller frames
and back up, and a tap the page does not take in POINTER_TIMEOUT_S takes the
stale taps queued behind it with it. The decisions are pure and tested here;
the screencast half runs against Chromium in test_browser_live_chrome.py.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from olano.browser.live import QUALITY_LADDER
from olano.runtime.routes.browser import (
    FAST_ACK_S,
    LEVEL_HOLD_S,
    SLOW_ACK_S,
    FramePacing,
    drop_stale_input,
)

STEPS = len(QUALITY_LADDER)


def test_a_slow_viewer_is_stepped_down_one_level_at_a_time_and_back_up():
    pacing = FramePacing()
    now = 100.0
    # Slow from the first frame: one step, then none until LEVEL_HOLD_S passed.
    assert pacing.shown(SLOW_ACK_S * 2, now, STEPS) == 1
    assert pacing.shown(SLOW_ACK_S * 2, now + 1, STEPS) is None
    assert pacing.shown(SLOW_ACK_S * 2, now + LEVEL_HOLD_S + 0.1, STEPS) == 2
    # Fast again: back up, a step at a time.
    later = now + 2 * LEVEL_HOLD_S + 1
    assert pacing.shown(FAST_ACK_S / 2, later, STEPS) == 1
    assert pacing.shown(FAST_ACK_S / 2, later + LEVEL_HOLD_S + 0.1, STEPS) == 0


def test_the_ladder_has_a_bottom_and_a_top():
    """A viewer slow for a long while ends at the smallest frames, never
    beyond; fast again for a while, it climbs back to the largest. The
    average follows the frames, so a stretch of either takes a few of them."""
    pacing = FramePacing()
    t = 0.0
    for _ in range(STEPS + 2):
        t += LEVEL_HOLD_S + 1
        for _ in range(20):
            pacing.shown(10.0, t, STEPS)
    assert pacing.level == STEPS - 1
    for _ in range(STEPS + 2):
        t += LEVEL_HOLD_S + 1
        for _ in range(20):
            pacing.shown(0.0, t, STEPS)
    assert pacing.level == 0


def test_one_slow_frame_among_fast_ones_changes_nothing():
    pacing = FramePacing()
    t = LEVEL_HOLD_S + 1
    for _ in range(10):
        assert pacing.shown(FAST_ACK_S / 2, t, STEPS) is None
    # The average moves a third of the way per frame: one spike stays under the bar.
    assert pacing.shown(SLOW_ACK_S * 1.5, t, STEPS) is None
    assert pacing.level == 0


def test_a_timed_out_tap_takes_the_stale_taps_with_it_and_keeps_releases():
    pending: deque[dict[str, Any]] = deque(
        [
            {"t": "move", "x": 0.1, "y": 0.1},
            {"t": "down", "x": 0.1, "y": 0.1},
            {"t": "up", "x": 0.1, "y": 0.1},
            {"t": "keydown", "key": "a"},
            {"t": "keyup", "key": "a"},
            {"t": "text", "text": "hi"},
            {"t": "wheel", "dx": 0, "dy": 40},
            {"t": "navigate", "url": "https://example.test/"},
            {"t": "select", "index": 1},
        ]
    )
    assert drop_stale_input(pending) == 6
    assert [e["t"] for e in pending] == ["up", "keyup", "navigate"]


def test_dropping_nothing_is_fine():
    pending: deque[dict[str, Any]] = deque()
    assert drop_stale_input(pending) == 0
    assert list(pending) == []
