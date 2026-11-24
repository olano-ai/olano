"""Deliver push notifications through the Olano Cloud relay.

One daemon thread drains a bounded queue and POSTs batches to
``{cloud}/api/v1/push`` with the deployment token. Producers never wait on the
network: :meth:`PushDispatcher.submit` only enqueues, and a full queue drops the
newest message rather than blocking an agent turn or an event-loop callback.

The relay's limits are mirrored here (:data:`MAX_NOTIFICATIONS`,
:data:`MAX_DEVICES_PER_NOTIFICATION`, :data:`MAX_SENDS_PER_REQUEST`) so a batch
is never refused for its size. The relay answers with the tokens Apple or
Google reported as permanently dead, and those rows are deleted here: a phone
that uninstalled the app stops costing a send on every notification.

Failures are reported, never raised: ``status()`` carries the last error, which
``GET /api/push/status`` shows to the app so "Notifications are not reaching
this phone" has a reason next to it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from olano.push.store import Device, DeviceStore, get_device_store

logger = logging.getLogger(__name__)

PUSH_PATH = "/api/v1/push"
POST_TIMEOUT_SECONDS = 15.0

# The relay's own limits (cloud repo, docs/push-notifications.md).
MAX_NOTIFICATIONS = 10
MAX_DEVICES_PER_NOTIFICATION = 10
MAX_SENDS_PER_REQUEST = 50
TITLE_MAX = 120
BODY_MAX = 240

QUEUE_MAX = 256

#: Reasons ``status()["last_error"]`` can carry.
ERROR_NOT_MANAGED = "not_managed"
ERROR_CLOUD_AUTH = "cloud_auth"
ERROR_CLOUD_UNCONFIGURED = "cloud_unconfigured"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_NETWORK = "network"
ERROR_CLOUD = "cloud_error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PushMessage:
    """One notification, to one or more phones."""

    devices: list[Device]
    title: str
    body: str = ""
    data: dict[str, str] = field(default_factory=dict)
    collapse_key: str = ""
    thread_id: str = ""
    channel: str = ""
    #: The app's button set: "approval" shows Approve and Decline, "reply" a
    #: Reply field.
    category: str = ""
    priority: str = "high"
    #: Messages sharing a throttle key within ``throttle_seconds`` are dropped
    #: after the first. Empty means never throttled.
    throttle_key: str = ""
    throttle_seconds: float = 0.0

    def entries(self) -> list[dict[str, Any]]:
        """The relay's ``notifications[]`` entries for this message."""
        base: dict[str, Any] = {
            "title": self.title[:TITLE_MAX] or "Olano",
            "priority": "normal" if self.priority == "normal" else "high",
        }
        if self.body:
            base["body"] = self.body[:BODY_MAX]
        if self.data:
            base["data"] = {str(k): str(v) for k, v in self.data.items()}
        if self.collapse_key:
            base["collapse_key"] = self.collapse_key[:64]
        if self.thread_id:
            base["thread_id"] = self.thread_id[:64]
        if self.channel:
            base["channel"] = self.channel
        if self.category:
            base["category"] = self.category
        out = []
        devs = [{"token": d.token, "platform": d.platform} for d in self.devices]
        for i in range(0, len(devs), MAX_DEVICES_PER_NOTIFICATION):
            out.append({**base, "devices": devs[i : i + MAX_DEVICES_PER_NOTIFICATION]})
        return out


