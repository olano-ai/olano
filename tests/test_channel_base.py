"""Tests for the BaseChannel adapter helpers: SendResult, retry classification,
truncation with code-fence preservation, and UTF-16 length counting."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from olano.channels.base import (
    BaseChannel,
    SendResult,
    is_retryable_error,
    is_timeout_error,
    truncate_message,
    utf16_len,
)


class _DummyChannel(BaseChannel):
    """Minimal BaseChannel subclass for exercising default behaviour."""

    name = "dummy"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self.sent: list[tuple[str, str]] = []
        self.send_errors: list[Exception] = []
        self.call_count = 0

    async def start(self) -> None:  # pragma: no cover - not used here
        pass

    async def stop(self) -> None:  # pragma: no cover - not used here
        await self.cancel_background_tasks()

    async def send(self, recipient: str, text: str) -> None:
        self.call_count += 1
        if self.send_errors:
            exc = self.send_errors.pop(0)
            raise exc
        self.sent.append((recipient, text))


class TestRetryClassifiers:
    @pytest.mark.parametrize(
        "error",
        [
            "ConnectError: could not connect",
            "NameResolutionError: dns lookup failed",
            "ConnectionRefusedError",
            "network is unreachable",
            "RemoteDisconnected: server closed",
        ],
    )
    def test_retryable_errors(self, error: str) -> None:
        assert is_retryable_error(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            "read timed out",
            "ReadTimeout: server stalled",
            "WriteTimeout",
        ],
    )
    def test_timeouts_are_not_retryable(self, error: str) -> None:
        assert is_retryable_error(error) is False
        assert is_timeout_error(error) is True

    def test_none_is_neither(self) -> None:
        assert is_retryable_error(None) is False
        assert is_timeout_error(None) is False

    def test_unknown_error_is_neither(self) -> None:
        assert is_retryable_error("bad request 400") is False
        assert is_timeout_error("bad request 400") is False


class TestUtf16Len:
    def test_ascii_matches_len(self) -> None:
        s = "hello"
        assert utf16_len(s) == len(s)

    def test_bmp_char_counts_as_one(self) -> None:
        # é is within BMP
        assert utf16_len("é") == 1

    def test_astral_emoji_counts_as_two(self) -> None:
        # 😀 is U+1F600 outside BMP → surrogate pair → 2 UTF-16 units
        assert len("😀") == 1
        assert utf16_len("😀") == 2


class TestTruncateMessage:
    def test_short_returns_single_chunk(self) -> None:
        assert truncate_message("hi", max_length=100) == ["hi"]

    def test_splits_long_text(self) -> None:
        text = "x" * 500
        chunks = truncate_message(text, max_length=200)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 200

    def test_indicator_suffix_on_multi_chunks(self) -> None:
        text = "a\n" * 500
        chunks = truncate_message(text, max_length=100)
        assert len(chunks) >= 2
        assert chunks[0].endswith(f"(1/{len(chunks)})")
        assert chunks[-1].endswith(f"({len(chunks)}/{len(chunks)})")

    def test_code_fence_preserved_across_split(self) -> None:
        body = "\n".join([f"line {i}" for i in range(200)])
        text = f"```python\n{body}\n```"
        chunks = truncate_message(text, max_length=400)
        assert len(chunks) >= 2
        # First chunk should close the fence it opened.
        assert chunks[0].count("```") % 2 == 0
        # A subsequent chunk should reopen the python fence.
        assert any(c.startswith("```python\n") for c in chunks[1:])
        # Every chunk must have a balanced number of fences.
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0

    def test_utf16_length_used_for_telegram(self) -> None:
        # Use a budget where pure emoji content will exceed UTF-16 but not len().
        text = "😀" * 50  # len == 50, utf16_len == 100
        chunks = truncate_message(text, max_length=60, len_fn=utf16_len)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert utf16_len(chunk) <= 60


class TestSendResult:
    def test_default_success(self) -> None:
        r = SendResult(success=True)
        assert r.success is True
        assert r.message_id is None
        assert r.error is None
        assert r.retryable is False

    def test_failure_fields(self) -> None:
        r = SendResult(success=False, error="boom", retryable=True)
        assert r.success is False
        assert r.retryable is True
        assert r.error == "boom"


class TestSendMessageWrapper:
    async def test_send_message_success(self) -> None:
        ch = _DummyChannel()
        result = await ch.send_message("user", "hi")
        assert result.success is True
        assert ch.sent == [("user", "hi")]

    async def test_send_message_classifies_retryable(self) -> None:
        ch = _DummyChannel()
        ch.send_errors = [RuntimeError("ConnectError: lost network")]
        result = await ch.send_message("user", "hi")
        assert result.success is False
        assert result.retryable is True

    async def test_send_message_classifies_non_retryable(self) -> None:
        ch = _DummyChannel()
        ch.send_errors = [RuntimeError("bad request 400")]
        result = await ch.send_message("user", "hi")
        assert result.success is False
        assert result.retryable is False


class TestSendWithRetry:
    async def test_success_first_try(self) -> None:
        ch = _DummyChannel()
        result = await ch.send_with_retry("user", "hi")
        assert result.success is True
        assert ch.call_count == 1

    async def test_retries_transient_then_succeeds(self) -> None:
        ch = _DummyChannel()
        ch.send_errors = [RuntimeError("ConnectError: transient")]
        result = await ch.send_with_retry("user", "hi", max_retries=2, base_delay=0.0)
        assert result.success is True
        assert ch.call_count == 2

    async def test_timeout_is_not_retried(self) -> None:
        ch = _DummyChannel()
        ch.send_errors = [
            RuntimeError("read timed out"),
            RuntimeError("read timed out"),
        ]
        result = await ch.send_with_retry("user", "hi", max_retries=3, base_delay=0.0)
        assert result.success is False
        # Timeout halts retry — exactly one attempt.
        assert ch.call_count == 1

    async def test_non_network_triggers_plain_text_fallback(self) -> None:
        ch = _DummyChannel()
        # First send fails with a formatting-style error → fallback once.
        ch.send_errors = [RuntimeError("bad markdown")]
        result = await ch.send_with_retry("user", "hello", max_retries=0, base_delay=0.0)
        # Fallback succeeded, so overall success.
        assert result.success is True
        assert ch.call_count == 2  # original + fallback
        # Fallback body mentions plain text.
        assert "plain text" in ch.sent[-1][1]


class TestBaseChannelState:
    def test_pending_queue_roundtrip(self) -> None:
        ch = _DummyChannel()
        from olano.channels.base import ChannelMessage

        msg = ChannelMessage(text="queued", user_id="u1", channel="dummy", thread_id="t1")
        assert ch.has_pending("t1") is False
        ch.queue_pending("t1", msg)
        assert ch.has_pending("t1") is True
        assert ch.pop_pending("t1") is msg
        assert ch.has_pending("t1") is False
        assert ch.pop_pending("t1") is None

    def test_typing_pause_toggle(self) -> None:
        ch = _DummyChannel()
        assert ch.is_typing_paused("chat1") is False
        ch.pause_typing("chat1")
        assert ch.is_typing_paused("chat1") is True
        ch.resume_typing("chat1")
        assert ch.is_typing_paused("chat1") is False

    async def test_background_task_tracking_and_cancel(self) -> None:
        ch = _DummyChannel()
        started = asyncio.Event()

        async def sleeper() -> None:
            started.set()
            await asyncio.sleep(3600)

        t = asyncio.create_task(sleeper())
        ch.track_task(t)
        await started.wait()
        assert t in ch._background_tasks
        await ch.cancel_background_tasks()
        assert t.cancelled() or t.done()
        assert ch._background_tasks == set()
