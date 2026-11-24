"""The deterministic activity pass (``memory/activity_log.py``).

It exists because the fact distiller drops the record of *work* by design, so
these pin the contract that matters: machinery is logged, conversations are
not, nothing is logged twice, nothing old is back-filled, and a busy day cannot
balloon the prompt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from olano.config.home import get_olano_paths
from olano.memory import activity_log
from olano.memory.activity_log import ActivityLogger, events_from_audit, events_from_rows
from olano.memory.markdown_memory import MarkdownMemory

AGENT = "maria"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date().isoformat()
YESTERDAY = (NOW - timedelta(days=1)).date().isoformat()


def _ts(day: str, hh: int, mm: int = 0) -> str:
    return f"{day}T{hh:02d}:{mm:02d}:00+00:00"


def _sessions_dir() -> Path:
    d = get_olano_paths().sessions_dir / AGENT
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_thread(stem: str, rows: list[dict]) -> Path:
    path = _sessions_dir() / f"{stem}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def _logger(tmp_workspace: Path, memory: MarkdownMemory, *, now: datetime = NOW) -> ActivityLogger:
    return ActivityLogger(AGENT, tmp_workspace, memory, now=now)


def _daily(memory: MarkdownMemory, day: str) -> str:
    return memory.read_daily(datetime.fromisoformat(f"{day}T00:00:00+00:00"))


@pytest.fixture
def memory(tmp_workspace: Path) -> MarkdownMemory:
    return MarkdownMemory(tmp_workspace)


# ── row folding (pure) ─────────────────────────────────────────────────────────


def test_scheduled_run_logs_prompt_and_reply() -> None:
    rows = [
        {"role": "user", "content": "Daily acquisition run for 3 prospects", "ts": _ts(TODAY, 8)},
        {"role": "assistant", "content": "1. Sureclean — angle …", "ts": _ts(TODAY, 8, 4)},
    ]
    (ev,) = events_from_rows(rows, thread_kind="scheduled")
    assert ev.day == TODAY
    assert ev.line.startswith("- 08:04 Scheduled run: Daily acquisition run for 3 prospects")
    assert "→ 1. Sureclean — angle" in ev.line


def test_peer_exchange_logs_sender_request_and_reply() -> None:
    rows = [
        {
            "role": "peer_inbound",
            "from": "aurelius",
            "content": "QA these drafts",
            "ts": _ts(TODAY, 9),
        },
        {
            "role": "assistant",
            "via_peer": "aurelius",
            "content": "PASS on all three",
            "ts": _ts(TODAY, 9, 2),
        },
    ]
    (ev,) = events_from_rows(rows, thread_kind="")
    assert ev.line == "- 09:02 Peer request from aurelius: QA these drafts → PASS on all three"


def test_human_conversation_is_never_activity() -> None:
    rows = [
        {"role": "user", "content": "hey what did you do yesterday?", "ts": _ts(TODAY, 10)},
        {"role": "assistant", "content": "Operational setup only.", "ts": _ts(TODAY, 10, 1)},
    ]
    assert events_from_rows(rows, thread_kind="") == []


def test_pinned_thread_schedule_is_recognised_by_channel_stamp() -> None:
    """A schedule pinned to an arbitrary ``thread_id`` carries no prefix; the
    runner's ``channel: scheduler`` stamp is what identifies it."""
    rows = [
        {"role": "user", "content": "weekly digest", "channel": "scheduler", "ts": _ts(TODAY, 7)},
        {"role": "assistant", "content": "Digest sent to 4 people.", "ts": _ts(TODAY, 7, 1)},
    ]
    (ev,) = events_from_rows(rows, thread_kind="")
    assert "Scheduled run: weekly digest → Digest sent to 4 people." in ev.line


def test_heartbeat_ok_is_quiet_and_findings_are_logged() -> None:
    rows = [
        {"role": "user", "content": "HEARTBEAT: Check inbox\n\nContext: …", "ts": _ts(TODAY, 6)},
        {"role": "assistant", "content": "HEARTBEAT_OK", "ts": _ts(TODAY, 6, 1)},
        {"role": "user", "content": "HEARTBEAT: Check inbox\n\nContext: …", "ts": _ts(TODAY, 6, 5)},
        {"role": "assistant", "content": "2 new leads need a reply.", "ts": _ts(TODAY, 6, 6)},
    ]
    (ev,) = events_from_rows(rows, thread_kind="heartbeat")
    assert ev.line == "- 06:06 Heartbeat: Check inbox → 2 new leads need a reply."


