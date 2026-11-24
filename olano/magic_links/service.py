"""Mint, unlock, drive and finalize magic links.

Every surface goes through here — the ``magic_link_connect_service`` / ``magic_link_request_secret``
agent tools, the ``/olano connect`` chat command, the dashboard Connections
tab, the CLI, and the public page's own routes — so the gates (feature switch,
TTL, PIN policy, group refusal, reachable origin) are enforced in exactly one
place and cannot drift between entry points.

State transitions are centralized in :func:`_sync`, which is the *only* writer
of a link's status: it polls the adapter, persists any change, and finalizes
exactly once (an in-process guard plus the persisted terminal status make it
idempotent, so the page's poll loop and the background watcher can both observe
completion without double-notifying or double-reloading).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any

from olano.config.system_settings import MagicLinkSettings, load_magic_link_settings
from olano.magic_links import flows as flow_registry
from olano.magic_links import store
from olano.magic_links.store import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_EXPIRED,
    STATUS_IN_PROGRESS,
    LinkRecord,
)

logger = logging.getLogger(__name__)

_NO_ORIGIN_REMEDIATION = (
    "This deployment has no browser-reachable public address, so a magic link "
    "cannot be built. The OPERATOR must set LIONCLAW_PUBLIC_URL to the "
    "deployment's public HTTPS origin (in .env or /opt/olano/olano.env) and "
    "restart. Cloud-provisioned instances get this automatically."
)


class MagicLinkError(RuntimeError):
    """An actionable refusal — safe to relay to the user verbatim."""


class InlinePinRefused(MagicLinkError):
    """An inline form's PIN did not match (or the link burned): a 403, not a 400."""


@dataclass(frozen=True)
class MintedLink:
    """What a caller shows the user. The token appears here and nowhere else."""

    id: str
    url: str
    pin: str  # "" when the PIN policy is off
    flow: str
    label: str
    expires_at: str
    ttl_minutes: int

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "pin": self.pin,
            "flow": self.flow,
            "label": self.label,
            "expires_at": self.expires_at,
            "ttl_minutes": self.ttl_minutes,
        }

    def chat_message(self) -> str:
        """A ready-to-send chat message. Callers may use their own wording."""
        lines = [f"🔐 {self.label} — open this link to finish:", self.url]
        if self.pin:
            lines.append(f"PIN: {self.pin}")
        lines.append(
            f"The link works for {self.ttl_minutes} minutes. "
            "I'll confirm here as soon as it's done."
        )
        return "\n".join(lines)


# ── availability ─────────────────────────────────────────────────────────────


def public_base_url() -> str | None:
    """The origin magic links are built on, or ``None`` when unreachable."""
    from olano.core.mcp_setup import public_origin

    return public_origin()


def magic_links_available(settings: MagicLinkSettings | None = None) -> tuple[bool, str]:
    """``(ok, reason)`` — whether links can be minted at all right now."""
    settings = settings or load_magic_link_settings()
    if not settings.enabled:
        return False, (
            "Magic links are disabled on this deployment (config.yaml → magic_links.enabled)."
        )
    if not public_base_url():
        return False, _NO_ORIGIN_REMEDIATION
    return True, ""


# ── minting ──────────────────────────────────────────────────────────────────


#: Bounds for a per-mint TTL override (setup sessions run longer than sign-ins).
_TTL_MIN_MINUTES = 5
_TTL_MAX_MINUTES = 240


