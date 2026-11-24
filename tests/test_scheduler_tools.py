"""Tests for scheduler agent tools."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def reset_db(tmp_path):
    """Reset the module-level DB singleton between tests."""
    import olano.tools.scheduler_tools as st
    from olano.scheduler.db import SchedulerDB

    st._db = SchedulerDB(db_path=tmp_path / "test_scheduler.db")
    yield
    st._db = None


def test_scheduler_create_recurring():
    from olano.tools.scheduler_tools import scheduler_create

    with patch("olano.tools.scheduler_tools.embed_text", None):
        result = scheduler_create.invoke(
            {
                "name": "Daily email check",
                "prompt": "Check my email and summarize unread messages",
                "schedule": "0 9 * * *",
            }
        )

    assert "Daily email check" in result
    assert "created" in result.lower()


def test_scheduler_create_with_model_and_effort_overrides(monkeypatch):
    """The per-job overrides are validated against the model's ladder, stored,
    echoed back, and shown by scheduler_get."""
    import olano.tools.scheduler_tools as st
    from olano.tools.scheduler_tools import scheduler_create, scheduler_get

    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    with patch("olano.tools.scheduler_tools.embed_text", None):
        result = scheduler_create.invoke(
            {
                "name": "Weekly report",
                "prompt": "Write the weekly report",
                "schedule": "0 9 * * 1",
                "model": "openai:gpt-6-sol",
                "reasoning_effort": "HIGH",
            }
        )
    assert "Task created!" in result
    assert "Model: openai:gpt-6-sol" in result
    assert "Reasoning effort: high" in result
    (task,) = st._db.list_tasks()
    assert task["model"] == "openai:gpt-6-sol"
    assert task["reasoning_effort"] == "high"
    shown = scheduler_get.invoke({"task_id": task["id"][:8]})
    assert "Model: openai:gpt-6-sol" in shown
    assert "Reasoning effort: high" in shown


def test_scheduler_create_rejects_an_effort_the_model_does_not_accept(monkeypatch):
    import olano.tools.scheduler_tools as st
    from olano.tools.scheduler_tools import scheduler_create

    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    with patch("olano.tools.scheduler_tools.embed_text", None):
        result = scheduler_create.invoke(
            {
                "name": "Weekly report",
                "prompt": "Write it",
                "schedule": "0 9 * * 1",
                "model": "openai:gpt-6-sol",
                "reasoning_effort": "ultra",
            }
        )
    assert result.startswith("Error:")
    assert "does not accept reasoning_effort 'ultra'" in result
    assert st._db.list_tasks() == []


def test_scheduler_update_clears_an_override_with_default(monkeypatch):
    """Empty keeps (as every other field); the word 'default' clears."""
    import olano.tools.scheduler_tools as st
    from olano.tools.scheduler_tools import scheduler_create, scheduler_update

    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    with patch("olano.tools.scheduler_tools.embed_text", None):
        scheduler_create.invoke(
            {
                "name": "Weekly report",
                "prompt": "Write it",
                "schedule": "0 9 * * 1",
                "model": "openai:gpt-6-sol",
                "reasoning_effort": "high",
            }
        )
    (task,) = st._db.list_tasks()
    # A bad new effort is refused against the STORED model.
    bad = scheduler_update.invoke({"task_id": task["id"], "reasoning_effort": "ultra"})
    assert bad.startswith("Error:")
    assert st._db.get_task(task["id"])["reasoning_effort"] == "high"
    ok = scheduler_update.invoke({"task_id": task["id"], "reasoning_effort": "default"})
    assert "updated" in ok
    after = st._db.get_task(task["id"])
    assert after["reasoning_effort"] == ""
    assert after["model"] == "openai:gpt-6-sol"  # empty argument kept it


def test_scheduler_create_one_time():
    from olano.tools.scheduler_tools import scheduler_create

    with patch("olano.tools.scheduler_tools.embed_text", None):
        result = scheduler_create.invoke(
            {
                "name": "Send report",
                "prompt": "Send the weekly report",
                "run_at": "2026-04-01T09:00:00",
            }
        )

    assert "Send report" in result
    assert "created" in result.lower()


def test_scheduler_create_missing_schedule():
    from olano.tools.scheduler_tools import scheduler_create

    result = scheduler_create.invoke(
        {
            "name": "Broken task",
            "prompt": "Do something",
        }
    )
    assert "Error" in result


def test_scheduler_list_empty():
    from olano.tools.scheduler_tools import scheduler_list

    result = scheduler_list.invoke({})
    assert "No scheduled tasks" in result


def test_scheduler_list_with_tasks():
    from olano.tools.scheduler_tools import scheduler_create, scheduler_list

    with patch("olano.tools.scheduler_tools.embed_text", None):
        scheduler_create.invoke(
            {
                "name": "Task A",
                "prompt": "Do A",
                "schedule": "0 9 * * *",
            }
        )
        scheduler_create.invoke(
            {
                "name": "Task B",
                "prompt": "Do B",
                "schedule": "0 10 * * *",
            }
        )

    result = scheduler_list.invoke({})
    assert "Task A" in result
    assert "Task B" in result


def test_scheduler_list_filter():
    from olano.tools.scheduler_tools import scheduler_create, scheduler_list

    with patch("olano.tools.scheduler_tools.embed_text", None):
        scheduler_create.invoke(
            {"name": "Email task", "prompt": "Check email", "schedule": "0 9 * * *"}
        )
        scheduler_create.invoke(
            {"name": "Deploy task", "prompt": "Deploy service", "schedule": "0 8 * * *"}
        )

    result = scheduler_list.invoke({"filter_text": "email"})
    assert "Email task" in result
    assert "Deploy task" not in result


def test_scheduler_update():
    from olano.tools.scheduler_tools import scheduler_create, scheduler_update

    with patch("olano.tools.scheduler_tools.embed_text", None):
        scheduler_create.invoke({"name": "Old name", "prompt": "Prompt", "schedule": "0 9 * * *"})

    # Get the ID
    import olano.tools.scheduler_tools as st

    tasks = st._db.list_tasks()
    task_id = tasks[0]["id"][:8]

    result = scheduler_update.invoke({"task_id": task_id, "name": "New name", "enabled": "false"})
    assert "updated" in result.lower()

    updated = st._db.list_tasks()[0]
    assert updated["name"] == "New name"
    assert updated["enabled"] is False


def test_scheduler_delete():
    from olano.tools.scheduler_tools import scheduler_create, scheduler_delete

    with patch("olano.tools.scheduler_tools.embed_text", None):
        scheduler_create.invoke(
            {"name": "To delete", "prompt": "Delete me", "schedule": "0 9 * * *"}
        )

    import olano.tools.scheduler_tools as st

    task_id = st._db.list_tasks()[0]["id"][:8]

    result = scheduler_delete.invoke({"task_id": task_id})
    assert "deleted" in result.lower()
    assert st._db.list_tasks() == []


def test_scheduler_history_empty():
    from olano.tools.scheduler_tools import scheduler_create, scheduler_history

    with patch("olano.tools.scheduler_tools.embed_text", None):
        scheduler_create.invoke({"name": "No history", "prompt": "Prompt", "schedule": "0 9 * * *"})

    import olano.tools.scheduler_tools as st

    task_id = st._db.list_tasks()[0]["id"][:8]

    result = scheduler_history.invoke({"task_id": task_id})
    assert "No execution history" in result


def test_scheduler_search_no_embeddings():
    from olano.tools.scheduler_tools import scheduler_create, scheduler_search

    with patch("olano.tools.scheduler_tools.embed_text", None):
        scheduler_create.invoke({"name": "Task", "prompt": "Do stuff", "schedule": "0 9 * * *"})

    result = scheduler_search.invoke({"query": "check email"})
    # Either finds nothing (no embeddings) or returns an error about embeddings
    assert isinstance(result, str)


# ── scheduler_get: the read the toolkit never had ─────────────────────────────
#
# scheduler_list emits no prompt at all, scheduler_search 100 characters, and
# scheduler_run_now 200 only as a side effect of firing the job. So an agent
# could never read the instructions it was about to replace — scheduler_update
# treats an empty argument as "keep", which makes every edit a blind overwrite.

LONG_PROMPT = ("Compile the weekly team progress cadence and post it. " * 20).strip()


def _bind(agent: str):
    """Bind the per-turn agent identity the scheduler tools scope against."""
    from olano.tools.talk_to import current_agent

    return current_agent.set(agent)


def _create_for(agent: str, name: str = "Team Progress Cadence", **kw) -> str:
    from olano.tools.scheduler_tools import _get_db

    defaults = {
        "name": name,
        "prompt": LONG_PROMPT,
        "schedule": "0 9 * * 1",
        "agent_name": agent,
        "created_by": agent,
        "visibility": "private",
    }
    return _get_db().create_task(**{**defaults, **kw})


def test_scheduler_get_returns_the_complete_prompt():
    from olano.tools.scheduler_tools import scheduler_get

    token = _bind("ops")
    try:
        task_id = _create_for("ops")
        out = scheduler_get.invoke({"task_id": task_id[:8]})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)

    assert LONG_PROMPT in out, "scheduler_get must not truncate the prompt"
    assert "Team Progress Cadence" in out
    assert "Created by: ops" in out


def test_scheduler_list_and_search_still_do_not_show_the_whole_prompt():
    """The contrast that makes scheduler_get necessary — pinned so a later
    change to either doesn't quietly make the full prompt available twice."""
    from olano.tools.scheduler_tools import scheduler_list

    token = _bind("ops")
    try:
        _create_for("ops")
        listing = scheduler_list.invoke({})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)

    assert "Team Progress Cadence" in listing
    assert LONG_PROMPT not in listing


