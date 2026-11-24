"""Deleting a conversation here deletes its traces in Langfuse too.

A thread's traced turns are recorded on its transcript rows (``trace_id`` on
assistant rows, written by the runner), so the ids are read from the JSONL
*before* the file goes, and deleted afterwards with the agent's own Langfuse
keys through ``DELETE /api/public/traces/{id}``. Best-effort and off the
request: a Langfuse that is down or a key that has since been removed leaves
the traces where they are, logged, and never fails the delete the person
asked for. No SDK import: plain HTTP.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: At most this many traces are deleted per thread (a very long thread's
#: oldest traces are left to the project's retention).
MAX_TRACES_PER_THREAD = 500
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def trace_ids_in_transcript(path: Path) -> list[str]:
    """The distinct Langfuse trace ids recorded in a transcript, newest last."""
    ids: list[str] = []
    seen: set[str] = set()
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"trace_id"' not in line:
                    continue
                try:
                    tid = json.loads(line).get("trace_id")
                except ValueError:
                    continue
                if isinstance(tid, str) and _TRACE_ID.match(tid) and tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
    except OSError:
        return []
    return ids[-MAX_TRACES_PER_THREAD:]


def delete_traces(agent: str, trace_ids: list[str]) -> int:
    """Delete ``trace_ids`` from ``agent``'s Langfuse project; returns how many
    Langfuse confirmed. Uses the keys the agent's turns export with, whether
    or not tracing is still on (a thread traced last week is still there)."""
    if not trace_ids:
        return 0
    import httpx

    from olano.config.system_settings import load_observability_settings, normalize_langfuse_host
    from olano.observability.credentials import resolve_langfuse_credentials
    from olano.observability.policy import DEFAULT_LANGFUSE_HOST

    creds = resolve_langfuse_credentials(agent)
    if creds is None:
        logger.info(
            "observability: %d trace(s) of %s left in Langfuse (no keys)", len(trace_ids), agent
        )
        return 0
    host = (
        load_observability_settings().langfuse.host
        or normalize_langfuse_host(creds.host)
        or DEFAULT_LANGFUSE_HOST
    )
    deleted = 0
    with httpx.Client(auth=(creds.public_key, creds.secret_key), timeout=15.0) as client:
        for tid in trace_ids:
            try:
                resp = client.delete(f"{host}/api/public/traces/{tid}")
            except httpx.HTTPError as exc:
                logger.info("observability: trace delete stopped (%s)", type(exc).__name__)
                break
            if resp.status_code in (200, 204, 404):
                deleted += 1
            elif resp.status_code in (401, 403):
                logger.info(
                    "observability: Langfuse refused the trace delete (HTTP %s)", resp.status_code
                )
                break
    return deleted
