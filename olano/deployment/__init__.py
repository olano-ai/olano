"""Deployment Settings — the vendor-controlled entitlements layer.

Resource caps, usage budgets, and feature/tab toggles for a *managed* olano
deployment, delivered as Ed25519-signed artifacts (a local license file or a
payload fetched from cloud.olano.ai). Sits above ``<home>/config.yaml``:
customers configure their agents; this layer decides what the deployment is
allowed to do. The architecture, state machine and trust model are written
up in the deployment-settings design record (internal engineering notes).

Unmanaged installations (no installation manifest) are completely unaffected —
every helper here short-circuits to "allowed".
"""

from olano.deployment.envelope import LicenseError, verify_license
from olano.deployment.schema import (
    KNOWN_FEATURES,
    KNOWN_TABS,
    OPEN_SETTINGS,
    QUARANTINE_SETTINGS,
    DeploymentSettings,
    SignedSettings,
)

__all__ = [
    "KNOWN_FEATURES",
    "KNOWN_TABS",
    "OPEN_SETTINGS",
    "QUARANTINE_SETTINGS",
    "DeploymentSettings",
    "LicenseError",
    "SignedSettings",
    "verify_license",
]