def test_scheduler_get_refuses_another_agents_private_task():
    from olano.tools.scheduler_tools import scheduler_get

    task_id = _create_for("finance")
    token = _bind("ops")
    try:
        out = scheduler_get.invoke({"task_id": task_id[:8]})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)

    assert "no task found" in out.lower()
    assert LONG_PROMPT not in out


def test_scheduler_get_reads_a_shared_task_from_another_agent():
    from olano.tools.scheduler_tools import scheduler_get

    task_id = _create_for("finance", visibility="shared")
    token = _bind("ops")
    try:
        out = scheduler_get.invoke({"task_id": task_id[:8]})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)

    assert LONG_PROMPT in out


# ── prefix resolution is scoped to the caller ─────────────────────────────────
#
# _resolve_task_id prefix-matched against every agent's tasks, so a guessed
# 8-char prefix reached another agent's private job through run_now and history
# — neither of which had any ownership check of their own.


def test_resolve_task_id_is_scoped_to_a_named_caller():
    from olano.tools.scheduler_tools import _get_db, _resolve_task_id

    task_id = _create_for("finance")
    assert _resolve_task_id(_get_db(), task_id[:8], caller="ops") is None
    assert _resolve_task_id(_get_db(), task_id[:8], caller="finance") == task_id


def test_resolve_task_id_stays_unscoped_with_no_caller_bound():
    """The non-breaking guarantee: the CLI, tests and ephemeral workers have no
    agent identity bound and must keep resolving everything."""
    from olano.tools.scheduler_tools import _get_db, _resolve_task_id

    task_id = _create_for("finance")
    assert _resolve_task_id(_get_db(), task_id[:8]) == task_id


