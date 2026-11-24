"""The fleet scheduler service layer — both stores, and the read it exists for.

The defect this module fixes: an agent could *list* scheduled tasks but never
read one's instructions. ``scheduler_list`` emits no prompt at all,
``scheduler_search`` emits 100 characters, and ``scheduler_run_now`` emits 200
only as a side effect of firing the job — so every prompt edit was a blind
overwrite of text nobody could see. ``get_task`` returning the whole prompt is
therefore the assertion that matters most here.

The second half is that "everything scheduled" spans **two** stores — the
scheduler database and each agent's ``agent.json5 → schedule:`` block — and a
surface that only reads one of them silently hides jobs. Both are covered.
"""

from __future__ import annotations

import pytest

from olano.admin import scheduler as sched
from olano.admin.agents import create_agent
from olano.scheduler.db import SchedulerDB

# Longer than every truncation point in the old read path (100 in search, 200 in
# run_now, 120 in the dead _format_task) so a regression to any of them fails.
LONG_PROMPT = ("Compile the weekly team progress cadence. " * 20).strip()


@pytest.fixture
def agent_with_schedule():
    """An agent carrying two agent.json5 schedule entries."""
    create_agent(
        "cadence-bot",
        model="fake:instant",
        extra={
            "schedule": [
                {
                    "name": "Team Progress Cadence",
                    "message": LONG_PROMPT,
                    "cron": "0 9 * * 1",
                    "timezone": "America/Costa_Rica",
                },
                {"name": "Second job", "message": "ping", "minute": "0", "hour": "*"},
            ]
        },
    )
    return "cadence-bot"


def _make_db_task(**kw) -> str:
    defaults = {
        "name": "DB job",
        "prompt": LONG_PROMPT,
        "schedule": "0 9 * * *",
        "agent_name": "cadence-bot",
        "created_by": "cadence-bot",
    }
    return SchedulerDB().create_task(**{**defaults, **kw})


# ── id shapes ─────────────────────────────────────────────────────────────────


def test_parse_config_job_id_round_trips_an_agent_name_with_a_colon():
    assert sched.parse_config_job_id("cfg:my:agent:3") == ("my:agent", 3)
    assert sched.config_job_id("my:agent", 3) == "cfg:my:agent:3"


@pytest.mark.parametrize("bad", ["cfg:", "cfg:agent", "cfg:agent:x", "cfg::1"])
def test_parse_config_job_id_raises_valueerror_not_httpexception(bad):
    """runtime/api.py translates this to its 404 — the service layer must stay
    runtime-free, so a malformed id is a plain ValueError here."""
    with pytest.raises(ValueError):
        sched.parse_config_job_id(bad)


def test_entry_cron_string_prefers_a_valid_five_field_cron():
    assert sched.entry_cron_string({"cron": "0 9 * * 1"}) == "0 9 * * 1"
    # A malformed `cron` falls back to the split fields rather than being trusted.
    assert sched.entry_cron_string({"cron": "0 9", "hour": "7"}) == "* 7 * * *"
    assert sched.entry_cron_string({}) == "* * * * *"


# ── config-schedule store ─────────────────────────────────────────────────────


def test_config_schedule_tasks_maps_entries_into_the_task_shape(agent_with_schedule):
    rows = sched.config_schedule_tasks()
    ours = [r for r in rows if r["agent_name"] == "cadence-bot"]
    assert len(ours) == 2
    first = ours[0]
    assert first["id"] == "cfg:cadence-bot:0"
    assert first["source"] == "config"
    assert first["name"] == "Team Progress Cadence"
    assert first["schedule"] == "0 9 * * 1"
    assert first["timezone"] == "America/Costa_Rica"
    assert first["index"] == 0
    # The split-field entry is rendered through entry_cron_string.
    assert ours[1]["schedule"] == "0 * * * *"
    # No overrides on either entry → the agent's own model / effort ("" on the
    # wire, the same spelling as a DB task row).
    assert first["model"] == ""
    assert first["reasoning_effort"] == ""