def mint_link(
    flow_id: str,
    *,
    params: dict[str, Any] | None = None,
    requested_by: dict[str, Any] | None = None,
    notify: dict[str, Any] | None = None,
    minted_by: str = "",
    settings: MagicLinkSettings | None = None,
    ttl_minutes: int | None = None,
) -> MintedLink:
    """Create a link for ``flow_id``. Raises :class:`MagicLinkError` on refusal.

    ``ttl_minutes`` overrides the deployment default for this one link (clamped
    to 5–240). A flow may also declare ``default_ttl_minutes`` — used when the
    caller passes nothing — so long-running flows like ``channel-connect`` get
    a workable window without every mint surface knowing about them.
    """
    settings = settings or load_magic_link_settings()
    ok, reason = magic_links_available(settings)
    if not ok:
        raise MagicLinkError(reason)

    flow = flow_registry.get_flow(flow_id)
    if flow is None:
        known = ", ".join(f.id for f in flow_registry.all_flows())
        raise MagicLinkError(f"Unknown magic-link flow '{flow_id}'. Available: {known}.")

    requested_by = dict(requested_by or {})
    if requested_by.get("is_group") and not settings.allow_groups:
        raise MagicLinkError(
            "I won't send a magic link in a group chat — the link and its PIN "
            "would be visible to everyone here. Message me directly instead."
        )

    try:
        validated = flow.validate(dict(params or {}))
    except ValueError as exc:
        raise MagicLinkError(str(exc)) from exc

    effective_ttl = (
        ttl_minutes or int(getattr(flow, "default_ttl_minutes", 0) or 0) or settings.ttl_minutes
    )
    effective_ttl = max(_TTL_MIN_MINUTES, min(int(effective_ttl), _TTL_MAX_MINUTES))

    pin = store.new_pin() if settings.pin == "required" else None
    record, raw_token = store.create(
        flow=flow_id,
        params=validated,
        requested_by=requested_by,
        notify=dict(notify or {}),
        ttl_minutes=effective_ttl,
        pin=pin,
        minted_by=minted_by,
    )
    base = public_base_url()
    logger.info(
        "magic-link: minted %s link %s for %s (expires %s)",
        flow_id,
        record.id,
        minted_by or "unknown",
        record.expires_at,
    )
    return MintedLink(
        id=record.id,
        url=f"{base}/api/connect/{raw_token}",
        pin=pin or "",
        flow=flow_id,
        label=getattr(flow, "label", flow_id),
        expires_at=record.expires_at,
        ttl_minutes=effective_ttl,
    )


# ── page session ─────────────────────────────────────────────────────────────


def resolve_token(raw_token: str) -> LinkRecord | None:
    """The link a raw token opens (may be expired/terminal), else ``None``."""
    return store.find_by_token(raw_token)


def unlock_link(record: LinkRecord, pin: str) -> tuple[bool, str, str]:
    """Verify a PIN. Returns ``(ok, raw_unlock_token, error_message)``.

    Delegates to :func:`store.attempt_unlock`, which does the check-and-count in
    one locked read-modify-write so the attempt cap holds even under concurrent
    requests.
    """
    settings = load_magic_link_settings()
    outcome = store.attempt_unlock(record.id, pin, settings.max_pin_attempts)
    return outcome.ok, outcome.raw_unlock, outcome.error


# ── state + actions ──────────────────────────────────────────────────────────


def _status_of(raw: dict[str, Any], fallback: str) -> str:
    status = str(raw.get("status") or "").strip()
    return status or fallback


def _sync(record: LinkRecord, live: dict[str, Any]) -> LinkRecord:
    """Persist a status change reported by an adapter; finalize exactly once."""
    status = _status_of(live, record.status)
    result = str(live.get("result") or "")
    error = str(live.get("error") or "")

    if status in (STATUS_DONE, STATUS_ERROR, STATUS_EXPIRED):
        if not record.is_terminal:
            updated = store.finish(record.id, status=status, result=result, error=error)
            record = updated or record
        _schedule_finalize(record)
        return record

    if status != record.status:

        def _apply(current: LinkRecord) -> None:
            current.status = status
            if result:
                current.result = result
            store.note(current, "status", detail=status)

        record = store.mutate(record.id, _apply) or record
    return record


def _schedule_finalize(record: LinkRecord) -> None:
    """Schedule the one-time completion side effects (notice + post_complete).

    The real once-only guard is :func:`store.claim_finalize`, taken inside
    :func:`watch.finalize`, so every path (this scheduler, and the restart
    resume that calls finalize directly) is covered. The cheap ``record.finalized``
    check here just avoids spawning a redundant task on every poll of an
    already-finished link.
    """
    if record.finalized:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (CLI / sync test): the magic link still completed and its token
        # is stored — only the chat notice is skipped.
        logger.debug("magic-link: no running loop — skipping finalize for %s", record.id)
        return
    from olano.magic_links.watch import finalize

    task = loop.create_task(finalize(record.id))
    _FINALIZE_TASKS.add(task)
    task.add_done_callback(_FINALIZE_TASKS.discard)


