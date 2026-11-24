"""A real agent turn, traced: ``build_agent_async`` → ``AgentRunner.astream_events``.

Real graph, real middleware, real tools; only the model is ``fake:instant``
(scripted to call a tool, then answer) and the OTLP exporter is an in-memory
sink in its exact place. This is what proves the runner wiring: one trace
per turn, the graph's model and tool spans under the turn's root, the trace
id on the transcript row, and nothing of the conversation in ``metadata``
capture.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from olano.config.home import get_olano_paths
from olano.observability import credentials, tracing
from olano.observability import exporter as obs_exporter

SECRET_IN_REPLY = "sk-ant-thisisnotarealkeybutlookslikeone"


def _enable(capture: str = "metadata", **extra) -> None:
    path = get_olano_paths().config_path
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    raw["observability"] = {
        "langfuse": {"enabled": True, "capture": capture, "project_id": "p1", **extra}
    }
    path.write_text(yaml.safe_dump(raw))


@pytest.fixture
def sink(monkeypatch):
    mem = InMemorySpanExporter()
    monkeypatch.setattr(obs_exporter, "build_otlp_exporter", lambda *a, **k: mem)
    monkeypatch.setattr(tracing, "_vault_values", lambda agent: [])
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-turn-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-turn-test-secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://jp.cloud.langfuse.com")
    credentials._reset_for_tests()
    tracing._reset_for_tests()
    yield mem
    tracing._reset_for_tests()


async def _run_turn(base_config, monkeypatch, message="what is in here?", thread_id="obs-t1"):
    monkeypatch.setenv(
        "LIONCLAW_FAKE_MODEL_SCRIPT",
        json.dumps(
            [{"tool": "ls", "args": {"path": "."}}, {"text": f"Found files. {SECRET_IN_REPLY}"}]
        ),
    )
    base_config.model = "fake:instant"
    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner

    agent = await build_agent_async(base_config)
    runner = AgentRunner(agent, base_config)
    text = ""
    async for etype, payload in runner.astream_events(
        message, thread_id=thread_id, turn_id="turn-1"
    ):
        if etype == "text":
            text += payload
    return runner, text


def _flush():
    for c in list(tracing._clients.values()):
        c.langfuse.flush()


async def test_a_traced_turn(base_config, monkeypatch, sink):
    _enable()
    runner, text = await _run_turn(base_config, monkeypatch)
    assert "Found files." in text
    assert tracing.current_trace() is None
    _flush()
    spans = sink.get_finished_spans()
    roots = [s for s in spans if s.name == "turn"]
    assert len(roots) == 1, [s.name for s in spans]
    root = roots[0]
    trace_hex = format(root.context.trace_id, "032x")
    assert {format(s.context.trace_id, "032x") for s in spans} == {trace_hex}

    from olano.core.checkpoints import scope_thread
    from olano.observability.ids import trace_id_for, turn_seed

    scoped = scope_thread(base_config.name, "obs-t1")
    assert trace_hex == trace_id_for(turn_seed(base_config.name, scoped, "turn-1"))
    assert root.attributes["session.id"] == scoped
    tags = set(root.attributes["langfuse.trace.tags"])
    assert {f"agent:{base_config.name}", "origin:interactive"} <= tags

    names = [s.name for s in spans]
    assert "ls" in names, names
    assert names.count("FakeChatModel") + names.count("ChatFake") >= 1 or any(
        s.attributes.get("langfuse.observation.type") == "generation" for s in spans
    )
    ids = {s.context.span_id for s in spans}
    assert all(s.parent is not None and s.parent.span_id in ids for s in spans if s is not root)

    # Usage reached the root at turn end.
    assert "langfuse.observation.metadata.olano_input_tokens" in root.attributes

    # metadata capture: nothing said or read leaves the process.
    blob = json.dumps([dict(s.attributes) for s in spans])
    for leaked in ("what is in here?", "Found files.", SECRET_IN_REPLY, "You are a test assistant"):
        assert leaked not in blob, leaked

    # The transcript row names its trace.
    rows = [json.loads(line) for line in runner._session_path("obs-t1").read_text().splitlines()]
    assistant = [r for r in rows if r.get("role") == "assistant"]
    assert assistant and assistant[-1]["trace_id"] == trace_hex
    # ...and the turn, which is what a feedback vote names.
    assert assistant[-1]["turn_id"] == "turn-1"
    # What the chat route puts on its `done` frame.
    assert runner.last_trace_id("obs-t1") == trace_hex
    assert all("trace_id" not in r for r in rows if r.get("role") == "user")


def _capture_scores(monkeypatch) -> list[dict]:
    """The score seam: what our client hands Langfuse's ingestion queue."""
    sent: list[dict] = []
    for c in tracing._clients.values():
        monkeypatch.setattr(c.langfuse, "create_score", lambda **kw: sent.append(kw))
    return sent


