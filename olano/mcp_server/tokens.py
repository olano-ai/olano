"""Bearer tokens for the inbound MCP and A2A endpoints.

A token is minted once — by an operator (dashboard, CLI, magic link) or by
Olano's own OAuth authorization server after an admin consents — shown once,
and stored **hashed** (SHA-256 over 32 random bytes — a KDF buys nothing
against a 256-bit random secret) in ``<home>/mcp_server/tokens.json`` with mode
0600. Each token names the agents it may address, an optional expiry, and a
label so a leaked token can be traced to the integration it was issued for and
revoked alone. OAuth-issued tokens also carry the client they were issued to
and the admin who consented, and come with a rotating refresh token.

Wire formats ``olano_mcp_<id>_<secret>`` (access) and ``olano_rt_<id>_<secret>``
(refresh): the fixed prefix lets secret scanners recognise them, the id lets the
verifier find the one record to compare against (constant-time) instead of
hashing every candidate.
"""

from __future__ import annotations

import builtins
import hashlib
import hmac
import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "olano_mcp_"
REFRESH_PREFIX = "olano_rt_"
ALL_AGENTS = "*"


def _active(expires_at: str | None, revoked_at: str | None) -> bool:
    if revoked_at:
        return False
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)
    except ValueError:
        return False


def _scopes_for(agents: list[str]) -> list[str]:
    return [f"agent:{a}" if a != ALL_AGENTS else "agents:*" for a in agents]


@dataclass
class McpToken:
    id: str
    label: str
    agents: list[str]
    sha256: str
    created_at: str
    created_by: str = ""
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    scopes: list[str] = field(default_factory=list)
    # "manual" = minted by an operator (dashboard/CLI/magic link); "oauth" =
    # issued by Olano's own authorization server after a consent.
    kind: str = "manual"
    client_id: str = ""
    subject: str = ""

    # ── derived ────────────────────────────────────────────────────────────
    @property
    def revoked(self) -> bool:
        return bool(self.revoked_at)

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.fromisoformat(self.expires_at) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    @property
    def active(self) -> bool:
        return not self.revoked and not self.expired

    def allows_agent(self, agent: str) -> bool:
        wanted = (agent or "").casefold()
        return any(a == ALL_AGENTS or a.casefold() == wanted for a in self.agents)

    def scope_list(self) -> list[str]:
        """OAuth-style scopes this token carries, as the MCP layer sees them."""
        out = _scopes_for(self.agents)
        out.extend(s for s in self.scopes if s not in out)
        return out

    def public(self) -> dict[str, Any]:
        """The record without the hash — what listings return."""
        data = asdict(self)
        data.pop("sha256", None)
        data["active"] = self.active
        data["expired"] = self.expired
        data["revoked"] = self.revoked
        return data


@dataclass
class RefreshToken:
    """A rotating refresh token issued by the authorization server. Hashed at
    rest like access tokens; ``access_token_id`` is the access token it was
    issued alongside, revoked together on rotation."""

    id: str
    sha256: str
    client_id: str
    agents: list[str]
    created_at: str
    expires_at: str | None = None
    subject: str = ""
    access_token_id: str = ""
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return _active(self.expires_at, self.revoked_at)

    def scope_list(self) -> list[str]:
        return _scopes_for(self.agents)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("sha256", None)
        data["active"] = self.active
        return data


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_prefixed(raw: str, prefix: str) -> str | None:
    if not raw or not raw.startswith(prefix):
        return None
    rest = raw[len(prefix) :]
    token_id, sep, secret = rest.partition("_")
    if not sep or not token_id or not secret:
        return None
    return token_id


def parse_token_id(raw: str) -> str | None:
    """The ``<id>`` segment of a wire access token, or ``None`` when it is not ours."""
    return _parse_prefixed(raw, TOKEN_PREFIX)


def parse_refresh_id(raw: str) -> str | None:
    return _parse_prefixed(raw, REFRESH_PREFIX)


