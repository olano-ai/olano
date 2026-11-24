"""Deterministic offline chat model — the ``fake:`` provider.

Exists for two jobs no real provider can do:

* **Profiling the agent loop itself.** A real model call buries olano's
  per-turn overhead (prompt assembly, middleware, checkpointer writes, stream
  translation) under seconds of network + inference variance. ``fake:instant``
  answers with zero latency, so a benchmarked turn measures *only* the
  framework; ``fake:realistic`` re-adds a configurable TTFT + per-token delay
  so streaming consumers can be exercised with realistic pacing but perfect
  reproducibility. ``scripts/bench_agent_turn.py`` is the intended harness.
* **Offline end-to-end tests.** The full ``build_agent → AgentRunner`` path can
  run in CI with no API key, including tool-call turns (see the script format
  below).

Model names select a latency profile; env vars override individual knobs:

    fake:instant     no artificial latency (the default for unknown names)
    fake:fast        150ms to first token, 4ms per token
    fake:realistic   1200ms to first token, 12ms per token

    LIONCLAW_FAKE_TTFT_MS          override time-to-first-token
    LIONCLAW_FAKE_TOKEN_DELAY_MS   override inter-token delay
    LIONCLAW_FAKE_TOKENS           override response length in tokens
    LIONCLAW_FAKE_MODEL_SCRIPT     scripted responses (JSON, or @/path/to.json)

The script is a JSON array consumed one entry per model call, letting a test
drive a full tool loop offline:

    [{"tool": "ls", "args": {"path": "."}}, {"text": "done"}]

Entries: ``{"text": str}`` responds verbatim; ``{"tool": str, "args": {}}``
emits a tool call; ``{"tokens": N}`` generates an N-token filler response.
When the script is exhausted (or absent) every call gets a deterministic
filler response of ``LIONCLAW_FAKE_TOKENS`` tokens.

A JSON **object** scripts several agents at once, keyed by a phrase that
appears in the conversation handed to the model (a system prompt sentence is
the natural key): the first key found in the messages selects its list, and
``"default"`` serves the rest. Each built model keeps one counter per key, so
a Cortex run that builds a reviewer worker, a promotion worker and an
execution worker in one test can give each its own tool loop:

    {"portfolio reviewer": [{"text": "[...ratings...]"}],
     "scaffold a small, runnable prototype": [{"tool": "write_file", "args": {...}}, {"text": "done"}],
     "default": [{"text": "ok"}]}
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable

_PROFILES: dict[str, tuple[float, float]] = {
    # name → (ttft_ms, token_delay_ms)
    "instant": (0.0, 0.0),
    "fast": (150.0, 4.0),
    "realistic": (1200.0, 12.0),
}

_DEFAULT_TOKENS = 60

_FILLER = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliett "
    "kilo lima mike november oscar papa quebec romeo sierra tango"
).split()


def _env_float(name: str) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _load_script() -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
    raw = (os.environ.get("LIONCLAW_FAKE_MODEL_SCRIPT") or "").strip()
    if not raw:
        return []
    try:
        if raw.startswith("@"):
            with open(raw[1:], encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.loads(raw)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        return {
            str(key): [e for e in steps if isinstance(e, dict)]
            for key, steps in data.items()
            if isinstance(steps, list)
        }
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _message_text(messages: list[BaseMessage]) -> str:
    parts: list[str] = []
    for m in messages:
        content = getattr(m, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "\n".join(parts)


class FakeChatModel(BaseChatModel):
    """A ``BaseChatModel`` that streams deterministic responses with fixed pacing.

    Supports ``bind_tools`` (tools are accepted and echoed into scripted tool
    calls) and emits ``usage_metadata`` on the final chunk so downstream cost /
    usage accounting paths run exactly as they do against a real provider.
    """

    model_name: str = "fake:instant"
    ttft_ms: float = 0.0
    token_delay_ms: float = 0.0
    n_tokens: int = _DEFAULT_TOKENS
    script: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] = []

    _call_counter: Any = None  # itertools.count, shared across clones via __init__
    _counter_lock: Any = None
    _keyed_counters: Any = None  # key → itertools.count, for an object script

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # One counter per built model (subagent clones made via .bind() share the
        # underlying instance, so the script advances across the whole turn).
        object.__setattr__(self, "_call_counter", itertools.count())
        object.__setattr__(self, "_counter_lock", threading.Lock())
        object.__setattr__(self, "_keyed_counters", {})

    @property
    def _llm_type(self) -> str:
        return "olano-fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    # ── token accounting (offline: never touch a network tokenizer) ──────────

    def get_num_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def get_num_tokens_from_messages(
        self, messages: list[BaseMessage], tools: Sequence[Any] | None = None
    ) -> int:
        return sum(self.get_num_tokens(str(m.content)) for m in messages)

    # ── tool binding ─────────────────────────────────────────────────────────

    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any
    ) -> Runnable[LanguageModelInput, AIMessage]:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        formatted = []
        for t in tools:
            try:
                formatted.append(convert_to_openai_tool(t))
            except Exception:
                continue
        return self.bind(tools=formatted)

    # ── response planning ────────────────────────────────────────────────────

    def _next_step(self, messages: list[BaseMessage]) -> dict[str, Any]:
        with self._counter_lock:
            idx = next(self._call_counter)
            if isinstance(self.script, dict):
                text = _message_text(messages)
                key = next((k for k in self.script if k != "default" and k in text), "default")
                steps = self.script.get(key) or []
                counter = self._keyed_counters.setdefault(key, itertools.count())
                pos = next(counter)
                if pos < len(steps):
                    return {**steps[pos], "_idx": idx}
                return {"tokens": self.n_tokens, "_idx": idx}
        if isinstance(self.script, list) and idx < len(self.script):
            return {**self.script[idx], "_idx": idx}
        return {"tokens": self.n_tokens, "_idx": idx}

    def _plan_tokens(self, step: dict[str, Any]) -> list[str]:
        if "text" in step:
            words = str(step["text"]).split(" ")
            return [w + " " for w in words[:-1]] + words[-1:]
        n = max(1, int(step.get("tokens", self.n_tokens)))
        toks = [f"Fake response #{step['_idx']}: "]
        toks += [_FILLER[i % len(_FILLER)] + " " for i in range(n - 1)]
        return toks

    def _usage(self, messages: list[BaseMessage], out_tokens: int) -> UsageMetadata:
        in_tokens = self.get_num_tokens_from_messages(messages)
        return UsageMetadata(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            total_tokens=in_tokens + out_tokens,
        )

    def _tool_call_message(self, step: dict[str, Any], messages: list[BaseMessage]) -> AIMessage:
        call_id = f"fake_call_{step['_idx']}"
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": str(step["tool"]),
                    "args": dict(step.get("args") or {}),
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
            usage_metadata=self._usage(messages, 8),
            response_metadata={"model_name": self.model_name},
        )

    # ── generation ───────────────────────────────────────────────────────────

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        step = self._next_step(messages)
        if self.ttft_ms:
            time.sleep(self.ttft_ms / 1000.0)
        if "tool" in step:
            msg = self._tool_call_message(step, messages)
        else:
            tokens = self._plan_tokens(step)
            if self.token_delay_ms:
                time.sleep(self.token_delay_ms * len(tokens) / 1000.0)
            msg = AIMessage(
                content="".join(tokens),
                usage_metadata=self._usage(messages, len(tokens)),
                response_metadata={"model_name": self.model_name},
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for chunk, delay in self._chunk_plan(messages):
            if delay:
                time.sleep(delay)
            if run_manager and chunk.message.content:
                run_manager.on_llm_new_token(str(chunk.message.content), chunk=chunk)
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk, delay in self._chunk_plan(messages):
            if delay:
                await asyncio.sleep(delay)
            if run_manager and chunk.message.content:
                await run_manager.on_llm_new_token(str(chunk.message.content), chunk=chunk)
            yield chunk

    def _chunk_plan(self, messages: list[BaseMessage]) -> list[tuple[ChatGenerationChunk, float]]:
        """The full stream for one call: (chunk, pre-yield delay seconds) pairs."""
        step = self._next_step(messages)
        ttft = self.ttft_ms / 1000.0
        tick = self.token_delay_ms / 1000.0
        out: list[tuple[ChatGenerationChunk, float]] = []
        if "tool" in step:
            call_id = f"fake_call_{step['_idx']}"
            chunk = AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": str(step["tool"]),
                        "args": json.dumps(dict(step.get("args") or {})),
                        "id": call_id,
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
                usage_metadata=self._usage(messages, 8),
                response_metadata={"model_name": self.model_name},
            )
            out.append((ChatGenerationChunk(message=chunk), ttft))
            return out
        tokens = self._plan_tokens(step)
        for i, tok in enumerate(tokens):
            out.append(
                (ChatGenerationChunk(message=AIMessageChunk(content=tok)), ttft if i == 0 else tick)
            )
        final = AIMessageChunk(
            content="",
            usage_metadata=self._usage(messages, len(tokens)),
            response_metadata={"model_name": self.model_name},
        )
        out.append((ChatGenerationChunk(message=final), 0.0))
        return out


def build_fake_model(model_str: str, **kwargs: Any) -> BaseChatModel:
    """Factory for ``build_model("fake:<profile>")``. Unknown kwargs are ignored —
    the fake model accepts any agent config (temperature, reasoning_effort, …)
    without complaint so it can stand in for any real model string."""
    name = model_str.partition(":")[2] or "instant"
    ttft_ms, token_delay_ms = _PROFILES.get(name, _PROFILES["instant"])
    env_ttft = _env_float("LIONCLAW_FAKE_TTFT_MS")
    env_tick = _env_float("LIONCLAW_FAKE_TOKEN_DELAY_MS")
    env_tokens = _env_float("LIONCLAW_FAKE_TOKENS")
    return FakeChatModel(
        model_name=model_str,
        ttft_ms=env_ttft if env_ttft is not None else ttft_ms,
        token_delay_ms=env_tick if env_tick is not None else token_delay_ms,
        n_tokens=int(env_tokens) if env_tokens is not None else _DEFAULT_TOKENS,
        script=_load_script(),
    )
