"""Passwordless sign-in for the Olano-run demo deployment.

The mobile app's "Try the demo" entry (``mobile/src/appConfig.ts``) connects
to one deployment Olano runs for App Review and for prospects, and signs the
visitor in there without a password: ``POST /api/auth/demo``. It exists so no
password ships in the app or in its public remote config; the procedure around
it (provisioning, hardening, the review notes, switching it off) is
``mobile/store/DEMO.md``.

It is off everywhere unless the deployment's environment names a demo account:

``OLANO_DEMO_ACCOUNT=<email>`` (``LIONCLAW_DEMO_ACCOUNT`` is the same variable,
``env_aliases``). Unset, the route answers ``404 demo_disabled``, exactly as a
runtime without it would, so a customer box can never hand out a session by
accident.

**Each visitor gets an account of their own** by default: the named account is
a *template*, and every sign-in creates a fresh member (``visitor-<hex>@`` +
``VISITOR_DOMAIN``, an unusable random password) holding the template's agent
grants, as members. Conversations are namespaced per account, so one visitor
never reads another's chats, which is what a shared demo with anonymous users
needs (App Review Guideline 1.2 on user-generated content): a visitor's threads
carry its ``u:<id>:`` prefix and every thread surface shows it only those
(``scopes_threads``, read by ``auth.thread_visibility_prefix``). The agents
themselves (their workspace, memory, Cortex) are still shared, which DEMO.md's
hardening covers. ``OLANO_DEMO_SHARED=1`` signs everyone in as the template
account itself instead.

Even when it is set, the route refuses (and logs why) unless:

* multi-user mode is on (a single-user box has no accounts to sign in to, and
  every visitor would be its owner);
* the template account exists and is active;
* the template account is **not an administrator**. This is checked on every
  call, never assumed: promoting it in the dashboard switches the route off
  instead of handing out admin sessions.

Each call mints a session of ``DEMO_SESSION_DAYS``; calls are capped
process-wide at ``DEMO_SIGNINS_PER_MINUTE`` and the box at
``MAX_VISITOR_ACCOUNTS`` visitor accounts, so a script cannot fill the users
or sessions tables.

**A visitor's account and conversations last a day.** ``asweep_visitors``
(hourly, from ``server._demo_visitor_sweep_loop``, only on a demo box) deletes
every visitor account older than ``VISITOR_TTL_HOURS`` together with the
threads under its prefix: transcripts, their sidecars and their checkpoints.
That is what the app's demo hint promises. What visitors share, the agents'
own memory and workspace, is outside it; ``mobile/store/DEMO.md`` covers that
reset.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Response

logger = logging.getLogger(__name__)

DEMO_ACCOUNT_ENV = ("OLANO_DEMO_ACCOUNT", "LIONCLAW_DEMO_ACCOUNT")
DEMO_SHARED_ENV = ("OLANO_DEMO_SHARED", "LIONCLAW_DEMO_SHARED")
DEMO_PATH = "/api/auth/demo"
# A visitor account is deleted after a day, so its session need not outlive it.
DEMO_SESSION_DAYS = 1
VISITOR_TTL_HOURS = 24
DEMO_SIGNINS_PER_MINUTE = 30
MAX_VISITOR_ACCOUNTS = 5000
# A reserved top-level domain (RFC 2606): no visitor address can receive mail.
VISITOR_DOMAIN = "demo.invalid"
VISITOR_NAME = "Demo visitor"


def _env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def demo_account_email() -> str | None:
    """The demo template account's email from the environment, or ``None`` when the route is off."""
    return _env(DEMO_ACCOUNT_ENV)


def shared_account() -> bool:
    """True when every visitor signs in as the template account itself."""
    return (_env(DEMO_SHARED_ENV) or "").lower() in {"1", "true", "yes", "on"}


def is_visitor_email(email: str) -> bool:
    return email.lower().endswith(f"@{VISITOR_DOMAIN}")


def scopes_threads(user: Any) -> bool:
    """True when this account may see only the conversations it started.

    Every other human granted an agent sees all of that agent's threads
    (``auth.thread_id_visible``), which is right for a team and wrong for a
    demo whose visitors are strangers: one would read another's chats in the
    list, in search and in the live stream. So on a demo box a visitor's
    threads carry its ``u:<id>:`` prefix and it sees nothing else. The
    template account and ``OLANO_DEMO_SHARED=1`` (where everyone *is* the
    template) are not scoped, and an administrator never is.
    """
    if user is None or getattr(user, "is_admin", False):
        return False
    if demo_account_email() is None:
        return False
    return is_visitor_email(str(getattr(user, "email", "") or ""))


