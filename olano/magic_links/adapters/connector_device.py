"""Sign in with a code — the connector broker's device-code method, from a chat.

Wraps :mod:`olano.core.connectors.device` for every provider that declares a
``DeviceAuth`` recipe (GitHub today): request the one-time code, show it with
a "Continue to <provider>" button, poll until approval. The page adds only a
way to see the code from a phone; the broker owns the flow and writes the
result as an ordinary connector token record at the scope frozen into the
link at mint time (``agent`` → that agent's own record, ``shared`` → marked
shared; no agent → deployment-wide) — exactly like ``connector-oauth``.

``applies`` is ``APPLY_NONE`` for the right reason: broker tokens are read
live by ``get_credential`` on every tool call, so nothing needs a restart.

``github-device`` survives as a thin alias (``service`` pinned to ``github``)
so existing pickers, ``/olano connect github`` transcripts and older
dashboards keep working; it is not a separate flow any more.
"""

from __future__ import annotations

from typing import Any

from olano.magic_links.adapters.connector_oauth import _provider_or_raise
from olano.magic_links.flows import (
    APPLY_NONE,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_EXPIRED,
    STATUS_IN_PROGRESS,
    BaseFlow,
    register,
)
from olano.magic_links.store import LinkRecord

# device session statuses → ours.
_STATUS_MAP = {
    "starting": STATUS_IN_PROGRESS,
    "pending": STATUS_IN_PROGRESS,
    "approved": STATUS_DONE,
    "error": STATUS_ERROR,
    "expired": STATUS_EXPIRED,
}


def _device_provider_or_raise(service: str) -> Any:
    provider = _provider_or_raise(service)
    if provider.device is None:
        raise ValueError(
            f"{provider.name} does not offer a sign-in code. Use the 'connector-oauth' "
            "flow (its OAuth app) or 'secret-entry' (a pasted key) instead."
        )
    return provider


class ConnectorDeviceFlow(BaseFlow):
    id = "connector-device"
    label = "Sign in with a code"
    kind = "device_code"
    # Broker tokens are read live on every tool call — nothing to restart.
    applies = APPLY_NONE
    instructions = (
        "Copy the code below, then continue to the provider and paste it there. "
        "The account you approve with is the one that gets connected."
    )

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        service = str(params.get("service") or "").strip().lower()
        if not service:
            raise ValueError("A service is required (e.g. 'github').")
        _device_provider_or_raise(service)
        out: dict[str, Any] = {"service": service, "shared": bool(params.get("shared"))}
        agent = str(params.get("agent") or "").strip()
        if agent:
            out["agent"] = agent
        return out

    def _provider(self, link: LinkRecord) -> Any:
        return _device_provider_or_raise(str(link.params.get("service") or ""))

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        provider = self._provider(link)
        base = super().descriptor(link)
        agent = link.params.get("agent")
        base["service"] = {"id": provider.id, "name": provider.name}
        base["title"] = f"Sign in to {provider.name}"
        if agent:
            base["instructions"] = (
                f"Copy the code below, then continue to {provider.name} and paste it "
                f"there. The account you approve with is stored for the agent '{agent}'."
            )
        return base

    def start(self, link: LinkRecord) -> dict[str, Any]:
        from olano.core.connectors import start_device_auth

        provider = self._provider(link)
        # Single-flight lives in the broker: a live code for the same
        # (service, agent) is handed back rather than a second one minted, so
        # a double tap is harmless.
        session = start_device_auth(
            provider,
            agent=str(link.params.get("agent") or "") or None,
            shared=bool(link.params.get("shared")),
        )
        return {"session_id": str(session.get("session_id") or "")}

    def state(self, link: LinkRecord) -> dict[str, Any]:
        from olano.core.connectors import poll_device_auth

        if not link.session_id:
            return {"status": link.status}
        session = poll_device_auth(link.session_id)
        if session is None:
            return {
                "status": STATUS_EXPIRED,
                "error": "This sign-in attempt is no longer active.",
            }
        status = _STATUS_MAP.get(str(session.get("status") or ""), STATUS_IN_PROGRESS)
        out: dict[str, Any] = {
            "status": status,
            "user_code": session.get("user_code") or "",
            "verification_url": session.get("verification_url") or "",
        }
        if status == STATUS_DONE:
            provider = self._provider(link)
            account = session.get("account") or ""
            scope = f" for {link.params['agent']}" if link.params.get("agent") else ""
            who = f" as {account}" if account else ""
            out["result"] = (
                f"{provider.name} connected{who}{scope} — its tools use it on the next call."
            )
        elif status in (STATUS_ERROR, STATUS_EXPIRED):
            out["error"] = str(session.get("error_message") or "")
        return out

    def cancel(self, link: LinkRecord) -> None:
        from olano.core.connectors import cancel_device_auth

        if link.session_id:
            cancel_device_auth(link.session_id)


class GitHubDeviceFlow(ConnectorDeviceFlow):
    """Back-compat alias: the pre-method flow id, pinned to GitHub."""

    id = "github-device"
    label = "GitHub"

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().validate({**params, "service": "github"})


register(ConnectorDeviceFlow())
register(GitHubDeviceFlow())
