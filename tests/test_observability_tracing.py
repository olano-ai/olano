"""Turn tracing internals: ids, the mask, the exporter and the run API.

Spans are captured in-process: ``build_otlp_exporter`` is swapped for an
OpenTelemetry ``InMemorySpanExporter``, which sits exactly where the real OTLP
exporter does, so what these tests read is what would have left the process,
after :class:`MaskingSpanExporter` applied the mask.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from olano.observability import exporter as obs_exporter
from olano.observability import tracing
from olano.observability.ids import (
    pseudonymous_user,
    sampled,
    session_url,
    trace_id_for,
    trace_url,
)
from olano.observability.masking import (
    SECRET_PLACEHOLDER,
    STRICT,
    MaskContext,
    mask_attributes,
    mask_status_description,
    mask_text,
    secret_values,
)
from olano.observability.policy import TracePolicy

SECRET = "sk-lf-supersecretvalue"


def _policy(**over) -> TracePolicy:
    base = dict(
        agent="merli",
        host="https://jp.cloud.langfuse.com",
        public_key="pk-lf-test-a",
        secret_key=SECRET,
        capture="metadata",
        sample_rate=1.0,
        environment="",
        trace_background=True,
        project_id="proj1",
        credential_source="env",
    )
    base.update(over)
    return TracePolicy(**base)


@pytest.fixture
def captured(monkeypatch):
    """Every client's exporter is one in-memory sink per public key."""
    sinks: dict[str, InMemorySpanExporter] = {}

    def _build(host, public_key, secret_key, **_kw):
        sinks.setdefault(public_key, InMemorySpanExporter())
        return sinks[public_key]

    monkeypatch.setattr(obs_exporter, "build_otlp_exporter", _build)
    monkeypatch.setattr(tracing, "_vault_values", lambda agent: [])
    tracing._reset_for_tests()
    yield sinks
    tracing._reset_for_tests()


def _flush_all() -> None:
    for c in list(tracing._clients.values()):
        c.langfuse.flush()


def _spans(sinks, pk="pk-lf-test-a"):
    _flush_all()
    return sinks[pk].get_finished_spans()


# ── ids ───────────────────────────────────────────────────────────────────────


def test_trace_id_matches_langfuse():
    from langfuse import Langfuse

    for seed in ("a", "olano:merli:merli::t1:turn", "ünïcode"):
        assert trace_id_for(seed) == Langfuse.create_trace_id(seed=seed)


def test_sampling_is_deterministic_and_proportional():
    ids = [trace_id_for(str(i)) for i in range(4000)]
    assert all(sampled(t, 1.0) for t in ids)
    assert not any(sampled(t, 0.0) for t in ids)
    share = sum(sampled(t, 0.25) for t in ids) / len(ids)
    assert 0.2 < share < 0.3
    assert [sampled(t, 0.5) for t in ids[:50]] == [sampled(t, 0.5) for t in ids[:50]]


def test_links_and_pseudonyms():
    assert trace_url("https://h/", "p1", "abc") == "https://h/project/p1/traces/abc"
    assert trace_url("https://h", "", "abc") == ""
    assert (
        session_url("https://h", "p1", "merli::t 1")
        == "https://h/project/p1/sessions/merli%3A%3At%201"
    )
    a = pseudonymous_user("+6591234567", b"salt")
    assert a == pseudonymous_user("+6591234567", b"salt")
    assert a != pseudonymous_user("+6591234567", b"other")
    assert "6591234567" not in a


# ── the mask ──────────────────────────────────────────────────────────────────


def test_metadata_mode_withholds_content_and_unsafe_metadata():
    attrs = {
        "langfuse.observation.input": '[{"role":"user","content":"my diagnosis is X"}]',
        "langfuse.observation.output": "reply text",
        "langfuse.observation.status_message": "failed reading /home/x/secret.txt",
        "gen_ai.prompt.0.content": "hello",
        "langfuse.observation.metadata.workspace_root": "/home/merli/workspace",
        "langfuse.observation.metadata.ls_provider": "anthropic",
        "langfuse.observation.metadata.olano_credits": 2,
        "langfuse.observation.type": "generation",
        "session.id": "merli::t1",
    }
    out = mask_attributes(attrs, STRICT)
    assert json.loads(out["langfuse.observation.input"]) == {
        "withheld_chars": len(attrs["langfuse.observation.input"])
    }
    assert json.loads(out["langfuse.observation.output"]) == {"withheld_chars": 10}
    assert "secret.txt" not in out["langfuse.observation.status_message"]
    assert "hello" not in out["gen_ai.prompt.0.content"]
    assert "langfuse.observation.metadata.workspace_root" not in out
    assert out["langfuse.observation.metadata.ls_provider"] == "anthropic"
    assert out["langfuse.observation.metadata.olano_credits"] == 2
    assert out["langfuse.observation.type"] == "generation"
    assert out["session.id"] == "merli::t1"


