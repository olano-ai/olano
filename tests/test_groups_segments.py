"""Rebuilding a group reply's ordered spans from the frames the route forwards.

A 1:1 turn gets its ``segments`` from inside the runner (``_take_text``), which
sees the raw event stream. A group turn cannot — ``groups_routes`` consumes the
runner's *output* — so it rebuilds them the way the CLIENT does, and this is
that algorithm on its own.

Three producers now build a turn's spans (the runner, this, and the dashboard's
``turnText.appendText``) and they have to agree on one invariant: **the spans
join back to the body**. ``turnBlocks.buildTurnBlocks`` silently falls back to
the old stacked layout when they do not (``segmentsCover``), with no error
anywhere, so a producer that drifts turns interleaving off product-wide rather
than failing. Every case here checks that join.
"""

from __future__ import annotations

from olano.groups.segments import SegmentAccumulator


def _joined(acc: SegmentAccumulator) -> str:
    return "".join(s["text"] for s in acc.spans)


def test_a_turn_with_no_tools_writes_no_segments():
    """One span is the pre-segment layout — the renderer handles its absence —
    so an ordinary reply's row stays byte-identical to earlier releases."""
    acc = SegmentAccumulator()
    acc.text("Hello ")
    acc.text("there.")
    assert acc.body == "Hello there."
    assert acc.segments() == []
    assert _joined(acc) == acc.body


def test_text_tool_text_opens_a_span_behind_the_call():
    acc = SegmentAccumulator()
    acc.text("Looking it up.")
    acc.tool_call("call-1")
    # The runner has already inserted the paragraph break; nothing here adds one.
    acc.text("\n\n")
    acc.text("Found it.")
    assert acc.segments() == [
        {"text": "Looking it up.", "after": None},
        {"text": "\n\nFound it.", "after": "call-1"},
    ]
    assert _joined(acc) == acc.body == "Looking it up.\n\nFound it."


def test_a_turn_that_starts_behind_a_tool_tags_its_first_span():
    acc = SegmentAccumulator()
    acc.tool_call("call-1")
    acc.text("Here it is.")
    assert acc.segments() == []  # one span — nothing to interleave
    assert acc.spans == [{"text": "Here it is.", "after": "call-1"}]


def test_two_calls_between_the_same_paragraphs_stay_one_break():
    """The span names the LAST call before it; the renderer flushes every tool
    from its cursor through that id, so both cards land in one block."""
    acc = SegmentAccumulator()
    acc.text("One.")
    acc.tool_call("c1")
    acc.tool_call("c2")
    acc.text("\n\nTwo.")
    assert acc.segments() == [
        {"text": "One.", "after": None},
        {"text": "\n\nTwo.", "after": "c2"},
    ]


def test_a_tool_that_never_gets_an_id_still_breaks_the_span():
    """A partial ``tool_call`` frame arrives before its id is readable. An empty
    ``after`` is an id no tool has, which the renderer treats as "flush every
    tool still pending" — a tool is never dropped."""
    acc = SegmentAccumulator()
    acc.text("Before.")
    acc.tool_call(None)
    acc.text("After.")
    assert [s["after"] for s in acc.spans] == [None, ""]


def test_a_trailing_tool_call_adds_no_empty_span():
    """A turn whose last event is a tool call has nothing after it to write."""
    acc = SegmentAccumulator()
    acc.text("Done.")
    acc.tool_call("c1")
    assert acc.segments() == []
    assert acc.body == "Done."


def test_empty_chunks_are_dropped_rather_than_stored():
    acc = SegmentAccumulator()
    acc.text("")
    acc.text("a")
    acc.text("")
    assert acc.spans == [{"text": "a", "after": None}]
    assert acc.body == "a"


def test_the_stripped_body_still_satisfies_the_join():
    """The route persists ``acc.body.strip()``. ``segmentsCover`` compares
    whitespace-stripped joins on both sides, so stripping is safe — anything
    that rewrote the body further would have to rewrite the spans with it."""
    acc = SegmentAccumulator()
    acc.text("  Looking.")
    acc.tool_call("c1")
    acc.text("\n\nFound it.  ")
    stripped = acc.body.strip()
    joined = "".join(s["text"] for s in acc.segments())
    assert "".join(joined.split()) == "".join(stripped.split())
