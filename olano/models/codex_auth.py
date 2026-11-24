"""OpenAI Codex OAuth device-code authentication.

This mirrors the Codex CLI/ChatGPT device-auth flow used by Hermes:

1. request a user code from auth.openai.com,
2. send the user to the Codex device page,
3. poll for an authorization code + PKCE verifier,
4. exchange that pair for access/refresh tokens.

Tokens are stored in ``<LIONCLAW_HOME>/auth.json`` under the
``providers.openai-codex`` key. They are not API keys and must not be written
to ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from olano.config.home import ensure_olano_home, get_olano_paths

AUTH_STORE_VERSION = 1
OPENAI_CODEX_PROVIDER = "openai-codex"
CODEX_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_DEVICE_URL = f"{CODEX_ISSUER}/codex/device"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
DEFAULT_CODEX_MODELS = [
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
]
_FORWARD_COMPAT_MODELS = [
    ("gpt-5.5", ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex")),
    ("gpt-5.4-mini", ("gpt-5.3-codex",)),
    ("gpt-5.4", ("gpt-5.3-codex",)),
    ("gpt-5.3-codex-spark", ("gpt-5.3-codex",)),
]
_CODEX_MODELS_CACHE: tuple[float, str, list[str]] | None = None
_CODEX_MODELS_CACHE_TTL_SECONDS = 300


class CodexAuthError(RuntimeError):
    """Raised when Codex OAuth credentials cannot be resolved."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "codex_auth_error",
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required


def auth_store_path() -> Path:
    """Return Olano's per-home auth store path."""
    paths = ensure_olano_home()
    return paths.root / "auth.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_auth_store() -> dict[str, Any]:
    path = auth_store_path()
    if not path.exists():
        return {"version": AUTH_STORE_VERSION, "providers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CodexAuthError(
            f"Invalid auth store JSON at {path}: {exc}",
            code="auth_store_invalid_json",
        ) from exc
    if not isinstance(data, dict):
        raise CodexAuthError(f"Invalid auth store at {path}", code="auth_store_invalid")
    data.setdefault("version", AUTH_STORE_VERSION)
    data.setdefault("providers", {})
    return data


def _save_auth_store(data: dict[str, Any]) -> None:
    path = auth_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(payload, encoding="utf-8")
    try:
        tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp_path.replace(path)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _decode_jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def codex_access_token_is_expiring(
    access_token: Any,
    skew_seconds: int = CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> bool:
    """Return True when a JWT access token is expired or near expiry."""
    claims = _decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, int | float):
        return False
    return float(exp) <= time.time() + max(0, int(skew_seconds))


