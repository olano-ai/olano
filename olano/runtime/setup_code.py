"""The one-time setup code that guards the creation of a box's first admin.

An unmanaged box with no accounts serves the *create your account* screen to
whoever reaches its URL first. On a laptop that is the owner; on a VPS with a
public address it can just as well be a stranger running a scanner, who then
owns the box: the vault, every agent, the terminal. Nothing distinguished the
two, because nothing about the first request says who sent it.

The setup code is that distinction. It is a short random value that only the
person who installed the box can read (the self-host ``setup.sh`` prints it and
writes it into ``.env``; otherwise the runtime writes it under the home and
prints it in its log, which is ``docker compose logs olano`` on a compose
install), and ``POST /api/auth/bootstrap`` refuses to create the first admin
without it.

Scope, decided by :func:`applies`:

* **Only unmanaged boxes.** A managed deployment provisions its admin from the
  environment before the API binds (``runtime/first_boot.py``, keyed on
  ``LIONCLAW_ADMIN_PASSWORD``), and its posture is never ``open``. Either fact
  takes the box out of scope, so the managed path is unchanged. A posture that
  cannot be read counts as unmanaged: the failure mode of asking for a code
  is a log line away, the failure mode of not asking is a stolen box.
* **Only while there are no accounts.** Once the first admin exists the code
  is spent: the generated file is deleted and the code's digest recorded, so
  the same value never opens the door twice, even if the users table were
  ever emptied again (a fresh code is generated in that case).

Where the code comes from, in order: ``OLANO_SETUP_CODE`` (or its legacy
twin, ``env_aliases`` mirrors the two), unless that value was already spent;
else ``<home>/run/setup-code`` (0600), generated on first need. Comparison is
constant-time over a normalized form (case, spaces and dashes do not matter,
so a code read aloud or retyped from a phone still matches), and wrong
attempts are rate-limited process-wide.

A setup code in the environment has one more meaning: the installer that put
it there expects other people to be able to reach the box. ``auth.
multi_user_enabled`` therefore defaults to on for such a box, so until the
first account exists every API path except the setup ones answers
``setup_required`` instead of treating the anonymous visitor as the owner.

Imported at the top of ``auth.py``: stdlib only at module level.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_NAME = "OLANO_SETUP_CODE"
LEGACY_ENV_NAME = "LIONCLAW_SETUP_CODE"
FILE_NAME = "setup-code"
CONSUMED_FILE_NAME = "setup-code.consumed"

# No 0/O, 1/I/L, so a code copied by hand from a terminal is unambiguous.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
GROUPS = 3
GROUP_LEN = 4

# Wrong attempts allowed per window, counted across the whole process. Every
# browser reaches the runtime through the same reverse proxy, so a per-address
# limit would be one bucket anyway. 31^12 codes make guessing hopeless without
# it; the limit keeps a scanner from even trying, and a lockout lasts at most
# one window.
MAX_WRONG_ATTEMPTS = 10
WINDOW_SECONDS = 15 * 60

# Tests switch enforcement off suite-wide (tests/conftest.py) because dozens
# of them create a first admin as setup; the tests of this module and the
# first-run e2e switch it back on. Never read from the environment: there is
# deliberately no production knob that turns the check off.
ENFORCED = True

_lock = threading.Lock()
_wrong: deque[float] = deque()


@dataclass(frozen=True)
class Verdict:
    """Outcome of :func:`verify`. ``detail`` is the API error code when not ok."""

    ok: bool
    detail: str = ""
    retry_after: int = 0


def generate() -> str:
    """A fresh code: ``ABCD-EFGH-JKMN`` from :data:`ALPHABET`."""
    return "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(GROUP_LEN)) for _ in range(GROUPS)
    )


def normalize(value: str) -> str:
    """Uppercase, without spaces or dashes: what is compared."""
    return "".join(ch for ch in (value or "").upper() if ch not in " -\t\r\n")


def _digest(code: str) -> str:
    return hashlib.sha256(normalize(code).encode("utf-8")).hexdigest()


def _run_dir() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().run_dir


def code_path() -> Path:
    return _run_dir() / FILE_NAME


def _consumed_path() -> Path:
    return _run_dir() / CONSUMED_FILE_NAME


def env_code() -> str:
    """The code the environment names, or ``""``."""
    return (os.environ.get(ENV_NAME) or os.environ.get(LEGACY_ENV_NAME) or "").strip()


def configured_in_env() -> bool:
    """Whether an installer put a setup code into the environment."""
    return bool(normalize(env_code()))


def _consumed_digests() -> set[str]:
    try:
        text = _consumed_path().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.chmod(0o600)
    tmp.replace(path)


def _user_count() -> int:
    from olano.users.store import get_user_store

    return int(get_user_store().db.count_users())


def applies() -> bool:
    """Whether this box guards its first admin with a setup code at all.

    False on a managed deployment (admin provisioned from the environment, or
    a posture other than ``open``); True everywhere else.
    """
    try:
        from olano.runtime.first_boot import provisioning_from_env

        if provisioning_from_env() is not None:
            return False
    except Exception:  # pragma: no cover - defensive
        logger.debug("first-boot provisioning check failed", exc_info=True)
    try:
        from olano.deployment import resolver

        return resolver.deployment_status().posture == "open"
    except Exception:  # pragma: no cover - defensive: unreadable ⇒ ask for the code
        logger.debug("deployment posture unreadable; requiring the setup code", exc_info=True)
        return True


def required() -> bool:
    """Whether a bootstrap request must carry the code right now."""
    if not ENFORCED:
        return False
    if not applies():
        return False
    try:
        return _user_count() == 0
    except Exception:  # pragma: no cover - defensive
        logger.debug("user count unreadable; requiring the setup code", exc_info=True)
        return True


def expected_code(*, announce: bool = True) -> str:
    """The code a bootstrap must present, generating and announcing one if none exists.

    An environment code wins unless it was already spent. Otherwise the file
    under the home is read, or created (0600) and logged.
    """
    with _lock:
        spent = _consumed_digests()
        from_env = env_code()
        if normalize(from_env) and _digest(from_env) not in spent:
            return from_env
        path = code_path()
        try:
            stored = path.read_text(encoding="utf-8").strip()
        except OSError:
            stored = ""
        if normalize(stored) and _digest(stored) not in spent:
            return stored
        code = generate()
        _write_private(path, code + "\n")
    if announce:
        log_banner(code, source=str(path))
    return code


def log_banner(code: str, *, source: str) -> None:
    """Print the code where an operator looking at the runtime's log will see it."""
    bar = "=" * 64
    logger.warning(
        "\n%s\n  FIRST-RUN SETUP CODE:  %s\n"
        "  Open the dashboard and enter this code to create the first admin account.\n"
        "  (from %s; it stops working once the account exists)\n%s",
        bar,
        code,
        source,
        bar,
    )