class _MinuteLimiter:
    """At most ``limit`` events in any rolling 60 seconds, process-wide."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            while self._events and moment - self._events[0] >= 60:
                self._events.popleft()
            if len(self._events) >= self.limit:
                return False
            self._events.append(moment)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


_limiter = _MinuteLimiter(DEMO_SIGNINS_PER_MINUTE)


def reset_for_tests() -> None:
    _limiter.reset()


def _template_user() -> Any:
    """The demo template account, or an ``HTTPException`` saying why not."""
    from olano.runtime.auth import multi_user_enabled
    from olano.users.store import get_user_store

    email = demo_account_email()
    if email is None:
        raise HTTPException(status_code=404, detail="demo_disabled")
    if not multi_user_enabled():
        logger.warning(
            "demo sign-in refused: %s is set but multi-user mode is off", DEMO_ACCOUNT_ENV[0]
        )
        raise HTTPException(status_code=404, detail="demo_unavailable")
    user = get_user_store().db.get_user_by_email(email)
    if user is None or not user.is_active:
        logger.warning("demo sign-in refused: the demo account does not exist or is disabled")
        raise HTTPException(status_code=404, detail="demo_unavailable")
    if user.is_admin:
        logger.error("demo sign-in refused: the demo account is an administrator; make it a member")
        raise HTTPException(status_code=403, detail="demo_account_is_admin")
    return user


def _new_visitor(template: Any) -> Any:
    """A fresh member account carrying the template's agents, for one visitor."""
    from olano.users.store import get_user_store

    store = get_user_store()
    visitors = sum(1 for u in store.db.list_users() if is_visitor_email(u.email))
    if visitors >= MAX_VISITOR_ACCOUNTS:
        logger.warning("demo sign-in refused: %d visitor accounts, the cap", visitors)
        raise HTTPException(status_code=429, detail="demo_busy", headers={"Retry-After": "3600"})
    visitor = store.create_user(
        email=f"visitor-{secrets.token_hex(6)}@{VISITOR_DOMAIN}",
        # Nobody ever knows it: the account is reached only through this route.
        password=secrets.token_urlsafe(32),
        name=VISITOR_NAME,
        timezone=template.timezone or "UTC",
    )
    for grant in store.db.list_agents_for_user(template.id):
        store.db.grant_access(
            visitor.id, grant["agent_name"], role="member", granted_by=template.id
        )
    return visitor


def _expired_visitors(now: datetime, ttl_hours: float) -> list[Any]:
    from olano.users.store import get_user_store

    cutoff = now - timedelta(hours=ttl_hours)
    expired = []
    for user in get_user_store().db.list_users():
        if not is_visitor_email(user.email) or user.is_admin:
            continue
        try:
            created = datetime.fromisoformat(user.created_at)
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created <= cutoff:
            expired.append(user)
    return expired


def _visitor_transcripts(sessions_root: Path, user_id: str) -> list[tuple[str, str, list[Path]]]:
    """``(agent, thread_id, files)`` for every thread under a visitor's prefix."""
    from olano.core.feedback import FEEDBACK_SUFFIX

    prefix = f"u:{user_id}:"
    found: list[tuple[str, str, list[Path]]] = []
    if not sessions_root.is_dir():
        return found
    for agent_dir in sessions_root.iterdir():
        if not agent_dir.is_dir():
            continue
        threads: dict[str, list[Path]] = {}
        for path in agent_dir.glob(f"{prefix}*"):
            name = path.name
            for suffix in (".jsonl.gz", ".jsonl", ".state.json", ".meta.json", FEEDBACK_SUFFIX):
                if name.endswith(suffix):
                    threads.setdefault(name[: -len(suffix)], []).append(path)
                    break
        found.extend((agent_dir.name, tid, files) for tid, files in threads.items())
    return found


async def asweep_visitors(
    *, now: datetime | None = None, ttl_hours: float = VISITOR_TTL_HOURS
) -> dict[str, int] | None:
    """Delete visitor accounts older than ``ttl_hours`` and their conversations.

    ``None`` off a demo box (nothing to do, ever). A thread whose checkpoint
    cannot be purged keeps nothing readable: its transcript is gone and its
    owner's account with it, and the next pass tries the checkpoint again only
    if the account still exists, so the account is deleted last.
    """
    if demo_account_email() is None:
        return None
    from olano.config.home import get_olano_paths
    from olano.core.checkpoints import adelete_thread
    from olano.users.store import get_user_store

    moment = now or datetime.now(tz=timezone.utc)
    expired = await asyncio.to_thread(_expired_visitors, moment, ttl_hours)
    sessions_root = get_olano_paths().sessions_dir
    accounts = threads = 0
    for user in expired:
        for agent, thread_id, files in await asyncio.to_thread(
            _visitor_transcripts, sessions_root, user.id
        ):
            try:
                await adelete_thread(agent, thread_id)
            except Exception:
                logger.warning("demo sweep: checkpoint purge failed for %s", agent, exc_info=True)
                continue
            for path in files:
                path.unlink(missing_ok=True)
            threads += 1
        await asyncio.to_thread(get_user_store().db.delete_user, user.id)
        accounts += 1
    if accounts:
        logger.info("demo sweep: deleted %d visitor accounts and %d threads", accounts, threads)
    return {"accounts": accounts, "threads": threads}


def register_demo_routes(app: Any) -> None:
    """Attach ``POST /api/auth/demo`` (public; see ``auth.PUBLIC_API_PATHS``)."""
    from olano.runtime.auth import issue_session_cookie
    from olano.users.store import get_user_store

    @app.post(DEMO_PATH)
    async def demo_sign_in(response: Response) -> dict:
        """Signs the visitor in to the demo deployment as a demo member.

        404 ``demo_disabled`` everywhere but a demo deployment; 404
        ``demo_unavailable`` / 403 ``demo_account_is_admin`` on a misconfigured
        one; 429 ``demo_busy`` over the per-minute or visitor-account cap.
        """
        template = await asyncio.to_thread(_template_user)
        if not _limiter.allow():
            raise HTTPException(status_code=429, detail="demo_busy", headers={"Retry-After": "60"})
        user = template if shared_account() else await asyncio.to_thread(_new_visitor, template)
        raw = await asyncio.to_thread(get_user_store().issue_session, user, DEMO_SESSION_DAYS)
        issue_session_cookie(response, raw, max_age=DEMO_SESSION_DAYS * 24 * 60 * 60)
        return {"user": user.public()}
