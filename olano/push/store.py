"""The registry of phones that asked for push notifications.

One JSON file, ``<home>/run/push_devices.json``, written 0600 because a push
token is a capability: anyone holding it, and the matching provider credential,
can put text on that phone's lock screen. The file is small (a handful of rows
per person), read on every call, and written atomically, so a crash mid-write
leaves the previous version rather than an empty registry.

A row belongs to one human (``user_id``: the users-table id, or ``"owner"`` on a
single-user install). The **token is the key**: the app registers on every
launch, and the same phone signing in as a different person moves the row to
that person instead of leaving the first one receiving the second one's
notifications.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.push import EVENT_KINDS

logger = logging.getLogger(__name__)

PLATFORMS: tuple[str, ...] = ("ios", "android")

#: A person signing in on more phones than this keeps the most recently seen
#: ones. Generous for real use, small enough that a misbehaving client cannot
#: grow the fan-out of every notification without bound.
MAX_DEVICES_PER_USER = 10

# APNs tokens are hex; FCM registration tokens are URL-safe base64 with ``:``,
# ``-`` and ``_``. Anything else is not a token either provider issued.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9:_\-.]{16,4096}$")
_NAME_MAX = 80
_VERSION_MAX = 32


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvalidDevice(ValueError):
    """A registration the store refuses, with a message fit for a 400."""


@dataclass
class Device:
    """One phone, for one person."""

    id: str
    user_id: str
    token: str
    platform: str
    name: str = ""
    app_version: str = ""
    events: list[str] = field(default_factory=lambda: list(EVENT_KINDS))
    created_at: str = field(default_factory=_now)
    last_seen_at: str = field(default_factory=_now)

    def public(self) -> dict[str, Any]:
        """The row as the API returns it: everything but the token itself."""
        data = asdict(self)
        token = data.pop("token")
        data["token_suffix"] = token[-6:]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Device | None:
        try:
            return cls(
                id=str(raw["id"]),
                user_id=str(raw["user_id"]),
                token=str(raw["token"]),
                platform=str(raw["platform"]),
                name=str(raw.get("name") or ""),
                app_version=str(raw.get("app_version") or ""),
                events=normalize_events(raw.get("events")),
                created_at=str(raw.get("created_at") or _now()),
                last_seen_at=str(raw.get("last_seen_at") or _now()),
            )
        except (KeyError, TypeError, ValueError):
            return None


def normalize_events(value: object) -> list[str]:
    """Keep the known kinds, in canonical order. ``None`` means "all of them"."""
    if value is None:
        return list(EVENT_KINDS)
    if not isinstance(value, list | tuple | set):
        raise InvalidDevice("events must be a list")
    wanted = {str(v).strip().lower() for v in value}
    unknown = wanted - set(EVENT_KINDS)
    if unknown:
        raise InvalidDevice(f"unknown event kinds: {', '.join(sorted(unknown))}")
    return [kind for kind in EVENT_KINDS if kind in wanted]


def validate_token(token: object) -> str:
    text = str(token or "").strip()
    if not _TOKEN_RE.match(text):
        raise InvalidDevice("token is not a push token")
    return text


def validate_platform(platform: object) -> str:
    text = str(platform or "").strip().lower()
    if text not in PLATFORMS:
        raise InvalidDevice(f"platform must be one of: {', '.join(PLATFORMS)}")
    return text


class DeviceStore:
    """File-backed device registry. Thread-safe within one process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    # ── persistence ──────────────────────────────────────────────────────
    def _read(self) -> list[Device]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.warning("push: %s is unreadable; treating it as empty", self.path)
            return []
        rows = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            device = Device.from_dict(row) if isinstance(row, dict) else None
            if device is not None:
                out.append(device)
        return out

    def _write(self, devices: list[Device]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex[:8]}.tmp")
        payload = json.dumps(
            {"version": 1, "devices": [asdict(d) for d in devices]}, indent=2, sort_keys=True
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            tmp.replace(self.path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # ── reads ────────────────────────────────────────────────────────────
    def all(self) -> list[Device]:
        with self._lock:
            return self._read()

    def for_user(self, user_id: str) -> list[Device]:
        return [d for d in self.all() if d.user_id == user_id]

    def get(self, device_id: str) -> Device | None:
        return next((d for d in self.all() if d.id == device_id), None)

    def for_users(self, user_ids: set[str], kind: str) -> list[Device]:
        """Devices of ``user_ids`` that opted into ``kind``."""
        return [d for d in self.all() if d.user_id in user_ids and kind in d.events]

    # ── writes ───────────────────────────────────────────────────────────
    def register(
        self,
        *,
        user_id: str,
        token: object,
        platform: object,
        name: object = "",
        app_version: object = "",
        events: object = None,
    ) -> Device:
        """Insert or refresh a phone. Raises :class:`InvalidDevice`."""
        token_text = validate_token(token)
        platform_text = validate_platform(platform)
        name_text = str(name or "").strip()[:_NAME_MAX]
        version_text = str(app_version or "").strip()[:_VERSION_MAX]
        with self._lock:
            devices = self._read()
            existing = next((d for d in devices if d.token == token_text), None)
            if existing is not None:
                if existing.user_id != user_id:
                    # Same phone, different person: the row moves, and the
                    # previous person's opt-outs do not follow it.
                    existing.user_id = user_id
                    existing.events = list(EVENT_KINDS)
                existing.platform = platform_text
                if name_text:
                    existing.name = name_text
                if version_text:
                    existing.app_version = version_text
                if events is not None:
                    existing.events = normalize_events(events)
                existing.last_seen_at = _now()
                device = existing
            else:
                device = Device(
                    id=uuid.uuid4().hex[:12],
                    user_id=user_id,
                    token=token_text,
                    platform=platform_text,
                    name=name_text,
                    app_version=version_text,
                    events=normalize_events(events),
                )
                devices.append(device)
            mine = sorted(
                (d for d in devices if d.user_id == user_id),
                key=lambda d: d.last_seen_at,
                reverse=True,
            )
            for stale in mine[MAX_DEVICES_PER_USER:]:
                devices.remove(stale)
            self._write(devices)
            return device

    def update(self, device_id: str, *, name: object = None, events: object = None) -> Device:
        with self._lock:
            devices = self._read()
            device = next((d for d in devices if d.id == device_id), None)
            if device is None:
                raise KeyError(device_id)
            if name is not None:
                device.name = str(name).strip()[:_NAME_MAX]
            if events is not None:
                device.events = normalize_events(events)
            self._write(devices)
            return device

    def delete(self, device_id: str) -> bool:
        with self._lock:
            devices = self._read()
            kept = [d for d in devices if d.id != device_id]
            if len(kept) == len(devices):
                return False
            self._write(kept)
            return True

    def delete_tokens(self, tokens: list[str]) -> int:
        """Drop rows whose token a provider reported as permanently dead."""
        dead = {str(t) for t in tokens if t}
        if not dead:
            return 0
        with self._lock:
            devices = self._read()
            kept = [d for d in devices if d.token not in dead]
            removed = len(devices) - len(kept)
            if removed:
                self._write(kept)
            return removed

    def delete_user(self, user_id: str) -> int:
        with self._lock:
            devices = self._read()
            kept = [d for d in devices if d.user_id != user_id]
            removed = len(devices) - len(kept)
            if removed:
                self._write(kept)
            return removed


_STORE: DeviceStore | None = None
_STORE_LOCK = threading.Lock()


def get_device_store() -> DeviceStore:
    """The process store for the active home. Re-resolved when the home moves
    (tests point ``LIONCLAW_HOME`` at a fresh directory per test)."""
    global _STORE
    from olano.config.home import get_olano_paths

    path = get_olano_paths().run_dir / "push_devices.json"
    with _STORE_LOCK:
        if _STORE is None or _STORE.path != path:
            _STORE = DeviceStore(path)
        return _STORE