# Strong refs so fire-and-forget finalize tasks are not garbage-collected.
_FINALIZE_TASKS: set[asyncio.Task[None]] = set()


def link_state(record: LinkRecord) -> dict[str, Any]:
    """The full page payload: descriptor + live status, with state persisted."""
    flow = flow_registry.get_flow(record.flow)
    if flow is None:  # pragma: no cover - a removed adapter with a live link
        return {
            "kind": "error",
            "status": STATUS_ERROR,
            "error": f"This magic-link type ('{record.flow}') is no longer available.",
        }

    if record.is_expired and not record.is_terminal:
        store.finish(record.id, status=STATUS_EXPIRED, error="The link expired.")
        record = store.get(record.id) or record

    live: dict[str, Any] = {}
    if record.is_terminal:
        # Covers both a link that just expired above and one finished by another
        # observer; finalizing is guarded, so asking again is free.
        _schedule_finalize(record)
    else:
        try:
            live = flow.state(record) or {}
        except Exception as exc:  # an adapter hiccup must not blank the page
            logger.warning("magic-link: state() failed for %s: %s", record.id, exc)
            live = {}
        record = _sync(record, live)

    descriptor = dict(flow.descriptor(record))
    descriptor.update({k: v for k, v in live.items() if k != "status"})
    descriptor.update(
        {
            "id": record.id,
            "status": record.status,
            "result": record.result or descriptor.get("result", ""),
            "error": record.error or descriptor.get("error", ""),
            "requested_by": dict(record.requested_by),
            "expires_at": record.expires_at,
            "started": bool(record.session_id),
        }
    )
    return descriptor


async def _maybe_await(value: Any) -> Any:
    """Adapters may implement ``start``/``submit`` as sync or async.

    Flows that only touch in-process session tables stay sync; ones that have to
    make a network call to begin (the Codex device flow requests its code over
    HTTP) are async. Bridging here — rather than in each adapter — keeps them
    from having to reach for the event loop themselves, which is exactly how a
    ``run_coroutine_threadsafe`` deadlock gets written.
    """
    return await value if inspect.isawaitable(value) else value


async def act_on_link(
    record: LinkRecord, op: str, fields: dict[str, str] | None = None
) -> dict[str, Any]:
    """Run ``start`` | ``submit`` | ``cancel`` and return the fresh page payload."""
    flow = flow_registry.get_flow(record.flow)
    if flow is None:
        raise MagicLinkError("This magic-link type is no longer available.")
    if not record.serves_actions():
        raise MagicLinkError("This link is no longer active.")

    if op == "start":
        if record.session_id:  # single-flight: a second tap re-reads state
            return link_state(record)
        try:
            updates = await _maybe_await(flow.start(record)) or {}
        except ValueError as exc:  # a refusal the page can show, not a 500
            raise MagicLinkError(str(exc)) from exc
        session_id = updates.get("session_id")

        def _apply(current: LinkRecord) -> None:
            if session_id:
                current.session_id = str(session_id)
            extra = updates.get("params")
            if isinstance(extra, dict):
                current.params.update(extra)
            current.status = STATUS_IN_PROGRESS
            store.note(current, "started")

        record = store.mutate(record.id, _apply) or record
        return link_state(record)

    if op == "submit":
        try:
            live = await _maybe_await(flow.submit(record, dict(fields or {}))) or {}
        except ValueError as exc:
            raise MagicLinkError(str(exc)) from exc
        record = _sync(record, live)
        payload = link_state(record)
        # A one-time secret the page must display (a freshly minted access
        # token) rides only on THIS response: it is never written to the record,
        # so the admin listing, the chat notice and later polls never carry it.
        reveal = live.get("reveal") if isinstance(live, dict) else None
        if reveal:
            payload = {**payload, "reveal": str(reveal)}
        return payload

    if op == "cancel":
        try:
            flow.cancel(record)
        except Exception:  # best-effort teardown
            logger.debug("magic-link: cancel() failed for %s", record.id, exc_info=True)
        store.finish(record.id, status=STATUS_ERROR, error="Cancelled.")
        return link_state(store.get(record.id) or record)

    raise MagicLinkError(f"Unknown action '{op}'.")


