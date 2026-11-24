"""A dashboard vote, sent to Langfuse as a ``user_feedback`` score.

The vote itself lives on the box (``olano/core/feedback.py``) whether or not
this runs. This only adds the score, and only when the reply's turn was traced
(its transcript row carries a ``trace_id``) and the agent is still traced now:
turning tracing off stops this export too. The comment is user content, so it
goes only in ``full`` capture, with every secret value and credential shape
masked first (scores do not pass through the span exporter's mask).

One score per ``(trace, person)``, under a deterministic id, so changing a vote
replaces the score and taking it back deletes it. The id is salted like the
pseudonymous sender, so it never carries the dashboard user's id.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def feedback_score_id(trace_id: str, user: str) -> str:
    from olano.observability.credentials import user_salt
    from olano.observability.ids import pseudonymous_user

    return "olano-fb-" + pseudonymous_user(f"{trace_id}:{user}", user_salt())[2:]


def send_feedback(config: Any, trace_id: str, *, user: str, value: int, comment: str = "") -> bool:
    """Score (or, for ``value`` 0, un-score) ``trace_id``. Blocking and never
    raising: the vote route runs it off the request path. Returns whether
    Langfuse was told."""
    if not trace_id:
        return False
    try:
        from olano.observability import tracing
        from olano.observability.policy import effective_trace_policy

        policy = effective_trace_policy(config)
        if policy is None:
            return False
        score_id = feedback_score_id(trace_id, user)
        if value == 0:
            return tracing.clear_score(policy, score_id)
        text = ""
        if policy.full_capture and comment:
            from olano.observability.masking import MaskContext, mask_text, secret_values
            from olano.tools._credentials import _build_overlay

            secrets = secret_values(
                tracing._secret_candidates(policy.agent, _build_overlay(config.env))
            )
            text = mask_text(comment, MaskContext(capture="full", secrets=secrets))
        return tracing.score(trace_id, policy, value=value, comment=text, score_id=score_id)
    except Exception:
        logger.debug("observability: feedback score skipped", exc_info=True)
        return False
