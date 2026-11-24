"""Bounds and defaults for the Cortex funnel's autonomous decisions.

One module, imported by both config tiers (``system_settings.SystemSettings``
for the home-wide ``cognition:`` block and ``agent_config.CognitionConfig`` for
``agent.json5``), so the two can never clamp the same knob differently. The
resolvers that combine the tiers live in :mod:`olano.config.system_settings`
next to every other cognition gate (``effective_promote_threshold`` and
friends); the triage phase (:mod:`olano.cognition.triage`) and the
``cortex_schedule`` tool read only those.
"""

from __future__ import annotations

#: Score (1-10) at or above which a "promote" recommendation moves an item to
#: the next funnel stage on its own under Fully Autonomous.
DEFAULT_PROMOTE_THRESHOLD = 8
PROMOTE_THRESHOLD_MIN, PROMOTE_THRESHOLD_MAX = 1, 10

#: Score at or below which a "discard" recommendation discards an item on its
#: own under Fully Autonomous (reversible: the folder stays, ``cortex_restore``
#: brings it back). Not configurable: it is the floor of the rating scale's
#: "not worth keeping" band, and the recommendation does the deciding.
DISCARD_SCORE_MAX = 3

#: Promotions one triage run may perform. Each is a deep worker run and counts
#: against the daily deep-run cap, which is the real ceiling.
DEFAULT_MAX_PROMOTIONS_PER_RUN = 1
MAX_PROMOTIONS_MIN, MAX_PROMOTIONS_MAX = 0, 5

#: Pending steps an agent may hold through ``cortex_schedule`` at once.
DEFAULT_MAX_SELF_SCHEDULED_TASKS = 6
SELF_SCHEDULED_MIN, SELF_SCHEDULED_MAX = 0, 20

#: Whether Fully Autonomous may discard items the triage phase rated as not
#: worth keeping.
DEFAULT_AUTO_DISCARD = True


def clamp_promote_threshold(value: int) -> int:
    return max(PROMOTE_THRESHOLD_MIN, min(PROMOTE_THRESHOLD_MAX, int(value)))


def clamp_max_promotions(value: int) -> int:
    return max(MAX_PROMOTIONS_MIN, min(MAX_PROMOTIONS_MAX, int(value)))


def clamp_self_scheduled(value: int) -> int:
    return max(SELF_SCHEDULED_MIN, min(SELF_SCHEDULED_MAX, int(value)))
