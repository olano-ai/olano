"""Tests for SQLite scheduler database."""

from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def db(tmp_path):
    from olano.scheduler.db import SchedulerDB

    return SchedulerDB(db_path=tmp_path / "scheduler.db")


def test_create_and_get_task(db):
    task_id = db.create_task(
        name="Daily check",
        prompt="Check my email",
        schedule="0 9 * * *",
    )
    assert task_id
    task = db.get_task(task_id)
    assert task is not None
    assert task["name"] == "Daily check"
    assert task["prompt"] == "Check my email"
    assert task["schedule"] == "0 9 * * *"
    assert task["enabled"] is True
    assert task["run_count"] == 0


def test_list_tasks_empty(db):
    tasks = db.list_tasks()
    assert tasks == []


def test_task_overrides_roundtrip_and_clear(db):
    """`model` / `reasoning_effort` persist per job; NULL means the agent's own."""
    task_id = db.create_task(
        name="Weekly report",
        prompt="Write it",
        schedule="0 9 * * 1",
        model="openai:gpt-6.1-sol",
        reasoning_effort="high",
    )
    task = db.get_task(task_id)
    assert task["model"] == "openai:gpt-6.1-sol"
    assert task["reasoning_effort"] == "high"
    assert db.update_task(task_id, reasoning_effort=None)
    assert db.get_task(task_id)["reasoning_effort"] == ""
    assert db.get_task(task_id)["model"] == "openai:gpt-6.1-sol"

    plain = db.get_task(db.create_task(name="Plain", prompt="p", schedule="0 9 * * *"))
    assert plain["model"] == ""
    assert plain["reasoning_effort"] == ""
    assert [t["name"] for t in db.list_tasks()] == ["Plain", "Weekly report"]


def test_legacy_table_gains_the_override_columns(tmp_path):
    """A scheduler.db created before the columns existed is migrated on open,
    and its rows still read back (the appended columns must stay last)."""
    import sqlite3

    from olano.scheduler.db import SchedulerDB

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL, "
        "schedule TEXT, run_at TEXT, timezone TEXT DEFAULT 'UTC', enabled INTEGER DEFAULT 1, "
        "agent_name TEXT, created_at TEXT NOT NULL, last_run TEXT, next_run TEXT, "
        "run_count INTEGER DEFAULT 0, embedding BLOB, visibility TEXT DEFAULT 'private', "
        "created_by TEXT, platform TEXT, user_id TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks (id, name, prompt, schedule, created_at, created_by) "
        "VALUES ('t1', 'Old job', 'p', '0 9 * * *', '2026-01-01T00:00:00+00:00', 'bob')"
    )
    conn.commit()
    conn.close()

    db = SchedulerDB(db_path=path)
    task = db.get_task("t1")
    assert task is not None
    assert task["created_by"] == "bob"
    assert task["model"] == ""
    assert task["reasoning_effort"] == ""
    assert db.update_task("t1", model="olano:pro", reasoning_effort="high")
    migrated = db.get_task("t1")
    assert migrated["model"] == "olano:pro"
    assert migrated["reasoning_effort"] == "high"


def test_history_stats_for_synthetic_config_id(db):
    """Config-schedule jobs have no `tasks` row — their run stats come straight
    from task_history under the synthetic cfg:… id."""
    job_id = "cfg:cindy:0"
    assert db.history_stats(job_id) == {"run_count": 0, "last_run": None}

    db.add_history(job_id, result="ok")
    db.add_history(job_id, error="boom")
    stats = db.history_stats(job_id)
    assert stats["run_count"] == 2
    assert stats["last_run"] is not None


def test_list_tasks(db):
    db.create_task("Task A", "Do A", schedule="0 9 * * *")
    db.create_task("Task B", "Do B", schedule="0 10 * * *")
    tasks = db.list_tasks()
    assert len(tasks) == 2
    names = {t["name"] for t in tasks}
    assert "Task A" in names
    assert "Task B" in names