@pytest.mark.parametrize("capture", ["metadata", "full"])
async def test_a_vote_scores_the_turns_trace(base_config, monkeypatch, sink, capture):
    from olano.core.feedback import turn_trace_id
    from olano.observability.feedback import feedback_score_id, send_feedback

    _enable(capture)
    runner, _ = await _run_turn(base_config, monkeypatch)
    trace_hex = runner.last_trace_id("obs-t1")
    assert turn_trace_id(base_config.name, "obs-t1", "turn-1") == trace_hex
    sent = _capture_scores(monkeypatch)
    comment = f"wrong file list {SECRET_IN_REPLY}"
    assert send_feedback(base_config, trace_hex, user="owner", value=-1, comment=comment)
    assert len(sent) == 1
    score = sent[0]
    assert score["trace_id"] == trace_hex
    assert score["name"] == "user_feedback"
    assert score["value"] == -1.0
    assert score["score_id"] == feedback_score_id(trace_hex, "owner")
    assert "owner" not in score["score_id"]
    if capture == "metadata":
        assert score["comment"] is None
    else:
        assert score["comment"].startswith("wrong file list")
        assert SECRET_IN_REPLY not in score["comment"]
    # A changed vote replaces the same score.
    send_feedback(base_config, trace_hex, user="owner", value=1)
    assert sent[1]["score_id"] == score["score_id"]


async def test_full_capture_keeps_text_but_masks_secrets(base_config, monkeypatch, sink):
    _enable("full")
    await _run_turn(base_config, monkeypatch)
    _flush()
    blob = json.dumps([dict(s.attributes) for s in sink.get_finished_spans()])
    assert "what is in here?" in blob
    assert "Found files." in blob
    assert SECRET_IN_REPLY not in blob


async def test_agent_override_off_means_no_trace(base_config, monkeypatch, sink):
    _enable()
    base_config.observability.langfuse.enabled = False
    runner, text = await _run_turn(base_config, monkeypatch)
    assert "Found files." in text
    _flush()
    assert sink.get_finished_spans() == ()
    rows = runner._session_path("obs-t1").read_text()
    assert "trace_id" not in rows
    assert runner.last_trace_id("obs-t1") == ""
    assert tracing._clients == {}
    # Feedback works without tracing: the reply is still named by its turn.
    from olano.core.feedback import turn_trace_id
    from olano.observability.feedback import send_feedback

    assert turn_trace_id(base_config.name, "obs-t1", "turn-1") == ""
    assert send_feedback(base_config, "", user="owner", value=1) is False


async def test_export_failure_never_touches_the_turn(base_config, monkeypatch, sink):
    class _Down:
        def export(self, spans):
            raise ConnectionError("host down")

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=0):
            return True

    monkeypatch.setattr(obs_exporter, "build_otlp_exporter", lambda *a, **k: _Down())
    tracing._reset_for_tests()
    _enable()
    _runner, text = await _run_turn(base_config, monkeypatch)
    assert "Found files." in text
    _flush()
    stats = tracing.client_stats()
    assert stats and stats[0]["failed_batches"] >= 1


async def test_a_begin_failure_never_touches_the_turn(base_config, monkeypatch, sink):
    _enable()

    def _boom(*a, **k):
        raise RuntimeError("sdk exploded")

    monkeypatch.setattr(tracing, "_build_client", _boom)
    _runner, text = await _run_turn(base_config, monkeypatch)
    assert "Found files." in text


def test_tracing_off_never_imports_langfuse(tmp_path):
    """With keys in the env but tracing off, a whole turn runs without ever
    importing the SDK (the memory/latency floor for every untraced box)."""
    code = textwrap.dedent(
        f"""
        import asyncio, os, sys
        os.environ["OLANO_HOME"] = {str(tmp_path / "home")!r}
        os.environ["LIONCLAW_HOME"] = {str(tmp_path / "home")!r}
        os.environ["LIONCLAW_CHECKPOINT_BACKEND"] = "sqlite"
        os.environ["LIONCLAW_STORE_BACKEND"] = "sqlite"
        os.environ["LIONCLAW_SYNC_CARTOGRAPHY"] = "1"
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-x"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-x"
        from olano.config.home import ensure_olano_home
        ensure_olano_home()
        from olano.config.agent_config import AgentConfig, MemoryConfig
        from olano.config.backend_config import FilesystemBackendConfig
        ws = {str(tmp_path / "ws")!r}
        os.makedirs(ws, exist_ok=True)
        open(os.path.join(ws, "MEMORY.md"), "w").write("# m")
        cfg = AgentConfig(name="t", model="fake:instant", workspace=ws,
                          memory=MemoryConfig(markdown=True, rag=False, self_learning=False),
                          backend=FilesystemBackendConfig(root_dir=ws, virtual_mode=False))
        from olano.core.builder import build_agent_async
        from olano.core.runner import AgentRunner
        async def main():
            runner = AgentRunner(await build_agent_async(cfg), cfg)
            async for _ in runner.astream_events("hi", thread_id="x"):
                pass
        asyncio.run(main())
        leaked = sorted(m for m in sys.modules if m == "langfuse" or m.startswith("langfuse."))
        print("LEAKED", leaked, flush=True)
        # Skip interpreter teardown: unrelated runtime threads can hold it up,
        # and what this test measures is already printed.
        os._exit(0)
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    assert "LEAKED []" in out.stdout, out.stdout[-2000:]
