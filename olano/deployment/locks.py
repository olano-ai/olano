"""Interprocess write lock for count-gated mutations.

Every "count resources → decide → write" sequence (agent create, config save,
channel/MCP/webhook add) runs under this lock so two concurrent creates cannot
both observe N-1 and both write the N+1th resource — whether they race across
threads or across processes (CLI vs runtime API).

Implementation: ``flock`` on ``<home>/deployment/.write.lock`` plus a
process-local re-entrant lock (flock is per-process, not per-thread).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import threading
import time
from collections.abc import Iterator

logger = logging.getLogger(__name__)

_THREAD_LOCK = threading.RLock()
_LOCK_FILE = ".write.lock"

# Nesting depth for the thread currently holding _THREAD_LOCK. Only the
# outermost acquisition takes the flock: flock locks are per *open file
# description*, so a nested acquisition opening the file again would be
# refused by the lock this very thread already holds and spin until timeout.
# Guarded by _THREAD_LOCK (an RLock the holder keeps for the whole body), so
# a plain int is safe — the counter is only ever read/written by its owner.
_depth = 0


class DeploymentLockTimeout(Exception):
    """Could not acquire the deployment write lock in time."""


@contextlib.contextmanager
def deployment_write_lock(timeout: float = 10.0) -> Iterator[None]:
    """Serialize resource-mutating writes across threads AND processes.

    Re-entrant: a read-modify-write that needs to span several helpers (see
    :func:`olano.config.loader.mutate_agent_config`) can hold this across a
    call to something that takes it again, and only the outermost holder
    touches the file lock.
    """
    global _depth

    acquired = _THREAD_LOCK.acquire(timeout=timeout)
    if not acquired:
        raise DeploymentLockTimeout("deployment write lock (thread) timed out")
    if _depth > 0:  # already ours — the flock below is already held
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
            _THREAD_LOCK.release()
        return
    fh = None
    try:
        from olano.config.home import get_olano_paths

        lock_dir = get_olano_paths().deployment_dir
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = (lock_dir / _LOCK_FILE).open("a+")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise DeploymentLockTimeout("deployment write lock (file) timed out") from None
                time.sleep(0.05)
        _depth = 1
        yield
    finally:
        _depth = 0
        if fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
        _THREAD_LOCK.release()
