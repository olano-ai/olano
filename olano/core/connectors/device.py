"""Device-code sign-in (RFC 8628) for any provider that declares ``DeviceAuth``.

The connector broker's second method. A provider's ``device`` recipe names a
*public* client id — no secret, nothing to register — so the person types a
short one-time code on the provider's site from any device: a phone in a
chat, a laptop next to a self-hosted box. We ask the provider for the code,
show it (the dashboard panel or a magic-link page renders it), poll the token
endpoint until approval, and save the result as an **ordinary broker token
record** at the requested scope (own / shared / system), ``auth_mode:
"device"``. From then on every consumer that resolves the provider's
``exposes`` names — toolkits through ``get_credential``, MCP ``env``/``headers``
refs, the git job — sees it exactly like a redirect-flow token.

This replaced ``github_cli.py``, which ran the same GitHub device flow and
then handed the token to the *runtime container's* ``gh`` (``gh auth login
--with-token``). That signed in a binary nothing invokes, on the one machine
the agent's shell never runs on (the sandbox is a sibling container), and the
toolkit still read ``[missing-credentials]`` because nothing wrote
``GITHUB_TOKEN``. The HTTP half survives here; the ``gh`` half is gone.

Session dicts mirror the OpenAI Codex device flow (``models/codex_auth.py``)
and the dashboard's ``DeviceSessionInfo``: statuses ``starting | pending |
approved | error | expired`` with ``user_code`` + ``verification_url``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from typing import Any

from olano.core.connectors.base import (
    ConnectorProvider,
    DeviceAuth,
    _save_record,
    _token_path_for,
    set_connector_shared,
)

logger = logging.getLogger(__name__)

# Fallbacks only — providers return both in the device-code response.
_DEFAULT_POLL_INTERVAL_S = 5.0
_DEFAULT_EXPIRES_IN_S = 15 * 60.0
# Retained a while past expiry so a panel can still read the final status.
_SESSION_MAX_AGE_S = 30 * 60.0

_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_GUARD = threading.Lock()
# Serializes the whole of start_device_auth: the single-flight check has to
# cover the (network) device-code request too, or a double-clicked Connect
# mints two codes and the one on screen is not the one being polled.
_START_GUARD = threading.Lock()

_USER_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,19}$")

# RFC 8628 §3.5 error codes → what the panel should say.
_TERMINAL_POLL_ERRORS: dict[str, tuple[str, str]] = {
    "access_denied": ("error", "Sign-in was denied on the provider's site."),
    "expired_token": ("expired", "The device code expired before approval."),
    "incorrect_device_code": ("error", "The provider rejected the device code. Start again."),
    "unsupported_grant_type": ("error", "The provider rejected the device-flow grant type."),
    "incorrect_client_credentials": (
        "error",
        "The provider rejected the OAuth client id. Unset the client-id override "
        "environment variable to use the built-in public client.",
    ),
}


# ── recipe resolution ───────────────────────────────────────────────────────


def device_client_id(auth: DeviceAuth) -> str:
    if auth.client_id_env:
        override = os.environ.get(auth.client_id_env, "").strip()
        if override:
            return override
    return auth.client_id


def device_scopes(auth: DeviceAuth) -> tuple[str, ...]:
    if auth.scopes_env:
        override = os.environ.get(auth.scopes_env, "").strip()
        if override:
            return tuple(s for s in override.replace(",", " ").split() if s)
    return auth.scopes


def _device_or_raise(provider: ConnectorProvider) -> DeviceAuth:
    if provider.device is None:
        raise ValueError(
            f"{provider.name} does not offer a sign-in code. Connect it with its "
            "OAuth app or a pasted key instead."
        )
    return provider.device


# ── HTTP ────────────────────────────────────────────────────────────────────


def _post_form(url: str, data: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST a form and return the JSON body.

    Providers answer the device endpoints with HTTP 200 and an ``error`` field
    for flow-level problems (``authorization_pending``, ``slow_down``, …), so
    only transport/HTTP failures raise here.
    """
    import httpx

    response = httpx.post(
        url,
        data=data,
        headers={"Accept": "application/json"},
        timeout=timeout,
        follow_redirects=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"{url} returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"{url} returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} returned an unexpected response.")
    return payload


