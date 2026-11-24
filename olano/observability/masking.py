"""What an exported span may carry: the one place content is withheld or masked.

Applied by :class:`olano.observability.exporter.MaskingSpanExporter` to every
span right before it leaves the process, so nothing the SDK or LangChain adds
on the way can route around it. Pure functions over plain dicts: no SDK and no
OpenTelemetry import, which is what lets the tests drive them directly.

Per span, the rules come from the :class:`MaskContext` registered for its
trace id when the run started. A span whose trace id has no context (a run
that ended long ago, or something unexpected) gets :data:`STRICT`, the
``metadata`` rules with no extra secrets: an unknown span never exports more
than a known one.

- ``metadata`` capture: message, tool and error text become lengths
  (``{"withheld_chars": 123}``); only allowlisted metadata keys survive; the
  span status description is dropped.
- ``full`` capture: content is kept, every string passes the secret mask.
- Both: every vault and env secret value the run could see is replaced by
  ``[secret]``, credential shapes too, and a managed ``olano:*`` tier's model
  name is reported as the tier, never the model behind it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SECRET_PLACEHOLDER = "[secret]"

# Values shorter than this are never treated as secrets to replace: a
# two-character "secret" would shred every message that contains it.
MIN_SECRET_LEN = 8

# Credential shapes, masked in every mode. Broader than the revisions
# detector (``revisions.engine._SECRET_CONTENT_RE``) because a false positive
# here costs one masked token in a trace, not a held-back file.
_SECRET_SHAPES = re.compile(
    r"(?:"
    r"\bsk-(?:ant-|proj-|live-|lf-)?[A-Za-z0-9_-]{16,}"
    r"|\bpk-lf-[A-Za-z0-9-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}"
    r"|\b[Bb]earer\s+[A-Za-z0-9._~+/-]{16,}=*"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s:@/]+:[^\s@/]+@"
    r")"
)

# Attributes that carry what was said or done: withheld in ``metadata`` mode.
_CONTENT_KEYS = frozenset(
    {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.observation.status_message",
        "langfuse.trace.input",
        "langfuse.trace.output",
    }
)
_CONTENT_PREFIXES = ("gen_ai.prompt", "gen_ai.completion", "gen_ai.input", "gen_ai.output")
_METADATA_PREFIXES = ("langfuse.observation.metadata.", "langfuse.trace.metadata.")
_MODEL_NAME_KEYS = frozenset(
    {"langfuse.observation.model.name", "gen_ai.request.model", "gen_ai.response.model"}
)

# Metadata keys that describe structure, not content: kept in ``metadata``
# mode. Everything else LangChain or the graph puts in run metadata (the
# workspace path, shared paths, peer targets) is dropped there.
_SAFE_METADATA_KEYS = frozenset(
    {
        "ls_provider",
        "ls_model_type",
        "ls_integration",
        "ls_max_tokens",
        "ls_temperature",
        "langgraph_node",
        "langgraph_step",
        "langgraph_triggers",
        "langgraph_path",
        "is_langchain_root",
        "lc_versions",
        "checkpoint_ns",
    }
)
_SAFE_METADATA_PREFIX = "olano_"


@dataclass(frozen=True)
class MaskContext:
    """How the spans of one trace are masked."""

    capture: str = "metadata"
    #: Exact strings replaced with :data:`SECRET_PLACEHOLDER`, longest first.
    secrets: tuple[str, ...] = ()
    #: The managed tier (``olano:pro``) to report instead of any model name.
    tier: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def full(self) -> bool:
        return self.capture == "full"


STRICT = MaskContext()

#: What an input or output becomes before the SDK serializes it, when the run
#: is in ``metadata`` capture (:func:`olano.observability.tracing` installs the
#: hook). The exporter keeps it as is.
WITHHELD_JSON = '{"withheld": true}'
WITHHELD = {"withheld": True}

MEDIA_PLACEHOLDER = "[media withheld]"
_MEDIA_NODE_CAP = 20_000


def _is_data_uri(value: str) -> bool:
    if not value.startswith("data:"):
        return False
    head = value[:256].split(",", 1)[0]
    return head.endswith(";base64")


def strip_media(data: Any) -> Any:
    """``data`` with embedded media replaced by :data:`MEDIA_PLACEHOLDER`.

    Attachments never leave the box, in any capture mode: base64 data URIs
    (OpenAI ``image_url`` blocks), Anthropic ``{"type": "base64", "data": …}``
    sources, and raw bytes. Walks at most :data:`_MEDIA_NODE_CAP` nodes; past
    that the rest is returned as is (the exporter's mask still applies).
    """
    budget = [_MEDIA_NODE_CAP]

    def walk(node: Any) -> Any:
        budget[0] -= 1
        if budget[0] < 0:
            return node
        if isinstance(node, str):
            return MEDIA_PLACEHOLDER if _is_data_uri(node) else node
        if isinstance(node, bytes | bytearray | memoryview):
            return MEDIA_PLACEHOLDER
        if isinstance(node, dict):
            if node.get("type") == "base64" and "data" in node:
                return {**{k: v for k, v in node.items() if k != "data"}, "data": MEDIA_PLACEHOLDER}
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list | tuple):
            return [walk(v) for v in node]
        return node

    return walk(data)


def secret_values(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize candidate secret values: unique, long enough, longest first."""
    seen = {str(v).strip() for v in values if v}
    return tuple(sorted((v for v in seen if len(v) >= MIN_SECRET_LEN), key=len, reverse=True))


def mask_text(text: str, ctx: MaskContext) -> str:
    """Replace every known secret value and credential shape in ``text``."""
    if not text:
        return text
    for value in ctx.secrets:
        if value in text:
            text = text.replace(value, SECRET_PLACEHOLDER)
    return _SECRET_SHAPES.sub(SECRET_PLACEHOLDER, text)


def _withheld(value: Any) -> str:
    if value == WITHHELD_JSON:
        return WITHHELD_JSON
    size = len(value) if isinstance(value, str) else len(str(value))
    return json.dumps({"withheld_chars": size})


def _mask_value(value: Any, ctx: MaskContext) -> Any:
    if isinstance(value, str):
        return mask_text(value, ctx)
    if isinstance(value, list | tuple):
        return type(value)(mask_text(v, ctx) if isinstance(v, str) else v for v in value)
    return value


def _metadata_key(attr: str) -> str | None:
    for prefix in _METADATA_PREFIXES:
        if attr.startswith(prefix):
            return attr[len(prefix) :]
    return None


def mask_attributes(attributes: Mapping[str, Any], ctx: MaskContext) -> dict[str, Any]:
    """The attributes of one span as they may leave the process."""
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if key in _MODEL_NAME_KEYS and ctx.tier:
            out[key] = ctx.tier
            continue
        meta_key = _metadata_key(key)
        if not ctx.full:
            if key in _CONTENT_KEYS or key.startswith(_CONTENT_PREFIXES):
                out[key] = _withheld(value)
                continue
            if meta_key is not None:
                top = meta_key.split(".", 1)[0]
                if top not in _SAFE_METADATA_KEYS and not top.startswith(_SAFE_METADATA_PREFIX):
                    continue
        out[key] = _mask_value(value, ctx)
    return out


def mask_status_description(description: str | None, ctx: MaskContext) -> str | None:
    """A span's status description (a failing tool's error text)."""
    if not description:
        return description
    if not ctx.full:
        return None
    return mask_text(description, ctx)


def mask_event_attributes(attributes: Mapping[str, Any], ctx: MaskContext) -> dict[str, Any]:
    """An event's attributes (exception events carry message and stack)."""
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if not ctx.full and key in ("exception.message", "exception.stacktrace"):
            out[key] = _withheld(value)
            continue
        out[key] = _mask_value(value, ctx)
    return out