def codex_default_headers(access_token: str) -> dict[str, str]:
    """Headers expected by the ChatGPT Codex backend."""
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Olano)",
        "originator": "codex_cli_rs",
    }
    claims = _decode_jwt_claims(access_token)
    account_id = (
        claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(claims.get("https://api.openai.com/auth"), dict)
        else None
    )
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def _with_forward_compat_models(model_ids: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id not in seen:
            ordered.append(model_id)
            seen.add(model_id)
    for synthetic, templates in _FORWARD_COMPAT_MODELS:
        if synthetic not in seen and any(template in seen for template in templates):
            ordered.append(synthetic)
            seen.add(synthetic)
    return ordered


def fetch_codex_models(access_token: str, *, timeout_seconds: float = 5.0) -> list[str]:
    """Fetch visible Codex backend model IDs for the authenticated account."""
    global _CODEX_MODELS_CACHE
    now = time.time()
    cache_key = access_token[-24:] if len(access_token) > 24 else access_token
    if _CODEX_MODELS_CACHE:
        cached_at, cached_key, cached_models = _CODEX_MODELS_CACHE
        if cached_key == cache_key and now - cached_at < _CODEX_MODELS_CACHE_TTL_SECONDS:
            return list(cached_models)

    try:
        response = httpx.get(
            f"{DEFAULT_CODEX_BASE_URL}/models?client_version=1.0.0",
            headers={
                **codex_default_headers(access_token),
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=timeout_seconds,
        )
        if response.status_code != 200:
            return []
        data = response.json()
    except Exception:
        return []

    entries = data.get("models", []) if isinstance(data, dict) else []
    sortable: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, int | float) else 10_000
        sortable.append((rank, slug.strip()))

    sortable.sort(key=lambda row: (row[0], row[1]))
    models = _with_forward_compat_models([slug for _, slug in sortable])
    if models:
        _CODEX_MODELS_CACHE = (now, cache_key, models)
    return models


def get_codex_model_ids(access_token: str | None = None) -> list[str]:
    """Return Codex model IDs, preferring live account discovery when possible."""
    token = (access_token or "").strip()
    if not token:
        try:
            token = str(resolve_codex_runtime_credentials(refresh_if_expiring=False)["api_key"])
        except CodexAuthError:
            token = ""
    if token:
        live = fetch_codex_models(token)
        if live:
            return live
    return _with_forward_compat_models(list(DEFAULT_CODEX_MODELS))


def read_codex_tokens() -> dict[str, Any]:
    """Read stored Codex OAuth tokens from ``<LIONCLAW_HOME>/auth.json``."""
    store = _load_auth_store()
    providers = store.get("providers")
    state = providers.get(OPENAI_CODEX_PROVIDER) if isinstance(providers, dict) else None
    if not isinstance(state, dict):
        raise CodexAuthError(
            "No OpenAI Codex credentials stored. Run `olano auth openai-codex`.",
            code="codex_auth_missing",
            relogin_required=True,
        )
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexAuthError(
            "OpenAI Codex auth state is missing tokens. Run `olano auth openai-codex`.",
            code="codex_auth_invalid_shape",
            relogin_required=True,
        )
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise CodexAuthError(
            "OpenAI Codex auth is missing access_token. Run `olano auth openai-codex`.",
            code="codex_auth_missing_access_token",
            relogin_required=True,
        )
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise CodexAuthError(
            "OpenAI Codex auth is missing refresh_token. Run `olano auth openai-codex`.",
            code="codex_auth_missing_refresh_token",
            relogin_required=True,
        )
    return {
        "tokens": dict(tokens),
        "last_refresh": state.get("last_refresh"),
        "auth_mode": state.get("auth_mode") or "chatgpt",
    }


def save_codex_tokens(tokens: dict[str, str], last_refresh: str | None = None) -> None:
    """Persist Codex OAuth tokens to Olano's auth store."""
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not access_token:
        raise CodexAuthError("Cannot save Codex tokens without access_token.")
    if not refresh_token:
        raise CodexAuthError("Cannot save Codex tokens without refresh_token.")

    store = _load_auth_store()
    providers = store.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        store["providers"] = providers
    providers[OPENAI_CODEX_PROVIDER] = {
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
        },
        "last_refresh": last_refresh or _now_iso(),
        "auth_mode": "chatgpt",
    }
    _save_auth_store(store)


