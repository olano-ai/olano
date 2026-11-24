"""The ``MagicLinkFlow`` protocol and its registry.

A flow adapter is a thin wrapper over auth machinery that **already exists** —
the connector broker (OAuth or a sign-in code), MCP OAuth, the vault. It owns
no state machine of its own: it starts the underlying session, translates that
session's status into the small vocabulary the page understands, and (for form
flows) applies a submission.

Adapters register at import time (``registry.register(...)``), exactly like
connector providers in :mod:`olano.core.connectors.registry`. Importing
:mod:`olano.magic_links.adapters` loads them all.

The page is driven entirely by the **descriptor** an adapter returns, so adding
a service never touches the HTML:

``kind``
    ``device_code`` — show ``user_code`` + a button to ``verification_url``.
    ``redirect``    — show a button to ``authorization_url``.
    ``form``        — render ``fields`` and POST them back.
    ``qr``          — render ``qr_data`` as a QR image plus its text form.
    ``mcp``         — ``mode: credentials`` renders an OAuth-app form (client
                      id/secret, the redirect URL to register, a guide, the
                      Composio alternative); ``mode: signin`` is ``redirect``.
``status``
    ``pending`` | ``in_progress`` | ``done`` | ``error`` | ``expired``. These are
    the store's statuses; adapters map their own vocabularies onto them.

Everything else (``service``, ``title``, ``instructions``, ``user_code``,
``verification_url``, ``authorization_url``, ``fields``, ``qr_data``,
``result``, ``error``) is optional presentation the page renders when present.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from olano.magic_links.store import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_EXPIRED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    LinkRecord,
)

# ``start``/``submit`` may be implemented sync or async — the service awaits an
# awaitable result (see ``service._maybe_await``), so an adapter that must make a
# network call to begin never has to reach for the event loop itself.
MaybeAsync = dict[str, Any] | Awaitable[dict[str, Any]]

# ── what makes a finished link's effect live ─────────────────────────────────
# A completed link has written something — a channel entry, a vault secret, an
# OAuth token — and the person who finished it wants to know whether the agent
# is actually using it yet. Two vocabularies for that, resolved by
# :func:`resolve_apply`.
#
# What a flow DECLARES up front (``BaseFlow.applies``) — three values:
APPLY_SELF = "self"  # post_complete applies it (restarts the agent, reloads MCP)
APPLY_RESTART = "restart"  # the agent named in params["agent"] must restart
APPLY_NONE = "none"  # nothing to apply (a dashboard sign-in, a file edit)
#
# What ``post_complete`` REPORTS once it has run, which outranks the
# declaration because it is the only observer that knows how it went. The
# failure value is the one that matters: an auto-restart is best-effort (no
# bound runtime, a boot error), and staying quiet about it is exactly how a
# link reports "connected" over an agent still serving its old config.
APPLY_RESTARTED = "restarted"
APPLY_RELOADED = "reloaded"
APPLY_RESTART_NEEDED = "restart_needed"

# Re-exported so adapters import their status vocabulary from one place.
__all__ = [
    "APPLY_NONE",
    "APPLY_RELOADED",
    "APPLY_RESTART",
    "APPLY_RESTARTED",
    "APPLY_RESTART_NEEDED",
    "APPLY_SELF",
    "STATUS_DONE",
    "STATUS_ERROR",
    "STATUS_EXPIRED",
    "STATUS_IN_PROGRESS",
    "STATUS_PENDING",
    "FlowField",
    "MagicLinkFlow",
    "all_flows",
    "get_flow",
    "register",
    "resolve_apply",
]


@dataclass(frozen=True)
class FlowField:
    """One input on a ``form``-kind page."""

    name: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    help: str = ""
    # Optional fixed choices → rendered as a <select> instead of a text input.
    options: tuple[tuple[str, str], ...] = ()  # (value, label)
    # Rendered as a <textarea> (long-form editing, e.g. the edit-prompt flow).
    multiline: bool = False
    # Prefill shown in the input/textarea. NEVER use for secrets — descriptor
    # fields travel to the browser (post-PIN); prefill exists for editor flows
    # where the current value is the whole point.
    value: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
            "placeholder": self.placeholder,
            "help": self.help,
            "options": [{"value": v, "label": lbl} for v, lbl in self.options],
            "multiline": self.multiline,
            "value": "" if self.secret else self.value,
        }


@runtime_checkable
class MagicLinkFlow(Protocol):
    """What every adapter implements. Only ``id``/``descriptor``/``state`` are
    mandatory in spirit; the defaults below cover flows that don't need the rest.
    """

    id: str
    #: Human label used in chat messages ("GitHub", "OpenAI API key").
    label: str
    #: Page kind — device_code | redirect | form | qr.
    kind: str
    #: What makes this link's effect live — one of the ``APPLY_*`` values.
    applies: str

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Normalize/validate mint parameters. Raise ``ValueError`` to refuse."""

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        """Static presentation for the page (title, instructions, fields)."""

    def start(self, link: LinkRecord) -> MaybeAsync:
        """Begin the underlying flow. Idempotent. Returns state fields to persist
        (e.g. ``{"session_id": ...}``). May be ``async`` — the service awaits an
        awaitable result, so a flow that must make a network call to begin need
        not reach for the event loop itself."""

    def state(self, link: LinkRecord) -> dict[str, Any]:
        """Live status for the page: ``{"status": ..., ...display fields}``.

        Always synchronous: it runs on the page's poll path and should only read
        an in-process session table, never make a network call of its own."""

    def submit(self, link: LinkRecord, fields: dict[str, str]) -> MaybeAsync:
        """Apply a form submission. Returns the same shape as :meth:`state`.
        May be ``async`` (see :meth:`start`)."""

    def cancel(self, link: LinkRecord) -> None:
        """Best-effort teardown of the underlying session."""

    async def post_complete(self, link: LinkRecord) -> str | None:
        """Runs once after a link completes (e.g. hot-reload MCP tools).

        Returns the ``APPLY_*`` outcome it actually achieved, overriding
        :attr:`applies` — a flow that tries to restart the agent and cannot
        must say ``APPLY_RESTART_NEEDED`` rather than let the notice claim it
        did. ``None`` keeps the declaration.
        """


