"""Who is traced, and how: the one resolver of the two ``observability`` blocks.

``config.yaml → observability.langfuse`` is the deployment's choice and
``agent.json5 → observability.langfuse`` the agent's override, with the rule
every cognition override follows: the agent wins when it has an opinion
(anything but ``None``), the deployment applies when it does not. Credentials
come from :mod:`.credentials`. Nothing here imports the Langfuse SDK, because
the runner asks on every turn and the answer is almost always "no".

Never read ``enabled`` from either block to decide whether a turn is traced:
an agent with ``enabled: null`` on a deployment with tracing on is traced, and
one with keys missing is not, whatever the switches say.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from olano.config.system_settings import (
    LANGFUSE_CAPTURE_DEFAULT,
    coerce_langfuse_capture,
    load_observability_settings,
    normalize_langfuse_host,
)

logger = logging.getLogger(__name__)

#: Where a deployment with no host anywhere sends traces: Langfuse Cloud (EU),
#: which is also the SDK's own default.
DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"


@dataclass(frozen=True)
class TracePolicy:
    """Everything one traced run needs, resolved once at its start."""

    agent: str
    host: str
    public_key: str
    secret_key: str
    capture: str
    sample_rate: float
    environment: str
    trace_background: bool
    project_id: str
    credential_source: str

    @property
    def full_capture(self) -> bool:
        return self.capture == "full"


def _agent_override(config: Any) -> Any:
    try:
        return config.observability.langfuse
    except AttributeError:
        return None


def trace_identity(config: Any) -> str:
    """The agent a run is traced as: a worker acting for its parent counts as
    the parent (``credential_scope``), the same seam its credentials use."""
    scope = str(getattr(config, "credential_scope", "") or "").strip()
    return scope or str(getattr(config, "name", "") or "")


def effective_trace_policy(config: Any) -> TracePolicy | None:
    """The tracing policy for ``config``'s runs, or ``None`` when untraced.

    ``None`` when the resolved switch is off, when no Langfuse keys resolve,
    or on any error: tracing must never be the reason a turn fails, so a
    resolution problem reads as "not traced".
    """
    try:
        settings = load_observability_settings().langfuse
        override = _agent_override(config)

        enabled = settings.enabled
        if override is not None and override.enabled is not None:
            enabled = bool(override.enabled)
        if not enabled:
            return None

        capture = settings.capture
        if override is not None and override.capture is not None:
            capture = coerce_langfuse_capture(override.capture) or LANGFUSE_CAPTURE_DEFAULT

        sample_rate = settings.sample_rate
        if override is not None and override.sample_rate is not None:
            sample_rate = float(override.sample_rate)
        sample_rate = max(0.0, min(1.0, sample_rate))

        agent = trace_identity(config)
        from olano.observability.credentials import resolve_langfuse_credentials

        creds = resolve_langfuse_credentials(agent)
        if creds is None:
            return None
        host = settings.host or normalize_langfuse_host(creds.host) or DEFAULT_LANGFUSE_HOST
        return TracePolicy(
            agent=agent,
            host=host,
            public_key=creds.public_key,
            secret_key=creds.secret_key,
            capture=capture,
            sample_rate=sample_rate,
            environment=settings.environment,
            trace_background=settings.trace_background,
            project_id=settings.project_id,
            credential_source=creds.source,
        )
    except Exception:
        logger.debug("observability: policy resolution failed", exc_info=True)
        return None
