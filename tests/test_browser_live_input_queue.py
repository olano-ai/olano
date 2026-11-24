"""The live view's input queue (olano/runtime/routes/browser.py).

Input is read off the socket as it arrives and replayed by a separate task, so
a page slow to answer cannot leave a tap waiting behind seconds of pointer
moves. Only the newest position matters, so a waiting move is replaced and
wheel deltas add up; every other event keeps its place and order.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from olano.runtime.routes.browser import _enqueue_input


def _queue(*events: dict[str, Any]) -> list[dict[str, Any]]:
    pending: deque[dict[str, Any]] = deque()
    for event in events:
        _enqueue_input(pending, dict(event))
    return list(pending)


def test_a_burst_of_moves_leaves_only_the_newest():
    moves = [{"t": "move", "x": i / 100, "y": 0.5} for i in range(50)]
    assert _queue(*moves) == [{"t": "move", "x": 0.49, "y": 0.5}]


def test_a_tap_keeps_its_place_between_moves():
    out = _queue(
        {"t": "move", "x": 0.1, "y": 0.1},
        {"t": "move", "x": 0.2, "y": 0.2},
        {"t": "down", "x": 0.2, "y": 0.2, "button": "left"},
        {"t": "up", "x": 0.2, "y": 0.2, "button": "left"},
        {"t": "move", "x": 0.3, "y": 0.3},
    )
    assert [e["t"] for e in out] == ["move", "down", "up", "move"]
    assert out[0]["x"] == 0.2


def test_wheel_deltas_add_up():
    out = _queue({"t": "wheel", "dx": 0, "dy": 40}, {"t": "wheel", "dx": 5, "dy": 60})
    assert out == [{"t": "wheel", "dx": 5.0, "dy": 100.0}]


def test_a_malformed_wheel_is_queued_on_its_own():
    out = _queue({"t": "wheel", "dx": 0, "dy": 40}, {"t": "wheel", "dx": "x", "dy": 1})
    assert len(out) == 2
    assert out[0] == {"t": "wheel", "dx": 0, "dy": 40}


def test_keys_and_text_are_never_folded():
    out = _queue(
        {"t": "keydown", "key": "a"},
        {"t": "keydown", "key": "a"},
        {"t": "text", "text": "hi"},
        {"t": "text", "text": "hi"},
    )
    assert len(out) == 4