def _request_device_code(provider: ConnectorProvider) -> dict[str, Any]:
    """Ask the provider for a one-time code. Raises ``RuntimeError`` with a fixable message."""
    auth = _device_or_raise(provider)
    try:
        payload = _post_form(
            auth.device_code_url,
            {"client_id": device_client_id(auth), "scope": " ".join(device_scopes(auth))},
            timeout=20.0,
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Could not reach {provider.name} to request a sign-in code: {exc}. "
            "Check the server's outbound network access and retry."
        ) from exc

    error = payload.get("error")
    if error == "device_flow_disabled":
        raise RuntimeError(
            f"{provider.name} reports the device flow is disabled for this OAuth app. "
            "Use the built-in public client, or enable device flow on the app you configured."
        )
    if error:
        detail = payload.get("error_description") or error
        raise RuntimeError(f"{provider.name} refused the sign-in request: {detail}")

    user_code = str(payload.get("user_code") or "").strip()
    device_code = str(payload.get("device_code") or "").strip()
    if not user_code or not device_code:
        raise RuntimeError(
            f"{provider.name}'s device-code response was missing user_code or device_code."
        )
    if not _USER_CODE_RE.match(user_code):
        raise RuntimeError(f"{provider.name} returned a one-time code in an unexpected format.")

    verification_url = (
        str(payload.get("verification_uri") or payload.get("verification_url") or "").strip()
        or auth.verification_url
    )
    return {
        "user_code": user_code,
        "device_code": device_code,
        "verification_url": verification_url,
        "interval": max(1.0, float(payload.get("interval") or _DEFAULT_POLL_INTERVAL_S)),
        "expires_in": max(60.0, float(payload.get("expires_in") or _DEFAULT_EXPIRES_IN_S)),
    }


def _lookup_account(provider: ConnectorProvider, token: str) -> str | None:
    """Best-effort "who am I" so the card can show the signed-in login."""
    auth = provider.device
    if auth is None or not auth.account_url:
        return None
    try:
        import httpx

        resp = httpx.get(
            auth.account_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        value = resp.json().get(auth.account_field)
        return str(value) if value else None
    except Exception:
        return None


# ── sessions ────────────────────────────────────────────────────────────────


def _prune_sessions() -> None:
    now = time.time()
    with _SESSIONS_GUARD:
        stale = [
            sid
            for sid, s in _SESSIONS.items()
            if now - s.get("created_at", now) > _SESSION_MAX_AGE_S
        ]
        for sid in stale:
            _SESSIONS.pop(sid, None)


def public_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "service": session["service"],
        "status": session["status"],
        "user_code": session.get("user_code"),
        "verification_url": session.get("verification_url"),
        # Cadence for the *dashboard's* poll of this API, not for the
        # provider's token endpoint (that one is `poll_interval`, server-side).
        "interval": 3,
        "expires_at": session.get("expires_at"),
        "error_message": session.get("error"),
        "account": session.get("account"),
        "agent": session.get("agent"),
        "shared": bool(session.get("shared")),
        "logs": list(session.get("logs", [])),
    }


def _log(session: dict[str, Any], level: str, message: str) -> None:
    session.setdefault("logs", []).append({"ts": time.time(), "level": level, "message": message})
    logger.log(
        logging.ERROR if level == "error" else logging.INFO,
        "connector-device[%s/%s]: %s",
        session["service"],
        session["session_id"],
        message,
    )


def _finish(session: dict[str, Any], provider: ConnectorProvider, payload: dict[str, Any]) -> None:
    """Approved token payload → saved broker record at the session's scope."""
    auth = _device_or_raise(provider)
    token = str(payload.get("access_token") or "").strip()
    now = time.time()
    expires_in = payload.get("expires_in")
    record: dict[str, Any] = {
        "service": provider.id,
        "access_token": token,
        "token_type": str(payload.get("token_type") or "Bearer"),
        "expires_at": (now + float(expires_in)) if expires_in is not None else None,
        "scopes": list(device_scopes(auth)),
        # A public client: the id is not secret and there is no secret. A
        # refresh, where the provider grants one, posts the id alone.
        "client_id": device_client_id(auth),
        "client_secret": "",
        "params": {},
        "extras": {},
        "obtained_at": now,
        "auth_mode": "device",
        "client_source": "public",
    }
    if payload.get("refresh_token"):
        record["refresh_token"] = payload["refresh_token"]
    account = _lookup_account(provider, token)
    if account:
        record["extras"]["account"] = account
        session["account"] = account
    agent = session.get("agent") or None
    _save_record(record, _token_path_for(provider.id, agent))
    if agent and session.get("shared"):
        set_connector_shared(provider.id, agent, True)
    session.update(error=None, status="approved")
    scope = f" for agent '{agent}'" if agent else " (deployment-wide)"
    _log(session, "info", f"{provider.name} connected{scope}.")


def _poll_for_approval(session: dict[str, Any], provider: ConnectorProvider) -> None:
    """Poll until the user approves, then save the record.

    Nothing may escape without a terminal status: a session left ``pending``
    would hold the single-flight slot for the whole device-code window, and
    the panel would spin against a dead poller.
    """
    try:
        _poll_for_approval_inner(session, provider)
    except Exception as exc:  # the poller thread's last line of defence
        session.update(error=f"{provider.name} sign-in failed: {exc}", status="error")
        logger.exception("connector-device poller crashed")
        _log(session, "error", session["error"])