def test_list_tasks_enabled_only(db):
    db.create_task("Active", "Do X", schedule="0 9 * * *")
    task_id = db.create_task("Disabled", "Do Y", schedule="0 10 * * *")
    db.update_task(task_id, enabled=0)

    enabled = db.list_tasks(enabled_only=True)
    assert len(enabled) == 1
    assert enabled[0]["name"] == "Active"


def test_update_task(db):
    task_id = db.create_task("Old name", "Old prompt", schedule="0 9 * * *")
    ok = db.update_task(task_id, name="New name", enabled=0)
    assert ok

    task = db.get_task(task_id)
    assert task["name"] == "New name"
    assert task["enabled"] is False


def test_delete_task(db):
    task_id = db.create_task("To delete", "Delete me", schedule="0 9 * * *")
    ok = db.delete_task(task_id)
    assert ok
    assert db.get_task(task_id) is None


def test_delete_nonexistent_task(db):
    ok = db.delete_task("nonexistent-id")
    assert not ok


def test_add_history(db):
    task_id = db.create_task("Task", "Prompt", schedule="0 9 * * *")
    db.add_history(task_id, result="Done")
    db.add_history(task_id, error="Failed")

    history = db.get_history(task_id)
    assert len(history) == 2

    task = db.get_task(task_id)
    assert task["run_count"] == 2


def test_get_history_limit(db):
    task_id = db.create_task("Task", "Prompt", schedule="0 9 * * *")
    for i in range(5):
        db.add_history(task_id, result=f"Run {i}")

    history = db.get_history(task_id, limit=3)
    assert len(history) == 3


def test_semantic_search(db):
    """Semantic search should find similar tasks."""
    # Use simple mock embeddings instead of running real sentence-transformers
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.8, 0.6, 0.0], dtype=np.float32)
    vec_c = np.array([0.0, 0.0, 1.0], dtype=np.float32)

    id_a = db.create_task("Email task", "Check email", schedule="0 9 * * *", embedding=vec_a)
    id_b = db.create_task("Message task", "Check messages", schedule="0 10 * * *", embedding=vec_b)
    id_c = db.create_task("Deploy task", "Deploy service", schedule="0 8 * * *", embedding=vec_c)

    # Query similar to vec_a (email / messaging domain)
    results = db.semantic_search(vec_a, k=2)
    assert len(results) == 2
    ids = [r["id"] for r in results]
    assert id_a in ids  # most similar to itself


def test_semantic_search_no_embeddings(db):
    db.create_task("No embedding task", "Prompt", schedule="0 9 * * *")
    results = db.semantic_search(np.zeros(3, dtype=np.float32), k=5)
    assert results == []


def test_one_time_task(db):
    task_id = db.create_task(
        name="One time",
        prompt="Send report",
        run_at="2026-04-01T09:00:00",
    )
    task = db.get_task(task_id)
    assert task["schedule"] is None
    assert task["run_at"] == "2026-04-01T09:00:00"


def test_model_column_round_trips_and_clears(db):
    """A task may pin its own `provider:model`; NULL/"" is the agent default.

    The column is appended after the multi-user pair (ALTER order), so a row
    read back carries it as the last key — and always as a string, "" for the
    default, so no reader has to special-case None."""
    task_id = db.create_task("Weekly deep dive", "Analyse", schedule="0 9 * * 1", model="olano:pro")
    assert db.get_task(task_id)["model"] == "olano:pro"

    plain = db.create_task("Nightly", "Digest", schedule="0 2 * * *")
    assert db.get_task(plain)["model"] == ""

    assert db.update_task(task_id, model="olano:turbo") is True
    assert db.get_task(task_id)["model"] == "olano:turbo"
    # "" and None both mean "back to the agent's configured model".
    assert db.update_task(task_id, model="") is True
    assert db.get_task(task_id)["model"] == ""
    assert db.update_task(plain, model=None) is True
    assert db.get_task(plain)["model"] == ""
