"""Ollama message-content compatibility shim.

``langchain_ollama`` converts every message it sends with
``_convert_messages_to_ollama_messages``. For *list-shaped* content that
converter understands exactly four block kinds — ``text``, ``tool_use``
(skipped), ``image_url``, and a langchain-core v1 data block — and raises
``ValueError("Unsupported message content type. Must either have type 'text'
or type 'image_url' with a string 'image_url' field.")`` on anything else.

That is a much narrower vocabulary than the one a real olano thread holds, and
the mismatch is not hypothetical:

* **Thread state deliberately survives a model switch.** The checkpointer is
  the source of truth for a thread's context (see ``core/checkpoints.py``), so
  switching an agent onto ``ollama:*`` replays whatever the *previous* provider
  wrote — Anthropic ``thinking`` / ``redacted_thinking`` blocks, ``tool_result``
  blocks, OpenAI ``reasoning`` blocks. Every one of those raises, on every
  turn, forever: the bad block is in the checkpoint, so the thread is bricked
  rather than merely failing once.
* **Olano emits Anthropic-shaped image blocks itself.**
  ``runtime/api.py:_compose_message_with_attachments`` builds
  ``{"type": "image", "source": {"type": "base64", ...}}`` because "LangChain's
  chat models translate these into the right per-provider format" — true for
  anthropic/openai/gemini, false for ollama, whose ``is_data_content_block``
  probe wants ``base64``/``mime_type`` at the top level.

The failure reaches the user as an opaque ``API error (ValueError)`` several
layers up (``[talk_to_agent error] Calling 'lucia' failed: …`` when a peer is
the one on ollama), naming nothing that a user could act on.

So sanitize before the converter sees the list. The single choke point is
``_convert_messages_to_ollama_messages`` — ``_chat_params`` is the only caller
and every entry point (``_generate``/``_agenerate``/``_stream``/``_astream``)
goes through it — which is why this is one override rather than four.

Reasoning blocks are **dropped, not coerced to text**: ollama does not accept
reasoning back on the wire (langchain_ollama's own v1 converter drops them for
the same reason), and re-injecting a model's private scratchpad as if the user
had said it changes what the next turn sees. Everything else degrades to text
so the turn keeps its meaning; only genuinely contentless blocks disappear.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Block types langchain_ollama's converter already handles. `tool_use` is on
# the list because the converter explicitly skips it (tool calls travel in
# `AIMessage.tool_calls`, not in content).
_PASSTHROUGH_TYPES = frozenset({"text", "tool_use", "image_url"})

# Provider-private chain-of-thought. Ollama takes no reasoning back on the
# wire, so these carry no meaning downstream — drop them.
_REASONING_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning", "reasoning_content"})


def _is_data_block(block: dict[str, Any]) -> bool:
    """Whether langchain-core would recognize *block* as a v1 data block."""
    try:
        from langchain_core.messages import is_data_content_block

        return bool(is_data_content_block(block))
    except Exception:  # pragma: no cover - langchain-core always ships this
        return False


def _anthropic_image_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """Translate an Anthropic-native image block into a shape ollama accepts.

    ``{"type": "image", "source": {"type": "base64", "media_type": m, "data": d}}``
    becomes a v1 data block (``base64``/``mime_type`` at the top level), and a
    URL source becomes an ``image_url`` block. Returns ``None`` when *block*
    isn't that shape.
    """
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "base64":
        data = source.get("data")
        if isinstance(data, str) and data:
            return {
                "type": "image",
                "base64": data,
                "mime_type": source.get("media_type") or "image/png",
            }
        return None
    if source_type == "url":
        url = source.get("url")
        if isinstance(url, str) and url:
            return {"type": "image_url", "image_url": url}
    return None


def _block_text(block: dict[str, Any]) -> str:
    """Best-effort readable text for a block ollama cannot represent.

    Prefers the block's own text-ish fields; falls back to a compact JSON dump
    so a tool result or a provider-specific block still reaches the model as
    *something* rather than being silently deleted.
    """
    for key in ("text", "content", "output", "value"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = [
                inner.get("text", "")
                for inner in value
                if isinstance(inner, dict) and isinstance(inner.get("text"), str)
            ]
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    payload = {k: v for k, v in block.items() if k != "type"}
    if not payload:
        return ""
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


def _sanitize_block(block: Any) -> dict[str, Any] | str | None:
    """Map one content block onto something ollama's converter accepts.

    Returns the block (possibly rewritten), or ``None`` to drop it.
    """
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        # The converter would raise on `.get`; render it rather than crash.
        text = str(block).strip()
        return {"type": "text", "text": text} if text else None

    block_type = block.get("type")
    if block_type in _PASSTHROUGH_TYPES:
        if block_type != "image_url":
            return block
        # An `image_url` whose payload is neither a string nor `{"url": str}`
        # raises its own ValueError inside the converter.
        image_url = block.get("image_url")
        if isinstance(image_url, str):
            return block
        if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
            return block
        return None

    if _is_data_block(block):
        return block

    if block_type == "image":
        translated = _anthropic_image_block(block)
        if translated is not None:
            return translated
        return None

    if block_type in _REASONING_TYPES:
        return None

    text = _block_text(block)
    return {"type": "text", "text": text} if text.strip() else None


def sanitize_content(content: Any) -> tuple[Any, int]:
    """Return ``(content, dropped)`` with every block ollama-representable.

    A string passes through untouched (the common case — no allocation, no
    copy). A list is rebuilt only when at least one block actually changed.
    """
    if not isinstance(content, list):
        return content, 0

    out: list[Any] = []
    changed = False
    dropped = 0
    for block in content:
        sanitized = _sanitize_block(block)
        if sanitized is None:
            changed = True
            dropped += 1
            continue
        if sanitized is not block:
            changed = True
        out.append(sanitized)
    return (out if changed else content), dropped


def sanitize_messages(messages: list[Any]) -> list[Any]:
    """Copy-on-write pass over *messages*, fixing list-shaped content.

    Messages langchain_ollama already unpacks itself (an ``AIMessage`` tagged
    ``output_version == "v1"``) are left alone — its ``_convert_from_v1_to_ollama``
    is the authority on that shape, and rewriting it here would only fight it.
    """
    result: list[Any] = []
    rewritten = 0
    dropped_total = 0
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            result.append(message)
            continue
        metadata = getattr(message, "response_metadata", None) or {}
        if isinstance(metadata, dict) and metadata.get("output_version") == "v1":
            result.append(message)
            continue
        sanitized, dropped = sanitize_content(content)
        if sanitized is content:
            result.append(message)
            continue
        rewritten += 1
        dropped_total += dropped
        try:
            result.append(message.model_copy(update={"content": sanitized}))
        except Exception:  # pragma: no cover - non-pydantic message stand-ins
            result.append(message)
    if rewritten:
        logger.debug(
            "ollama: rewrote content on %d message(s) (%d block(s) dropped) "
            "so the request stays convertible",
            rewritten,
            dropped_total,
        )
    return result


_CHAT_CLASS: Any = None


def chat_ollama_class() -> Any:
    """``ChatOllama`` subclass that sanitizes content before converting it.

    Built once per process — `models/reasoning.py:_probe()` instantiates a chat
    class per catalog model, and a fresh class object per model would be pure
    waste. Raises ``ImportError`` with an install hint when ``langchain-ollama``
    is missing, matching the bare-``ChatOllama`` path it replaces.
    """
    global _CHAT_CLASS
    if _CHAT_CLASS is not None:
        return _CHAT_CLASS
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise ImportError(
            "Ollama support requires langchain-ollama. Install with: uv add langchain-ollama"
        ) from exc

    class _ChatOllamaCompat(ChatOllama):  # type: ignore[misc]
        """``ChatOllama`` that tolerates other providers' content blocks."""

        def _convert_messages_to_ollama_messages(self, messages: list[Any]) -> Any:
            return super()._convert_messages_to_ollama_messages(sanitize_messages(messages))

    _CHAT_CLASS = _ChatOllamaCompat
    return _CHAT_CLASS