def test_full_mode_keeps_content_but_masks_secrets_and_shapes():
    ctx = MaskContext(capture="full", secrets=secret_values(["vault-value-123456", "short"]))
    attrs = {
        "langfuse.observation.input": "use vault-value-123456 and sk-ant-abcdefghijklmnopqrstuvwx",
        "langfuse.observation.metadata.workspace_root": "/w",
        "langfuse.trace.tags": ("a", "Bearer abcdefghijklmnopqrstu"),
    }
    out = mask_attributes(attrs, ctx)
    assert out["langfuse.observation.input"] == f"use {SECRET_PLACEHOLDER} and {SECRET_PLACEHOLDER}"
    assert out["langfuse.observation.metadata.workspace_root"] == "/w"
    assert out["langfuse.trace.tags"] == ("a", SECRET_PLACEHOLDER)
    # A short value is never treated as a secret: it would shred prose.
    assert "short" not in ctx.secrets


@pytest.mark.parametrize(
    "token",
    [
        "sk-proj-abcdefghijklmnopqrst",
        "pk-lf-12345678-abcd",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "xoxb-1234567890-abcdef",
        "AKIAABCDEFGHIJKLMNOP",  # gitleaks:allow (fake key: a mask fixture)
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",  # gitleaks:allow (fake JWT: a mask fixture)
        "postgres://user:pw@db.example.com/x",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_credential_shapes_masked_in_every_mode(token):
    full = MaskContext(capture="full")
    assert token not in mask_text(f"x {token} y", full)


def test_tier_replaces_every_model_name():
    ctx = MaskContext(capture="full", tier="olano:pro")
    out = mask_attributes(
        {"langfuse.observation.model.name": "some-backing-model", "gen_ai.response.model": "m"}, ctx
    )
    assert out == {
        "langfuse.observation.model.name": "olano:pro",
        "gen_ai.response.model": "olano:pro",
    }


def test_status_description():
    assert mask_status_description("boom /x", STRICT) is None
    full = MaskContext(capture="full", secrets=("vault-value-123456",))
    assert mask_status_description("bad vault-value-123456", full) == f"bad {SECRET_PLACEHOLDER}"


# ── the exporter ──────────────────────────────────────────────────────────────


def test_exporter_endpoint_and_headers_match_the_sdk():
    from langfuse._client.span_processor import LangfuseSpanProcessor

    sdk = LangfuseSpanProcessor(
        public_key="pk-lf-a", secret_key="sk-lf-b", base_url="https://h.example.com"
    )
    try:
        sdk_exp = sdk._batch_processor._exporter
        ours = obs_exporter.build_otlp_exporter("https://h.example.com", "pk-lf-a", "sk-lf-b")
        assert ours._endpoint == sdk_exp._endpoint
        assert ours._client._headers == sdk_exp._client._headers
    finally:
        sdk.shutdown()


def test_masking_failure_drops_the_batch(captured):
    sink = InMemorySpanExporter()

    def _boom(_trace):
        raise RuntimeError("mask bug")

    exp = obs_exporter.MaskingSpanExporter(sink, _boom)
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    span = provider.get_tracer("t").start_span("s", attributes={"langfuse.observation.input": "x"})
    span.end()
    assert exp.export([span]) == SpanExportResult.SUCCESS
    assert sink.get_finished_spans() == ()
    assert exp.stats.snapshot()["masking_failures"] == 1


def test_inner_exporter_failure_is_counted_not_raised():
    class _Down:
        def export(self, spans):
            raise ConnectionError("refused")

        def shutdown(self):
            pass

    exp = obs_exporter.MaskingSpanExporter(_Down(), lambda _t: STRICT)
    from opentelemetry.sdk.trace import TracerProvider

    span = TracerProvider().get_tracer("t").start_span("s")
    span.end()
    assert exp.export([span]) == SpanExportResult.FAILURE
    assert "ConnectionError" in exp.stats.snapshot()["last_error"]


# ── the run API ───────────────────────────────────────────────────────────────


@tool
def lookup(q: str) -> str:
    """Look something up."""
    return f"result for {q} with {SECRET}"


@tool
def explode(q: str) -> str:
    """Fails."""
    raise ValueError(f"cannot read /private/{q}")


def test_run_metadata_mode_end_to_end(captured):
    trace, token = tracing.begin_run(
        _policy(),
        seed="s1",
        name="turn",
        session_id="merli::t1",
        sender="+6591234567",
        tags=["agent:merli"],
        metadata={"olano_origin": "interactive"},
    )
    assert trace is not None and tracing.current_trace() is trace
    assert trace.trace_id == trace_id_for("s1")
    assert trace.url == f"https://jp.cloud.langfuse.com/project/proj1/traces/{trace.trace_id}"
    model = GenericFakeChatModel(messages=iter([AIMessage(content="private reply")]))
    model.invoke("private question", config=tracing.run_config_additions())
    lookup.invoke({"q": "x"}, config=tracing.run_config_additions())
    with pytest.raises(ValueError):
        explode.invoke({"q": "y"}, config=tracing.run_config_additions())
    tracing.end_run(trace, token, metadata={"olano_credits": 1.5})
    assert tracing.current_trace() is None

    spans = _spans(captured)
    assert {s.name for s in spans} == {"turn", "GenericFakeChatModel", "lookup", "explode"}
    root = next(s for s in spans if s.name == "turn")
    assert all(s.context.trace_id == root.context.trace_id for s in spans)
    assert all(s.parent.span_id == root.context.span_id for s in spans if s is not root)
    assert root.attributes["session.id"] == "merli::t1"
    assert root.attributes["user.id"].startswith("u-")
    assert json.loads(root.attributes["langfuse.observation.metadata.olano_credits"]) == 1.5
    blob = json.dumps([dict(s.attributes) for s in spans]) + "".join(
        str(s.status.description) for s in spans
    )
    for leaked in (
        "private question",
        "private reply",
        "result for",
        "/private/",
        SECRET,
        "6591234567",
    ):
        assert leaked not in blob, leaked


def test_run_full_mode_masks_the_secret(captured):
    trace, token = tracing.begin_run(
        _policy(capture="full"), seed="s2", name="turn", session_id="m::t", sender="ann"
    )
    lookup.invoke({"q": "x"}, config=tracing.run_config_additions())
    tracing.end_run(trace, token)
    spans = _spans(captured)
    tool_span = next(s for s in spans if s.name == "lookup")
    assert "result for x" in tool_span.attributes["langfuse.observation.output"]
    assert SECRET not in tool_span.attributes["langfuse.observation.output"]
    assert next(s for s in spans if s.name == "turn").attributes["user.id"] == "ann"


def test_nested_config_attaches_under_the_turn_without_streaming(captured):
    trace, token = tracing.begin_run(_policy(), seed="s3", name="turn", session_id="m::t")
    cfg = tracing.nested_config({"tags": ["nostream"]})
    assert "nostream" in cfg["tags"]
    assert len(cfg["callbacks"]) == 1
    GenericFakeChatModel(messages=iter([AIMessage(content="v")])).invoke("img", config=cfg)
    tracing.end_run(trace, token)
    spans = _spans(captured)
    root = next(s for s in spans if s.name == "turn")
    nested = next(s for s in spans if s.name == "GenericFakeChatModel")
    assert nested.parent.span_id == root.context.span_id
    # Outside a trace nothing is added.
    assert tracing.nested_config({"tags": ["nostream"]}) == {"tags": ["nostream"]}
    assert tracing.run_config_additions() == {}


def test_unsampled_and_untraced_runs_do_nothing(captured):
    assert tracing.begin_run(None, seed="x", name="t", session_id="s") == (None, None)
    assert tracing.begin_run(_policy(sample_rate=0.0), seed="x", name="t", session_id="s") == (
        None,
        None,
    )
    tracing.end_run(None, None)
    assert captured == {}


async def test_two_projects_in_one_process_never_mix(captured):
    async def _turn(pk, label):
        trace, token = tracing.begin_run(
            _policy(public_key=pk, secret_key=f"sk-lf-{label}-secret"),
            seed=label,
            name="turn",
            session_id=label,
        )
        for _ in range(3):
            m = GenericFakeChatModel(messages=iter([AIMessage(content=label)]))
            await m.ainvoke(label, config=tracing.run_config_additions())
            await asyncio.sleep(0)
        tracing.end_run(trace, token)
        return trace.trace_id

    async def _run(pk, label):
        # Each turn in its own task, as the runtime runs them.
        return await asyncio.create_task(_turn(pk, label))

    ta, tb = await asyncio.gather(_run("pk-lf-test-a", "A"), _run("pk-lf-test-b", "B"))
    a, b = _spans(captured, "pk-lf-test-a"), _spans(captured, "pk-lf-test-b")
    assert len(a) == 4 and len(b) == 4
    assert {format(s.context.trace_id, "032x") for s in a} == {ta}
    assert {format(s.context.trace_id, "032x") for s in b} == {tb}


def test_changed_secret_rebuilds_the_client(captured):
    first = tracing._client_for(_policy())
    assert tracing._client_for(_policy()) is first
    second = tracing._client_for(_policy(secret_key="sk-lf-rotated-secret"))
    assert second is not first
    assert second.secret_digest != first.secret_digest


def test_unknown_trace_gets_the_strict_mask():
    assert tracing._mask_context_for("0" * 32) is STRICT


def test_global_tracer_provider_is_untouched(captured):
    import opentelemetry.trace as ot

    before = type(ot.get_tracer_provider())
    trace, token = tracing.begin_run(_policy(), seed="g", name="turn", session_id="s")
    tracing.end_run(trace, token)
    assert type(ot.get_tracer_provider()) is before


# ── media and the SDK's own pipeline ──────────────────────────────────────────

_IMAGE_URI = "data:image/png;base64," + "iVBORw0KGgo" * 50


def test_strip_media_shapes():
    from olano.observability.masking import MEDIA_PLACEHOLDER, strip_media

    data = [
        {"type": "image_url", "image_url": {"url": _IMAGE_URI}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "text", "text": "keep me"},
        b"\x89PNG",
    ]
    out = strip_media(data)
    assert out[0]["image_url"]["url"] == MEDIA_PLACEHOLDER
    assert out[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": MEDIA_PLACEHOLDER,
    }
    assert out[2]["text"] == "keep me"
    assert out[3] == MEDIA_PLACEHOLDER


def test_media_guard_is_installed_and_nothing_is_uploaded(captured):
    from olano.observability.masking import MEDIA_PLACEHOLDER

    client = tracing._client_for(_policy(capture="full"))
    manager = client.langfuse._resources._media_manager
    # The SDK must still route every media path through this method, or the
    # guard silently stops guarding (it is an instance override of a private one).
    from langfuse._task_manager.media_manager import MediaManager

    assert hasattr(MediaManager, "_find_and_process_media")
    assert manager._find_and_process_media.__name__ == "_guard"

    trace, token = tracing.begin_run(
        _policy(capture="full"), seed="m1", name="turn", session_id="s"
    )
    content = [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": _IMAGE_URI}},
    ]
    GenericFakeChatModel(messages=iter([AIMessage(content="a cat")])).invoke(
        [{"role": "user", "content": content}], config=tracing.run_config_additions()
    )
    tracing.end_run(trace, token)
    spans = _spans(captured)
    blob = json.dumps([dict(s.attributes) for s in spans])
    assert "iVBORw0KGgo" not in blob
    assert MEDIA_PLACEHOLDER in blob
    assert "what is this" in blob
    assert manager._queue.empty()  # no upload was ever queued


def test_metadata_mode_withholds_before_serializing_and_runs_inline(captured):
    trace, token = tracing.begin_run(_policy(), seed="m2", name="turn", session_id="s")
    handler = trace.handler()
    assert handler.run_inline is True
    GenericFakeChatModel(messages=iter([AIMessage(content="x")])).invoke(
        "hello", config=tracing.run_config_additions()
    )
    tracing.end_run(trace, token)
    gen = next(s for s in _spans(captured) if s.name == "GenericFakeChatModel")
    assert gen.attributes["langfuse.observation.input"] == '{"withheld": true}'
    full_trace, full_token = tracing.begin_run(
        _policy(capture="full"), seed="m3", name="turn", session_id="s"
    )
    assert full_trace.handler().run_inline is False
    tracing.end_run(full_trace, full_token)


def test_sdk_none_attribute_warning_is_filtered(captured, caplog):
    import logging

    tracing._client_for(_policy())
    with caplog.at_level(logging.WARNING, logger="langfuse"):
        logging.getLogger("langfuse").warning(
            "Propagated attribute 'metadata.lc_agent_name' is None. Dropping value."
        )
        logging.getLogger("langfuse").warning("something real")
    messages = [r.getMessage() for r in caplog.records if r.name == "langfuse"]
    assert messages == ["something real"]