def refresh_codex_oauth(
    access_token: str,
    refresh_token: str,
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, str]:
    """Refresh Codex OAuth tokens without mutating local state."""
    del access_token
    if not refresh_token:
        raise CodexAuthError(
            "OpenAI Codex auth is missing refresh_token. Run `olano auth openai-codex`.",
            code="codex_auth_missing_refresh_token",
            relogin_required=True,
        )

    try:
        with httpx.Client(timeout=httpx.Timeout(max(5.0, timeout_seconds))) as client:
            response = client.post(
                CODEX_OAUTH_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                },
            )
    except Exception as exc:
        raise CodexAuthError(
            f"OpenAI Codex token refresh failed: {exc}",
            code="codex_refresh_failed",
        ) from exc

    if response.status_code != 200:
        code = "codex_refresh_failed"
        relogin_required = response.status_code in {400, 401, 403}
        message = f"OpenAI Codex token refresh failed with status {response.status_code}."
        try:
            payload = response.json()
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                err_code = err.get("code") or err.get("type")
                err_message = err.get("message")
                if isinstance(err_code, str) and err_code:
                    code = err_code
                if isinstance(err_message, str) and err_message:
                    message = f"OpenAI Codex token refresh failed: {err_message}"
            elif isinstance(err, str) and err:
                code = err
                desc = payload.get("error_description") or payload.get("message")
                if isinstance(desc, str) and desc:
                    message = f"OpenAI Codex token refresh failed: {desc}"
        except Exception:
            pass
        if code in {"invalid_grant", "invalid_token", "invalid_request", "refresh_token_reused"}:
            relogin_required = True
        raise CodexAuthError(message, code=code, relogin_required=relogin_required)

    try:
        payload = response.json()
    except Exception as exc:
        raise CodexAuthError(
            "OpenAI Codex token refresh returned invalid JSON.",
            code="codex_refresh_invalid_json",
            relogin_required=True,
        ) from exc

    refreshed_access = payload.get("access_token")
    if not isinstance(refreshed_access, str) or not refreshed_access.strip():
        raise CodexAuthError(
            "OpenAI Codex token refresh response was missing access_token.",
            code="codex_refresh_missing_access_token",
            relogin_required=True,
        )

    next_refresh = payload.get("refresh_token")
    return {
        "access_token": refreshed_access.strip(),
        "refresh_token": (
            next_refresh.strip()
            if isinstance(next_refresh, str) and next_refresh.strip()
            else refresh_token.strip()
        ),
    }