# ── inline forms (dashboard chat + mobile app) ───────────────────────────────

#: Flows the dashboard chat and the mobile app render **inline**, as a form
#: card under the agent's reply, instead of a link to open in a browser.
#:
#: The link and its PIN exist for a channel that is unauthenticated and has no
#: UI of its own (a messenger). A person in the dashboard or the app is already
#: signed in as an admin, on the deployment's own origin, so the detour through
#: a new tab and a PIN buys nothing; what must survive is the other property,
#: that the value never travels as a chat message. The inline card keeps it:
#: it posts to :func:`submit_inline_form`, the same flow record and the same
#: ``submit`` as the page, and the value lands in the vault with the model
#: learning only the name from the completion notice. Only short, single-field
#: secret forms qualify; an OAuth redirect has to leave the page anyway, and
#: the editor flows carry file contents in their descriptor.
INLINE_FORM_FLOWS = frozenset({"secret-entry", "provider-key"})

_INLINE_FORM_KEYS = (
    "id",
    "flow",
    "kind",
    "status",
    "title",
    "instructions",
    "fields",
    "result",
    "error",
    "expires_at",
)


def inline_form_state(record: LinkRecord) -> dict[str, Any]:
    """What an inline card needs from a link, and nothing it does not.

    Raises :class:`MagicLinkError` for a flow that is not rendered inline. The
    payload is :func:`link_state` trimmed to the form itself: never
    ``requested_by`` (chat ids), never a prefilled secret value
    (``FlowField.public`` already blanks those), plus ``pin_required`` so the
    client knows whether the PIN from the tool result has to ride along.
    """
    if record.flow not in INLINE_FORM_FLOWS:
        raise MagicLinkError("This magic link is not an inline form.")
    state = link_state(record)
    out = {key: state.get(key) for key in _INLINE_FORM_KEYS}
    out["agent"] = str(record.params.get("agent") or "")
    out["pin_required"] = record.requires_pin
    return out


async def submit_inline_form(
    record: LinkRecord, pin: str, fields: dict[str, str] | None
) -> dict[str, Any]:
    """Submit an inline form: the PIN check and the page's ``submit`` in one call.

    The caller is already an authenticated admin (the route lives under the
    admin-only ``/api/magic-links`` prefix); the PIN still has to match when
    the deployment's policy issued one, so a link id read off the admin listing
    cannot be driven by itself, and the attempt cap burns the link exactly as
    it does on the public page (:func:`store.attempt_unlock`).
    """
    if record.flow not in INLINE_FORM_FLOWS:
        raise MagicLinkError("This magic link is not an inline form.")
    if not record.serves_actions():
        raise MagicLinkError("This link is no longer active.")
    if record.requires_pin:
        outcome = store.attempt_unlock(record.id, pin, load_magic_link_settings().max_pin_attempts)
        if not outcome.ok:
            raise InlinePinRefused(outcome.error or "Incorrect PIN.")
        record = store.get(record.id) or record
    await act_on_link(record, "submit", fields)
    fresh = store.get(record.id) or record
    return inline_form_state(fresh)


# ── admin surface ────────────────────────────────────────────────────────────


def list_links(*, include_terminal: bool = True) -> list[dict[str, Any]]:
    return [record.public() for record in store.list_all(include_terminal=include_terminal)]


def revoke_link(link_id: str) -> bool:
    """Kill a link immediately. Returns False when it doesn't exist."""
    record = store.get(link_id)
    if record is None:
        return False
    flow = flow_registry.get_flow(record.flow)
    if flow is not None and not record.is_terminal:
        try:
            flow.cancel(record)
        except Exception:
            logger.debug("magic-link: cancel() failed during revoke", exc_info=True)
    store.finish(link_id, status=store.STATUS_REVOKED, error="Revoked by the owner.")
    return True
