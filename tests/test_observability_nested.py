"""Model calls that run detached from the turn's callbacks still trace.

``stream_isolation`` cuts an in-graph model call (vision) and a Cortex worker
off the turn's callback tree on purpose, so their tokens never reach the
reply. Tracing re-attaches only the Langfuse handler: a vision call lands
under the turn that made it, a worker started from a turn (``delegate_task``)
nests under that turn, and a worker started on its own becomes its own
``cognition:<label>`` trace.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from olano.config.home import get_olano_paths
from olano.observability import credentials, tracing
from olano.observability import exporter as obs_exporter
from olano.observability.policy import effective_trace_policy


@pytest.fixture
def sink(monkeypatch):
    mem = InMemorySpanExporter()
    monkeypatch.setattr(obs_exporter, "build_otlp_exporter", lambda *a, **k: mem)
    monkeypatch.setattr(tracing, "_vault_values", lambda agent: [])
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-nested")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-nested-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    credentials._reset_for_tests()
    tracing._reset_for_tests()
    path = get_olano_paths().config_path
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    raw["observability"] = {"langfuse": {"enabled": True}}
    path.write_text(yaml.safe_dump(raw))
    yield mem
    tracing._reset_for_tests()


def _flush():
    for c in list(tracing._clients.values()):
        c.langfuse.flush()


def test_vision_call_lands_under_the_turn(base_config, sink, monkeypatch):
    from olano.tools import vision

    monkeypatch.setattr(
        vision,
        "_build_vision_model",
        lambda _m: GenericFakeChatModel(messages=iter([AIMessage(content="a cat")])),
    )
    trace, token = tracing.begin_run(
        effective_trace_policy(base_config), seed="v1", name="turn", session_id="s"
    )
    attempt = vision._invoke_vision(
        "anthropic:claude-haiku-4-5-20251001", [{"type": "text", "text": "describe"}]
    )
    tracing.end_run(trace, token)
    assert "a cat" in str(getattr(attempt, "text", attempt))
    _flush()
    spans = sink.get_finished_spans()
    root = next(s for s in spans if s.name == "turn")
    gen = next(s for s in spans if s.name == "GenericFakeChatModel")
    assert gen.parent.span_id == root.context.span_id


def _child(content="Done.") -> MagicMock:
    child = MagicMock()
    msg = MagicMock()
    msg.content = content
    child.ainvoke = AsyncMock(return_value={"messages": [msg]})
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=None))
    return child


async def test_worker_on_its_own_is_a_cognition_trace(base_config, sink, tmp_path):
    from langfuse.langchain import CallbackHandler

    from olano.cognition.worker import run_worker

    child = _child()
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="memory_garden",
        )
    assert outcome.status == "completed"
    config = child.ainvoke.call_args.kwargs["config"]
    assert any(isinstance(cb, CallbackHandler) for cb in config["callbacks"])
    assert "nostream" in config["tags"]
    assert tracing.current_trace() is None
    _flush()
    roots = [s for s in sink.get_finished_spans() if s.name == "cognition:memory_garden"]
    assert len(roots) == 1
    assert "origin:cognition" in roots[0].attributes["langfuse.trace.tags"]


async def test_worker_inside_a_turn_nests_under_it(base_config, sink, tmp_path):
    from olano.cognition.worker import run_worker

    child = _child()
    trace, token = tracing.begin_run(
        effective_trace_policy(base_config), seed="t1", name="turn", session_id="s"
    )
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="sub__research",
        )
    assert tracing.current_trace() is trace
    tracing.end_run(trace, token)
    from langfuse.langchain import CallbackHandler

    callbacks = child.ainvoke.call_args.kwargs["config"]["callbacks"]
    handler = next(cb for cb in callbacks if isinstance(cb, CallbackHandler))
    # Parented to the turn's root, not a trace of its own.
    assert handler._trace_context == {"trace_id": trace.trace_id, "parent_span_id": trace.root.id}
    _flush()
    names = [s.name for s in sink.get_finished_spans()]
    assert names.count("turn") == 1
    assert not any(n.startswith("cognition:") for n in names)


async def test_background_tracing_can_be_turned_off(base_config, sink, tmp_path):
    from olano.cognition.worker import run_worker

    path = get_olano_paths().config_path
    raw = yaml.safe_load(path.read_text())
    raw["observability"]["langfuse"]["trace_background"] = False
    path.write_text(yaml.safe_dump(raw))
    child = _child()
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="x",
        )
    assert "callbacks" not in child.ainvoke.call_args.kwargs["config"]
    assert tracing._clients == {}