def resolve_codex_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Return runtime credentials for the Codex backend, refreshing if needed."""
    data = read_codex_tokens()
    tokens = dict(data["tokens"])
    access_token = str(tokens.get("access_token", "") or "").strip()
    should_refresh = force_refresh or (
        refresh_if_expiring and codex_access_token_is_expiring(access_token)
    )
    if should_refresh:
        tokens = refresh_codex_oauth(
            access_token,
            str(tokens.get("refresh_token", "") or "").strip(),
            timeout_seconds=timeout_seconds,
        )
        save_codex_tokens(tokens)
        access_token = tokens["access_token"]

    return {
        "provider": OPENAI_CODEX_PROVIDER,
        "base_url": (
            os.environ.get("LIONCLAW_CODEX_BASE_URL", "").strip().rstrip("/")
            or os.environ.get("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
            or DEFAULT_CODEX_BASE_URL
        ),
        "api_key": access_token,
        "last_refresh": data.get("last_refresh"),
        "auth_mode": "chatgpt",
    }


def import_codex_cli_tokens() -> dict[str, str] | None:
    """Read usable tokens from ``~/.codex/auth.json`` without modifying it."""
    codex_home = os.environ.get("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    auth_path = Path(codex_home).expanduser() / "auth.json"
    if not auth_path.is_file():
        return None
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        return None
    if not access_token.strip() or not refresh_token.strip():
        return None
    if codex_access_token_is_expiring(access_token, 0):
        return None
    return {"access_token": access_token.strip(), "refresh_token": refresh_token.strip()}


def codex_device_code_login(
    *,
    timeout_seconds: float = 15.0,
    max_wait_seconds: int = 15 * 60,
    display: Any = print,
) -> dict[str, Any]:
    """Run the interactive OpenAI Codex device-code login flow."""
    with httpx.Client(timeout=httpx.Timeout(timeout_seconds)) as client:
        try:
            response = client.post(
                f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            raise CodexAuthError(
                f"Failed to request OpenAI Codex device code: {exc}",
                code="device_code_request_failed",
            ) from exc

    if response.status_code != 200:
        raise CodexAuthError(
            f"OpenAI Codex device code request returned status {response.status_code}.",
            code="device_code_request_error",
        )

    device_data = response.json()
    user_code = str(device_data.get("user_code") or "").strip()
    device_auth_id = str(device_data.get("device_auth_id") or "").strip()
    poll_interval = max(3, int(device_data.get("interval") or 5))
    if not user_code or not device_auth_id:
        raise CodexAuthError(
            "OpenAI Codex device code response missing user_code or device_auth_id.",
            code="device_code_incomplete",
        )

    display("To continue, follow these steps:\n")
    display("  1. Open this URL in your browser:")
    display(f"     {CODEX_DEVICE_URL}\n")
    display("  2. Enter this code:")
    display(f"     {user_code}\n")
    display("Waiting for sign-in... (press Ctrl+C to cancel)")

    deadline = time.monotonic() + max_wait_seconds
    code_response: dict[str, Any] | None = None
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds)) as client:
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{CODEX_ISSUER}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_response = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue
                raise CodexAuthError(
                    f"OpenAI Codex device auth polling returned status {poll.status_code}.",
                    code="device_code_poll_error",
                )
    except KeyboardInterrupt:
        raise

    if code_response is None:
        raise CodexAuthError(
            "OpenAI Codex login timed out before approval.",
            code="device_code_timeout",
        )

    authorization_code = str(code_response.get("authorization_code") or "").strip()
    code_verifier = str(code_response.get("code_verifier") or "").strip()
    if not authorization_code or not code_verifier:
        raise CodexAuthError(
            "OpenAI Codex device auth response missing authorization_code or code_verifier.",
            code="device_code_incomplete_exchange",
        )

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds)) as client:
            token_response = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{CODEX_ISSUER}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        raise CodexAuthError(
            f"OpenAI Codex token exchange failed: {exc}",
            code="token_exchange_failed",
        ) from exc

    if token_response.status_code != 200:
        raise CodexAuthError(
            f"OpenAI Codex token exchange returned status {token_response.status_code}.",
            code="token_exchange_error",
            relogin_required=True,
        )

    token_payload = token_response.json()
    access_token = token_payload.get("access_token")
    refresh_token = token_payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise CodexAuthError(
            "OpenAI Codex token exchange did not return an access_token.",
            code="token_exchange_no_access_token",
            relogin_required=True,
        )
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise CodexAuthError(
            "OpenAI Codex token exchange did not return a refresh_token.",
            code="token_exchange_no_refresh_token",
            relogin_required=True,
        )

    base_url = (
        os.environ.get("LIONCLAW_CODEX_BASE_URL", "").strip().rstrip("/")
        or os.environ.get("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
        or DEFAULT_CODEX_BASE_URL
    )
    return {
        "tokens": {"access_token": access_token.strip(), "refresh_token": refresh_token.strip()},
        "base_url": base_url,
        "last_refresh": _now_iso(),
        "auth_mode": "chatgpt",
        "source": "device-code",
    }


def auth_status() -> dict[str, Any]:
    """Return a small, non-secret Codex auth status payload."""
    try:
        creds = resolve_codex_runtime_credentials(refresh_if_expiring=False)
    except CodexAuthError as exc:
        return {
            "logged_in": False,
            "auth_store": str(get_olano_paths().root / "auth.json"),
            "error": str(exc),
            "code": exc.code,
        }
    token = str(creds.get("api_key") or "")
    return {
        "logged_in": bool(token),
        "auth_store": str(get_olano_paths().root / "auth.json"),
        "base_url": creds.get("base_url"),
        "token_preview": f"{token[:8]}...{token[-4:]}" if len(token) > 16 else "<stored>",
        "expires_soon": codex_access_token_is_expiring(token),
    }


# ── Device-code flow (server-driven) ─────────────────────────────────────────
# The runtime API and the magic-link page both need to start a Codex sign-in and
# watch it complete, so the flow lives here rather than inside either surface.
# ``runtime/api.py`` binds ``_codex_auth_sessions`` to :data:`_DEVICE_SESSIONS`
# (the same dict object) so its routes and their tests are unchanged.

_DEVICE_SESSIONS: dict[str, dict[str, Any]] = {}


async def _run_device_poll(
    session_id: str,
    *,
    device_auth_id: str,
    user_code: str,
    interval: int,
    expires_in: int,
) -> None:
    """Poll OpenAI's Codex device-auth endpoint and persist tokens on approval."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + expires_in
    code_response: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(max(3, int(interval)))
                poll = await client.post(
                    f"{CODEX_ISSUER}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_response = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue
                _DEVICE_SESSIONS[session_id].update(
                    {
                        "status": "error",
                        "error_message": f"Device auth polling returned status {poll.status_code}.",
                    }
                )
                return

        if code_response is None:
            _DEVICE_SESSIONS[session_id].update(
                {"status": "expired", "error_message": "Device code expired before approval."}
            )
            return

        authorization_code = str(code_response.get("authorization_code") or "").strip()
        code_verifier = str(code_response.get("code_verifier") or "").strip()
        if not authorization_code or not code_verifier:
            _DEVICE_SESSIONS[session_id].update(
                {
                    "status": "error",
                    "error_message": (
                        "Device auth response missing authorization_code or code_verifier."
                    ),
                }
            )
            return

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            token_response = await client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{CODEX_ISSUER}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_response.status_code != 200:
            _DEVICE_SESSIONS[session_id].update(
                {
                    "status": "error",
                    "error_message": f"Token exchange returned status {token_response.status_code}.",
                }
            )
            return
        token_payload = token_response.json()
        access_token = token_payload.get("access_token")
        refresh_token = token_payload.get("refresh_token")
        if not isinstance(access_token, str) or not access_token.strip():
            _DEVICE_SESSIONS[session_id].update(
                {"status": "error", "error_message": "Token exchange did not return access_token."}
            )
            return
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            _DEVICE_SESSIONS[session_id].update(
                {"status": "error", "error_message": "Token exchange did not return refresh_token."}
            )
            return

        save_codex_tokens({"access_token": access_token, "refresh_token": refresh_token})
        _DEVICE_SESSIONS[session_id].update({"status": "approved", "error_message": None})
    except Exception as exc:
        session = _DEVICE_SESSIONS.get(session_id)
        if session is not None:
            session.update({"status": "error", "error_message": str(exc)})


async def start_codex_device_flow() -> dict[str, Any]:
    """Start Codex device auth and launch the background poller.

    Returns the session dict (``session_id``, ``status``, ``user_code``,
    ``verification_url``, ``interval``, ``expires_at``). Raises ``RuntimeError``
    with an actionable message when OpenAI refuses or is unreachable.
    """
    import asyncio
    import uuid

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(
                f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        raise RuntimeError(f"Failed to request OpenAI Codex device code: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"OpenAI Codex device code request returned status {response.status_code}."
        )

    device_data = response.json()
    user_code = str(device_data.get("user_code") or "").strip()
    device_auth_id = str(device_data.get("device_auth_id") or "").strip()
    interval = max(3, int(device_data.get("interval") or 5))
    expires_in = int(device_data.get("expires_in") or 15 * 60)
    if not user_code or not device_auth_id:
        raise RuntimeError("OpenAI Codex device code response missing user_code or device_auth_id.")

    session_id = uuid.uuid4().hex
    _DEVICE_SESSIONS[session_id] = {
        "session_id": session_id,
        "status": "pending",
        "user_code": user_code,
        "verification_url": CODEX_DEVICE_URL,
        "interval": interval,
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in,
        "error_message": None,
    }
    asyncio.create_task(
        _run_device_poll(
            session_id,
            device_auth_id=device_auth_id,
            user_code=user_code,
            interval=interval,
            expires_in=expires_in,
        )
    )
    return dict(_DEVICE_SESSIONS[session_id])


def poll_codex_device_flow(session_id: str) -> dict[str, Any] | None:
    """The current state of a device-auth session, or ``None`` if unknown."""
    session = _DEVICE_SESSIONS.get(session_id)
    return dict(session) if session else None
