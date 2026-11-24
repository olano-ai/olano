"""Codex server_error failover: retry the identical request on sibling models.

The ChatGPT Codex backend deterministically returns an opaque in-stream
``server_error`` for certain (model, exact-prompt-bytes) combinations (observed
2026-07 on gpt-5.6-luna/gpt-5.5; same bytes pass on gpt-5.6-sol/terra). The
provider retries on ``CODEX_SERVER_ERROR_FALLBACKS`` when the error arrives
before any output.
"""

from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from openai import APIError

from olano.models import (
    CODEX_SERVER_ERROR_FALLBACKS,
    codex_server_error_fallbacks,
    is_codex_server_error,
)


def _server_error() -> APIError:
    return APIError(
        "An error occurred while processing your request.",
        request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses"),
        body={"type": "server_error", "code": "server_error"},
    )


def _other_api_error() -> APIError:
    return APIError(
        "insufficient quota",
        request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses"),
        body={"type": "insufficient_quota", "code": "insufficient_quota"},
    )


def test_is_codex_server_error_matches_only_server_error():
    assert is_codex_server_error(_server_error())
    assert not is_codex_server_error(_other_api_error())
    assert not is_codex_server_error(ValueError("boom"))


@pytest.mark.parametrize(
    "body",
    [
        {"type": "server_error", "code": "server_error"},
        # OpenAI's standard envelope: the classifier lives in `type` and `code`
        # is null for server faults. `APIError` copies each straight off the
        # body, so a code-only check missed this — the most common shape of the
        # very error the failover exists for. Live symptom (agent=andrew): the
        # user got "API error (APIError) … include the request ID …", no
        # "retrying on" line, and the same message failed every time while a
        # differently worded one went through.
        {"type": "server_error", "code": None},
        {"type": "server_error"},
    ],
)
def test_server_error_detected_however_the_body_labels_it(body):
    exc = APIError(
        "An error occurred while processing your request.",
        request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses"),
        body=body,
    )
    assert is_codex_server_error(exc)


@pytest.mark.parametrize(
    "body",
    [
        {"type": "invalid_request_error", "code": "context_length_exceeded"},
        {"type": "authentication_error", "code": "invalid_api_key"},
        {"type": "rate_limit_error", "code": "rate_limit_exceeded"},
    ],
)
def test_non_server_errors_are_never_retried_on_a_sibling(body):
    """Widening to `type` must not turn deterministic rejections into retries —
    replaying those on another model just burns quota and delays the real error.
    """
    exc = APIError(
        "nope",
        request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses"),
        body=body,
    )
    assert not is_codex_server_error(exc)


def _converter_error(chunk: object) -> Exception:
    """The exception langchain-openai's own stream converter raises for ``chunk``."""
    import langchain_openai.chat_models.base as lc_base

    try:
        lc_base._convert_responses_chunk_to_generation_chunk(chunk, 0, -1, -1)
    except Exception as exc:  # the exception is the result
        return exc
    raise AssertionError("the converter accepted a stream error event")


def _error_event(code: str) -> object:
    from openai.types.responses import ResponseErrorEvent

    return ResponseErrorEvent(
        type="error", code=code, message="An error occurred.", param=None, sequence_number=3
    )


def _failed_event(code: str) -> object:
    from openai.types.responses import Response, ResponseFailedEvent
    from openai.types.responses.response_error import ResponseError

    response = Response.model_construct(
        id="resp_1", error=ResponseError(code=code, message="An error occurred.")
    )
    return ResponseFailedEvent.model_construct(
        type="response.failed", response=response, sequence_number=4
    )


@pytest.mark.parametrize("make_event", [_error_event, _failed_event])
def test_langchain_openai_stream_error_shapes_are_server_errors(make_event):
    """langchain-openai 1.7 raises ``ValueError`` for a Responses ``error`` /
    ``response.failed`` event (1.6 ignored both). Built through the real
    converter, so an upstream rewording fails here instead of silently
    disabling the sibling retry."""
    assert is_codex_server_error(_converter_error(make_event("server_error")))
    assert not is_codex_server_error(_converter_error(make_event("rate_limit_exceeded")))


def test_fallback_chain_excludes_current_model():
    chain = codex_server_error_fallbacks("gpt-5.6-sol")
    assert "gpt-5.6-sol" not in chain
    assert chain == [m for m in CODEX_SERVER_ERROR_FALLBACKS if m != "gpt-5.6-sol"]


def test_fallback_chain_env_override(monkeypatch):
    monkeypatch.setenv("LIONCLAW_CODEX_FALLBACK_MODELS", "gpt-5.6-terra, gpt-5.5")
    assert codex_server_error_fallbacks("gpt-5.6-luna") == ["gpt-5.6-terra", "gpt-5.5"]
    monkeypatch.setenv("LIONCLAW_CODEX_FALLBACK_MODELS", "")
    assert codex_server_error_fallbacks("gpt-5.6-luna") == []


def _build_codex_model():
    """Build the real _ChatOpenAICodex with stubbed credentials."""
    with (
        patch(
            "olano.models.codex_auth.resolve_codex_runtime_credentials",
            return_value={"api_key": "test-token", "base_url": "https://example.invalid/v1"},
        ),
        patch(
            "olano.models.codex_auth.codex_default_headers",
            return_value={},
        ),
    ):
        from olano.models import build_model

        return build_model("openai-codex:gpt-5.6-luna")


def _chunk(text: str) -> ChatGenerationChunk:
    return ChatGenerationChunk(message=AIMessageChunk(content=text))


