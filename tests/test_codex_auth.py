from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=")
    return f"h.{payload.decode('ascii')}.s"


def test_codex_tokens_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from olano.models.codex_auth import read_codex_tokens, save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))

    save_codex_tokens({"access_token": "access", "refresh_token": "refresh"})
    data = read_codex_tokens()

    assert data["tokens"]["access_token"] == "access"
    assert data["tokens"]["refresh_token"] == "refresh"
    assert data["auth_mode"] == "chatgpt"


def test_resolve_codex_runtime_credentials_refreshes_expiring_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import olano.models.codex_auth as codex_auth

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    expired = _jwt({"exp": int(time.time()) - 10})
    codex_auth.save_codex_tokens({"access_token": expired, "refresh_token": "refresh-old"})

    def fake_refresh(access_token: str, refresh_token: str, *, timeout_seconds: float = 20.0):
        assert access_token == expired
        assert refresh_token == "refresh-old"
        return {"access_token": "access-new", "refresh_token": "refresh-new"}

    monkeypatch.setattr(codex_auth, "refresh_codex_oauth", fake_refresh)

    creds = codex_auth.resolve_codex_runtime_credentials()

    assert creds["api_key"] == "access-new"
    assert codex_auth.read_codex_tokens()["tokens"]["refresh_token"] == "refresh-new"


def test_codex_default_headers_include_account_id() -> None:
    from olano.models.codex_auth import codex_default_headers

    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})

    headers = codex_default_headers(token)

    assert headers["originator"] == "codex_cli_rs"
    assert headers["ChatGPT-Account-ID"] == "acct_123"


def test_fetch_codex_models_discovers_visible_models(monkeypatch: pytest.MonkeyPatch) -> None:
    import olano.models.codex_auth as codex_auth

    codex_auth._CODEX_MODELS_CACHE = None

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "models": [
                    {"slug": "hidden-model", "visibility": "hidden", "priority": 1},
                    {"slug": "gpt-5.3-codex", "priority": 20},
                    {"slug": "gpt-5.4-mini", "priority": 5},
                ]
            }

    def fake_get(url, *, headers, timeout):
        assert url.endswith("/models?client_version=1.0.0")
        assert headers["Authorization"] == "Bearer access-token"
        assert timeout == 5.0
        return FakeResponse()

    monkeypatch.setattr(codex_auth.httpx, "get", fake_get)

    models = codex_auth.fetch_codex_models("access-token")

    assert models == [
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.3-codex-spark",
    ]