def test_config_schedule_tasks_carries_the_per_job_overrides():
    """An entry's `model` / `reasoning_effort` are projected under the same keys
    a DB task row uses, so the Scheduler tab's one form edits both stores."""
    create_agent(
        "override-bot",
        model="fake:instant",
        extra={
            "schedule": [
                {
                    "name": "Weekly report",
                    "message": "write it",
                    "cron": "0 9 * * 1",
                    "model": "olano:pro",
                    "reasoning_effort": " HIGH ",
                }
            ]
        },
    )
    (row,) = [r for r in sched.config_schedule_tasks("override-bot")]
    assert row["model"] == "olano:pro"
    assert row["reasoning_effort"] == "high"


def test_config_schedule_tasks_agent_filter_scopes_the_walk(agent_with_schedule):
    create_agent("other-bot", model="fake:instant", extra={"schedule": [{"message": "x"}]})
    assert {r["agent_name"] for r in sched.config_schedule_tasks("cadence-bot")} == {"cadence-bot"}
    assert {r["agent_name"] for r in sched.config_schedule_tasks()} >= {"cadence-bot", "other-bot"}


def test_config_entry_update_canonicalizes_onto_cron_and_drops_split_fields(agent_with_schedule):
    """Leaving `minute`/`hour` behind next to a new `cron` is an every-minute
    trap the moment someone later clears `cron`."""
    changed = sched.apply_config_entry_update("cadence-bot", 1, cron="30 6 * * *")
    assert changed == ["schedule"]
    entry = sched.read_config_entry("cadence-bot", 1)
    assert entry["cron"] == "30 6 * * *"
    assert "minute" not in entry and "hour" not in entry


def test_config_entry_update_ignores_a_blank_cron(agent_with_schedule):
    """An empty schedule must not blank the entry into all-`*` every-minute."""
    changed = sched.apply_config_entry_update("cadence-bot", 0, cron="   ", name="Renamed")
    assert changed == ["name"]
    assert sched.read_config_entry("cadence-bot", 0)["cron"] == "0 9 * * 1"


def test_delete_config_entry_removes_exactly_one_and_leaves_the_neighbour(agent_with_schedule):
    sched.delete_config_entry("cadence-bot", 0)
    rows = sched.config_schedule_tasks("cadence-bot")
    assert len(rows) == 1
    assert rows[0]["name"] == "Second job"
    # Positional ids: the survivor moved down to index 0.
    assert rows[0]["id"] == "cfg:cadence-bot:0"


def test_append_config_entry_round_trips_through_get_task(agent_with_schedule):
    task_id = sched.append_config_entry(
        "cadence-bot", name="New routine", message=LONG_PROMPT, cron="0 7 * * *"
    )
    assert task_id == "cfg:cadence-bot:2"
    assert sched.get_task(task_id)["prompt"] == LONG_PROMPT


# ── the read gap ──────────────────────────────────────────────────────────────


def test_get_task_returns_the_complete_db_prompt(agent_with_schedule):
    """The bug. Every pre-existing read path truncated at 100/120/200 chars."""
    task_id = _make_db_task()
    record = sched.get_task(task_id)
    assert record["prompt"] == LONG_PROMPT
    assert len(record["prompt"]) == len(LONG_PROMPT) > 200


def test_get_task_returns_the_complete_config_message(agent_with_schedule):
    record = sched.get_task("cfg:cadence-bot:0")
    assert record["prompt"] == LONG_PROMPT
    assert record["source"] == "config"


def test_get_task_carries_history_and_runtime_state(agent_with_schedule):
    task_id = _make_db_task()
    db = SchedulerDB()
    db.add_history(task_id, result="ok")
    db.add_history(task_id, error="boom" * 100)
    record = sched.get_task(task_id, history=10)
    assert len(record["history"]) == 2
    # Errors are deliberately untruncated — reading the whole failure is the point.
    assert any(len(h.get("error") or "") == 400 for h in record["history"])
    assert record["runtime"]["scheduler_active"] is False