def _batches(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split relay entries into request bodies within the relay's limits."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    sends = 0
    for entry in entries:
        n = len(entry["devices"])
        if current and (len(current) >= MAX_NOTIFICATIONS or sends + n > MAX_SENDS_PER_REQUEST):
            batches.append(current)
            current, sends = [], 0
        current.append(entry)
        sends += n
    if current:
        batches.append(current)
    return batches


def _default_cloud() -> tuple[str, str] | None:
    from olano.deployment.sources import cloud_settings_config

    cfg = cloud_settings_config()
    return (cfg.url, cfg.token) if cfg is not None else None


class PushDispatcher:
    """Background sender. ``transport`` and ``cloud`` are injectable for tests."""

    def __init__(
        self,
        *,
        store: DeviceStore | None = None,
        cloud: Callable[[], tuple[str, str] | None] = _default_cloud,
        transport: Any | None = None,
    ) -> None:
        self._store = store
        self._cloud = cloud
        self._transport = transport
        self._queue: queue.Queue[PushMessage | None] = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._throttle: dict[str, float] = {}
        self._lock = threading.Lock()
        self._paused_until = 0.0
        self._stats: dict[str, Any] = {
            "sent": 0,
            "failed": 0,
            "dropped": 0,
            "invalid_pruned": 0,
            "last_sent_at": None,
            "last_error": None,
            "last_error_at": None,
        }

    @property
    def store(self) -> DeviceStore:
        return self._store or get_device_store()

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="push-dispatcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── producing ────────────────────────────────────────────────────────
    def submit(self, message: PushMessage) -> bool:
        """Queue ``message``. Never blocks, never raises. False when dropped."""
        if not message.devices:
            return False
        if message.throttle_key and message.throttle_seconds > 0:
            now = time.monotonic()
            with self._lock:
                last = self._throttle.get(message.throttle_key, 0.0)
                if last and now - last < message.throttle_seconds:
                    return False
                self._throttle[message.throttle_key] = now
                if len(self._throttle) > 2048:
                    cutoff = now - 6 * 3600
                    self._throttle = {k: v for k, v in self._throttle.items() if v > cutoff}
        try:
            self._queue.put_nowait(message)
            return True
        except queue.Full:
            with self._lock:
                self._stats["dropped"] += 1
            logger.warning("push: queue full; dropping a notification")
            return False

    # ── consuming ────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                first = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if first is None:
                break
            pending = [first]
            while len(pending) < MAX_NOTIFICATIONS:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._stopping.set()
                    break
                pending.append(nxt)
            wait = self._paused_until - time.monotonic()
            if wait > 0:
                # The relay asked us to slow down. Sleep it off rather than
                # drop: the queue is bounded, so a long pause sheds load at
                # submit() instead of here.
                self._stopping.wait(min(wait, 300.0))
            try:
                self.send(pending)
            except Exception:  # pragma: no cover - the thread must survive
                logger.exception("push: unexpected failure sending a batch")

    def send(self, messages: list[PushMessage]) -> dict[str, Any]:
        """Deliver ``messages`` now (blocking). Returns a summary; never raises."""
        entries = [e for m in messages for e in m.entries()]
        summary: dict[str, Any] = {"sent": 0, "failed": 0, "invalid_tokens": [], "error": None}
        if not entries:
            return summary
        cloud = None
        try:
            cloud = self._cloud()
        except Exception:
            logger.debug("push: could not resolve the cloud channel", exc_info=True)
        if cloud is None:
            total = sum(len(e["devices"]) for e in entries)
            summary["failed"] = total
            summary["error"] = ERROR_NOT_MANAGED
            self._note(failed=total, error=ERROR_NOT_MANAGED)
            return summary
        url, token = cloud
        import httpx

        client_kwargs: dict[str, Any] = {"timeout": POST_TIMEOUT_SECONDS}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        batches = _batches(entries)
        with httpx.Client(**client_kwargs) as client:
            for index, body in enumerate(batches):
                sends = sum(len(e["devices"]) for e in body)
                error, sent, failed, invalid = self._post(client, url, token, body, sends)
                summary["sent"] += sent
                summary["failed"] += failed
                summary["invalid_tokens"].extend(invalid)
                if error:
                    summary["error"] = error
                if error in (ERROR_CLOUD_AUTH, ERROR_CLOUD_UNCONFIGURED, ERROR_RATE_LIMITED):
                    # Every later batch would get the same answer, so they
                    # count as failed without another round trip.
                    rest = sum(len(e["devices"]) for b in batches[index + 1 :] for e in b)
                    if rest:
                        summary["failed"] += rest
                        self._note(failed=rest)
                    break
        if summary["invalid_tokens"]:
            try:
                removed = self.store.delete_tokens(summary["invalid_tokens"])
            except Exception:
                removed = 0
                logger.debug("push: could not prune dead tokens", exc_info=True)
            with self._lock:
                self._stats["invalid_pruned"] += removed
        return summary

    def _post(
        self, client: Any, url: str, token: str, body: list[dict[str, Any]], sends: int
    ) -> tuple[str | None, int, int, list[str]]:
        try:
            resp = client.post(
                f"{url}{PUSH_PATH}",
                headers={"Authorization": f"Bearer {token}"},
                json={"notifications": body},
            )
        except Exception as exc:
            logger.info("push: relay unreachable (%s)", type(exc).__name__)
            self._note(failed=sends, error=ERROR_NETWORK)
            return ERROR_NETWORK, 0, sends, []
        status = resp.status_code
        payload: dict[str, Any] = {}
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            pass
        if status == 200:
            sent = int(payload.get("sent") or 0)
            failed = int(payload.get("failed") or 0)
            invalid = [str(t) for t in payload.get("invalid_tokens") or [] if t]
            # The relay counts a dead token as failed too. Those are pruned
            # below and are routine (an uninstalled app), not a delivery fault.
            faulty = failed > len(invalid)
            self._note(sent=sent, failed=failed, error=ERROR_CLOUD if faulty else None)
            return (ERROR_CLOUD if faulty else None), sent, failed, invalid
        if status == 429:
            retry = payload.get("retry_after") or resp.headers.get("Retry-After") or 60
            try:
                delay = max(1.0, float(retry))
            except (TypeError, ValueError):
                delay = 60.0
            self._paused_until = time.monotonic() + min(delay, 3600.0)
            self._note(failed=sends, error=ERROR_RATE_LIMITED)
            return ERROR_RATE_LIMITED, 0, sends, []
        if status in (401, 403):
            self._note(failed=sends, error=ERROR_CLOUD_AUTH)
            return ERROR_CLOUD_AUTH, 0, sends, []
        if status == 503 and payload.get("error") == "push_unconfigured":
            self._note(failed=sends, error=ERROR_CLOUD_UNCONFIGURED)
            return ERROR_CLOUD_UNCONFIGURED, 0, sends, []
        logger.info("push: relay answered HTTP %s", status)
        self._note(failed=sends, error=ERROR_CLOUD)
        return ERROR_CLOUD, 0, sends, []

    def _note(self, *, sent: int = 0, failed: int = 0, error: str | None = None) -> None:
        with self._lock:
            self._stats["sent"] += sent
            self._stats["failed"] += failed
            if sent:
                self._stats["last_sent_at"] = _now_iso()
            if error:
                self._stats["last_error"] = error
                self._stats["last_error_at"] = _now_iso()
            elif sent:
                self._stats["last_error"] = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.running, "queued": self._queue.qsize(), **self._stats}


