"""Tests for olano.admin.diagnostics — runtime/agent/MCP status + hang detection.

No live runtime is bound in the test process, so an autouse fixture forces the
offline/disk path deterministically (cross-test state could otherwise leave a
stale ``talk_to._RUNTIME`` bound). One test fakes an in-process runtime to
exercise the live path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from olano import admin
from olano.admin import diagnostics
from olano.config.home import get_olano_paths


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the offline/disk path: no in-process runtime, no daemon API."""
    monkeypatch.setattr(diagnostics, "_get_runtime", lambda: None)
    monkeypatch.setattr(diagnostics, "_api_get_json", lambda *a, **k: None)


def _write_session(agent: str, thread_id: str, records: list[dict]) -> Path:
    sdir = get_olano_paths().sessions_dir / agent
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{thread_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _ts(seconds_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


# ── runtime + agent status ───────────────────────────────────────────────────


def test_runtime_status_offline_reports_config_snapshot() -> None:
    admin.create_agent("alpha", description="a")
    out = diagnostics.runtime_status()
    assert out["running"] is False
    assert out["source"] == "disk"
    assert "alpha" in out["agents_offline"]
    assert out["agents_online"] == []


def test_agent_status_all_splits_online_offline() -> None:
    admin.create_agent("beta", description="b")
    out = diagnostics.agent_status()
    assert out["source"] == "disk"
    offline_names = {r["name"] for r in out["offline"]}
    assert "beta" in offline_names
    assert out["online"] == []


def test_agent_status_single_is_offline_configured() -> None:
    admin.create_agent("gamma", description="g")
    out = diagnostics.agent_status("gamma")
    assert out["online"] is False
    assert out["state"] == "configured"
    assert out["paused"] is False


def test_agent_status_reflects_paused_flag() -> None:
    from olano.config.agent_state import set_agent_paused

    admin.create_agent("delta", description="d")
    set_agent_paused("delta", True)
    out = diagnostics.agent_status("delta")
    assert out["online"] is False
    assert out["state"] == "paused"
    assert out["paused"] is True


def test_agent_status_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown agent"):
        diagnostics.agent_status("does-not-exist")


def test_agent_status_from_in_process_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A booted agent in ``_runners`` is reported online via the runtime source."""
    admin.create_agent("live", description="x", model="anthropic:claude-sonnet-4-6")
    cfg = SimpleNamespace(model="anthropic:claude-sonnet-4-6", channels=[], mcp_servers=[])
    fake_runner = SimpleNamespace(config=cfg)
    fake_registry = SimpleNamespace(get=lambda name: None)
    fake_runtime = SimpleNamespace(_runners={"live": fake_runner}, registry=fake_registry)
    monkeypatch.setattr(diagnostics, "_get_runtime", lambda: fake_runtime)

    out = diagnostics.agent_status("live")
    assert out["source"] == "runtime"
    assert out["online"] is True
    assert out["state"] == "running"


# ── MCP status ───────────────────────────────────────────────────────────────


def test_mcp_status_single_config_snapshot() -> None:
    admin.create_agent("mcp1", description="m")
    admin.add_mcp_server("mcp1", "https://mcp.example.com", transport="sse")
    out = diagnostics.mcp_status("mcp1")
    assert out["source"] == "config"
    assert out["running"] is False
    assert any(s["url"] == "https://mcp.example.com" for s in out["servers"])
    assert out["overall"] == "unknown"


def test_mcp_status_all_only_includes_agents_with_servers() -> None:
    admin.create_agent("with_mcp", description="m")
    admin.add_mcp_server("with_mcp", "https://mcp.example.com", transport="sse")
    admin.create_agent("no_mcp", description="n")
    out = diagnostics.mcp_status()
    names = {a["name"] for a in out["agents"]}
    assert "with_mcp" in names
    assert "no_mcp" not in names


# ── activity / hang detection ────────────────────────────────────────────────


def test_agent_activity_no_sessions() -> None:
    admin.create_agent("quiet", description="q")
    out = diagnostics.agent_activity("quiet")
    assert out["has_activity"] is False
    assert out["in_progress"] is False
    assert out["running_tool"] is None


def test_agent_activity_detects_running_tool() -> None:
    admin.create_agent("worker", description="w")
    _write_session(
        "worker",
        "t1",
        [
            {"ts": _ts(20), "role": "user", "content": "do it"},
            {"ts": _ts(10), "role": "assistant", "content": "ok"},
            {"ts": _ts(5), "role": "tool_call", "name": "web_search", "content": "web_search: q"},
        ],
    )
    out = diagnostics.agent_activity("worker")
    assert out["has_activity"] is True
    assert out["running_tool"] == "web_search"
    assert out["in_progress"] is True
    # Offline agent mid-tool is "interrupted", not "hanging".
    assert out["likely_stuck"] is False
    assert "offline" in out["detail"]


def test_agent_activity_tool_completed_is_idle() -> None:
    admin.create_agent("done", description="d")
    _write_session(
        "done",
        "t1",
        [
            {"ts": _ts(30), "role": "user", "content": "hi"},
            {"ts": _ts(20), "role": "tool_call", "name": "web_search", "content": "q"},
            {"ts": _ts(15), "role": "tool_result", "name": "web_search", "content": "r"},
            {"ts": _ts(10), "role": "assistant", "content": "here you go"},
        ],
    )
    out = diagnostics.agent_activity("done")
    assert out["running_tool"] is None
    assert out["in_progress"] is False
    assert out["last_role"] == "assistant"


def test_agent_activity_flags_hang_when_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """Online + inbound message pending past the threshold ⇒ likely_stuck."""
    admin.create_agent("stuck", description="s")
    _write_session("stuck", "t1", [{"ts": _ts(600), "role": "user", "content": "hello?"}])
    monkeypatch.setattr(
        diagnostics,
        "_agent_rows",
        lambda: ([{"name": "stuck", "online": True}], "runtime"),
    )
    out = diagnostics.agent_activity("stuck", stuck_after_seconds=120)
    assert out["in_progress"] is True
    assert out["likely_stuck"] is True
    assert "HANG" in out["detail"]


# ── one-shot diagnosis ───────────────────────────────────────────────────────


def test_diagnose_agent_offline_lists_problem() -> None:
    admin.create_agent("sick", description="s")
    out = diagnostics.diagnose_agent("sick")
    assert out["online"] is False
    assert any("offline" in p for p in out["problems"])
    assert out["summary"].startswith("'sick'")


# ── per-agent error log filtering ────────────────────────────────────────────


def test_agent_errors_filters_by_name() -> None:
    logs_dir = get_olano_paths().logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "olano.log").write_text(
        "INFO something unrelated happened\n"
        "ERROR Failed to boot agent 'needle': boom\n"
        "INFO another line about needle that is not an error\n"
        "ERROR unrelated other agent failure\n",
        encoding="utf-8",
    )
    out = admin.agent_errors("needle", lines=50)
    assert out["matched"] >= 1
    assert any("boom" in e["line"] for e in out["errors"])
    assert all("needle" in e["line"] for e in out["errors"])
