"""Security profile presets."""

from __future__ import annotations

from dataclasses import dataclass

from olano.security.trust_levels import TrustLevel


@dataclass
class SecurityProfile:
    name: str
    trust_level: int
    pii_action: str  # "warn" | "redact" | "block"
    input_scan: bool
    output_scan: bool
    skill_audit_level: str
    hitl_enabled: bool
    hitl_auto_approve_low_risk: bool
    memory_visibility: str  # "full" | "filtered"


_PROFILES: dict[str, SecurityProfile] = {
    "safe": SecurityProfile(
        name="safe",
        trust_level=TrustLevel.COLLABORATOR,
        pii_action="redact",
        input_scan=True,
        output_scan=True,
        skill_audit_level="strict",
        hitl_enabled=True,
        hitl_auto_approve_low_risk=False,
        memory_visibility="full",
    ),
    "developer": SecurityProfile(
        name="developer",
        trust_level=TrustLevel.DEVELOPER,
        pii_action="warn",
        input_scan=True,
        output_scan=True,
        skill_audit_level="strict",
        # "Trusted — act freely" still enforces a minimal approval floor
        # (money + secret writes); see hitl_policy.PROFILE_CATEGORIES. Set
        # security.hitl_enabled=False explicitly for full autonomy.
        hitl_enabled=True,
        hitl_auto_approve_low_risk=True,
        memory_visibility="full",
    ),
    "production": SecurityProfile(
        name="production",
        trust_level=TrustLevel.AUTONOMOUS,
        pii_action="block",
        input_scan=True,
        output_scan=True,
        skill_audit_level="paranoid",
        hitl_enabled=True,
        hitl_auto_approve_low_risk=False,
        memory_visibility="filtered",
    ),
}


def get_profile(name: str) -> SecurityProfile:
    profile = _PROFILES.get(name)
    if profile is None:
        raise ValueError(f"Unknown security profile '{name}'. Valid: {list(_PROFILES)}")
    return profile


def list_profiles() -> list[str]:
    return list(_PROFILES)