def test_run_now_refuses_another_agents_private_task():
    from olano.tools.scheduler_tools import scheduler_run_now

    task_id = _create_for("finance")
    token = _bind("ops")
    try:
        out = scheduler_run_now.invoke({"task_id": task_id[:8]})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)

    assert "no task found" in out.lower()
    # The old path leaked the first 200 characters of the prompt here.
    assert LONG_PROMPT[:200] not in out


def test_history_refuses_another_agents_private_task():
    from olano.tools.scheduler_tools import _get_db, scheduler_history

    task_id = _create_for("finance")
    _get_db().add_history(task_id, result="a private result")
    token = _bind("ops")
    try:
        out = scheduler_history.invoke({"task_id": task_id[:8]})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)

    assert "no task found" in out.lower()
    assert "a private result" not in out


def test_semantic_search_hides_another_agents_private_task():
    """db.semantic_search had no visibility predicate at all, so it ranked every
    agent's private tasks and returned their names plus a prompt preview."""
    import numpy as np

    from olano.tools.scheduler_tools import _get_db

    vec = np.ones(8, dtype=np.float32)
    _create_for("finance", name="Payroll run", embedding=vec)
    db = _get_db()

    assert [t["name"] for t in db.semantic_search(vec, k=5, caller="finance")] == ["Payroll run"]
    assert db.semantic_search(vec, k=5, caller="ops") == []
    # No caller bound → unchanged behaviour for the dashboard and the CLI.
    assert [t["name"] for t in db.semantic_search(vec, k=5)] == ["Payroll run"]


def test_scheduler_create_with_a_model(monkeypatch):
    from olano.tools.scheduler_tools import scheduler_create, scheduler_get, scheduler_update

    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    with patch("olano.tools.scheduler_tools.embed_text", None):
        result = scheduler_create.invoke(
            {
                "name": "Deep dive",
                "prompt": "Analyse",
                "schedule": "0 9 * * 1",
                "model": "olano:pro",
            }
        )
    assert "Model: olano:pro" in result
    task_id = result.split("ID: ")[1].split("\n")[0]
    assert "olano:pro" in scheduler_get.invoke({"task_id": task_id})

    # 'default' is the one spelling that clears (the tool reads "" as keep).
    result = scheduler_update.invoke({"task_id": task_id[:8], "model": "default"})
    assert "updated" in result and "model" in result


def test_scheduler_create_refuses_an_unusable_model(monkeypatch):
    from olano.tools.scheduler_tools import scheduler_create, scheduler_list

    monkeypatch.setattr(
        "olano.admin.models.model_provider_connected", lambda model: (False, "no key")
    )
    with patch("olano.tools.scheduler_tools.embed_text", None):
        result = scheduler_create.invoke(
            {"name": "x", "prompt": "y", "schedule": "0 9 * * *", "model": "openai:gpt-5"}
        )
    assert result.startswith("Error:") and "openai:gpt-5" in result
    assert "x" not in scheduler_list.invoke({}) or "No tasks" in scheduler_list.invoke({})
