"""Helpers for suppressing noisy startup output while building agents.

These intentionally use real temporary files instead of ``io.StringIO``.
Child processes spawned during the wrapped call—most notably ``stdio`` MCP
servers—inherit ``sys.stdout``/``sys.stderr`` and call ``.fileno()`` on them
via ``subprocess.Popen``. ``StringIO`` has no ``fileno()``, which raises
``io.UnsupportedOperation: fileno`` and breaks stdio MCP servers in headless,
ACP, and chat startup paths. A ``TemporaryFile`` exposes a valid descriptor.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _log_captured(stdout_buffer: Any, stderr_buffer: Any) -> None:
    stdout_buffer.flush()
    stderr_buffer.flush()
    stdout_buffer.seek(0)
    stderr_buffer.seek(0)
    captured = (stdout_buffer.read() + stderr_buffer.read()).strip()
    if captured:
        logger.debug("Suppressed startup output:\n%s", captured[:8000])


def run_quietly(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run ``fn`` with stdout/stderr suppressed to temp files (fileno-safe)."""
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr_buffer,
    ):
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            result = fn(*args, **kwargs)
        _log_captured(stdout_buffer, stderr_buffer)
    return result


async def run_quietly_async(fn: Callable[..., Awaitable[_T]], *args: Any, **kwargs: Any) -> _T:
    """Async variant of :func:`run_quietly` (fileno-safe for stdio MCP)."""
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr_buffer,
    ):
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            result = await fn(*args, **kwargs)
        _log_captured(stdout_buffer, stderr_buffer)
    return result
