"""AgentFather lifecycle tools — start / stop (pause) / restart another agent.

They drive the SAME endpoints as the dashboard's buttons so lifecycle behavior
exists once (pause persistence, boot-error translation, SSE events); with no
runtime reachable, start/stop fall back to persisting the pause flag for the
next boot, and restart refuses honestly.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

import olano.admin.lifecycle as lifecycle
from olano.admin import restart_agent, start_agent, stop_agent
from olano.config.agent_state import is_agent_paused, set_agent_paused


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    agent_dir = home / "agents" / "merli"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.json5").write_text(json.dumps({"name": "merli"}), encoding="utf-8")
    return home


@pytest.fixture
def api(monkeypatch):
    """A reachable runtime API whose POSTs are recorded and scripted."""
    calls: list[str] = []
    responses: dict[str, object] = {}
    monkeypatch.setattr(lifecycle, "_api_reachable", lambda: True)

    def fake_post(path: str, timeout: float):
        calls.append(path)
        result = responses.get(path, {"ok": True, "status": "done"})
        if isinstance(result, Exception):
            raise result
        return dict(result)

    monkeypatch.setattr(lifecycle, "_api_post_json", fake_post)
    return calls, responses


# ── live runtime: the endpoints do the work ───────────────────────────────────


def test_start_posts_the_dashboard_endpoint(home, api):
    calls, _ = api
    result = start_agent("merli")
    assert calls == ["/api/agents/merli/start"]
    assert result["applied"] == "now"
    assert result["name"] == "merli"


def test_stop_posts_the_dashboard_endpoint(home, api):
    calls, _ = api
    result = stop_agent("merli")
    assert calls == ["/api/agents/merli/stop"]
    assert result["applied"] == "now"


def test_restart_posts_the_dashboard_endpoint(home, api):
    calls, _ = api
    result = restart_agent("merli")
    assert calls == ["/api/agents/merli/restart"]
    assert result["applied"] == "now"


def test_boot_failure_detail_reaches_the_caller(home, api):
    """The endpoint's structured boot errors (vault hints) must surface."""
    _, responses = api
    responses["/api/agents/merli/start"] = RuntimeError(
        "merli needs the secret DEEPSEEK_API_KEY to start, but your vault is locked."
    )
    with pytest.raises(RuntimeError, match="vault is locked"):
        start_agent("merli")


# ── no runtime: honest fallback to the persisted pause flag ───────────────────


def test_start_without_runtime_clears_the_pause_for_next_boot(home, monkeypatch):
    monkeypatch.setattr(lifecycle, "_api_reachable", lambda: False)
    set_agent_paused("merli", True)

    result = start_agent("merli")

    assert is_agent_paused("merli") is False
    assert result["applied"] == "next_boot"


def test_stop_without_runtime_persists_the_pause(home, monkeypatch):
    monkeypatch.setattr(lifecycle, "_api_reachable", lambda: False)

    result = stop_agent("merli")

    assert is_agent_paused("merli") is True
    assert result["applied"] == "next_boot"
    assert result["status"] == "paused"


def test_restart_without_runtime_refuses_honestly(home, monkeypatch):
    monkeypatch.setattr(lifecycle, "_api_reachable", lambda: False)
    with pytest.raises(RuntimeError, match="nothing to restart"):
        restart_agent("merli")


def test_unknown_agent_is_a_clear_error(home):
    with pytest.raises(ValueError, match="does not exist"):
        start_agent("ghost")


# ── the HTTP helper itself ────────────────────────────────────────────────────


def test_post_helper_surfaces_structured_boot_errors(home, monkeypatch):
    """An HTTP 503 with the endpoint's {detail: {message: …}} body becomes a
    RuntimeError carrying the human-readable message."""
    monkeypatch.setattr("olano.runtime.daemon._runtime_api_port", lambda: 1)
    body = json.dumps(
        {"detail": {"code": "vault_locked", "message": "vault is locked — unlock it"}}
    ).encode()

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "boom", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="vault is locked — unlock it"):
        lifecycle._api_post_json("/api/agents/x/start", timeout=1)