class BaseFlow:
    """Convenience base: sane no-op defaults for the optional protocol methods.

    Deliberately a plain class, not a dataclass — subclasses declare their
    identity as class attributes (``id = "connector-device"``), and a generated
    ``__init__`` would reset every one of them to the base defaults on
    instantiation.
    """

    #: Registry key, also the ``flow`` value stored on a link.
    id: str = ""
    #: Human label used on the page and in chat messages.
    label: str = ""
    #: Page kind — device_code | redirect | form | qr.
    kind: str = "redirect"
    #: What makes this link's effect live once it completes (an ``APPLY_*``).
    #: ``APPLY_RESTART`` is the default deliberately: a flow that writes config
    #: and forgets to declare anything should over-offer a restart, never
    #: under-offer one — an unnecessary restart costs a few seconds, a missing
    #: one leaves the user staring at a connection that never came up.
    applies: str = APPLY_RESTART
    #: Optional one-liner shown under the title on the page.
    instructions: str = ""
    #: Form inputs (``form`` kind only).
    fields: tuple[FlowField, ...] = ()

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        return dict(params)

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "flow": self.id,
            "service": {"id": self.id, "name": self.label},
            "title": self.label,
            "instructions": self.instructions,
            "fields": [f.public() for f in self.fields],
        }

    def start(self, link: LinkRecord) -> MaybeAsync:
        return {}

    def state(self, link: LinkRecord) -> dict[str, Any]:
        return {"status": link.status}

    def submit(self, link: LinkRecord, fields: dict[str, str]) -> MaybeAsync:
        raise ValueError("This magic link does not accept a form submission.")

    def cancel(self, link: LinkRecord) -> None:
        return None

    async def post_complete(self, link: LinkRecord) -> str | None:
        return None


def resolve_apply(flow: Any, link: LinkRecord, reported: str | None = None) -> str:
    """What still stands between this finished link and the agent using it.

    ``reported`` is whatever ``post_complete`` returned and always wins — it is
    the only observer that knows whether the restart it attempted worked.
    Otherwise the flow's declaration applies.

    Either way, an answer that asks for a restart needs an agent to restart:
    a link minted deployment-wide (``params["agent"]`` empty) degrades to
    ``APPLY_NONE`` rather than promising a button that cannot name its target.
    """
    answer = str(reported or getattr(flow, "applies", APPLY_RESTART) or APPLY_RESTART)
    if (
        answer in (APPLY_RESTART, APPLY_RESTART_NEEDED)
        and not str(link.params.get("agent") or "").strip()
    ):
        return APPLY_NONE
    return answer


_FLOWS: dict[str, Any] = {}


def register(flow: Any) -> Any:
    """Register an adapter (idempotent by id). Returns it, so it can decorate."""
    if not getattr(flow, "id", ""):
        raise ValueError("magic-link flow must declare a non-empty id")
    _FLOWS[flow.id] = flow
    return flow


def get_flow(flow_id: str) -> Any | None:
    _load_adapters()
    return _FLOWS.get(flow_id)


def all_flows() -> list[Any]:
    _load_adapters()
    return [_FLOWS[key] for key in sorted(_FLOWS)]


_LOADED = False


def _load_adapters() -> None:
    """Import the adapter package once, so registration happens on first use."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        import olano.magic_links.adapters  # noqa: F401
    except Exception:  # pragma: no cover - an adapter import must not kill the API
        import logging

        logging.getLogger(__name__).exception("magic-link: adapter registration failed")