def test_list_tasks_previews_prompts_and_reports_the_true_length(agent_with_schedule):
    _make_db_task()
    payload = sched.list_tasks(agent="cadence-bot")
    assert payload["db_count"] == 1
    assert payload["config_count"] == 2
    assert payload["count"] == 3
    row = payload["tasks"][0]
    # A 60-job fleet must not dump 60 full prompts into one turn — but the reader
    # has to be able to tell it got a preview, which is what the old list could not do.
    assert "prompt" not in row
    assert len(row["prompt_preview"]) == 200
    assert row["prompt_chars"] == len(LONG_PROMPT)
    assert row["prompt_truncated"] is True


def test_list_tasks_source_filter_selects_one_store(agent_with_schedule):
    _make_db_task()
    assert sched.list_tasks(source="db")["config_count"] == 0
    assert sched.list_tasks(source="config")["db_count"] == 0
    with pytest.raises(ValueError, match="source must be"):
        sched.list_tasks(source="sqlite")


# ── resolution is fleet-wide on purpose ───────────────────────────────────────


def test_resolve_task_ref_finds_another_agents_private_task(agent_with_schedule):
    """Pinned deliberately: this resolver is the fleet one. The caller-scoped
    resolver is scheduler_tools._resolve_task_id, and a future 'fix' that scopes
    this one would silently break every olano_scheduler_* tool."""
    task_id = _make_db_task(
        agent_name="someone-else", created_by="someone-else", visibility="private"
    )
    assert sched.resolve_task_ref(task_id[:8]) == ("db", task_id)


def test_resolve_task_ref_refuses_an_ambiguous_prefix(agent_with_schedule, monkeypatch):
    a = _make_db_task()
    b = _make_db_task()
    real = SchedulerDB.list_tasks

    def _twins(self, *args, **kw):
        rows = real(self, *args, **kw)
        for row in rows:
            row["id"] = "dupe" + row["id"][4:]
        return rows

    monkeypatch.setattr(SchedulerDB, "list_tasks", _twins)
    with pytest.raises(ValueError, match="Ambiguous"):
        sched.resolve_task_ref("dupe")
    assert a != b


def test_resolve_task_ref_reports_an_unknown_id(agent_with_schedule):
    with pytest.raises(ValueError, match="No scheduled task found"):
        sched.resolve_task_ref("deadbeef")


# ── mutations ─────────────────────────────────────────────────────────────────


def test_create_task_writes_to_the_chosen_store(agent_with_schedule):
    db_res = sched.create_task("cadence-bot", "Ad hoc", "do a thing", schedule="0 * * * *")
    assert db_res["source"] == "db"
    assert sched.get_task(db_res["id"])["prompt"] == "do a thing"

    cfg_res = sched.create_task(
        "cadence-bot", "Routine", "do it weekly", schedule="0 9 * * 1", store="config"
    )
    assert cfg_res["source"] == "config"
    assert cfg_res["id"].startswith("cfg:cadence-bot:")


def test_create_task_validates_its_inputs(agent_with_schedule):
    with pytest.raises(ValueError, match="does not exist"):
        sched.create_task("no-such-agent", "n", "p", schedule="0 9 * * *")
    with pytest.raises(ValueError, match="needs a prompt"):
        sched.create_task("cadence-bot", "n", "   ", schedule="0 9 * * *")
    with pytest.raises(ValueError, match="either a cron"):
        sched.create_task("cadence-bot", "n", "p")
    with pytest.raises(ValueError, match="cron-only"):
        sched.create_task("cadence-bot", "n", "p", run_at="2030-01-01T00:00:00", store="config")


def test_update_task_treats_none_as_keep_and_blank_as_refused(agent_with_schedule):
    task_id = _make_db_task()
    res = sched.update_task(task_id, enabled=False)
    assert res["changed"] == ["enabled"]
    assert sched.get_task(task_id)["prompt"] == LONG_PROMPT  # untouched by omission

    # The ordinary scheduler_update tool reads "" as keep, so a field can never be
    # cleared. Here "" is free to mean clear — but never for a prompt or a name.
    with pytest.raises(ValueError, match="prompt cannot be blank"):
        sched.update_task(task_id, prompt="")
    with pytest.raises(ValueError, match="name cannot be blank"):
        sched.update_task(task_id, name="   ")


