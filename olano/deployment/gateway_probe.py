"""Which managed model groups does the gateway actually *serve*?

Everything else in this codebase answers a different question. ``gateway_key``
answers "does this box hold a credential", ``enforcement.service_entitled``
answers "does this plan include the service", and the availability probes in
``tools/vision.py`` / ``tools/image_tools.py`` combine the two. All three can say
yes while the gateway serves no such model group — the routing table lives in the
cloud, and nothing on this box is told when a group it depends on is missing.

That gap is not theoretical. A managed deployment asked for ``olano-vision``, the
gateway answered ``Invalid model name passed in model=olano-vision``, and every
local surface still reported managed vision as available — so the failure was
only visible by reading a chat transcript, and only diagnosable by someone who
could query the gateway by hand. This module closes it: one authenticated
``GET {gateway}/v1/models``, compared against the groups this box would actually
ask for, surfaced through ``/api/diagnostics/gateway``, ``olano models gateway
--probe`` and the dashboard's gateway card.

Deliberate limits:

- **Never on the agent-build path.** This makes a network call; builds must not.
  Callers are diagnostics surfaces, which already run off the event loop.
- **Cached, and stale-readable.** ``cached()`` never touches the network, so the
  default diagnostics read stays free; ``probe(force=True)`` is the explicit
  refresh. A failed probe keeps the last good answer rather than overwriting it
  with an empty one — the same rule the dashboard catalog store learned the hard
  way (a cached failure reads as "nothing is available" forever).
- **Advisory only, with one bounded exception.** Nothing here authorizes a call
  — the gateway does that itself. ``known_missing()`` is the single consumer
  allowed to *skip* a rung, and only on a recent successful reading: it saves a
  call we already know the gateway will reject, and heals on its own once the
  group is published.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Short: this runs behind an HTTP request a human is waiting on, and a gateway
# that cannot answer a model list in 5s is itself the finding.
PROBE_TIMEOUT_SECONDS = 5.0
# The routing table changes on a publish, not per minute. Long enough that
# repeated dashboard loads cost nothing, short enough that a fix shows up
# without anyone restarting the runtime.
PROBE_CACHE_TTL_SECONDS = 300.0

_lock = threading.Lock()
_cached: GatewayProbe | None = None
_cached_at: float = float("-inf")  # monotonic


@dataclass(frozen=True)
class GatewayProbe:
    """One reading of the gateway's model list.

    ``missing`` is the finding: groups this deployment is entitled to and would
    ask for, that the gateway does not serve. It is only meaningful when ``ok``
    — an unreachable gateway tells you nothing about what it routes, and
    reporting every group as missing would be a false alarm.
    """

    ok: bool
    served: tuple[str, ...]
    expected: tuple[str, ...]
    missing: tuple[str, ...]
    error: str | None
    checked_at: float  # wall-clock epoch seconds

    def to_dict(self) -> dict[str, object]:
        from datetime import UTC, datetime

        return {
            "ok": self.ok,
            "served": list(self.served),
            "expected": list(self.expected),
            "missing": list(self.missing),
            "error": self.error,
            "checked_at": datetime.fromtimestamp(self.checked_at, UTC).isoformat(),
        }


def expected_groups() -> tuple[str, ...]:
    """The ``olano-*`` groups this box would ask the gateway for.

    Entitlement-aware, because a plan that withholds a service legitimately has
    no such group and must not be reported as broken. Each answer is taken from
    the same predicate the calling code uses, never a second copy of the rule —
    a probe that disagrees with the tool it describes is worse than no probe.
    """
    groups: list[str] = []
    try:
        from olano.deployment import enforcement
        from olano.models.olano_gateway import UNGATED_TIERS, offered_tiers
        from olano.tools.image_tools import gateway_media_reachable

        if not gateway_media_reachable():
            return ()  # unmanaged, or no gateway credential: nothing to expect

        if enforcement.feature_enabled("olano_models"):
            for tier in offered_tiers():
                if tier in UNGATED_TIERS or enforcement.feature_enabled(f"olano_model_{tier}"):
                    groups.append(f"olano-{tier}")

        for service, wire in (
            ("vision", "olano-vision"),
            ("image", "olano-image"),
            ("tts", "olano-tts"),
            ("stt", "olano-stt"),
        ):
            if enforcement.service_entitled(service):
                groups.append(wire)
    except Exception:  # pragma: no cover - diagnostics must never raise
        logger.debug("gateway probe: could not resolve expected groups", exc_info=True)
        return ()
    return tuple(groups)


def _fetch_served() -> tuple[tuple[str, ...], str | None]:
    """``(served_model_ids, error)`` from one authenticated model-list read."""
    try:
        import httpx

        from olano.deployment.gateway_key import gateway_key
        from olano.models.olano_gateway import gateway_base_url

        key = gateway_key()
        if not key:
            return (), "this deployment holds no gateway key"

        resp = httpx.get(
            f"{gateway_base_url()}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        return (), f"{type(exc).__name__}: {str(exc)[:200]}"

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return (), "the gateway returned no model list"
    served = {str(row.get("id")).strip() for row in rows if isinstance(row, dict) and row.get("id")}
    return tuple(sorted(served)), None


def probe(*, force: bool = False, ttl: float = PROBE_CACHE_TTL_SECONDS) -> GatewayProbe:
    """Read the gateway's model list, honouring the cache unless *force*.

    Serialized: a burst of dashboard loads must cost one request, not one each.
    """
    global _cached, _cached_at
    with _lock:
        fresh = _cached is not None and (time.monotonic() - _cached_at) < ttl
        if _cached is not None and fresh and not force:
            return _cached

        served, error = _fetch_served()
        expected = expected_groups()
        if error is not None:
            # Keep the last good reading. Overwriting it with an empty list would
            # turn one timeout into "the gateway serves nothing", which reads as
            # a far bigger incident than it is.
            if _cached is not None and _cached.ok:
                return _cached
            result = GatewayProbe(False, (), expected, (), error, time.time())
        else:
            missing = tuple(g for g in expected if g not in set(served))
            result = GatewayProbe(True, served, expected, missing, None, time.time())
            if missing:
                logger.warning(
                    "olano gateway serves no %s group(s) this deployment expects: %s "
                    "— managed calls to them will fail with an invalid-model error",
                    len(missing),
                    ", ".join(missing),
                )
        _cached = result
        _cached_at = time.monotonic()
        return result


def cached() -> GatewayProbe | None:
    """The last reading, or ``None``. Never touches the network."""
    with _lock:
        return _cached


# How old a reading may be and still steer a tool away from a group. Matches the
# vision failure memo's window, so both hints forget on the same clock — a group
# published in the cloud must start working here without a restart.
KNOWN_MISSING_MAX_AGE_SECONDS = 600.0


def known_missing(group: str, *, max_age: float = KNOWN_MISSING_MAX_AGE_SECONDS) -> bool:
    """Did a RECENT, SUCCESSFUL probe report *group* as unserved?

    Never touches the network — this is called from tool paths, which must not
    wait on I/O. Every uncertain case answers ``False`` (attempt the call), so
    the worst outcome of a stale or absent probe is the behaviour we had before:
    one gateway error, classified and memoized by the caller.

    Three guards, each load-bearing:

    - ``ok`` only. A failed probe knows nothing about routing; treating its
      empty ``missing`` list as fact would be inventing an answer.
    - ``max_age``. ``cached()`` deliberately ignores TTL so diagnostics can show
      a stale reading, but suppressing a rung on an hours-old one would keep a
      fixed group broken until someone restarted the runtime.
    - Wall-clock, not monotonic, so this stays honest across the process's own
      clock sources; ``checked_at`` is epoch seconds.
    """
    reading = cached()
    if reading is None or not reading.ok:
        return False
    if (time.time() - reading.checked_at) > max_age:
        return False
    return group in reading.missing


def reset_cache() -> None:
    """Drop the cached reading (tests, and an explicit refresh)."""
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = float("-inf")