_DISPATCHER: PushDispatcher | None = None
_DISPATCHER_LOCK = threading.Lock()


def get_push_dispatcher(start: bool = False) -> PushDispatcher | None:
    """The process dispatcher, or None when push is off or impossible here.

    ``start=True`` is the runtime server's boot call: it creates and starts the
    dispatcher when ``push.enabled`` is on and the box has a cloud channel.
    ``start=False`` returns the running one (or None) with no side effects, which
    is what every producer asks, so nothing sends outside the server.
    """
    global _DISPATCHER
    with _DISPATCHER_LOCK:
        if _DISPATCHER is not None or not start:
            return _DISPATCHER
        from olano.config.system_settings import load_push_settings

        if not load_push_settings().enabled:
            return None
        if _default_cloud() is None:
            return None
        _DISPATCHER = PushDispatcher()
        _DISPATCHER.start()
        return _DISPATCHER


def set_push_dispatcher(dispatcher: PushDispatcher | None) -> None:
    """Install (or clear) the process dispatcher. Tests and the server's stop."""
    global _DISPATCHER
    with _DISPATCHER_LOCK:
        if _DISPATCHER is not None and _DISPATCHER is not dispatcher:
            _DISPATCHER.stop(timeout=1.0)
        _DISPATCHER = dispatcher
