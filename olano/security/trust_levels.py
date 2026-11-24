"""Trust level definitions and gate helpers."""

from __future__ import annotations

from enum import IntEnum


class TrustLevel(IntEnum):
    """Agent autonomy trust level (0–4).

    Higher level = more autonomy granted to the agent.
    """

    OBSERVER = 0  # Read-only access, can only suggest via PULSE
    ASSISTANT = 1  # Write to memory/logs only
    COLLABORATOR = 2  # Create/edit workspace files, Docker shell allowed
    AUTONOMOUS = 3  # Full workspace, Docker, auto-approve low-risk operations
    DEVELOPER = 4  # Local shell, auto-approve most (dev/CI use only)


def can_write_files(level: int) -> bool:
    return level >= TrustLevel.COLLABORATOR


def can_run_shell(level: int) -> bool:
    return level >= TrustLevel.COLLABORATOR


def can_run_local_shell(level: int) -> bool:
    return level >= TrustLevel.DEVELOPER


def can_auto_approve(level: int, risk: str = "low") -> bool:
    """Return True if the trust level allows auto-approval for given risk tier."""
    if risk == "low":
        return level >= TrustLevel.AUTONOMOUS
    if risk == "medium":
        return level >= TrustLevel.DEVELOPER
    # "high" risk always requires HITL
    return False


def requires_hitl(level: int, risk: str = "low") -> bool:
    return not can_auto_approve(level, risk)


TRUST_LEVEL_DESCRIPTIONS = {
    TrustLevel.OBSERVER: "Read-only. Can only suggest via PULSE.",
    TrustLevel.ASSISTANT: "Can write to memory and logs only.",
    TrustLevel.COLLABORATOR: "Can edit workspace files and run shell commands via the configured backend.",
    TrustLevel.AUTONOMOUS: "Full workspace access; auto-approves low-risk operations.",
    TrustLevel.DEVELOPER: "May use local_shell backend; auto-approves most operations.",
}