def _poll_for_approval_inner(session: dict[str, Any], provider: ConnectorProvider) -> None:
    auth = _device_or_raise(provider)
    device_code = session["device_code"]
    interval = float(session.get("poll_interval") or _DEFAULT_POLL_INTERVAL_S)
    deadline = float(session["expires_at"])

    while True:
        # Sleep in short slices so Cancel is honoured within ~0.5s rather than
        # after a full provider poll interval.
        slept = 0.0
        while slept < interval:
            if session.get("cancel"):
                return  # cancel_device_auth already wrote the status
            if time.time() >= deadline:
                break
            time.sleep(0.5)
            slept += 0.5
        if session.get("cancel"):
            return
        if time.time() >= deadline:
            session.update(error="The device code expired before approval.", status="expired")
            _log(session, "error", session["error"])
            return

        try:
            payload = _post_form(
                auth.token_url,
                {
                    "client_id": device_client_id(auth),
                    "device_code": device_code,
                    "grant_type": auth.grant_type,
                },
                timeout=20.0,
            )
        except Exception as exc:
            # A blip while the user is still on the provider's site shouldn't
            # kill a live code — back off and retry until the code expires.
            _log(session, "info", f"Polling {provider.name} failed ({exc}); retrying.")
            interval = min(interval * 2, 30.0)
            continue

        error = payload.get("error")
        if not error:
            if not str(payload.get("access_token") or "").strip():
                session.update(
                    error=f"{provider.name} approved the sign-in but returned no access token.",
                    status="error",
                )
                _log(session, "error", session["error"])
                return
            _finish(session, provider, payload)
            return

        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = float(payload.get("interval") or interval + 5)
            continue

        status, message = _TERMINAL_POLL_ERRORS.get(
            str(error),
            ("error", str(payload.get("error_description") or error)),
        )
        session.update(error=message, status=status)
        _log(session, "error", message)
        return


# ── public API ──────────────────────────────────────────────────────────────


def start_device_auth(
    provider: ConnectorProvider, *, agent: str | None = None, shared: bool = False
) -> dict[str, Any]:
    """Request a one-time code and start polling; returns the public session.

    The code is fetched **before** returning, so a panel never sees a session
    without one. Single-flight per ``(service, agent)``: while a code is live
    the same session is returned instead of minting a second one — the person
    would otherwise read one code off the panel while we poll another. A fresh
    attempt is possible as soon as the previous one reaches a terminal state.
    """
    _device_or_raise(provider)
    agent = (agent or "").strip() or None
    with _START_GUARD:
        _prune_sessions()
        now = time.time()
        with _SESSIONS_GUARD:
            for existing in _SESSIONS.values():
                if (
                    existing["service"] == provider.id
                    and (existing.get("agent") or None) == agent
                    and existing["status"] == "pending"
                    and existing.get("user_code")
                    and existing["expires_at"] > now
                ):
                    return public_session(existing)

        device = _request_device_code(provider)
        now = time.time()
        session: dict[str, Any] = {
            "session_id": uuid.uuid4().hex,
            "service": provider.id,
            "agent": agent,
            "shared": bool(shared) if agent else False,
            "status": "pending",
            "user_code": device["user_code"],
            "device_code": device["device_code"],
            "verification_url": device["verification_url"],
            "poll_interval": device["interval"],
            "error": None,
            "account": None,
            "created_at": now,
            "expires_at": now + device["expires_in"],
            "logs": [],
        }
        # The code itself stays out of the logger — whoever enters it first on
        # the provider's site binds the login to *their* account.
        _log(session, "info", "One-time code ready — enter it on the provider's device page.")
        with _SESSIONS_GUARD:
            _SESSIONS[session["session_id"]] = session
        thread = threading.Thread(
            target=_poll_for_approval,
            args=(session, provider),
            name=f"device-flow-{provider.id}",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            # Never leave an unmanned session holding the single-flight slot.
            with _SESSIONS_GUARD:
                _SESSIONS.pop(session["session_id"], None)
            raise
        return public_session(session)


def poll_device_auth(session_id: str) -> dict[str, Any] | None:
    session = _SESSIONS.get(session_id)
    return public_session(session) if session else None


def cancel_device_auth(session_id: str) -> bool:
    """Ask a running sign-in to stop; the poller notices within ~0.5s.

    Without this, dismissing a panel would leave the server polling the
    provider with a live device code for the rest of the window — and
    single-flight would hand that stale code to the next Connect.
    """
    session = _SESSIONS.get(session_id)
    if session is None or session["status"] not in ("starting", "pending"):
        return False
    session["cancel"] = True
    # Mark it terminal here rather than waiting for the poller's next tick, so
    # a Connect issued right after a Cancel can't be handed the dead session.
    session.update(error="Sign-in cancelled.", status="error")
    _log(session, "info", "Cancelled — dropping the device code.")
    return True


def drop_sessions_for(service: str, agent: str | None = None) -> None:
    """Forget in-flight sessions for a service (called on disconnect)."""
    with _SESSIONS_GUARD:
        live = [
            s
            for s in _SESSIONS.values()
            if s["service"] == service
            and (agent is None or (s.get("agent") or None) == agent)
            and s["status"] in ("starting", "pending")
        ]
    for stale in live:
        stale["cancel"] = True
        stale.update(error="Disconnected.", status="error")
