"""Agent Father greets a new owner when the setup wizard completes.

runtime/onboarding_greeting.py runs ONE turn on a deterministic thread with a
platform-written prompt that the transcript hides. These pin the prompt, the
request context the turn runs under (dashboard surface, scheduled origin so
HITL auto-resolves, hidden-prompt marker), the rail label, and the three
"never": never a second hello, never a failure the owner can see, never a
turn without the runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from olano.runtime import onboarding_greeting as og
from olano.security.request_context import HIDDEN_PROMPT_KEY, ORIGIN_KEY, get_request_context

OWNER = SimpleNamespace(
    id="u-123", name="Carlos Rivera", username="carlos-rivera", email="carlos@example.com"
)


class _FakeRunner:
    """Just enough of AgentRunner: a session path and an ainvoke that records
    the request context it was called under."""

    def __init__(self, sessions: Path, *, fail: bool = False) -> None:
        self.sessions = sessions
        self.fail = fail
        self.calls: list[dict] = []
        self.rows: list[dict] = []
        self.error_notes: list[str] = []

    def _append_session(self, thread_id: str, role: str, content: str, *, extra=None) -> None:
        self.rows.append(
            {"thread_id": thread_id, "role": role, "content": content, **(extra or {})}
        )

    def record_turn_error(
        self, thread_id: str, message: str, *, code: str = "", stage: str = "turn"
    ) -> None:
        self.error_notes.append(message)

    def _session_path(self, thread_id: str) -> Path:
        return self.sessions / f"{thread_id}.jsonl"

    async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
        ctx = get_request_context()
        self.calls.append(
            {
                "message": message,
                "thread_id": thread_id,
                "source": ctx.source,
                "channel": ctx.channel,
                "user_id": ctx.user_id,
                "session_id": ctx.session_id,
                "metadata": dict(ctx.metadata),
            }
        )
        if self.fail:
            raise RuntimeError("model down")
        self._session_path(thread_id or "").write_text('{"role":"assistant","content":"hi"}\n')
        return "hi"


@pytest.fixture
def sessions(tmp_path: Path) -> Path:
    d = tmp_path / "sessions" / "agentfather"
    d.mkdir(parents=True)
    return d


def _runtime(runner=None, blocked: dict | None = None):
    rt = SimpleNamespace(_runners={}, _blocked_agents=blocked or {})
    if runner is not None:
        rt._runners["agentfather"] = runner
    return rt


# ── prompt / thread id ────────────────────────────────────────────────────────


def test_prompt_names_the_owner_and_forbids_changes() -> None:
    prompt = og.build_greeting_prompt(OWNER)
    assert "Carlos Rivera" in prompt
    assert "@carlos-rivera" in prompt
    assert "carlos@example.com" in prompt
    assert "first time" in prompt
    assert "Do not change anything" in prompt


def test_prompt_survives_a_nameless_owner() -> None:
    prompt = og.build_greeting_prompt(SimpleNamespace(id="x", name="", username="", email=""))
    assert "the new owner" in prompt


def test_thread_id_is_per_user_and_reads_as_a_dashboard_conversation() -> None:
    from olano.core.thread_labels import SYSTEM_THREAD_PREFIXES

    tid = og.welcome_thread_id("u-123")
    assert tid == "welcome-u-123"
    assert ":" not in tid
    assert not any(tid.startswith(p) for p in SYSTEM_THREAD_PREFIXES)


# ── the turn ─────────────────────────────────────────────────────────────────


async def test_greeting_runs_one_hidden_prompt_turn_and_labels_the_thread(sessions) -> None:
    runner = _FakeRunner(sessions)
    result = await og.greet_new_owner(_runtime(runner), OWNER, "welcome-u-123", wait_seconds=1)

    assert result == {"thread_id": "welcome-u-123", "reply": "hi"}
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["thread_id"] == "welcome-u-123"
    assert call["message"] == og.build_greeting_prompt(OWNER)
    # Dashboard surface, owner identity, the thread as the session.
    assert (call["source"], call["channel"]) == ("api", "api")
    assert call["user_id"] == "u-123"
    assert call["session_id"] == "welcome-u-123"
    # Hidden from the transcript; HITL resolves headlessly (no human to ask).
    assert call["metadata"][HIDDEN_PROMPT_KEY] is True
    origin = call["metadata"][ORIGIN_KEY]
    assert origin["root_source"] == "scheduled" and origin["interactive"] is False
    assert origin["root_session_id"] == "welcome-u-123"
    # Titled before the first row lands, the way the rename route writes it.
    meta = json.loads((sessions / "welcome-u-123.meta.json").read_text())
    assert meta == {"name": og.THREAD_LABEL}


async def test_greeting_never_repeats_on_an_existing_thread(sessions) -> None:
    runner = _FakeRunner(sessions)
    (sessions / "welcome-u-123.jsonl").write_text('{"role":"assistant","content":"hi"}\n')
    result = await og.greet_new_owner(_runtime(runner), OWNER, "welcome-u-123", wait_seconds=1)
    assert result == {"thread_id": "welcome-u-123", "skipped": "exists"}
    assert runner.calls == []


async def test_greeting_gives_up_when_the_runner_never_comes_up(sessions) -> None:
    result = await og.greet_new_owner(
        _runtime(), OWNER, "welcome-u-123", wait_seconds=0.05, poll=0.01
    )
    assert result is None


async def test_greeting_waits_for_a_runner_that_boots_late(sessions) -> None:
    rt = _runtime()
    runner = _FakeRunner(sessions)

    async def boot_soon():
        await asyncio.sleep(0.03)
        rt._runners["agentfather"] = runner

    task = asyncio.ensure_future(boot_soon())
    result = await og.greet_new_owner(rt, OWNER, "welcome-u-123", wait_seconds=1, poll=0.01)
    await task
    assert result and result["reply"] == "hi"


async def test_greeting_failure_is_logged_not_raised(sessions, caplog) -> None:
    runner = _FakeRunner(sessions, fail=True)
    result = await og.greet_new_owner(_runtime(runner), OWNER, "welcome-u-123", wait_seconds=1)
    assert result is None
    assert "welcome greeting failed" in caplog.text


async def test_a_failed_greeting_ends_the_wait_with_a_visible_reason(sessions) -> None:
    # The dashboard holds the welcome thread on "waking up" until a visible row
    # lands; a failed turn used to write none, so the owner watched the spinner
    # for six minutes (a new managed box with no credits yet).
    runner = _FakeRunner(sessions, fail=True)
    await og.greet_new_owner(_runtime(runner), OWNER, "welcome-u-123", wait_seconds=1)
    assert len(runner.rows) == 1
    row = runner.rows[0]
    assert row["thread_id"] == "welcome-u-123"
    assert row["role"] == "assistant"
    assert row["content"].startswith("I couldn't say hello just now.")
    # The classified, user-facing message: never the exception type.
    assert "RuntimeError" not in row["content"]
    assert runner.error_notes and runner.error_notes[0] in row["content"]


# ── the greeting starts Agent Father when nothing else did ────────────────────
#
# The tour lands in Agent Father's chat. On a managed box the cloud installs it
# after the runtime took its roster snapshot, the wizard's install POST answers
# 409, and nothing on the completion path booted it — so the owner met an
# offline agent. The greeting now boots it through the runtime's own _boot_agent.


def _bootable_runtime(sessions: Path, *, in_flight: bool = False):
    rt = _runtime()
    rt.boots: list[str] = []
    rt.agent_boot_in_flight = lambda name: in_flight

    async def _boot(name: str) -> None:
        rt.boots.append(name)
        rt._runners[name] = _FakeRunner(sessions)

    rt._boot_agent = _boot
    return rt


async def test_greeting_boots_an_installed_but_offline_agent_father(sessions) -> None:
    rt = _bootable_runtime(sessions)
    result = await og.greet_new_owner(rt, OWNER, "welcome-u-123", wait_seconds=1, poll=0.01)
    assert rt.boots == ["agentfather"]
    assert result and result["reply"] == "hi"
    assert len(rt._runners["agentfather"].calls) == 1


async def test_greeting_does_not_boot_a_running_agent_father(sessions) -> None:
    rt = _bootable_runtime(sessions)
    rt._runners["agentfather"] = _FakeRunner(sessions)
    await og.greet_new_owner(rt, OWNER, "welcome-u-123", wait_seconds=1)
    assert rt.boots == []


async def test_greeting_waits_instead_of_booting_when_a_boot_is_in_flight(sessions) -> None:
    rt = _bootable_runtime(sessions, in_flight=True)
    runner = _FakeRunner(sessions)

    async def someone_elses_boot():
        await asyncio.sleep(0.03)
        rt._runners["agentfather"] = runner

    task = asyncio.ensure_future(someone_elses_boot())
    result = await og.greet_new_owner(rt, OWNER, "welcome-u-123", wait_seconds=1, poll=0.01)
    await task
    assert rt.boots == []
    assert result and result["reply"] == "hi"


async def test_greeting_respects_an_owner_who_paused_agent_father(sessions, caplog) -> None:
    from olano.config.agent_state import set_agent_paused

    set_agent_paused("agentfather", True)
    rt = _bootable_runtime(sessions)
    # The "paused" line is logged at INFO; capture it explicitly so the assert
    # does not depend on an earlier test having lowered the logger's level.
    caplog.set_level(logging.INFO, logger=og.logger.name)
    result = await og.greet_new_owner(rt, OWNER, "welcome-u-123", wait_seconds=0.05, poll=0.01)
    assert rt.boots == []
    assert result is None
    assert "paused by the owner" in caplog.text


async def test_a_boot_that_raises_costs_only_the_hello(sessions, caplog) -> None:
    rt = _bootable_runtime(sessions)

    async def _broken(name: str) -> None:
        raise RuntimeError("model build failed")

    rt._boot_agent = _broken
    result = await og.greet_new_owner(rt, OWNER, "welcome-u-123", wait_seconds=0.05, poll=0.01)
    assert result is None
    assert "starting agentfather failed" in caplog.text


# ── scheduling / availability ─────────────────────────────────────────────────


def test_available_when_installed_or_running_but_not_when_blocked(monkeypatch) -> None:
    from olano.config import loader

    monkeypatch.setattr(loader, "find_agent_config_path", lambda name, source="auto": None)
    assert og.greeting_available(_runtime()) is False
    assert og.greeting_available(_runtime(runner=object())) is True
    assert (
        og.greeting_available(_runtime(runner=object(), blocked={"agentfather": "plan"})) is False
    )
    monkeypatch.setattr(
        loader, "find_agent_config_path", lambda name, source="auto": Path("/x/agent.json5")
    )
    assert og.greeting_available(_runtime()) is True  # on disk, boot still pending


async def test_schedule_returns_the_pointer_and_runs_in_the_background(
    sessions, monkeypatch
) -> None:
    runner = _FakeRunner(sessions)
    rt = _runtime(runner)
    pointer = og.schedule_greeting(rt, OWNER)
    assert pointer == {"agent": "agentfather", "thread_id": "welcome-u-123"}
    assert runner.calls == []  # not yet: the POST must not wait on the model
    await asyncio.sleep(0)  # let the task start
    for _ in range(50):
        if runner.calls:
            break
        await asyncio.sleep(0.01)
    assert len(runner.calls) == 1


def test_schedule_answers_none_without_agent_father(monkeypatch) -> None:
    from olano.config import loader

    monkeypatch.setattr(loader, "find_agent_config_path", lambda name, source="auto": None)
    assert og.schedule_greeting(_runtime(), OWNER) is None
