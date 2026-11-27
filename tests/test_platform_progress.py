"""Tests for live progress rendering (olano/platforms/progress.py).

The pure layer: folding ``astream_events`` tuples into a ProgressState and
rendering it per verbosity + markup dialect.
"""

from __future__ import annotations

import json

from olano.platforms.progress import (
    MAX_NORMAL_STEPS,
    MAX_PROGRESS_CHARS,
    ProgressState,
    TodoItem,
    describe_tool,
    feed_event,
    normalize_tool_name,
    parse_todos,
    render_progress,
    settle_todos,
)
from olano.platforms.verbosity import PLAIN, TELEGRAM_LEGACY


def _call(name: str, args: dict, call_id: str = "c1") -> tuple[str, dict]:
    return ("tool_call", {"id": call_id, "name": name, "args": json.dumps(args)})


def _result(content: str, call_id: str = "c1", name: str = "tool") -> tuple[str, dict]:
    return ("tool_result", {"id": call_id, "name": name, "content": content})


def _todos(*items: tuple[str, str]) -> tuple[str, dict]:
    return _call(
        "write_todos",
        {"todos": [{"content": c, "status": s} for c, s in items]},
        call_id="todo-1",
    )


# ── tool descriptions ───────────────────────────────────────────────────────


def test_normalize_strips_mcp_and_namespace_prefixes():
    assert normalize_tool_name("mcp__github__search_code") == "search_code"
    assert normalize_tool_name("server.read_file") == "read_file"
    assert normalize_tool_name("ns/grep") == "grep"


def test_describe_picks_the_salient_input():
    desc = describe_tool("read_file", {"file_path": "olano/core/runner.py", "offset": 10})
    assert desc.verb == "Reading"
    assert desc.detail == "olano/core/runner.py"


def test_describe_recognizes_memory_search():
    desc = describe_tool("search_memory", {"query": "telegram token"})
    assert desc.icon == "🧠"
    assert desc.verb == "Searching memory"
    assert desc.detail == "telegram token"


def test_describe_labels_customer_memory_tools_by_what_they_do():
    """`remember` saves a fact; it used to read "Searching memory"."""
    saved = describe_tool("remember", {"fact": "prefers email", "section": "General"})
    assert saved.verb == "Saving to memory"
    assert saved.detail == "prefers email"
    assert describe_tool("memory_save", {"fact": "x"}).verb == "Saving to memory"
    recall = describe_tool("recall", {"query": "shipping address"})
    assert recall.verb == "Searching memory"
    assert recall.detail == "shipping address"
    assert describe_tool("recall_all", {}).verb == "Reading memory"
    forget = describe_tool("forget_me", {})
    assert forget.verb == "Forgetting what is stored"
    assert describe_tool("memory_read", {}).verb == "Reading memory"


def test_describe_recognizes_web_search():
    desc = describe_tool("web_search", {"query": "spiderman showtimes"})
    assert desc.verb == "Searching the web"
    assert desc.detail == "spiderman showtimes"


def test_describe_peer_and_subagent_calls():
    peer = describe_tool("talk_to_agent", {"agent_name": "kevin", "message": "status?"})
    assert peer.verb == "Asking kevin"
    assert peer.detail == "status?"
    sub = describe_tool("task", {"subagent_type": "research", "description": "find prior art"})
    assert sub.verb == "Delegating to research"
    assert sub.detail == "find prior art"


def test_unknown_tool_falls_back_to_its_name_and_first_scalar_arg():
    desc = describe_tool("frobnicate", {"widget": "left-handed"})
    assert desc.verb == "frobnicate"
    assert "left-handed" in desc.detail


def test_describe_survives_empty_args():
    desc = describe_tool("read_file", {})
    assert desc.verb == "Reading"
    assert desc.detail == ""


# ── todos ───────────────────────────────────────────────────────────────────


def test_parse_todos_accepts_double_encoded_json():
    args = {"todos": json.dumps([{"content": "step one", "status": "pending"}])}
    todos = parse_todos(args)
    assert [t.content for t in todos] == ["step one"]


def test_parse_todos_defaults_unknown_status_to_pending():
    todos = parse_todos({"todos": [{"content": "x", "status": "banana"}]})
    assert todos[0].status == "pending"


def test_settle_promotes_in_progress_but_leaves_pending():
    settled = settle_todos(
        [
            TodoItem("a", "completed"),
            TodoItem("b", "in_progress"),
            TodoItem("c", "pending"),
        ]
    )
    assert [t.status for t in settled] == ["completed", "completed", "pending"]


def test_todo_call_replaces_the_list_and_never_becomes_a_step():
    state = ProgressState()
    assert feed_event(state, *_todos(("first", "completed"), ("second", "in_progress")))
    assert feed_event(state, *_todos(("first", "completed"), ("second", "completed")))
    assert [t.status for t in state.todos] == ["completed", "completed"]
    assert state.steps == []


def test_empty_todo_list_is_not_a_visible_change():
    state = ProgressState()
    assert feed_event(state, *_call("write_todos", {"todos": []}, call_id="t")) is False


# ── state folding ───────────────────────────────────────────────────────────


def test_tool_result_closes_its_call():
    state = ProgressState()
    feed_event(state, *_call("read_file", {"file_path": "a.py"}))
    assert state.steps[0].finished is False
    feed_event(state, *_result("file contents"))
    assert state.steps[0].finished is True
    assert state.steps[0].failed is False
    assert state.done_count == 1


def test_error_results_are_flagged():
    state = ProgressState()
    feed_event(state, *_call("execute", {"command": "pytest"}))
    feed_event(state, *_result("Error: command not found"))
    assert state.steps[0].failed is True


