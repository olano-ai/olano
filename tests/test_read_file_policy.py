"""``read_file`` refuses audio, video, documents and binaries (``security/read_file_policy``).

deepagents returns those formats as one base64 block that the context counter
charges by length and the eviction middleware never offloads: a 30 second
voice note read with ``read_file`` was ~320k tokens, and the agent turn that
did it took the runtime past its memory limit.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.prebuilt.tool_node import ToolCallRequest

from olano.config.agent_config import AgentConfig
from olano.security import deepagents_middleware as mw
from olano.security import read_file_policy as policy
from olano.security.read_file_policy import read_file_refusal


def _wav(path: Path, seconds: float = 1.0) -> Path:
    """16 kHz mono 16-bit PCM, what the mobile app records."""
    pcm = b"\x01\x02" * int(16000 * seconds)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
    )
    path.write_bytes(header + pcm)
    return path


# ── the policy ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "tool"),
    [
        ("voice-1.wav", "audio_to_text"),
        ("song.MP3", "audio_to_text"),
        ("memo.m4a", "audio_to_text"),
        ("clip.mp4", "video_to_text"),
        ("screen.mkv", "video_to_text"),
        ("report.pdf", "document_to_text"),
        ("deck.pptx", "document_to_text"),
        ("letter.docx", "document_to_text"),
        ("photo.heic", "image_describe"),
    ],
)
def test_media_and_documents_point_at_their_tool(tmp_path: Path, name: str, tool: str) -> None:
    target = tmp_path / name
    target.write_bytes(b"\x00\x01binary")
    reason = read_file_refusal(target)
    assert f"{tool}(" in reason
    assert str(target) in reason


def test_a_type_is_refused_before_the_file_exists(tmp_path: Path) -> None:
    """The model learns the right tool even when it guessed the path wrong."""
    assert "audio_to_text" in read_file_refusal(tmp_path / "missing.wav")


def test_audio_refusal_says_a_transcript_needs_no_second_pass(tmp_path: Path) -> None:
    reason = read_file_refusal(_wav(tmp_path / "voice-1.wav"))
    assert "already in the conversation" in reason


def test_archives_point_at_the_shell(tmp_path: Path) -> None:
    target = tmp_path / "bundle.tar.gz"
    target.write_bytes(b"\x1f\x8b\x08\x00")
    assert "tar -tf" in read_file_refusal(target)


@pytest.mark.parametrize("name", ["notes.md", "page.html", "chart.svg", "data.csv", "Makefile"])
def test_text_reads_through(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    target.write_text("<p>hello</p>\n", encoding="utf-8")
    assert read_file_refusal(target) == ""


def test_an_unknown_binary_is_refused_by_its_bytes(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    target.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    assert "binary file" in read_file_refusal(target)


def test_a_small_image_reads_through_and_a_large_one_does_not(tmp_path: Path) -> None:
    small = tmp_path / "chart.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000)
    assert read_file_refusal(small) == ""

    large = tmp_path / "scan.jpg"
    large.write_bytes(b"\xff\xd8" + b"\x00" * policy.MAX_INLINE_IMAGE_BYTES)
    reason = read_file_refusal(large)
    assert "too large" in reason
    assert "image_describe(" in reason


def test_missing_text_files_are_left_to_the_tool(tmp_path: Path) -> None:
    assert read_file_refusal(tmp_path / "nope.md") == ""


def test_every_format_deepagents_inlines_is_decided_here() -> None:
    """An upstream addition to the base64 table must not slip past the guard.

    Images stay readable inline; every other type deepagents maps (and the
    optional video containers) must be refused by extension.
    """
    from deepagents.backends.utils import _EXTENSION_TO_FILE_TYPE, _VIDEO_EXTRA_EXTENSIONS

    refused = (
        policy.AUDIO_EXTENSIONS
        | policy.VIDEO_EXTENSIONS
        | policy.CONVERTED_DOCUMENT_EXTENSIONS
        | policy.OTHER_IMAGE_EXTENSIONS
    )
    for ext, kind in _EXTENSION_TO_FILE_TYPE.items():
        if kind == "image":
            assert ext in policy.INLINE_IMAGE_EXTENSIONS | policy.OTHER_IMAGE_EXTENSIONS, ext
        else:
            assert ext in refused, ext
    assert _VIDEO_EXTRA_EXTENSIONS <= policy.VIDEO_EXTENSIONS


# ── through the middleware ────────────────────────────────────────────────────


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        name="reader",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="reader",
        workspace=str(workspace),
        memory={"markdown": False, "rag": False, "self_learning": False},
    )


def _request(path: str) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "read_file", "args": {"file_path": path}, "id": "r1"},
        tool=None,
        state={},
        runtime=SimpleNamespace(),
    )


async def test_middleware_refuses_a_voice_note_without_reading_it(tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    wav = _wav(tmp_path / "uploads" / "voice-1.wav")
    handler = AsyncMock(return_value=ToolMessage(content="bytes", tool_call_id="r1"))
    middleware = mw.OlanoSecurityMiddleware(config=_config(tmp_path), workspace_root=tmp_path)

    out = await middleware.awrap_tool_call(_request(str(wav)), handler)

    handler.assert_not_awaited()
    assert out.status == "error"
    assert "audio_to_text" in str(out.content)


async def test_middleware_resolves_the_sandbox_alias(tmp_path: Path) -> None:
    """``/workspace/...`` is the same file; the alias must not dodge the guard."""
    (tmp_path / "uploads").mkdir()
    _wav(tmp_path / "uploads" / "voice-1.wav")
    handler = AsyncMock()
    middleware = mw.OlanoSecurityMiddleware(config=_config(tmp_path), workspace_root=tmp_path)

    out = await middleware.awrap_tool_call(_request("/workspace/uploads/voice-1.wav"), handler)

    handler.assert_not_awaited()
    assert "audio_to_text" in str(out.content)


async def test_middleware_passes_text_to_the_tool(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("hi", encoding="utf-8")
    handler = AsyncMock(return_value=ToolMessage(content="hi", tool_call_id="r1"))
    middleware = mw.OlanoSecurityMiddleware(config=_config(tmp_path), workspace_root=tmp_path)

    out = await middleware.awrap_tool_call(_request(str(tmp_path / "notes.md")), handler)

    handler.assert_awaited_once()
    assert out.content == "hi"


# ── one real graph ────────────────────────────────────────────────────────────


class _ScriptedModel(BaseChatModel):
    script: Iterator[AIMessage]
    prompts: list[list[BaseMessage]]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.prompts.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=next(self.script))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        return self


async def test_the_model_never_receives_the_recording(tmp_path: Path) -> None:
    """deepagents' own read_file on a real filesystem backend: what reaches the
    next model call is the refusal, not a base64 audio block."""
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langgraph.checkpoint.memory import InMemorySaver

    (tmp_path / "uploads").mkdir()
    wav = _wav(tmp_path / "uploads" / "voice-1.wav", seconds=30)
    config = _config(tmp_path)
    model = _ScriptedModel(
        script=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": str(wav)},
                            "id": "r1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        ),
        prompts=[],
    )
    graph = create_deep_agent(
        model=model,
        backend=FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False),
        middleware=mw.build_security_middleware(config=config, workspace_root=tmp_path),
        checkpointer=InMemorySaver(),
    )

    await graph.ainvoke(
        {"messages": [{"role": "user", "content": "what did I say?"}]},
        config={"configurable": {"thread_id": "t1"}},
    )

    result = [m for m in model.prompts[1] if isinstance(m, ToolMessage)][-1]
    assert "audio_to_text" in str(result.content)
    assert not any(
        isinstance(block, dict) and block.get("type") == "audio"
        for block in (result.content if isinstance(result.content, list) else [])
    )
    assert len(str(result.content)) < 2000
