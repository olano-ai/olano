"""OpenAI Codex sign-in (ChatGPT OAuth), over its device-code flow.

Codex is the one model provider that is *not* an API key: it authenticates with
a ChatGPT account, so it can't ride :mod:`provider_key`. Like GitHub's, this is
a device flow — no redirect URI, nothing to register — so the only thing the
page adds is getting the code onto the user's phone.

Tokens land in ``<home>/auth.json`` under ``providers.openai-codex`` (never in
``OPENAI_API_KEY``), which is what makes ``openai-codex:<model>`` usable in an
agent's config afterwards.
"""

from __future__ import annotations

from typing import Any

from olano.magic_links.flows import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_EXPIRED,
    STATUS_IN_PROGRESS,
    BaseFlow,
    register,
)
from olano.magic_links.store import LinkRecord

_STATUS_MAP = {
    "pending": STATUS_IN_PROGRESS,
    "approved": STATUS_DONE,
    "error": STATUS_ERROR,
    "expired": STATUS_EXPIRED,
}


class CodexDeviceFlow(BaseFlow):
    id = "codex-device"
    label = "OpenAI Codex"
    kind = "device_code"
    instructions = (
        "Copy the code, open OpenAI, and approve the sign-in with your ChatGPT "
        "account. This connects Codex models — not an API key."
    )

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"service": "openai-codex"}

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        base["service"] = {"id": "openai-codex", "name": "OpenAI"}
        base["title"] = "Connect OpenAI Codex"
        return base

    async def start(self, link: LinkRecord) -> dict[str, Any]:
        """Async because the starter requests the code over HTTP and spawns its
        own poller task — the service awaits an awaitable ``start``."""
        from olano.models.codex_auth import start_codex_device_flow

        session = await start_codex_device_flow()
        return {"session_id": str(session.get("session_id") or "")}

    def state(self, link: LinkRecord) -> dict[str, Any]:
        from olano.models.codex_auth import poll_codex_device_flow

        if not link.session_id:
            return {"status": link.status}
        session = poll_codex_device_flow(link.session_id)
        if session is None:
            return {"status": STATUS_EXPIRED, "error": "This sign-in attempt expired."}
        status = _STATUS_MAP.get(str(session.get("status") or ""), STATUS_IN_PROGRESS)
        out: dict[str, Any] = {
            "status": status,
            "user_code": session.get("user_code") or "",
            "verification_url": session.get("verification_url") or "",
        }
        if status == STATUS_DONE:
            out["result"] = "OpenAI Codex connected — its models are available to agents now."
        elif status in (STATUS_ERROR, STATUS_EXPIRED):
            out["error"] = str(session.get("error_message") or "")
        return out


register(CodexDeviceFlow())
