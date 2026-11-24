"""Shared helper classes for Olano platform adapters.

Extracts common patterns that were duplicated across 5-7 adapters:
message deduplication, text batch aggregation, markdown stripping,
and thread participation tracking.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union

# ─── Per-agent defaults ───────────────────────────────────────────────────────
# Helpers used by collision-prone adapters (WhatsApp, Webhook, SMS, …) to
# derive sensible per-agent defaults so two agents on the same platform
# don't fight over the same TCP port or session directory.


def per_agent_port(base_port: int, agent_name: str, span: int = 1000) -> int:
    """Return a deterministic ``base_port + offset`` for ``agent_name``.

    Without an agent name, returns ``base_port`` unchanged so legacy
    single-agent installs keep using the documented default.

    Uses SHA1 of the agent name modulo ``span`` for the offset — the
    distribution is uniform enough across realistic agent name sets
    that collisions are rare, and choosing a fixed span keeps the
    derived port within a predictable range for firewall configs.
    """
    if not agent_name:
        return base_port
    digest = hashlib.sha1(agent_name.encode("utf-8")).hexdigest()
    return base_port + (int(digest, 16) % span)


def per_agent_dir(base_dir: Path, agent_name: str, fallback: str = "_default") -> Path:
    """Return ``base_dir / <agent_name or fallback>``.

    Used to give each agent its own session/auth/state directory so two
    agents on the same platform don't corrupt each other's credentials.
    """
    return Path(base_dir) / (agent_name or fallback)


if TYPE_CHECKING:
    from olano.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


# ─── Message Deduplication ────────────────────────────────────────────────────


class MessageDeduplicator:
    """TTL-based message deduplication cache."""

    def __init__(self, max_size: int = 2000, ttl_seconds: float = 300):
        self._seen: dict[str, float] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

    def is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self._seen:
            if now - self._seen[msg_id] < self._ttl:
                return True
            del self._seen[msg_id]
        self._seen[msg_id] = now
        if len(self._seen) > self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        return False

    def clear(self) -> None:
        self._seen.clear()


class PersistentMessageDeduplicator:
    """Message-id dedupe that survives a process restart.

    :class:`MessageDeduplicator` forgets everything on restart, which is
    exactly when a messenger redelivers: WhatsApp replays offline messages
    when a linked device reconnects, and the bridge process outlives the
    runtime that spawned it. A message processed before the restart would
    then run the agent a second time (a duplicate trade instruction, a second
    charge).

    Ids are appended one per line to ``path`` and the last ``limit`` are
    loaded on start; the file is rewritten to that size once it doubles, so it
    stays bounded with one small append per message. A file that cannot be
    read or written degrades to in-memory dedupe, never to dropping messages.
    """

    def __init__(self, path: Path | str, limit: int = 5000) -> None:
        from collections import OrderedDict

        self._path = Path(path)
        self._limit = max(1, limit)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lines = 0
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        except OSError as exc:
            logger.warning("dedupe store %s unreadable (%s); starting empty", self._path, exc)
            lines = []
        for line in lines[-self._limit :]:
            if line:
                self._seen[line] = None
        self._lines = len(lines)

    def is_duplicate(self, msg_id: str) -> bool:
        """True when ``msg_id`` was seen before; otherwise record it."""
        msg_id = (msg_id or "").replace("\n", " ").strip()
        if not msg_id:
            return False
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = None
        while len(self._seen) > self._limit:
            self._seen.popitem(last=False)
        self._persist(msg_id)
        return False

    def _persist(self, msg_id: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._lines >= 2 * self._limit:
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_text("\n".join(self._seen) + "\n", encoding="utf-8")
                tmp.replace(self._path)
                self._lines = len(self._seen)
                return
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(msg_id + "\n")
            self._lines += 1
        except OSError as exc:
            logger.debug("dedupe store %s not writable: %s", self._path, exc)


# ─── Text Batch Aggregation ──────────────────────────────────────────────────


class TextBatchAggregator:
    """Aggregates rapid-fire text events into single messages."""

    def __init__(
        self,
        handler,
        *,
        batch_delay: float = 0.6,
        split_delay: float = 2.0,
        split_threshold: int = 4000,
    ):
        self._handler = handler
        self._batch_delay = batch_delay
        self._split_delay = split_delay
        self._split_threshold = split_threshold
        self._pending: dict[str, MessageEvent] = {}
        self._pending_tasks: dict[str, asyncio.Task] = {}

    def is_enabled(self) -> bool:
        return self._batch_delay > 0

    def enqueue(self, event: MessageEvent, key: str) -> None:
        chunk_len = len(event.text or "")
        existing = self._pending.get(key)
        if not existing:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending[key] = event
        else:
            existing.text = f"{existing.text}\n{event.text}"
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]

        prior = self._pending_tasks.get(key)
        if prior and not prior.done():
            prior.cancel()
        self._pending_tasks[key] = asyncio.create_task(self._flush(key))

    async def _flush(self, key: str) -> None:
        current_task = self._pending_tasks.get(key)
        pending = self._pending.get(key)
        last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0

        delay = self._split_delay if last_len >= self._split_threshold else self._batch_delay
        await asyncio.sleep(delay)

        event = self._pending.pop(key, None)
        if event:
            try:
                await self._handler(event)
            except Exception:
                logger.exception(
                    "[TextBatchAggregator] Error dispatching batched event for %s", key
                )

        if self._pending_tasks.get(key) is current_task:
            self._pending_tasks.pop(key, None)

    def cancel_all(self) -> None:
        for task in self._pending_tasks.values():
            if not task.done():
                task.cancel()
        self._pending_tasks.clear()
        self._pending.clear()


# ─── Markdown Stripping ──────────────────────────────────────────────────────

_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_ITALIC_STAR = re.compile(r"\*(.+?)\*", re.DOTALL)
_RE_BOLD_UNDER = re.compile(r"__(.+?)__", re.DOTALL)
_RE_ITALIC_UNDER = re.compile(r"_(.+?)_", re.DOTALL)
_RE_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\n?")
_RE_INLINE_CODE = re.compile(r"`(.+?)`")
_RE_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """Strip markdown formatting for plain-text platforms (SMS, iMessage, etc.)."""
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC_STAR.sub(r"\1", text)
    text = _RE_BOLD_UNDER.sub(r"\1", text)
    text = _RE_ITALIC_UNDER.sub(r"\1", text)
    text = _RE_CODE_BLOCK.sub("", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_LINK.sub(r"\1", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def markdown_to_whatsapp(content: str) -> str:
    """Convert standard markdown to WhatsApp's own markup.

    WhatsApp understands ``*bold*``, ``_italic_``, ``~strikethrough~``,
    triple-backtick blocks and monospaced ``\\`inline\\```. Markdown spells
    bold, underline-bold and strikethrough differently, so those collapse
    here; headings become bold lines and links become ``text (url)``.

    Fenced code blocks and inline code are protected from every rewrite via
    placeholder substitution, so a ``**kwargs`` inside a fence survives.

    Shared by the Baileys adapter (``whatsapp.py``) and the Twilio
    Conversations adapter (``whatsapp_twilio.py``): both talk to the same
    WhatsApp client, and one copy is how they cannot render differently.
    """
    if not content:
        return content

    # --- 1. Protect fenced code blocks from formatting changes ---
    _FENCE_PH = "\x00FENCE"
    fences: list[str] = []

    def _save_fence(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"{_FENCE_PH}{len(fences) - 1}\x00"

    result = re.sub(r"```[\s\S]*?```", _save_fence, content)

    # --- 2. Protect inline code ---
    _CODE_PH = "\x00CODE"
    codes: list[str] = []

    def _save_code(m: re.Match) -> str:
        codes.append(m.group(0))
        return f"{_CODE_PH}{len(codes) - 1}\x00"

    result = re.sub(r"`[^`\n]+`", _save_code, result)

    # --- 3. Convert markdown formatting to WhatsApp syntax ---
    # Bold: **text** or __text__ -> *text*
    result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
    result = re.sub(r"__(.+?)__", r"*\1*", result)
    # Strikethrough: ~~text~~ -> ~text~
    result = re.sub(r"~~(.+?)~~", r"~\1~", result)
    # Italic: *text* and _text_ are already WhatsApp italic, left as-is.

    # --- 4. Convert markdown headers to bold text ---
    result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

    # --- 5. Convert markdown links: [text](url) -> text (url) ---
    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

    # --- 6. Restore protected sections ---
    for i, fence in enumerate(fences):
        result = result.replace(f"{_FENCE_PH}{i}\x00", fence)
    for i, code in enumerate(codes):
        result = result.replace(f"{_CODE_PH}{i}\x00", code)

    return result


# ─── Thread Participation Tracking ───────────────────────────────────────────


class ThreadParticipationTracker:
    """Persistent tracking of threads the bot has participated in.

    Namespaced by ``(platform, agent_name)`` so two agents that each run
    the same platform adapter don't share the participation set — sharing
    it would let one agent's "I've already replied here" record suppress
    the other agent's first-touch behaviour in the same chat.
    """

    _MAX_TRACKED = 500

    def __init__(
        self,
        platform_name: str,
        max_tracked: int = 500,
        agent_name: str = "",
    ):
        self._platform = platform_name
        self._agent_name = agent_name
        self._max_tracked = max_tracked
        self._threads: set = self._load()

    def _state_path(self) -> Path:
        from olano.config.home import get_olano_home

        suffix = f"_{self._agent_name}" if self._agent_name else ""
        return get_olano_home() / f"{self._platform}{suffix}_threads.json"

    def _load(self) -> set:
        path = self._state_path()
        if path.exists():
            try:
                return set(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return set()

    def _save(self) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        thread_list = list(self._threads)
        if len(thread_list) > self._max_tracked:
            thread_list = thread_list[-self._max_tracked :]
            self._threads = set(thread_list)
        path.write_text(json.dumps(thread_list), encoding="utf-8")

    def mark(self, thread_id: str) -> None:
        if thread_id not in self._threads:
            self._threads.add(thread_id)
            self._save()

    def __contains__(self, thread_id: str) -> bool:
        return thread_id in self._threads

    def clear(self) -> None:
        self._threads.clear()


# ─── Phone Number Redaction ──────────────────────────────────────────────────


# ─── Atomic JSON Write ───────────────────────────────────────────────────────


def _preserve_file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    except OSError:
        return None


def _restore_file_mode(path: Path, mode: int | None) -> None:
    if mode is None:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_json_write(
    path: Union[str, Path],
    data: Any,
    *,
    indent: int = 2,
    **dump_kwargs: Any,
) -> None:
    """Write JSON data atomically via temp file + fsync + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    original_mode = _preserve_file_mode(path)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _restore_file_mode(path, original_mode)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def redact_phone(phone: str) -> str:
    """Redact a phone number for logging, preserving country code and last 4."""
    if not phone:
        return "<none>"
    if len(phone) <= 8:
        return phone[:2] + "****" + phone[-2:] if len(phone) > 4 else "****"
    return phone[:4] + "****" + phone[-4:]