def _rows(model: Any, rows: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            rec = model(**{k: v for k, v in row.items() if k in model.__dataclass_fields__})
        except TypeError:
            continue
        out[rec.id] = rec
    return out


class McpTokenStore:
    """JSON-file token store. Every read re-checks the file's mtime, so a token
    minted or revoked from the CLI is honoured by the running server at once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cache: dict[str, McpToken] = {}
        self._refresh_cache: dict[str, RefreshToken] = {}
        self._cache_stat: tuple[int, int] | None = None

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> dict[str, McpToken]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._cache, self._refresh_cache, self._cache_stat = {}, {}, None
            return self._cache
        sig = (st.st_mtime_ns, st.st_size)
        if self._cache_stat == sig:
            return self._cache
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            logger.warning("MCP token store unreadable at %s — treating as empty", self.path)
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self._cache = _rows(McpToken, raw.get("tokens"))
        self._refresh_cache = _rows(RefreshToken, raw.get("refresh_tokens"))
        self._cache_stat = sig
        return self._cache

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "tokens": [asdict(t) for t in self._cache.values()],
            "refresh_tokens": [asdict(r) for r in self._refresh_cache.values()],
        }
        from olano.security.private_files import write_private_text

        # Owner-only from the first byte: hashed bearer + refresh tokens.
        write_private_text(self.path, json.dumps(payload, indent=2))
        self._cache_stat = None  # force reload

    # ── access tokens ──────────────────────────────────────────────────────
    def mint(
        self,
        *,
        label: str,
        agents: list[str],
        ttl_days: int | None,
        created_by: str = "",
        scopes: list[str] | None = None,
        ttl_seconds: int | None = None,
        kind: str = "manual",
        client_id: str = "",
        subject: str = "",
    ) -> tuple[str, McpToken]:
        """Create a token. Returns ``(raw_token, record)`` — the raw value is
        never stored and cannot be recovered later. ``ttl_seconds`` (used for
        OAuth access tokens) takes precedence over ``ttl_days``."""
        label = (label or "").strip()
        if not label:
            raise ValueError("a label is required (which integration this token is for)")
        cleaned = sorted({(a or "").strip() for a in agents if (a or "").strip()})
        if not cleaned:
            raise ValueError("at least one agent name (or '*') is required")
        with self._lock:
            tokens = self._load()
            token_id = secrets.token_hex(6)
            while token_id in tokens:  # pragma: no cover - 48-bit collision
                token_id = secrets.token_hex(6)
            secret = secrets.token_urlsafe(32)
            raw = f"{TOKEN_PREFIX}{token_id}_{secret}"
            expires_at = None
            now = datetime.now(timezone.utc)
            if ttl_seconds is not None and ttl_seconds > 0:
                expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
            elif ttl_days and ttl_days > 0:
                expires_at = (now + timedelta(days=ttl_days)).isoformat()
            record = McpToken(
                id=token_id,
                label=label,
                agents=cleaned,
                sha256=_hash(raw),
                created_at=_now_iso(),
                created_by=created_by,
                expires_at=expires_at,
                scopes=list(scopes or []),
                kind=kind,
                client_id=client_id,
                subject=subject,
            )
            tokens[token_id] = record
            self._save()
        return raw, record

    def list(self) -> builtins.list[McpToken]:
        with self._lock:
            return sorted(self._load().values(), key=lambda t: t.created_at, reverse=True)

    def get(self, token_id: str) -> McpToken | None:
        with self._lock:
            return self._load().get(token_id)

    def revoke(self, token_id: str) -> bool:
        with self._lock:
            tokens = self._load()
            tok = tokens.get(token_id)
            if tok is None or tok.revoked:
                return False
            tok.revoked_at = _now_iso()
            self._save()
            return True

    def delete(self, token_id: str) -> bool:
        with self._lock:
            tokens = self._load()
            if token_id not in tokens:
                return False
            tokens.pop(token_id)
            self._save()
            return True

    def verify(self, raw: str) -> McpToken | None:
        """The active record ``raw`` proves possession of, else ``None``.

        Constant-time on the hash so a wrong token cannot be narrowed byte by
        byte; expired and revoked records fail like unknown ones.
        """
        token_id = parse_token_id(raw)
        if token_id is None:
            return None
        with self._lock:
            tok = self._load().get(token_id)
            if tok is None:
                return None
            if not hmac.compare_digest(tok.sha256, _hash(raw)):
                return None
            if not tok.active:
                return None
            return tok

    def touch(self, token_id: str) -> None:
        """Record use. Throttled to once a minute per token so a chatty client
        does not rewrite the file on every call."""
        with self._lock:
            tokens = self._load()
            tok = tokens.get(token_id)
            if tok is None:
                return
            now = datetime.now(timezone.utc)
            if tok.last_used_at:
                try:
                    if now - datetime.fromisoformat(tok.last_used_at) < timedelta(minutes=1):
                        return
                except ValueError:
                    pass
            tok.last_used_at = now.isoformat()
            try:
                self._save()
            except OSError:  # pragma: no cover - usage stamps are best effort
                logger.debug("MCP token store: could not persist last_used_at", exc_info=True)

    # ── refresh tokens (authorization server) ──────────────────────────────
    def mint_refresh(
        self,
        *,
        client_id: str,
        agents: builtins.list[str],  # `list` is shadowed by the method above
        ttl_days: int,
        subject: str = "",
        access_token_id: str = "",
    ) -> tuple[str, RefreshToken]:
        cleaned = sorted({(a or "").strip() for a in agents if (a or "").strip()})
        if not cleaned:
            raise ValueError("at least one agent name (or '*') is required")
        with self._lock:
            self._load()
            refresh = self._refresh_cache
            rid = secrets.token_hex(6)
            while rid in refresh:  # pragma: no cover
                rid = secrets.token_hex(6)
            raw = f"{REFRESH_PREFIX}{rid}_{secrets.token_urlsafe(32)}"
            record = RefreshToken(
                id=rid,
                sha256=_hash(raw),
                client_id=client_id,
                agents=cleaned,
                created_at=_now_iso(),
                expires_at=(
                    (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()
                    if ttl_days and ttl_days > 0
                    else None
                ),
                subject=subject,
                access_token_id=access_token_id,
            )
            refresh[rid] = record
            self._save()
        return raw, record

    def verify_refresh(self, raw: str) -> RefreshToken | None:
        rid = parse_refresh_id(raw)
        if rid is None:
            return None
        with self._lock:
            self._load()
            rec = self._refresh_cache.get(rid)
            if rec is None or not hmac.compare_digest(rec.sha256, _hash(raw)):
                return None
            return rec if rec.active else None

    def revoke_refresh(self, rid: str) -> bool:
        with self._lock:
            self._load()
            rec = self._refresh_cache.get(rid)
            if rec is None or rec.revoked_at:
                return False
            rec.revoked_at = _now_iso()
            self._save()
            return True

    def list_refresh(self) -> builtins.list[RefreshToken]:
        with self._lock:
            self._load()
            return sorted(self._refresh_cache.values(), key=lambda r: r.created_at, reverse=True)

    def revoke_client(self, client_id: str) -> int:
        """Revoke every access and refresh token issued to an OAuth client."""
        with self._lock:
            tokens = self._load()
            now = _now_iso()
            n = 0
            for tok in tokens.values():
                if tok.client_id == client_id and not tok.revoked_at:
                    tok.revoked_at = now
                    n += 1
            for rec in self._refresh_cache.values():
                if rec.client_id == client_id and not rec.revoked_at:
                    rec.revoked_at = now
                    n += 1
            if n:
                self._save()
            return n

    def revoke_by_hash(self, raw: str) -> bool:
        """Revoke whichever access or refresh token ``raw`` is (RFC 7009)."""
        if parse_refresh_id(raw) is not None:
            rec = self.verify_refresh(raw)
            return bool(rec and self.revoke_refresh(rec.id))
        tok = self.verify(raw)
        return bool(tok and self.revoke(tok.id))


def token_store_path() -> Path:
    return get_olano_paths().root / "mcp_server" / "tokens.json"


_STORE: McpTokenStore | None = None
_STORE_PATH: Path | None = None


def get_token_store() -> McpTokenStore:
    """Process-wide store for the active home (re-created when the home moves,
    which is what the test suite does per test)."""
    global _STORE, _STORE_PATH
    path = token_store_path()
    if _STORE is None or path != _STORE_PATH:
        _STORE = McpTokenStore(path)
        _STORE_PATH = path
    return _STORE