def test_hidden_prompt_rows_and_empty_replies_are_skipped() -> None:
    rows = [
        {"role": "user", "content": "(onboarding)", "hidden": True, "ts": _ts(TODAY, 5)},
        {"role": "assistant", "content": "Hello!", "ts": _ts(TODAY, 5, 1)},
        {"role": "user", "content": "run it", "ts": _ts(TODAY, 5, 2)},
        {"role": "assistant", "content": "   ", "ts": _ts(TODAY, 5, 3)},
    ]
    assert events_from_rows(rows, thread_kind="scheduled") == []


def test_lines_are_bounded_and_single_line() -> None:
    rows = [
        {"role": "user", "content": "x " * 500 + "\n\nmore", "ts": _ts(TODAY, 8)},
        {"role": "assistant", "content": "y\n" * 500, "ts": _ts(TODAY, 8, 1)},
    ]
    (ev,) = events_from_rows(rows, thread_kind="scheduled")
    assert "\n" not in ev.line
    assert len(ev.line) <= activity_log.MAX_LINE_CHARS


def test_audit_logs_outcomes_only_and_respects_watermark() -> None:
    actions = [
        {
            "ts": _ts(TODAY, 4, 30),
            "action": "cortex_memory_garden",
            "summary": "Memory gardened.",
            "detail": {"label": "Memory", "status": "completed"},
        },
        {
            "ts": _ts(TODAY, 4, 35),
            "action": "cortex_ideas",
            "summary": "daily deep-run cap reached",
            "detail": {"label": "Ideas", "status": "skipped"},
        },
        {
            "ts": _ts(TODAY, 4, 40),
            "action": "cortex_pulse",
            "summary": "Vault is locked.",
            "detail": {"label": "Pulse", "status": "error"},
        },
        {
            "ts": _ts(TODAY, 4, 45),
            "action": "pulse_approve",
            "summary": "approved",
            "detail": {"status": "completed"},
        },
        {
            "ts": _ts(YESTERDAY, 4, 30),
            "action": "cortex_checks",
            "summary": "old",
            "detail": {"label": "Self-Checks", "status": "completed"},
        },
    ]
    lines = [e.line for e in events_from_audit(actions, after_ts=_ts(YESTERDAY, 23))]
    assert lines == [
        "- 04:30 Cortex Memory → Memory gardened.",
        "- 04:40 Cortex Pulse failed → Vault is locked.",
    ]


# ── the pass end to end ────────────────────────────────────────────────────────


def test_pass_writes_into_the_event_day_and_is_idempotent(
    tmp_workspace: Path, memory: MarkdownMemory
) -> None:
    _write_thread(
        "scheduled_maria_1",
        [
            {"role": "user", "content": "Find 3 Singapore prospects", "ts": _ts(YESTERDAY, 21)},
            {
                "role": "assistant",
                "content": "Found Petopia, White Room, Picasso.",
                "ts": _ts(YESTERDAY, 21, 9),
            },
        ],
    )
    _write_thread(
        "acp::aurelius--maria",
        [
            {
                "role": "peer_inbound",
                "from": "aurelius",
                "content": "Round 1 reflection",
                "ts": _ts(TODAY, 1),
            },
            {
                "role": "assistant",
                "via_peer": "aurelius",
                "content": "Candid view: …",
                "ts": _ts(TODAY, 1, 3),
            },
        ],
    )
    assert _logger(tmp_workspace, memory).run() == 2

    yesterday = _daily(memory, YESTERDAY)
    today = _daily(memory, TODAY)
    assert "Activity:" in yesterday
    assert "- 21:09 Scheduled run: Find 3 Singapore prospects → Found Petopia" in yesterday
    assert "Peer request from aurelius: Round 1 reflection → Candid view" in today
    assert "Scheduled run" not in today  # each event lands on its own day

    # Second pass: nothing new → nothing written, files byte-identical.
    assert _logger(tmp_workspace, memory).run() == 0
    assert _daily(memory, YESTERDAY) == yesterday
    assert _daily(memory, TODAY) == today

    # Only the appended rows are picked up next time.
    _write_thread(
        "acp::aurelius--maria",
        [
            {
                "role": "peer_inbound",
                "from": "aurelius",
                "content": "Round 2 challenge",
                "ts": _ts(TODAY, 2),
            },
            {
                "role": "assistant",
                "via_peer": "aurelius",
                "content": "What should change: …",
                "ts": _ts(TODAY, 2, 4),
            },
        ],
    )
    assert _logger(tmp_workspace, memory).run() == 1
    assert _daily(memory, TODAY).count("Round 1 reflection") == 1
    assert "Round 2 challenge" in _daily(memory, TODAY)


