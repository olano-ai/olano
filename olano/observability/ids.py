"""Trace ids, sampling and links, without the SDK.

The trace id is derived from the turn, so the dashboard can link to a trace
the moment the turn starts, and a transcript row can name its trace without
anything having been exported yet. The derivation is Langfuse's own
``create_trace_id(seed=...)``: the first 16 bytes of ``sha256(seed)``, as
32 lowercase hex characters. ``tests/test_observability_ids.py`` pins the two
in sync, so a change upstream cannot silently break every stored link.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import quote


def trace_id_for(seed: str) -> str:
    """Langfuse's deterministic trace id for ``seed``."""
    return hashlib.sha256(seed.encode("utf-8")).digest()[:16].hex()


def turn_seed(agent: str, scoped_thread_id: str, turn_id: str) -> str:
    return f"olano:{agent}:{scoped_thread_id}:{turn_id}"


def sampled(trace_id: str, rate: float) -> bool:
    """Whether a run is traced at ``rate``, decided by its id.

    Deterministic, so a resumed turn and a nested call agree with the turn
    they belong to. The first 8 hex digits are uniform in sha256's output.
    """
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    try:
        bucket = int(trace_id[:8], 16)
    except ValueError:
        return False
    return bucket < int(rate * 0x1_0000_0000)


def pseudonymous_user(sender: str, salt: bytes) -> str:
    """A stable stand-in for ``sender`` that Langfuse can group by and nobody
    can reverse without the deployment's salt (``metadata`` capture)."""
    return "u-" + hmac.new(salt, sender.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def trace_url(host: str, project_id: str, trace_id: str) -> str:
    """Link to a trace in the Langfuse UI, or ``""`` without a project id."""
    if not (host and project_id and trace_id):
        return ""
    return f"{host.rstrip('/')}/project/{quote(project_id, safe='')}/traces/{trace_id}"


def session_url(host: str, project_id: str, session_id: str) -> str:
    """Link to a session (one thread) in the Langfuse UI."""
    if not (host and project_id and session_id):
        return ""
    return (
        f"{host.rstrip('/')}/project/{quote(project_id, safe='')}"
        f"/sessions/{quote(session_id, safe='')}"
    )