def test_update_task_clears_run_at_with_an_empty_string(agent_with_schedule):
    task_id = _make_db_task(schedule=None, run_at="2030-01-01T00:00:00")
    sched.update_task(task_id, run_at="", schedule="0 9 * * *")
    assert sched.get_task(task_id)["run_at"] is None


def test_update_task_reassigns_the_running_agent(agent_with_schedule):
    create_agent("other-bot", model="fake:instant")
    task_id = _make_db_task()
    res = sched.update_task(task_id, agent="other-bot")
    assert res["agent"] == "other-bot"
    with pytest.raises(ValueError, match="does not exist"):
        sched.update_task(task_id, agent="ghost")


def test_update_task_refuses_db_only_fields_on_a_config_job(agent_with_schedule):
    """A silent no-op would leave the caller believing the write landed."""
    with pytest.raises(ValueError, match="cannot be set on a config-schedule job"):
        sched.update_task("cfg:cadence-bot:0", visibility="private")


def test_update_task_edits_a_config_job_in_place(agent_with_schedule):
    res = sched.update_task("cfg:cadence-bot:0", prompt="new instructions", enabled=False)
    assert set(res["changed"]) == {"prompt", "enabled"}
    record = sched.get_task("cfg:cadence-bot:0")
    assert record["prompt"] == "new instructions"
    assert record["enabled"] is False


def test_delete_task_removes_from_either_store(agent_with_schedule):
    task_id = _make_db_task()
    assert sched.delete_task(task_id)["source"] == "db"
    with pytest.raises(ValueError, match="No scheduled task found"):
        sched.resolve_task_ref(task_id)

    assert sched.delete_task("cfg:cadence-bot:1")["source"] == "config"
    assert len(sched.config_schedule_tasks("cadence-bot")) == 1


def test_run_task_now_needs_a_live_scheduler(agent_with_schedule):
    task_id = _make_db_task()
    with pytest.raises(ValueError, match="Scheduler is not active"):
        sched.run_task_now(task_id)


# ── per-task model ────────────────────────────────────────────────────────────


def test_create_task_pins_a_model_in_either_store(agent_with_schedule, monkeypatch):
    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    db_res = sched.create_task(
        "cadence-bot", "Deep", "analyse", schedule="0 9 * * 1", model="olano:pro"
    )
    assert sched.get_task(db_res["id"])["model"] == "olano:pro"

    cfg_res = sched.create_task(
        "cadence-bot", "Routine", "weekly", schedule="0 9 * * 1", store="config", model="olano:max"
    )
    assert sched.get_task(cfg_res["id"])["model"] == "olano:max"
    # The entry on disk carries it; an entry created without one carries none
    # (the config writer dumps the full ScheduleConfig, so "none" reads as a
    # null value, never as "").
    entries = sched.read_config_entry("cadence-bot", 2), sched.read_config_entry("cadence-bot", 0)
    assert entries[0]["model"] == "olano:max"
    assert not entries[1].get("model")


def test_update_task_changes_and_clears_the_model_in_either_store(agent_with_schedule, monkeypatch):
    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    task_id = _make_db_task(model="olano:pro")
    assert sched.update_task(task_id, model="olano:turbo")["changed"] == ["model"]
    assert sched.get_task(task_id)["model"] == "olano:turbo"
    sched.update_task(task_id, model="")
    assert sched.get_task(task_id)["model"] == ""

    res = sched.update_task("cfg:cadence-bot:0", model="olano:pro")
    assert res["changed"] == ["model"]
    assert sched.read_config_entry("cadence-bot", 0)["model"] == "olano:pro"
    sched.update_task("cfg:cadence-bot:0", model="")
    assert not sched.read_config_entry("cadence-bot", 0).get("model")