async def test_astream_retries_on_server_error_with_fallback_model():
    model = _build_codex_model()
    calls: list[str | None] = []

    async def fake_super_astream(self, *args, **kwargs):
        calls.append(kwargs.get("model"))
        if len(calls) == 1:
            raise _server_error()
        yield _chunk("ok")

    with patch("langchain_openai.chat_models.base.ChatOpenAI._astream", fake_super_astream):
        chunks = [c async for c in model._astream([HumanMessage("hi")])]

    assert [c.message.content for c in chunks] == ["ok"]
    # First attempt: no override (configured model); second: first fallback.
    assert calls == [None, CODEX_SERVER_ERROR_FALLBACKS[0]]


async def test_astream_does_not_retry_other_api_errors():
    model = _build_codex_model()
    calls: list[str | None] = []

    async def fake_super_astream(self, *args, **kwargs):
        calls.append(kwargs.get("model"))
        raise _other_api_error()
        yield  # pragma: no cover

    with patch("langchain_openai.chat_models.base.ChatOpenAI._astream", fake_super_astream):
        with pytest.raises(APIError):
            [c async for c in model._astream([HumanMessage("hi")])]

    assert calls == [None]


async def test_astream_does_not_retry_after_output_started():
    model = _build_codex_model()
    calls: list[str | None] = []

    async def fake_super_astream(self, *args, **kwargs):
        calls.append(kwargs.get("model"))
        yield _chunk("partial")
        raise _server_error()

    with patch("langchain_openai.chat_models.base.ChatOpenAI._astream", fake_super_astream):
        with pytest.raises(APIError):
            [c async for c in model._astream([HumanMessage("hi")])]

    assert calls == [None]


async def test_astream_raises_when_all_fallbacks_fail():
    model = _build_codex_model()
    calls: list[str | None] = []

    async def fake_super_astream(self, *args, **kwargs):
        calls.append(kwargs.get("model"))
        raise _server_error()
        yield  # pragma: no cover

    with patch("langchain_openai.chat_models.base.ChatOpenAI._astream", fake_super_astream):
        with pytest.raises(APIError):
            [c async for c in model._astream([HumanMessage("hi")])]

    assert calls == [None, *CODEX_SERVER_ERROR_FALLBACKS]


def test_stream_sync_retries_on_server_error():
    model = _build_codex_model()
    calls: list[str | None] = []

    def fake_super_stream(self, *args, **kwargs):
        calls.append(kwargs.get("model"))
        if len(calls) == 1:
            raise _server_error()
        yield _chunk("ok")

    with patch("langchain_openai.chat_models.base.ChatOpenAI._stream", fake_super_stream):
        chunks = list(model._stream([HumanMessage("hi")]))

    assert [c.message.content for c in chunks] == ["ok"]
    assert calls == [None, CODEX_SERVER_ERROR_FALLBACKS[0]]


# ── terminal-event usage salvage ───────────────────────────────────────────────
#
# langchain-openai attaches usage_metadata to a Responses stream only while
# parsing the final ``response.completed`` event; the Codex backend's terminal
# payload trips that parser with NoneType TypeErrors, so every call used to end
# with no usage recorded anywhere (no ledger event, nothing for Insights to
# mine). The salvage patch recovers token usage straight from the raw event.


class _FakeUsage:
    def model_dump(self):
        return {
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 0},
        }


class _FakeResponse:
    model = "gpt-5.6-luna"
    usage = _FakeUsage()


class _FakeTerminalChunk:
    type = "response.completed"
    response = _FakeResponse()


class _FakeDeltaChunk:
    type = "response.output_text.delta"


@pytest.fixture()
def _patched_converter(monkeypatch):
    """Install a converter that always raises, apply the salvage patch on top,
    and hand back the wrapped callable (originals restored afterwards)."""
    import langchain_openai.chat_models.base as lc_base

    from olano.models import _patch_responses_terminal_usage_salvage

    original = lc_base._convert_responses_chunk_to_generation_chunk

    def _raiser(chunk, *args, **kwargs):
        raise TypeError("'NoneType' object is not iterable")

    monkeypatch.setattr(lc_base, "_convert_responses_chunk_to_generation_chunk", _raiser)
    _patch_responses_terminal_usage_salvage()
    yield lc_base._convert_responses_chunk_to_generation_chunk
    monkeypatch.setattr(lc_base, "_convert_responses_chunk_to_generation_chunk", original)


def test_terminal_parse_crash_salvages_usage(_patched_converter):
    idx, out_idx, sub_idx, generation = _patched_converter(_FakeTerminalChunk(), 1, 2, 3)
    assert (idx, out_idx, sub_idx) == (1, 2, 3)
    assert isinstance(generation, ChatGenerationChunk)
    msg = generation.message
    assert isinstance(msg, AIMessageChunk)
    assert msg.usage_metadata is not None
    assert msg.usage_metadata["input_tokens"] == 120
    assert msg.usage_metadata["output_tokens"] == 30
    assert msg.response_metadata["model_name"] == "gpt-5.6-luna"


def test_non_terminal_parse_crash_still_raises(_patched_converter):
    with pytest.raises(TypeError):
        _patched_converter(_FakeDeltaChunk(), 0, 0, 0)


def test_salvage_patch_is_idempotent(_patched_converter):
    import langchain_openai.chat_models.base as lc_base

    from olano.models import _patch_responses_terminal_usage_salvage

    once = lc_base._convert_responses_chunk_to_generation_chunk
    _patch_responses_terminal_usage_salvage()
    assert lc_base._convert_responses_chunk_to_generation_chunk is once