def test_watermark_survives_a_new_instance(tmp_workspace: Path, memory: MarkdownMemory) -> None:
    """The point of persisting: a restart must not replay the day."""
    _write_thread(
        "scheduled_maria_2",
        [
            {"role": "user", "content": "task", "ts": _ts(TODAY, 3)},
            {"role": "assistant", "content": "done", "ts": _ts(TODAY, 3, 1)},
        ],
    )
    assert _logger(tmp_workspace, memory).run() == 1
    state_path = tmp_workspace / ".olano" / "state" / "self_learning" / "activity_watermark.json"
    assert state_path.exists()
    assert _logger(tmp_workspace, memory).run() == 0
    assert _daily(memory, TODAY).count("Scheduled run: task") == 1


def test_cortex_phases_come_from_the_audit_log(tmp_workspace: Path, memory: MarkdownMemory) -> None:
    from olano.cognition import audit

    with patch("olano.cognition.audit.datetime") as dt:
        dt.now.return_value = NOW.replace(hour=4, minute=31)
        audit.log_action(
            tmp_workspace,
            "cortex_memory_garden",
            summary="Memory gardened.",
            detail={"label": "Memory", "status": "completed"},
        )
    assert _logger(tmp_workspace, memory).run() == 1
    assert "- 04:31 Cortex Memory → Memory gardened." in _daily(memory, TODAY)
    assert _logger(tmp_workspace, memory).run() == 0


def test_old_events_are_consumed_but_never_written(
    tmp_workspace: Path, memory: MarkdownMemory
) -> None:
    """First run on a long-lived box: months of history must not be back-filled
    into daily files the roll-up already archived."""
    old_day = (NOW - timedelta(days=30)).date().isoformat()
    path = _write_thread(
        "scheduled_maria_old",
        [
            {"role": "user", "content": "ancient task", "ts": _ts(old_day, 3)},
            {"role": "assistant", "content": "ancient result", "ts": _ts(old_day, 3, 1)},
        ],
    )
    assert _logger(tmp_workspace, memory).run() == 0
    assert not (tmp_workspace / "memories" / f"{old_day}.md").exists()
    # …and the rows are consumed, not re-read forever.
    state = json.loads(
        (tmp_workspace / ".olano/state/self_learning/activity_watermark.json").read_text()
    )
    assert state["sessions"][AGENT][str(path)] == path.stat().st_size


def test_truncated_transcript_resets_its_offset(
    tmp_workspace: Path, memory: MarkdownMemory
) -> None:
    path = _write_thread(
        "scheduled_maria_3",
        [
            {"role": "user", "content": "a" * 400, "ts": _ts(TODAY, 3)},
            {"role": "assistant", "content": "first", "ts": _ts(TODAY, 3, 1)},
        ],
    )
    assert _logger(tmp_workspace, memory).run() == 1
    # Rotated: rewritten much shorter than the persisted offset.
    path.write_text(
        json.dumps({"role": "user", "content": "b", "ts": _ts(TODAY, 4)})
        + "\n"
        + json.dumps({"role": "assistant", "content": "second", "ts": _ts(TODAY, 4, 1)})
        + "\n",
        encoding="utf-8",
    )
    assert _logger(tmp_workspace, memory).run() == 1
    assert "→ second" in _daily(memory, TODAY)


def test_daily_cap_holds_across_passes(tmp_workspace: Path, memory: MarkdownMemory) -> None:
    rows: list[dict] = []
    for i in range(activity_log.MAX_LINES_PER_DAY + 10):
        rows.append({"role": "user", "content": f"run {i}", "ts": _ts(TODAY, 1, i % 60)})
        rows.append({"role": "assistant", "content": f"result {i}", "ts": _ts(TODAY, 1, i % 60)})
    _write_thread("scheduled_maria_bulk", rows)
    written = _logger(tmp_workspace, memory).run()
    assert written == activity_log.MAX_LINES_PER_DAY + 1  # + the one overflow note
    today = _daily(memory, TODAY)
    assert "10 more event(s) not logged" in today

    # The cap is per day, not per pass: a later pass adds nothing more today.
    _write_thread(
        "scheduled_maria_bulk2",
        [
            {"role": "user", "content": "late run", "ts": _ts(TODAY, 11)},
            {"role": "assistant", "content": "late result", "ts": _ts(TODAY, 11, 1)},
        ],
    )
    assert _logger(tmp_workspace, memory).run() == 0
    assert "late result" not in _daily(memory, TODAY)
    assert _daily(memory, TODAY).count("not logged") == 1