def test_orphan_result_is_ignored():
    state = ProgressState()
    assert feed_event(state, *_result("stray", call_id="nope")) is False


def test_unparseable_args_still_produce_a_step():
    state = ProgressState()
    # astream_events yields args as a raw JSON string that can be truncated
    # mid-stream — the step must survive with no detail rather than vanish.
    assert feed_event(state, "tool_call", {"id": "c1", "name": "grep", "args": '{"pattern": "fo'})
    assert state.steps[0].name == "grep"


def test_repeated_subagent_text_only_counts_once():
    state = ProgressState()
    payload = {"agents": ["kevin"], "namespace": ("peer",)}
    assert feed_event(state, "subagent_text", payload) is True
    assert feed_event(state, "subagent_text", payload) is False


def test_text_events_do_not_affect_progress():
    state = ProgressState()
    assert feed_event(state, "text", "hello") is False
    assert state.has_content is False


# ── rendering ───────────────────────────────────────────────────────────────


def test_quiet_renders_nothing():
    state = ProgressState()
    feed_event(state, *_call("read_file", {"file_path": "a.py"}))
    assert render_progress(state, "quiet") == ""


def test_empty_state_renders_nothing():
    assert render_progress(ProgressState(), "normal") == ""


def test_normal_shows_checklist_and_salient_inputs():
    state = ProgressState()
    feed_event(state, *_todos(("Read the diff", "completed"), ("Fix the fallback", "in_progress")))
    feed_event(state, *_call("read_file", {"file_path": "src/app.py"}))
    out = render_progress(state, "normal", PLAIN)
    assert "📋 Plan — 1/2" in out
    assert "✅ Read the diff" in out
    assert "⏳ Fix the fallback" in out
    assert "Reading src/app.py" in out


def test_normal_hides_tool_results():
    state = ProgressState()
    feed_event(state, *_call("read_file", {"file_path": "a.py"}))
    feed_event(state, *_result("SECRET FILE BODY"))
    assert "SECRET FILE BODY" not in render_progress(state, "normal", PLAIN)


def test_verbose_shows_all_inputs_and_the_returned_output():
    state = ProgressState()
    feed_event(state, *_call("grep", {"pattern": "occurrence", "path": "src/"}))
    feed_event(state, *_result("3 matches found"))
    out = render_progress(state, "verbose", PLAIN)
    assert "occurrence" in out
    assert "src/" in out
    assert "3 matches found" in out


def test_steps_append_and_all_stay_visible_up_to_the_cap():
    # The view grows like a log — each tool call joins the list (openclaw
    # style), it does not slide a tiny window.
    state = ProgressState()
    for i in range(8):
        feed_event(state, *_call("read_file", {"file_path": f"f{i}.py"}, call_id=f"c{i}"))
    out = render_progress(state, "normal", PLAIN)
    for i in range(8):
        assert f"f{i}.py" in out
    assert "earlier step(s)" not in out


def test_steps_beyond_the_cap_roll_off_oldest_first():
    state = ProgressState()
    for i in range(MAX_NORMAL_STEPS + 4):
        feed_event(state, *_call("read_file", {"file_path": f"f{i}.py"}, call_id=f"c{i}"))
    out = render_progress(state, "normal", PLAIN)
    assert "earlier step(s)" in out
    assert "f0.py" not in out
    assert f"f{MAX_NORMAL_STEPS + 3}.py" in out


def test_over_budget_render_drops_oldest_steps_but_keeps_the_newest():
    # When the full log would blow the message budget, the window shrinks
    # from the top — end-truncation would cut the very lines the user is
    # watching for.
    state = ProgressState()
    for i in range(15):
        feed_event(state, *_call("execute", {"command": f"cmd-{i} " + "x" * 200}, call_id=f"c{i}"))
        feed_event(state, *_result("y" * 280, call_id=f"c{i}"))
    out = render_progress(state, "verbose", PLAIN)
    assert len(out) <= MAX_PROGRESS_CHARS
    assert "cmd-14" in out
    assert "cmd-0 " not in out
    assert "earlier step(s)" in out


def test_settled_drops_the_working_header_and_promotes_in_progress():
    state = ProgressState()
    feed_event(state, *_todos(("Ship it", "in_progress")))
    live = render_progress(state, "normal", PLAIN)
    settled = render_progress(state, "normal", PLAIN, settled=True)
    assert "🧭 Working…" in live
    assert "🧭" not in settled
    assert "✅ Ship it" in settled


def test_failed_step_is_marked():
    state = ProgressState()
    feed_event(state, *_call("execute", {"command": "pytest"}))
    feed_event(state, *_result("Error: boom"))
    assert "⚠️" in render_progress(state, "normal", PLAIN)


def test_telegram_legacy_dialect_emits_no_nested_entities():
    state = ProgressState()
    feed_event(state, *_todos(("Fix the parser", "in_progress")))
    feed_event(state, *_call("read_file", {"file_path": "a_b_c.py"}))
    out = render_progress(state, "normal", TELEGRAM_LEGACY)
    # Legacy Markdown has no ** and rejects nested entities; the underscore-y
    # path must sit inside a code span, not an italic run.
    assert "**" not in out
    assert "`a_b_c.py`" in out


def test_render_is_capped():
    state = ProgressState()
    for i in range(40):
        feed_event(state, *_call("execute", {"command": "x" * 500}, call_id=f"c{i}"))
        feed_event(state, *_result("y" * 2000, call_id=f"c{i}"))
    assert len(render_progress(state, "verbose", PLAIN)) <= MAX_PROGRESS_CHARS