def test_post_helper_sends_the_dashboard_token(home, monkeypatch):
    monkeypatch.setattr("olano.runtime.daemon._runtime_api_port", lambda: 1)
    monkeypatch.setenv("LIONCLAW_DASHBOARD_TOKEN", "tok-123")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["auth"] = req.headers.get("Authorization")
        seen["method"] = req.get_method()
        return io.BytesIO(json.dumps({"ok": True}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = lifecycle._api_post_json("/api/agents/x/stop", timeout=1)

    assert result == {"ok": True}
    assert seen["auth"] == "Bearer tok-123"
    assert seen["method"] == "POST"


def test_in_process_runtime_counts_as_reachable(home, monkeypatch):
    """Inside `olano serve` there is no daemon pidfile — the in-process
    runtime handle is the reachability signal."""
    import olano.admin.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_get_runtime", lambda: object())
    assert lifecycle._api_reachable() is True


def test_post_helper_makes_the_auth_gate_actionable(home, monkeypatch):
    """A 401 {"detail": "auth_required"} from the multi-user cookie gate must
    not surface as the bare gate code — the tool caller can't act on that."""
    monkeypatch.setattr("olano.runtime.daemon._runtime_api_port", lambda: 1)
    body = json.dumps({"detail": "auth_required"}).encode()

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc:
        lifecycle._api_post_json("/api/agents/x/start", timeout=1)
    message = str(exc.value)
    assert message != "auth_required"
    assert "multi-user" in message
    assert "dashboard" in message


# ── in-process dispatch (inside `olano serve`) ─────────────────────────────


@pytest.fixture
def inproc_runtime(home, monkeypatch):
    """A live in-process 'runtime': a real loop on a background thread, a
    duck-typed RuntimeServer bound to it, and any HTTP attempt fails the test.

    This is the AgentFather's actual situation — and the regression scenario
    for `olano_start_agent` dying with `RuntimeError: auth_required`: the
    old loopback POST carried no session cookie, so on multi-user installs the
    cookie gate 401'd it before any handler ran.
    """
    import asyncio
    import threading
    from types import SimpleNamespace

    import olano.admin.diagnostics as diagnostics
    from olano.runtime import api as api_mod

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    events: list[tuple[str, dict]] = []

    class FakeRuntime:
        def __init__(self) -> None:
            self._loop = loop
            self._runners: dict[str, object] = {}
            self._agent_resources: dict[str, object] = {}
            self.registry = SimpleNamespace(get=lambda name: None)
            self.events = events

        async def _boot_agent(self, name: str) -> None:
            self._runners[name] = object()

        async def _stop_agent(self, name: str) -> None:
            self._runners.pop(name, None)
            self._agent_resources.pop(name, None)

    rt = FakeRuntime()
    monkeypatch.setattr(diagnostics, "_get_runtime", lambda: rt)
    monkeypatch.setattr(api_mod, "_push_event", lambda t, d: events.append((t, d)))

    def _no_http(*args, **kwargs):
        raise AssertionError("in-process dispatch must never fall back to HTTP")

    monkeypatch.setattr("urllib.request.urlopen", _no_http)
    monkeypatch.setattr(lifecycle, "_api_post_json", _no_http)

    yield rt

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_start_dispatches_in_process_without_http(home, inproc_runtime):
    set_agent_paused("merli", True)

    result = start_agent("merli")

    assert result["ok"] is True
    assert result["status"] == "running"
    assert result["applied"] == "now"
    assert "merli" in inproc_runtime._runners
    assert is_agent_paused("merli") is False
    assert ("agent_started", {"name": "merli"}) in inproc_runtime.events


def test_stop_dispatches_in_process_without_http(home, inproc_runtime):
    inproc_runtime._runners["merli"] = object()

    result = stop_agent("merli")

    assert result["status"] == "stopped"
    assert result["applied"] == "now"
    assert "merli" not in inproc_runtime._runners
    assert is_agent_paused("merli") is True
    assert ("agent_stopped", {"name": "merli"}) in inproc_runtime.events


def test_restart_dispatches_in_process_without_http(home, inproc_runtime):
    inproc_runtime._runners["merli"] = object()

    result = restart_agent("merli")

    assert result["status"] == "running"
    assert result.get("restarted") is True
    assert result["applied"] == "now"
    assert "merli" in inproc_runtime._runners


def test_multi_user_mode_cannot_401_the_in_process_path(home, inproc_runtime):
    """The original bug: multi_user_enabled installs 401'd the loopback POST
    (no session cookie) and the tool surfaced `RuntimeError: auth_required`.
    In-process dispatch never touches HTTP, so the cookie gate is out of the
    picture entirely — this must succeed with multi-user mode ON."""
    (home / "config.yaml").write_text("multi_user_enabled: true\n", encoding="utf-8")

    result = start_agent("merli")

    assert result["ok"] is True
    assert result["status"] == "running"


def test_in_process_boot_failure_surfaces_the_real_cause(home, inproc_runtime):
    """A failed in-process boot must raise the endpoint's own error message —
    same translation as the HTTP path's {detail: ...} handling."""
    from types import SimpleNamespace

    async def _broken_boot(name):
        return None  # never registers a runner

    inproc_runtime._boot_agent = _broken_boot
    inproc_runtime.registry = SimpleNamespace(
        get=lambda name: SimpleNamespace(error="the model exploded")
    )

    with pytest.raises(RuntimeError, match="the model exploded"):
        start_agent("merli")


def test_in_process_timeout_is_a_clear_error(home, inproc_runtime):
    import asyncio

    async def _slow():
        await asyncio.sleep(30)
        return {}

    with pytest.raises(RuntimeError, match="timed out"):
        lifecycle._run_inproc(_slow, 0.2, inproc_runtime._loop)


def test_structured_detail_translation_matches_http_path():
    """In-process HTTPException details and HTTP body details go through the
    same translator, so the tool sees identical messages either way."""
    with pytest.raises(RuntimeError, match="vault is locked — unlock it"):
        lifecycle._raise_lifecycle_detail(
            {"code": "vault_locked", "message": "vault is locked — unlock it"},
            "fallback",
            Exception("cause"),
        )
    with pytest.raises(RuntimeError, match="plain detail"):
        lifecycle._raise_lifecycle_detail("plain detail", "fallback", Exception("cause"))


def test_duck_typed_runtime_without_loop_falls_back_to_http(home, monkeypatch):
    """Runtimes without a real loop (MagicMock in tests, partial boots) must
    not be dispatched to in-process — the HTTP path stays their behavior."""
    from unittest.mock import MagicMock

    import olano.admin.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_get_runtime", lambda: MagicMock())
    assert lifecycle._inproc_runtime() is None

    monkeypatch.setattr(diagnostics, "_get_runtime", lambda: object())
    assert lifecycle._inproc_runtime() is None


# ── policy ────────────────────────────────────────────────────────────────────


def test_lifecycle_tools_are_hitl_gated():
    from olano.security.hitl_policy import CATEGORY_TOOLS

    gated = CATEGORY_TOOLS["write_external"]
    for name in ("olano_start_agent", "olano_stop_agent", "olano_restart_agent"):
        assert name in gated