def test_openai_codex_provider_builds_chat_openai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langchain_openai

    from olano.models import build_model, parse_model_string
    from olano.models.codex_auth import save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    token = _jwt({"exp": int(time.time()) + 3600})
    save_codex_tokens({"access_token": token, "refresh_token": "refresh"})

    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)

    model = build_model("openai-codex:gpt-5.3-codex")

    assert isinstance(model, FakeChatOpenAI)
    assert parse_model_string("openai-codex:gpt-5.3-codex") == (
        "openai-codex",
        "gpt-5.3-codex",
    )
    assert captured["model"] == "gpt-5.3-codex"
    assert captured["api_key"].get_secret_value() == token
    assert captured["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert captured["use_responses_api"] is True
    assert captured["streaming"] is True
    assert captured["store"] is False
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert captured["default_headers"]["originator"] == "codex_cli_rs"


def test_openai_codex_model_lifts_system_prompt_to_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import HumanMessage, SystemMessage

    from olano.models import build_model
    from olano.models.codex_auth import save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    token = _jwt({"exp": int(time.time()) + 3600})
    save_codex_tokens({"access_token": token, "refresh_token": "refresh"})

    model = build_model("openai-codex:gpt-5.4-mini")
    payload = model._get_request_payload(
        [SystemMessage(content="You are Kevin."), HumanMessage(content="Hello")]
    )

    assert payload["instructions"] == "You are Kevin."
    assert payload["stream"] is True
    assert payload["store"] is False
    assert [item["role"] for item in payload["input"]] == ["user"]


def test_openai_codex_model_sends_tools_non_strict_unless_chosen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Responses API reads a missing ``strict`` as strict, which makes every
    optional MCP field required (Lusha ``contacts_search`` got placeholder
    values beside ``contacts`` and refused the call)."""
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import StructuredTool

    from olano.models import build_model
    from olano.models.codex_auth import save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    token = _jwt({"exp": int(time.time()) + 3600})
    save_codex_tokens({"access_token": token, "refresh_token": "refresh"})

    tool = StructuredTool(
        name="contacts_search",
        description="Look up contacts.",
        args_schema={"type": "object", "properties": {"email": {"type": "string"}}},
        func=lambda **_: "ok",
    )
    model = build_model("openai-codex:gpt-5.5")
    msgs = [HumanMessage(content="Hello")]

    default = model.bind_tools([tool])
    payload = default.bound._get_request_payload(msgs, **default.kwargs)
    assert payload["tools"][0]["strict"] is False

    chosen = model.bind_tools([tool], strict=True)
    payload = chosen.bound._get_request_payload(msgs, **chosen.kwargs)
    assert payload["tools"][0]["strict"] is True


def test_openai_codex_model_forces_required_runtime_payload_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import HumanMessage

    from olano.models import build_model
    from olano.models.codex_auth import save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    token = _jwt({"exp": int(time.time()) + 3600})
    save_codex_tokens({"access_token": token, "refresh_token": "refresh"})

    model = build_model("openai-codex:gpt-5.5", store=True, streaming=False)
    payload = model._get_request_payload(
        [HumanMessage(content="Hello")],
        stream=False,
        store=True,
    )

    assert payload["instructions"]
    assert payload["stream"] is True
    assert payload["store"] is False


def test_openai_codex_model_drops_unstored_reasoning_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reasoning item without encrypted_content references an unpersisted
    server item under store=False, so it must be stripped to avoid a 404."""
    from langchain_core.messages import AIMessage, HumanMessage

    from olano.models import build_model
    from olano.models.codex_auth import save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    token = _jwt({"exp": int(time.time()) + 3600})
    save_codex_tokens({"access_token": token, "refresh_token": "refresh"})

    model = build_model("openai-codex:gpt-5.5")
    ai = AIMessage(
        content=[
            {
                "type": "reasoning",
                "id": "rs_abc",
                "summary": [{"type": "summary_text", "text": "x"}],
            },
            {"type": "text", "text": "hi", "id": "msg_1"},
        ]
    )
    payload = model._get_request_payload([HumanMessage(content="hi"), ai])

    reasoning_items = [
        item
        for item in payload["input"]
        if isinstance(item, dict) and item.get("type") == "reasoning"
    ]
    assert reasoning_items == []
    assert payload["store"] is False


def test_openai_codex_model_keeps_encrypted_reasoning_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning carrying inline encrypted_content round-trips without server
    storage, so it must be preserved to keep chain-of-thought across turns."""
    from langchain_core.messages import AIMessage, HumanMessage

    from olano.models import build_model
    from olano.models.codex_auth import save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    token = _jwt({"exp": int(time.time()) + 3600})
    save_codex_tokens({"access_token": token, "refresh_token": "refresh"})

    model = build_model("openai-codex:gpt-5.5")
    ai = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_enc", "encrypted_content": "ENC", "summary": []},
            {"type": "text", "text": "answer", "id": "msg_2"},
        ]
    )
    payload = model._get_request_payload([HumanMessage(content="hi"), ai])

    reasoning_items = [
        item
        for item in payload["input"]
        if isinstance(item, dict) and item.get("type") == "reasoning"
    ]
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["encrypted_content"] == "ENC"


@pytest.mark.asyncio
async def test_openai_codex_async_stream_keeps_tool_chunk_on_terminal_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langchain_openai

    from olano.models import build_model
    from olano.models.codex_auth import save_codex_tokens

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    token = _jwt({"exp": int(time.time()) + 3600})
    save_codex_tokens({"access_token": token, "refresh_token": "refresh"})

    async def fake_astream(self, *args, **kwargs):
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": "buy_stock",
                        "args": '{"symbol":"NVDA","qty":1}',
                        "id": "call_1",
                        "index": 0,
                    }
                ],
            )
        )
        raise TypeError("'NoneType' object is not iterable")

    monkeypatch.setattr(langchain_openai.ChatOpenAI, "_astream", fake_astream)

    model = build_model("openai-codex:gpt-5.5")
    chunks = []
    async for chunk in model._astream("Buy one NVDA share"):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].message.tool_call_chunks[0]["name"] == "buy_stock"


# ── Codex terminal stream-parse error guard ────────────────────────────────


def test_codex_terminal_parse_error_recognizes_nonetype_family() -> None:
    from olano.models import is_codex_terminal_parse_error

    # The two real langchain_openai Responses-streaming terminal errors we've seen.
    assert is_codex_terminal_parse_error(TypeError("'NoneType' object is not iterable"))
    assert is_codex_terminal_parse_error(
        AttributeError("'NoneType' object has no attribute 'search'")
    )
    # Same family, defensive coverage.
    assert is_codex_terminal_parse_error(AttributeError("'NoneType' object has no attribute 'get'"))
    assert is_codex_terminal_parse_error(TypeError("'NoneType' object is not subscriptable"))


def test_codex_terminal_parse_error_ignores_real_errors() -> None:
    from olano.models import is_codex_terminal_parse_error

    # A genuine bug must still propagate — not be swallowed as end-of-stream.
    assert not is_codex_terminal_parse_error(ValueError("boom"))
    assert not is_codex_terminal_parse_error(AttributeError("'Foo' object has no attribute 'bar'"))
    assert not is_codex_terminal_parse_error(RuntimeError("'NoneType' object has no attribute 'x'"))
