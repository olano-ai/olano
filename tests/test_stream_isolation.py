"""Tests for olano.core.stream_isolation — keeping nested calls off the stream.

The bug this guards: a model invoked *inside* the graph (``image_describe``'s
vision call, a ``delegate_task`` worker) inherits the turn's streaming callbacks
and its tokens are rendered as the agent's own reply. With two such calls running
concurrently — one Telegram photo burst, two ``image_describe`` calls — the user
got two transcriptions interleaved chunk-by-chunk into one unreadable answer.
"""

from __future__ import annotations

from langchain_core.runnables.config import var_child_runnable_config

from olano.core.stream_isolation import (
    NOSTREAM_TAG,
    detached_runnable_config,
    has_nostream_tag,
    nostream_config,
)


def test_nostream_tag_matches_langgraph():
    """The suppression is worthless if the tag drifts from LangGraph's.

    ``NOSTREAM_TAG`` is a literal so toolkits can import this module cheaply;
    this is the pin that makes an upstream rename a red CI run rather than a
    silent return of the garble.
    """
    from langgraph.constants import TAG_NOSTREAM

    assert NOSTREAM_TAG == TAG_NOSTREAM


def test_detach_hides_the_ambient_config_and_restores_it():
    sentinel = {"callbacks": ["parent-stream-handler"]}
    token = var_child_runnable_config.set(sentinel)
    try:
        with detached_runnable_config():
            assert var_child_runnable_config.get() == {}
        assert var_child_runnable_config.get() is sentinel
    finally:
        var_child_runnable_config.reset(token)


def test_detach_restores_even_when_the_body_raises():
    sentinel = {"callbacks": ["parent-stream-handler"]}
    token = var_child_runnable_config.set(sentinel)
    try:
        try:
            with detached_runnable_config():
                raise RuntimeError("model blew up")
        except RuntimeError:
            pass
        assert var_child_runnable_config.get() is sentinel
    finally:
        var_child_runnable_config.reset(token)


def test_detach_is_a_noop_for_a_top_level_caller():
    """Scheduler/API/CLI callers already have an empty ambient config."""
    with detached_runnable_config():
        assert var_child_runnable_config.get() == {}


def test_nostream_config_from_nothing():
    assert nostream_config() == {"tags": [NOSTREAM_TAG]}


def test_nostream_config_merges_rather_than_replaces():
    """A caller's recursion_limit / thread_id / own tags must survive."""
    base = {
        "recursion_limit": 12,
        "configurable": {"thread_id": "cog::merli"},
        "tags": ["cognition"],
    }
    tagged = nostream_config(base)
    assert tagged["recursion_limit"] == 12
    assert tagged["configurable"] == {"thread_id": "cog::merli"}
    assert tagged["tags"] == ["cognition", NOSTREAM_TAG]
    # The caller's dict is not mutated under it.
    assert base["tags"] == ["cognition"]


def test_nostream_config_is_idempotent():
    once = nostream_config()
    assert nostream_config(once)["tags"] == [NOSTREAM_TAG]


def test_has_nostream_tag_shapes():
    assert has_nostream_tag({"tags": [NOSTREAM_TAG]})
    assert has_nostream_tag({"tags": ("other", NOSTREAM_TAG)})
    assert has_nostream_tag({"tags": NOSTREAM_TAG})
    assert not has_nostream_tag({"tags": ["other"]})
    assert not has_nostream_tag({"tags": None})
    assert not has_nostream_tag({})
    assert not has_nostream_tag(None)
    assert not has_nostream_tag("nostream")