def test_in_flight_prompt_waits_for_its_answer(tmp_workspace: Path, memory: MarkdownMemory) -> None:
    """A pass that lands mid-turn must not consume the prompt and then log the
    answer as an orphan (or not at all)."""
    fresh = (NOW - timedelta(minutes=5)).isoformat()
    path = _write_thread(
        "scheduled_maria_live", [{"role": "user", "content": "long job", "ts": fresh}]
    )
    assert _logger(tmp_workspace, memory).run() == 0
    _write_thread(
        "scheduled_maria_live",
        [{"role": "assistant", "content": "long job finished", "ts": NOW.isoformat()}],
    )
    assert _logger(tmp_workspace, memory).run() == 1
    assert "Scheduled run: long job → long job finished" in _daily(memory, TODAY)
    state = json.loads(
        (tmp_workspace / ".olano/state/self_learning/activity_watermark.json").read_text()
    )
    assert state["sessions"][AGENT][str(path)] == path.stat().st_size


def test_dead_turn_prompt_is_consumed_after_the_grace_period(
    tmp_workspace: Path, memory: MarkdownMemory
) -> None:
    """Every scheduled run gets its own thread file, so a turn that errored leaves
    a lone prompt row forever — it must not pin the offset on every pass."""
    stale = (NOW - activity_log.DANGLING_PROMPT_MAX_AGE - timedelta(minutes=1)).isoformat()
    path = _write_thread(
        "scheduled_maria_dead", [{"role": "user", "content": "crashed", "ts": stale}]
    )
    assert _logger(tmp_workspace, memory).run() == 0
    state = json.loads(
        (tmp_workspace / ".olano/state/self_learning/activity_watermark.json").read_text()
    )
    assert state["sessions"][AGENT][str(path)] == path.stat().st_size


def test_two_agents_sharing_a_workspace_log_cortex_once(
    tmp_workspace: Path, memory: MarkdownMemory
) -> None:
    from olano.cognition import audit

    with patch("olano.cognition.audit.datetime") as dt:
        dt.now.return_value = NOW.replace(hour=4, minute=31)
        audit.log_action(
            tmp_workspace,
            "cortex_pulse",
            summary="2 items.",
            detail={"label": "Pulse", "status": "completed"},
        )
    assert ActivityLogger("maria", tmp_workspace, memory, now=NOW).run() == 1
    assert ActivityLogger("aurelius", tmp_workspace, memory, now=NOW).run() == 0
    assert _daily(memory, TODAY).count("Cortex Pulse") == 1


# ── wiring into the self-learning cycle ───────────────────────────────────────


async def test_cycle_logs_activity_even_when_the_deep_gate_is_shut(
    base_config, tmp_workspace: Path, memory: MarkdownMemory
) -> None:
    """The whole reason this pass is not part of the distiller: a spent
    deep-run cap skips distillation, and activity must still land."""
    from olano.memory.self_learning import SelfLearningService

    base_config.name = AGENT
    svc = SelfLearningService(
        agent_config=base_config, workspace_root=tmp_workspace, markdown_memory=memory
    )
    svc._last_weekly_distill = datetime.now(timezone.utc)  # keep the weekly pass out of this
    now = datetime.now(timezone.utc)
    _write_thread(
        "scheduled_maria_cycle",
        [
            {
                "role": "user",
                "content": f"turn {i}",
                "ts": (now - timedelta(minutes=10 - i)).isoformat(),
            }
            if i % 2 == 0
            else {
                "role": "assistant",
                "content": f"done {i}",
                "ts": (now - timedelta(minutes=10 - i)).isoformat(),
            }
            for i in range(6)
        ],
    )
    with (
        patch(
            "olano.cognition.orchestrator.deep_gate",
            return_value=(False, "daily deep-run cap reached"),
        ),
        patch.object(svc, "sync_graph_to_memory_md", return_value=False),
        patch.object(svc, "_distill_facts") as distill,
    ):
        await svc._run_cycle()
    distill.assert_not_called()
    assert svc.status()["activity_lines"] == 3
    assert memory.read_today().count("Scheduled run") == 3
