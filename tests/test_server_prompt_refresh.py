"""Tests for the RuntimeServer prompt refresh sweep — fingerprint check,
hot-swap rebuild, nudge plumbing, and the reload_agent_mcp delegation."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.config.agent_config import AgentConfig
from olano.core.prompt_state import PromptState
from olano.core.runner import AgentRunner
from olano.runtime.server import RuntimeServer

STATE_A = PromptState(
    sections={"instructions": "old text"},
    fingerprint={"instructions": "hash-a", "identity": "same"},
    built_at="t0",
)
STATE_B = PromptState(
    sections={"instructions": "new text"},
    fingerprint={"instructions": "hash-b", "identity": "same"},
    built_at="t1",
)


def make_server() -> RuntimeServer:
    """A bare RuntimeServer with just the attributes the sweep methods use —
    no scheduler, channels, or API."""
    srv = RuntimeServer.__new__(RuntimeServer)
    srv._runners = {}
    srv._boot_locks = {}
    srv._prompt_refresh_pending = set()
    srv._prompt_refresh_wake = asyncio.Event()
    return srv


@pytest.fixture
def server_and_runner(base_config: AgentConfig, mock_agent) -> tuple[RuntimeServer, AgentRunner]:
    srv = make_server()
    runner = AgentRunner(mock_agent, base_config)
    runner._prompt_state = STATE_A
    srv._runners[base_config.name] = runner
    return srv, runner


class TestRequestPromptRefresh:
    def test_named_nudge_sets_pending_and_wakes(self) -> None:
        srv = make_server()
        srv.request_prompt_refresh(["alpha"])
        assert srv._prompt_refresh_pending == {"alpha"}
        assert srv._prompt_refresh_wake.is_set()

    def test_none_means_all(self) -> None:
        srv = make_server()
        srv.request_prompt_refresh(None)
        assert srv._prompt_refresh_pending == {"*"}
        assert srv._prompt_refresh_wake.is_set()


class TestPromptRefreshCheck:
    async def test_equal_fingerprints_skip_rebuild(self, server_and_runner) -> None:
        srv, runner = server_and_runner
        with (
            patch("olano.runtime.server.load_agent_config", return_value=runner.config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_A),
            patch("olano.runtime.server.build_agent_async", new_callable=AsyncMock) as build,
        ):
            result = await srv._prompt_refresh_check(runner.config.name)
        assert result == {"ok": True, "rebuilt": False, "changed": []}
        build.assert_not_awaited()

    async def test_changed_fingerprint_hot_swaps(self, server_and_runner) -> None:
        srv, runner = server_and_runner
        old_agent = runner.agent
        new_agent = MagicMock(name="rebuilt-agent")
        events: list = []
        with (
            patch("olano.runtime.server.load_agent_config", return_value=runner.config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_B),
            patch(
                "olano.runtime.server.build_agent_async",
                new_callable=AsyncMock,
                return_value=new_agent,
            ) as build,
        ):
            result = await srv._prompt_refresh_check(
                runner.config.name, push_event=lambda kind, data: events.append((kind, data))
            )
        assert result["ok"] and result["rebuilt"]
        assert result["changed"] == ["instructions"]
        build.assert_awaited_once()
        assert runner.agent is new_agent and runner.agent is not old_agent
        # The runner's snapshot now describes the freshly built graph, so the
        # per-thread notice path (diff vs seen map) fires on the next turn.
        assert runner.prompt_state() is STATE_B
        assert events == [
            ("prompt_updated", {"agent": runner.config.name, "changed": ["instructions"]})
        ]

    async def test_inert_baseline_never_reads_as_changed(self, server_and_runner) -> None:
        srv, runner = server_and_runner
        runner._prompt_state = PromptState()  # empty = unavailable
        with (
            patch("olano.runtime.server.load_agent_config", return_value=runner.config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_B),
            patch("olano.runtime.server.build_agent_async", new_callable=AsyncMock) as build,
        ):
            result = await srv._prompt_refresh_check(runner.config.name)
        assert result == {"ok": True, "rebuilt": False, "changed": []}
        build.assert_not_awaited()

    async def test_not_running_reports_reason(self) -> None:
        srv = make_server()
        result = await srv._prompt_refresh_check("ghost")
        assert result["ok"] is False and result["reason"] == "agent not running"


class TestHotSwapAgent:
    async def test_swap_updates_agent_config_and_state(self, server_and_runner) -> None:
        srv, runner = server_and_runner
        new_agent = MagicMock(name="rebuilt-agent")
        new_config = runner.config.model_copy(update={"system_prompt": "rewritten"})
        with (
            patch("olano.runtime.server.load_agent_config", return_value=new_config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_B),
            patch(
                "olano.runtime.server.build_agent_async",
                new_callable=AsyncMock,
                return_value=new_agent,
            ),
        ):
            result = await srv._hot_swap_agent(runner.config.name, reason="test")
        assert result == {"ok": True, "running": True}
        assert runner.agent is new_agent
        assert runner.config.system_prompt == "rewritten"
        assert runner.prompt_state() is STATE_B

    async def test_build_failure_keeps_old_graph(self, server_and_runner) -> None:
        srv, runner = server_and_runner
        old_agent = runner.agent
        with (
            patch("olano.runtime.server.load_agent_config", return_value=runner.config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_B),
            patch(
                "olano.runtime.server.build_agent_async",
                new_callable=AsyncMock,
                side_effect=RuntimeError("mcp down"),
            ),
        ):
            result = await srv._hot_swap_agent(runner.config.name, reason="test")
        assert result["ok"] is False and result["running"] is True
        assert "mcp down" in result["reason"]
        assert runner.agent is old_agent
        assert runner.prompt_state() is STATE_A

    async def test_not_running(self) -> None:
        srv = make_server()
        result = await srv._hot_swap_agent("ghost", reason="test")
        assert result == {"ok": False, "running": False, "reason": "agent not running"}


class TestReloadAgentMcpDelegation:
    """reload_agent_mcp keeps its public return shape while delegating the
    rebuild to _hot_swap_agent."""

    async def test_success_shape(self, server_and_runner) -> None:
        srv, runner = server_and_runner
        with (
            patch("olano.core.builder.get_mcp_summary", return_value=[]),
            patch("olano.runtime.server.load_agent_config", return_value=runner.config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_B),
            patch(
                "olano.runtime.server.build_agent_async",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
        ):
            result = await srv.reload_agent_mcp(runner.config.name)
        assert result["ok"] is True and result["running"] is True
        assert "servers" in result and "notice" in result

    async def test_failure_shape(self, server_and_runner) -> None:
        srv, runner = server_and_runner
        with (
            patch("olano.core.builder.get_mcp_summary", return_value=[]),
            patch("olano.runtime.server.load_agent_config", return_value=runner.config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_B),
            patch(
                "olano.runtime.server.build_agent_async",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = await srv.reload_agent_mcp(runner.config.name)
        assert result == {"ok": False, "running": True, "reason": "boom"}

    async def test_not_running_shape(self) -> None:
        srv = make_server()
        with patch("olano.core.builder.get_mcp_summary", return_value=[]):
            result = await srv.reload_agent_mcp("ghost")
        assert result == {"ok": False, "running": False, "reason": "agent not running"}


class TestApplyMcpHealth:
    """The health sweep proposes reconnects; the runtime performs them."""

    async def test_reload_requested_triggers_a_reconnect(self, server_and_runner) -> None:
        from olano.core import mcp_health

        srv, runner = server_and_runner
        srv._mcp_health = {}
        report = mcp_health.AgentReport(agent=runner.config.name, checked_at=0.0)
        report.reload_urls = ["https://mcp.example.com/mcp"]
        calls: list[tuple[str, bool]] = []

        async def _reload(name: str, *, reset_backoff: bool = True) -> dict:
            calls.append((name, reset_backoff))
            return {"ok": True, "running": True, "servers": []}

        srv.reload_agent_mcp = _reload  # type: ignore[method-assign]
        with patch("olano.core.builder.get_mcp_summary", return_value=[]):
            await srv._apply_mcp_health({runner.config.name: report})

        # reset_backoff=False: the sweep already recorded this attempt, and must
        # not reset the schedule it is riding on.
        assert calls == [(runner.config.name, False)]

    async def test_healthy_report_reconnects_nothing(self, server_and_runner) -> None:
        from olano.core import mcp_health

        srv, runner = server_and_runner
        report = mcp_health.AgentReport(agent=runner.config.name, checked_at=0.0)
        srv.reload_agent_mcp = AsyncMock()  # type: ignore[method-assign]
        await srv._apply_mcp_health({runner.config.name: report})
        srv.reload_agent_mcp.assert_not_awaited()

    async def test_recovery_clears_the_backoff_for_that_server(self, server_and_runner) -> None:
        from olano.core import mcp_health

        srv, runner = server_and_runner
        url = "https://mcp.example.com/mcp"
        report = mcp_health.AgentReport(agent=runner.config.name, checked_at=0.0)
        report.reload_urls = [url]
        mcp_health._RECOVERY[(runner.config.name, url)] = mcp_health._Recovery(
            attempts=4, last_attempt=1.0
        )

        srv.reload_agent_mcp = AsyncMock(return_value={"ok": True, "running": True})  # type: ignore[method-assign]
        with patch(
            "olano.core.builder.get_mcp_summary",
            return_value=[{"url": url, "ok": True, "status": "connected", "tools": []}],
        ):
            await srv._apply_mcp_health({runner.config.name: report})

        assert (runner.config.name, url) not in mcp_health._RECOVERY
        mcp_health.reset_recovery()

    async def test_snapshot_shape(self, monkeypatch) -> None:
        from olano.backends import lionclaw_docker as ld

        monkeypatch.setattr(ld, "_LAST_REAP", {})
        srv = make_server()
        srv._mcp_health = {"emma": {"agent": "emma", "servers": []}}
        srv._mcp_health_checked_at = 123.0
        assert srv.mcp_health_snapshot() == {
            "checked_at": 123.0,
            "agents": {"emma": {"agent": "emma", "servers": []}},
            # Empty until the first ephemeral-container reap; then its report.
            "ephemeral_containers": {},
        }
        ld._LAST_REAP.update({"reaped": 3, "families": {"mcp": 3, "job": 0}})
        assert srv.mcp_health_snapshot()["ephemeral_containers"] == {
            "reaped": 3,
            "families": {"mcp": 3, "job": 0},
        }


class TestBootStateSeeding:
    async def test_runner_prompt_state_survives_thread_turns(self, server_and_runner) -> None:
        """The eagerly seeded snapshot is what threads diff against: after a
        hot-swap to STATE_B, a thread that saw STATE_A gets exactly the
        changed-section notice."""
        srv, runner = server_and_runner
        thread_id = str(uuid.uuid4())
        runner.update_thread_state(thread_id, prompt_sections_seen=dict(STATE_A.fingerprint))
        runner._append_session(thread_id, "user", "seed")

        with (
            patch("olano.runtime.server.load_agent_config", return_value=runner.config),
            patch("olano.runtime.server.compute_prompt_state", return_value=STATE_B),
            patch(
                "olano.runtime.server.build_agent_async",
                new_callable=AsyncMock,
                return_value=runner.agent,
            ),
        ):
            await srv._hot_swap_agent(runner.config.name, reason="test")

        # `ainvoke` is a wrapper over the stream path: read the payload off
        # the graph's `astream`.
        from unittest.mock import MagicMock

        captured: list = []

        async def _astream(payload, *args, **kwargs):
            captured.append(payload)
            msg = MagicMock()
            msg.content = "ok"
            yield (msg, {})

        runner.agent.astream = _astream
        await runner.ainvoke("hello again", thread_id=thread_id)
        msgs = captured[-1]["messages"]
        assert msgs[0]["role"] == "system"
        assert "new text" in msgs[0]["content"]
        seen = runner.load_thread_state(thread_id)["prompt_sections_seen"]
        assert seen["instructions"] == "hash-b"