def test_task_model_is_refused_when_its_provider_is_not_connected(agent_with_schedule, monkeypatch):
    monkeypatch.setattr(
        "olano.admin.models.model_provider_connected",
        lambda model: (False, "ANTHROPIC_API_KEY is not set"),
    )
    with pytest.raises(ValueError, match="anthropic:claude-opus-4-6.*ANTHROPIC_API_KEY"):
        sched.create_task(
            "cadence-bot", "n", "p", schedule="0 9 * * *", model="anthropic:claude-opus-4-6"
        )
    with pytest.raises(ValueError, match="not usable here"):
        sched.update_task("cfg:cadence-bot:0", model="anthropic:claude-opus-4-6")
    # The default is never validated against a provider: "" always passes.
    assert sched.validate_task_model("   ") == ""


def test_create_task_stores_a_reasoning_effort_in_either_store(agent_with_schedule, monkeypatch):
    """The effort is checked against the model the job runs on (its own, else the
    agent's) and lands beside the model in both stores, lower-cased."""
    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    db_res = sched.create_task(
        "cadence-bot",
        "Deep",
        "think",
        schedule="0 9 * * 1",
        model="openai:gpt-6-sol",
        reasoning_effort="HIGH",
    )
    assert sched.get_task(db_res["id"])["reasoning_effort"] == "high"
    cfg_res = sched.create_task(
        "cadence-bot",
        "Deep routine",
        "think weekly",
        schedule="0 9 * * 1",
        store="config",
        model="openai:gpt-6-sol",
        reasoning_effort="low",
    )
    entry = sched.read_config_entry("cadence-bot", 2)
    assert entry["model"] == "openai:gpt-6-sol" and entry["reasoning_effort"] == "low"
    assert sched.get_task(cfg_res["id"])["reasoning_effort"] == "low"


def test_task_effort_is_refused_when_the_model_does_not_accept_it(agent_with_schedule, monkeypatch):
    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    with pytest.raises(ValueError, match="does not accept reasoning_effort 'ultra'"):
        sched.create_task(
            "cadence-bot",
            "Deep",
            "think",
            schedule="0 9 * * 1",
            model="openai:gpt-6-sol",
            reasoning_effort="ultra",
        )
    # The agent's own model (fake:instant) has no effort ladder, so an effort on
    # a job without its own model is refused too, with the reason.
    with pytest.raises(ValueError, match="no reasoning-effort control"):
        sched.create_task(
            "cadence-bot", "Deep", "think", schedule="0 9 * * 1", reasoning_effort="high"
        )


def test_update_task_validates_the_pair_as_it_will_be_and_clears_with_an_empty_string(
    agent_with_schedule, monkeypatch
):
    monkeypatch.setattr("olano.admin.models.model_provider_connected", lambda model: (True, "ok"))
    task_id = _make_db_task(model="openai:gpt-6-sol", reasoning_effort="high")
    # A new effort is checked against the STORED model.
    with pytest.raises(ValueError, match="does not accept reasoning_effort 'ultra'"):
        sched.update_task(task_id, reasoning_effort="ultra")
    # A model change is checked against the STORED effort: the agent's own
    # fake:instant has no ladder, so clearing the model alone is refused...
    with pytest.raises(ValueError, match="no reasoning-effort control"):
        sched.update_task(task_id, model="")
    # ...and clearing both together is fine.
    res = sched.update_task(task_id, model="", reasoning_effort="")
    assert set(res["changed"]) == {"model", "reasoning_effort"}
    record = sched.get_task(task_id)
    assert record["model"] == "" and record["reasoning_effort"] == ""

    # Same rules on a config job; the cleared key leaves agent.json5 entirely.
    sched.update_task("cfg:cadence-bot:0", model="openai:gpt-6-sol", reasoning_effort="medium")
    assert sched.read_config_entry("cadence-bot", 0)["reasoning_effort"] == "medium"
    res = sched.update_task("cfg:cadence-bot:0", reasoning_effort="")
    assert res["changed"] == ["reasoning_effort"]
    assert "reasoning_effort" not in sched.read_config_entry("cadence-bot", 0)