def announce_at_boot() -> None:
    """At ``serve`` startup: make sure a pending setup has a code, and print it.

    A no-op on a box that does not need one (managed, or accounts exist), and
    on such a box a leftover generated file is removed so nothing on disk
    looks like a live code.
    """
    if not ENFORCED or not applies():
        return
    try:
        users = _user_count()
    except Exception:
        logger.debug("user count unreadable at boot; skipping setup-code banner", exc_info=True)
        return
    if users > 0:
        try:
            code_path().unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove a spent setup-code file", exc_info=True)
        return
    from_env = env_code()
    if normalize(from_env) and _digest(from_env) not in _consumed_digests():
        log_banner(from_env, source=f"{ENV_NAME} in the environment (.env)")
        return
    expected_code(announce=True)


def verify(submitted: str | None) -> Verdict:
    """Check a bootstrap's code. Call only when :func:`required` is True."""
    now = time.monotonic()
    with _lock:
        while _wrong and _wrong[0] <= now - WINDOW_SECONDS:
            _wrong.popleft()
        if len(_wrong) >= MAX_WRONG_ATTEMPTS:
            retry = max(1, int(WINDOW_SECONDS - (now - _wrong[0])))
            return Verdict(False, "setup_code_rate_limited", retry)
    if not normalize(submitted or ""):
        return Verdict(False, "setup_code_required")
    expected = expected_code()
    if hmac.compare_digest(
        normalize(submitted or "").encode("utf-8"), normalize(expected).encode("utf-8")
    ):
        return Verdict(True)
    with _lock:
        _wrong.append(now)
    logger.warning("First-run setup: a bootstrap request carried a wrong setup code")
    return Verdict(False, "setup_code_invalid")


def consume() -> None:
    """After the first admin is created: the code never works again."""
    with _lock:
        spent = _consumed_digests()
        for candidate in (env_code(), _read_file_code()):
            if normalize(candidate):
                spent.add(_digest(candidate))
        try:
            if spent:
                _write_private(_consumed_path(), "\n".join(sorted(spent)) + "\n")
            code_path().unlink(missing_ok=True)
        except OSError:
            logger.warning("could not record the spent setup code", exc_info=True)
        _wrong.clear()


def _read_file_code() -> str:
    try:
        return code_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def reset_attempts_for_tests() -> None:
    with _lock:
        _wrong.clear()
